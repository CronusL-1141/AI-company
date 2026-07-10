"""Tests for EcosystemTagger — three-layer tagging service."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from aiteam.services.ecosystem_tagger import (
    CONFIDENCE_RULE,
    CONFIDENCE_TOPIC,
    LLM_FALLBACK_TAG_THRESHOLD,
    MAX_LLM_CONCURRENCY,
    EcosystemTagger,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemRepoProfile,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
)


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _seed_default_tags(repo: StorageRepository) -> None:
    """Mirror api/deps.py::_DEFAULT_ECOSYSTEM_TAGS subset needed for tests."""
    seed: list[tuple[str, EcosystemTagCategory]] = [
        ("memory_system", EcosystemTagCategory.CAPABILITY),
        ("skill_system", EcosystemTagCategory.CAPABILITY),
        ("agent_orchestration", EcosystemTagCategory.CAPABILITY),
        ("mcp_server", EcosystemTagCategory.CAPABILITY),
        ("mcp_framework", EcosystemTagCategory.CAPABILITY),
        ("multi_agent", EcosystemTagCategory.CAPABILITY),
        ("python", EcosystemTagCategory.TECH_STACK),
        ("typescript", EcosystemTagCategory.TECH_STACK),
        ("rust", EcosystemTagCategory.TECH_STACK),
        ("official_anthropic", EcosystemTagCategory.MATURITY),
        ("battle_tested", EcosystemTagCategory.MATURITY),
        ("experimental", EcosystemTagCategory.MATURITY),
        ("framework", EcosystemTagCategory.POSITIONING),
        ("library", EcosystemTagCategory.POSITIONING),
        ("plugin", EcosystemTagCategory.POSITIONING),
    ]
    for name, cat in seed:
        await repo.upsert_tag(EcosystemTag(name=name, category=cat))


async def _seed_profile(
    repo: StorageRepository,
    *,
    repo_full_name: str,
    name: str,
    owner: str,
    description: str | None,
    topics: list[str],
    language: str | None = "Python",
) -> EcosystemRepoProfile:
    p = EcosystemRepoProfile(
        repo_full_name=repo_full_name,
        name=name,
        owner=owner,
        description=description,
        topics=topics,
        language=language,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(p)
    saved = await repo.get_ecosystem_profile(repo_full_name)
    assert saved is not None
    return saved


# ============================================================
# Layer 1: GitHub topic mapping
# ============================================================


async def test_layer1_topic_match_creates_repo_tag(repo: StorageRepository) -> None:
    """A repo with topic 'mcp-server' should get the mcp_server tag (Layer 1)."""
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="acme/mcp-tool",
        name="mcp-tool",
        owner="acme",
        description="Some mcp tool",
        topics=["mcp-server"],
    )

    tagger = EcosystemTagger(repo)
    result = await tagger.tag_repo(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        name=profile.name,
        description=profile.description,
        topics=profile.topics,
        owner=profile.owner,
    )

    assert "mcp_server" in result.layer1_tags
    rows = await repo.list_repo_tags(repo_id=profile.id)
    sources = {r.source for r in rows}
    assert EcosystemTagSource.GITHUB_TOPIC in sources
    target = next(r for r in rows if r.source == EcosystemTagSource.GITHUB_TOPIC)
    assert target.confidence == CONFIDENCE_TOPIC


async def test_layer1_skips_unknown_tag_in_dictionary(
    repo: StorageRepository,
) -> None:
    """If alias maps to a tag NOT yet in the dictionary, record it as skipped."""
    # Seed only python; skill_system is intentionally absent.
    await repo.upsert_tag(
        EcosystemTag(name="python", category=EcosystemTagCategory.TECH_STACK)
    )
    profile = await _seed_profile(
        repo,
        repo_full_name="acme/skills",
        name="skills",
        owner="acme",
        description="A skills system",
        topics=["claude-skill", "python"],
    )

    tagger = EcosystemTagger(repo)
    result = await tagger.tag_repo(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        name=profile.name,
        description=profile.description,
        topics=profile.topics,
        owner=profile.owner,
    )

    assert "python" in result.layer1_tags
    assert "skill_system" in result.skipped_unknown


# ============================================================
# Layer 2: keyword rules
# ============================================================


async def test_layer2_owner_anthropics_forces_official_tag(
    repo: StorageRepository,
) -> None:
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="anthropics/claude-code",
        name="claude-code",
        owner="anthropics",
        description="Claude coding assistant",
        topics=[],
    )

    tagger = EcosystemTagger(repo)
    result = await tagger.tag_repo(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        name=profile.name,
        description=profile.description,
        topics=profile.topics,
        owner=profile.owner,
    )

    assert "official_anthropic" in result.layer2_tags
    rows = await repo.list_repo_tags(repo_id=profile.id)
    matched = next(
        r for r in rows if r.source == EcosystemTagSource.AUTO_RULE
    )
    assert matched.confidence == CONFIDENCE_RULE


async def test_layer2_keyword_match_when_topic_absent(
    repo: StorageRepository,
) -> None:
    """A repo with no topics but description mentioning 'memory system' should
    get memory_system via Layer 2 only."""
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="indie/mem",
        name="mem",
        owner="indie",
        description="Long-term memory layer for agents",
        topics=[],
    )

    tagger = EcosystemTagger(repo)
    result = await tagger.tag_repo(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        name=profile.name,
        description=profile.description,
        topics=profile.topics,
        owner=profile.owner,
    )

    assert result.layer1_tags == []
    assert "memory_system" in result.layer2_tags


async def test_layer1_takes_precedence_over_layer2(
    repo: StorageRepository,
) -> None:
    """When the same tag matches both layers, only Layer 1 should fire."""
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="indie/mem-v2",
        name="mem-v2",
        owner="indie",
        description="Long-term memory layer for agents",
        topics=["memory"],
    )

    tagger = EcosystemTagger(repo)
    result = await tagger.tag_repo(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        name=profile.name,
        description=profile.description,
        topics=profile.topics,
        owner=profile.owner,
    )

    assert "memory_system" in result.layer1_tags
    assert "memory_system" not in result.layer2_tags

    rows = await repo.list_repo_tags(repo_id=profile.id)
    mem_rows = [
        r
        for r in rows
        if (await repo.get_tag(r.tag_id)).name == "memory_system"  # type: ignore[union-attr]
    ]
    # only 1 row (idempotent upsert)
    assert len(mem_rows) == 1
    assert mem_rows[0].source == EcosystemTagSource.GITHUB_TOPIC


# ============================================================
# needs_llm threshold + Layer 3 dispatch
# ============================================================


async def test_low_match_count_flags_needs_llm(repo: StorageRepository) -> None:
    """A repo with <2 auto-matched tags should set needs_llm=True."""
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="indie/obscure",
        name="obscure",
        owner="indie",
        description="A vague library",
        topics=[],
    )

    tagger = EcosystemTagger(repo)
    result = await tagger.tag_repo(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        name=profile.name,
        description=profile.description,
        topics=profile.topics,
        owner=profile.owner,
    )
    assert result.total_applied() < LLM_FALLBACK_TAG_THRESHOLD
    assert result.needs_llm is True


async def test_high_match_count_no_llm(repo: StorageRepository) -> None:
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="anthropics/claude-code",
        name="claude-code",
        owner="anthropics",
        description="Battle-tested Python framework",
        topics=["mcp-server", "python"],
    )

    tagger = EcosystemTagger(repo)
    result = await tagger.tag_repo(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        name=profile.name,
        description=profile.description,
        topics=profile.topics,
        owner=profile.owner,
    )
    assert result.needs_llm is False


async def test_build_llm_dispatch_plan_caps_concurrency(
    repo: StorageRepository,
) -> None:
    await _seed_default_tags(repo)
    profiles = []
    for i in range(MAX_LLM_CONCURRENCY + 5):
        p = await _seed_profile(
            repo,
            repo_full_name=f"indie/repo-{i}",
            name=f"repo-{i}",
            owner="indie",
            description="x",
            topics=[],
        )
        profiles.append(p)

    repo_dicts = [
        {
            "id": p.id,
            "repo_full_name": p.repo_full_name,
            "description": p.description,
            "topics": p.topics,
            "language": p.language,
        }
        for p in profiles
    ]

    tagger = EcosystemTagger(repo)
    plan = await tagger.build_llm_dispatch_plan(repo_dicts)
    assert plan["dispatched"] == MAX_LLM_CONCURRENCY
    assert plan["skipped_due_to_limit"] == 5
    assert plan["total_requested"] == MAX_LLM_CONCURRENCY + 5
    # Each dispatch entry must contain a launch_call with team_name
    for d in plan["dispatch"]:
        assert d["launch_call"]["params"]["team_name"] == "ecosystem-platform"
        assert "prompt" in d["launch_call"]["params"]
        assert len(d["launch_call"]["params"]["prompt"]) < 2000


async def test_dispatch_plan_includes_existing_tags_in_prompt(
    repo: StorageRepository,
) -> None:
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="indie/has-existing",
        name="has-existing",
        owner="indie",
        description="x",
        topics=[],
    )
    tagger = EcosystemTagger(repo)
    # Pre-tag with python via manual_tag
    await tagger.manual_tag(
        repo_id=profile.id, tag_name="python", agent_id="seeder"
    )

    plan = await tagger.build_llm_dispatch_plan(
        [
            {
                "id": profile.id,
                "repo_full_name": profile.repo_full_name,
                "description": profile.description,
                "topics": profile.topics,
                "language": profile.language,
            }
        ]
    )
    assert plan["dispatched"] == 1
    prompt = plan["dispatch"][0]["launch_call"]["params"]["prompt"]
    assert "python" in prompt


# ============================================================
# apply_llm_tags / Layer 3 result write
# ============================================================


async def test_apply_llm_tags_persists_with_auto_llm_source(
    repo: StorageRepository,
) -> None:
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="indie/llm-target",
        name="llm-target",
        owner="indie",
        description="x",
        topics=[],
    )

    tagger = EcosystemTagger(repo)
    result = await tagger.apply_llm_tags(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        llm_output_tags=[
            {"name": "memory_system", "confidence": 0.85},
            {"name": "python", "confidence": 0.95},
        ],
        agent_id="llm-tagger-1",
    )

    assert set(result.layer3_tags) == {"memory_system", "python"}
    rows = await repo.list_repo_tags(repo_id=profile.id)
    assert all(r.source == EcosystemTagSource.AUTO_LLM for r in rows)
    confidences = {r.confidence for r in rows}
    assert 0.85 in confidences and 0.95 in confidences


async def test_apply_llm_tags_skips_unknown_tags(repo: StorageRepository) -> None:
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="indie/llm-mixed",
        name="llm-mixed",
        owner="indie",
        description="x",
        topics=[],
    )

    tagger = EcosystemTagger(repo)
    result = await tagger.apply_llm_tags(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        llm_output_tags=[
            {"name": "memory_system", "confidence": 0.7},
            {"name": "fictional_tag", "confidence": 0.99},
        ],
    )
    assert "memory_system" in result.layer3_tags
    assert "fictional_tag" in result.skipped_unknown


async def test_apply_llm_tags_clamps_confidence(repo: StorageRepository) -> None:
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="indie/clamp",
        name="clamp",
        owner="indie",
        description="x",
        topics=[],
    )
    tagger = EcosystemTagger(repo)
    await tagger.apply_llm_tags(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        llm_output_tags=[
            {"name": "python", "confidence": 5.0},  # > 1
            {"name": "framework", "confidence": -0.5},  # < 0
        ],
    )
    rows = await repo.list_repo_tags(repo_id=profile.id)
    for r in rows:
        assert 0.0 <= r.confidence <= 1.0


# ============================================================
# Manual tag + remove
# ============================================================


async def test_manual_tag_uses_manual_source(repo: StorageRepository) -> None:
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="indie/manual",
        name="manual",
        owner="indie",
        description="x",
        topics=[],
    )

    tagger = EcosystemTagger(repo)
    rt = await tagger.manual_tag(
        repo_id=profile.id, tag_name="memory_system", agent_id="reviewer"
    )
    assert rt is not None
    assert rt.source == EcosystemTagSource.MANUAL
    assert rt.confidence == 1.0
    assert rt.agent_id == "reviewer"


async def test_manual_tag_unknown_returns_none(repo: StorageRepository) -> None:
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="indie/x",
        name="x",
        owner="indie",
        description="x",
        topics=[],
    )
    tagger = EcosystemTagger(repo)
    rt = await tagger.manual_tag(repo_id=profile.id, tag_name="zzz_unknown")
    assert rt is None


async def test_remove_tag_deletes_association(repo: StorageRepository) -> None:
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="indie/del",
        name="del",
        owner="indie",
        description="x",
        topics=[],
    )
    tagger = EcosystemTagger(repo)
    await tagger.manual_tag(repo_id=profile.id, tag_name="python")

    removed = await tagger.remove_tag(profile.id, "python")
    assert removed is True

    rows = await repo.list_repo_tags(repo_id=profile.id)
    assert rows == []


# ============================================================
# Batch processing
# ============================================================


async def test_tag_repos_batch_aggregates_stats(repo: StorageRepository) -> None:
    await _seed_default_tags(repo)
    profiles = [
        await _seed_profile(
            repo,
            repo_full_name="anthropics/claude-code",
            name="claude-code",
            owner="anthropics",
            description="Battle-tested Python framework with mcp",
            topics=["mcp-server", "python"],
        ),
        await _seed_profile(
            repo,
            repo_full_name="indie/vague",
            name="vague",
            owner="indie",
            description="x",
            topics=[],
        ),
    ]
    repo_dicts = [
        {
            "id": p.id,
            "repo_full_name": p.repo_full_name,
            "name": p.name,
            "owner": p.owner,
            "description": p.description,
            "topics": p.topics,
            "language": p.language,
        }
        for p in profiles
    ]

    tagger = EcosystemTagger(repo)
    stats = await tagger.tag_repos_batch(repo_dicts)

    assert stats.repos_processed == 2
    assert stats.layer1_applied >= 2  # mcp-server + python at least
    assert stats.repos_needing_llm >= 1  # the indie/vague one
    assert stats.repos_failed == 0
    assert len(stats.by_repo) == 2


async def test_idempotent_re_tagging(repo: StorageRepository) -> None:
    """Calling tag_repo twice should not duplicate associations."""
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="acme/lib",
        name="lib",
        owner="acme",
        description="A python library",
        topics=["python"],
    )
    tagger = EcosystemTagger(repo)
    for _ in range(3):
        await tagger.tag_repo(
            repo_id=profile.id,
            repo_full_name=profile.repo_full_name,
            name=profile.name,
            description=profile.description,
            topics=profile.topics,
            owner=profile.owner,
        )

    rows = await repo.list_repo_tags(repo_id=profile.id)
    # No duplicates: each (repo_id, tag_id) appears once
    pairs = {(r.repo_id, r.tag_id) for r in rows}
    assert len(rows) == len(pairs)


# ============================================================
# K4 follow-up: replace_auto mode (rule upgrade cleanup)
# ============================================================


async def test_replace_auto_clears_stale_topic_tag_but_keeps_manual(
    repo: StorageRepository,
) -> None:
    """Simulate the K4 follow-up scenario:
    1. Old rule tagged a repo with mcp_framework via GitHub topic.
    2. Manual override added a different tag.
    3. After rule upgrade, replace_auto=True must remove the stale auto tag
       but preserve the manual one.
    """
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="acme/legacy-repo",
        name="legacy-repo",
        owner="acme",
        description="Workflow tool",
        topics=["mcp"],  # only this topic — old rule would have hit mcp_framework
        language="TypeScript",
    )

    # Step 1: write a stale GITHUB_TOPIC tag pretending old rule applied it
    mcp_fw_tag = await repo.get_tag_by_name("mcp_framework")
    assert mcp_fw_tag is not None
    from aiteam.types import EcosystemRepoTag

    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=profile.id,
            tag_id=mcp_fw_tag.id,
            confidence=CONFIDENCE_TOPIC,
            source=EcosystemTagSource.GITHUB_TOPIC,
            agent_id="old-agent",
        )
    )

    # Step 2: a manual tag (e.g. team curator added 'framework')
    framework_tag = await repo.get_tag_by_name("framework")
    assert framework_tag is not None
    await repo.add_repo_tag(
        EcosystemRepoTag(
            repo_id=profile.id,
            tag_id=framework_tag.id,
            confidence=1.0,
            source=EcosystemTagSource.MANUAL,
            agent_id="curator",
        )
    )

    pre_rows = await repo.list_repo_tags(repo_id=profile.id)
    pre_sources = {r.source for r in pre_rows}
    assert EcosystemTagSource.GITHUB_TOPIC in pre_sources
    assert EcosystemTagSource.MANUAL in pre_sources

    # Step 3: re-run with the new rules + replace_auto=True
    tagger = EcosystemTagger(repo)
    result = await tagger.tag_repo(
        repo_id=profile.id,
        repo_full_name=profile.repo_full_name,
        name=profile.name,
        description=profile.description,
        topics=profile.topics,
        owner=profile.owner,
        language=profile.language,
        replace_auto=True,
    )

    # mcp_framework must NOT be among the new auto tags (rule no longer matches "mcp")
    assert "mcp_framework" not in result.layer1_tags
    assert "mcp_framework" not in result.layer2_tags

    # Inspect DB state
    post_rows = await repo.list_repo_tags(repo_id=profile.id)
    tag_names_by_source: dict[EcosystemTagSource, set[str]] = {}
    for row in post_rows:
        tag = await repo.get_tag(row.tag_id)
        if tag is None:
            continue
        tag_names_by_source.setdefault(row.source, set()).add(tag.name)

    # Stale GITHUB_TOPIC mcp_framework should be gone
    assert "mcp_framework" not in tag_names_by_source.get(
        EcosystemTagSource.GITHUB_TOPIC, set()
    )
    # Manual 'framework' should still be there
    assert "framework" in tag_names_by_source.get(EcosystemTagSource.MANUAL, set())
    # New rules should at least pick up typescript via language field
    assert "typescript" in tag_names_by_source.get(EcosystemTagSource.AUTO_RULE, set())


async def test_replace_auto_false_is_default_and_preserves_existing_auto(
    repo: StorageRepository,
) -> None:
    """Default replace_auto=False keeps the legacy upsert (append) semantics:
    re-running the tagger with the same inputs leaves the row count unchanged.
    """
    await _seed_default_tags(repo)
    profile = await _seed_profile(
        repo,
        repo_full_name="acme/idempotent",
        name="idempotent",
        owner="acme",
        description="A tool",
        topics=["mcp-server", "python"],
        language="Python",
    )
    tagger = EcosystemTagger(repo)
    await tagger.tag_repo(
        repo_id=profile.id, repo_full_name=profile.repo_full_name,
        name=profile.name, description=profile.description,
        topics=profile.topics, owner=profile.owner, language=profile.language,
    )
    first = await repo.list_repo_tags(repo_id=profile.id)
    # Run again with default replace_auto=False
    await tagger.tag_repo(
        repo_id=profile.id, repo_full_name=profile.repo_full_name,
        name=profile.name, description=profile.description,
        topics=profile.topics, owner=profile.owner, language=profile.language,
    )
    second = await repo.list_repo_tags(repo_id=profile.id)
    # Same count (upsert behavior, no duplicates)
    assert len(first) == len(second)
