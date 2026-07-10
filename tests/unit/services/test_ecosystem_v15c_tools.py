"""v1.5.0-C — Unit tests for the ecosystem lifecycle service (Stage 1/2/3).

Covers:
- Stage 1: ``request_deep_review_batch`` candidate filtering + dispatch intent.
- Stage 1 writeback: ``apply_architecture_md`` advances stage_status.
- Stage 2: ``trigger_debate`` returns DebateDispatchIntent + ``link_debate_meeting``.
- Stage 2 writeback: ``apply_debate_result`` advances to ``debated``.
- Stage 3 reference path: ``mark_as_reference`` adds tag + advances state.
- Stage 3 integrate path: ``start_integration`` returns TaskDispatchIntent.

The service is intentionally dispatch-only — it never spawns sub-agents,
creates meetings, or creates tasks. Tests verify state transitions and
intent payload shape only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from aiteam.services.ecosystem_lifecycle import (
    LIFECYCLE_TAG_INTEGRATED,
    LIFECYCLE_TAG_REFERENCE,
    DebateDispatchIntent,
    DeepReviewBatchIntent,
    EcosystemLifecycleService,
    TaskDispatchIntent,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemProjectSettings,
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemStageStatus,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
    IntegrationRecommendation,
)

# ============================================================
# Fixtures
# ============================================================


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _settings(repo: StorageRepository, project_id: str = "p1") -> None:
    await repo.upsert_ecosystem_project_settings(
        EcosystemProjectSettings(
            project_id=project_id,
            min_stars=1000,
            top_n=50,
            shallow_concurrency=5,
        )
    )


async def _make_profile(
    repo: StorageRepository,
    full_name: str,
    *,
    stars: int = 5000,
    project_id: str = "p1",
    is_active: bool = True,
    shallow_summary: str = "默认浅扫总结",
    is_deleted: bool = False,
    is_private_now: bool = False,
) -> str:
    profile = EcosystemRepoProfile(
        project_id=project_id,
        repo_full_name=full_name,
        name=full_name.split("/")[-1],
        owner=full_name.split("/")[0],
        stars=stars,
        is_active=is_active,
        shallow_summary=shallow_summary,
        is_deleted=is_deleted,
        is_private_now=is_private_now,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile, project_id=project_id)
    fetched = await repo.get_ecosystem_profile(full_name, project_id=project_id)
    assert fetched is not None
    return fetched.id


async def _seed_tag(
    repo: StorageRepository,
    *,
    repo_id: str,
    tag_name: str,
    category: EcosystemTagCategory = EcosystemTagCategory.CAPABILITY,
    project_id: str = "p1",
) -> str:
    """Create the tag dictionary entry + apply it to the repo."""
    tag = EcosystemTag(name=tag_name, category=category)
    await repo.upsert_tag(tag)
    fetched = await repo.get_tag_by_name(tag_name)
    assert fetched is not None
    await repo.add_repo_tag(
        EcosystemRepoTag(
            project_id=project_id,
            repo_id=repo_id,
            tag_id=fetched.id,
            confidence=0.95,
            source=EcosystemTagSource.MANUAL,
        ),
        project_id=project_id,
    )
    return fetched.id


# ============================================================
# Stage 1
# ============================================================


async def test_request_deep_review_batch_filters_candidates(
    repo: StorageRepository,
) -> None:
    """Only active + shallow_done + tag-matching profiles are dispatched."""
    await _settings(repo)
    eligible = await _make_profile(
        repo, "owner/eligible", stars=5000, shallow_summary="ok"
    )
    no_summary = await _make_profile(
        repo, "owner/no-summary", stars=5000, shallow_summary=""
    )
    inactive = await _make_profile(
        repo, "owner/inactive", stars=5000, is_active=False, shallow_summary="ok"
    )
    deleted = await _make_profile(
        repo,
        "owner/deleted",
        stars=5000,
        is_deleted=True,
        is_active=False,
        shallow_summary="ok",
    )

    await _seed_tag(repo, repo_id=eligible, tag_name="memory_system")
    await _seed_tag(repo, repo_id=no_summary, tag_name="memory_system")
    await _seed_tag(repo, repo_id=inactive, tag_name="memory_system")
    await _seed_tag(repo, repo_id=deleted, tag_name="memory_system")

    service = EcosystemLifecycleService(repo, project_id="p1")
    intents = await service.request_deep_review_batch(
        tags=["memory_system"],
        research_goal="升级系统记忆功能",
    )

    assert len(intents) == 1, "only eligible repo should produce a dispatch intent"
    intent = intents[0]
    assert isinstance(intent, DeepReviewBatchIntent)
    assert intent.repo_id == eligible
    assert intent.repo_full_name == "owner/eligible"
    assert "升级系统记忆功能" in intent.prompt
    assert "owner/eligible" in intent.prompt

    # D5: status is derived from stage_status at creation (shallow_done ->
    # completed); in-flight Stage 1 is expressed by claimed_by='lifecycle:*'.
    review = await repo.get_deep_review(intent.deep_review_id, project_id="p1")
    assert review is not None
    assert review.status == EcosystemDeepReviewStatus.COMPLETED
    assert review.stage_status == EcosystemStageStatus.SHALLOW_DONE
    assert review.claimed_by is not None
    assert review.claimed_by.startswith("lifecycle:")
    assert review.dispatch_prompt


async def test_request_deep_review_batch_rejects_empty_tags(
    repo: StorageRepository,
) -> None:
    """Empty tags must raise ValueError."""
    await _settings(repo)
    service = EcosystemLifecycleService(repo, project_id="p1")
    with pytest.raises(ValueError):
        await service.request_deep_review_batch(tags=[])


async def test_apply_architecture_md_advances_stage(
    repo: StorageRepository,
) -> None:
    """apply_architecture_md persists md and flips to architecture_done."""
    await _settings(repo)
    rid = await _make_profile(repo, "owner/eligible", stars=5000)
    await _seed_tag(repo, repo_id=rid, tag_name="memory_system")

    service = EcosystemLifecycleService(repo, project_id="p1")
    [intent] = await service.request_deep_review_batch(tags=["memory_system"])

    review = await service.apply_architecture_md(
        intent.deep_review_id,
        architecture_md="## 架构\n核心模块说明……",
        agent_id="backend-architect-1",
    )
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.ARCHITECTURE_DONE
    assert review.architecture_md.startswith("## 架构")
    assert review.architecture_completed_at is not None
    assert review.status == EcosystemDeepReviewStatus.COMPLETED
    assert review.agent_id == "backend-architect-1"


async def test_apply_architecture_md_rejects_empty(
    repo: StorageRepository,
) -> None:
    await _settings(repo)
    service = EcosystemLifecycleService(repo, project_id="p1")
    with pytest.raises(ValueError):
        await service.apply_architecture_md("nonexistent", architecture_md="")


# ============================================================
# Stage 2
# ============================================================


async def _make_architecture_done_review(
    repo: StorageRepository,
    repo_id: str,
    *,
    project_id: str = "p1",
) -> str:
    """Helper: insert a review already in architecture_done."""
    review = EcosystemDeepReview(
        project_id=project_id,
        repo_id=repo_id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        stage_status=EcosystemStageStatus.ARCHITECTURE_DONE,
        architecture_md="## 已分析",
    )
    await repo.create_deep_review(review, project_id=project_id)
    return review.id


async def test_trigger_debate_returns_intent(repo: StorageRepository) -> None:
    """trigger_debate collects architecture_done reviews + builds intent."""
    await _settings(repo)
    a = await _make_profile(repo, "owner/a", stars=5000)
    b = await _make_profile(repo, "owner/b", stars=4000)
    await _make_architecture_done_review(repo, a)
    await _make_architecture_done_review(repo, b)

    service = EcosystemLifecycleService(repo, project_id="p1")
    intent = await service.trigger_debate(
        repo_ids=[a, b],
        research_goal="升级系统记忆功能",
    )
    assert isinstance(intent, DebateDispatchIntent)
    assert len(intent.review_ids) == 2
    assert set(intent.repo_full_names) == {"owner/a", "owner/b"}
    assert "升级系统记忆功能" in intent.suggested_topic
    assert "owner/a" in intent.suggested_topic
    assert intent.suggested_advocate == "backend-architect"


async def test_trigger_debate_rejects_when_no_architecture_done(
    repo: StorageRepository,
) -> None:
    """If no repo has an architecture_done review, raise ValueError."""
    await _settings(repo)
    a = await _make_profile(repo, "owner/a", stars=5000)
    service = EcosystemLifecycleService(repo, project_id="p1")
    with pytest.raises(ValueError):
        await service.trigger_debate(repo_ids=[a], research_goal="x")


async def test_link_debate_meeting_writes_back(repo: StorageRepository) -> None:
    """link_debate_meeting populates debate_meeting_id on review rows."""
    await _settings(repo)
    a = await _make_profile(repo, "owner/a", stars=5000)
    review_id = await _make_architecture_done_review(repo, a)

    service = EcosystemLifecycleService(repo, project_id="p1")
    updated = await service.link_debate_meeting(
        review_ids=[review_id], meeting_id="meet-123"
    )
    assert updated == 1

    fresh = await repo.get_deep_review(review_id, project_id="p1")
    assert fresh is not None
    assert fresh.debate_meeting_id == "meet-123"


async def test_apply_debate_result_advances_to_debated(
    repo: StorageRepository,
) -> None:
    """apply_debate_result records risks/learnings/integration + flips state."""
    await _settings(repo)
    a = await _make_profile(repo, "owner/a", stars=5000)
    review_id = await _make_architecture_done_review(repo, a)

    service = EcosystemLifecycleService(repo, project_id="p1")
    review = await service.apply_debate_result(
        review_id,
        risks_md="风险点 A / B",
        learnings_md="借鉴：异步队列设计",
        integration_md="集成方案: 引入 worker pattern",
        integration_recommendation="integrate",
        agent_id="judge-1",
    )
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.DEBATED
    assert review.risks_md.startswith("风险点")
    assert review.learnings_md.startswith("借鉴")
    assert review.integration_md.startswith("集成方案")
    assert review.debated_at is not None
    assert review.integration_recommendation == IntegrationRecommendation.INTEGRATE
    assert review.agent_id == "judge-1"


async def test_apply_debate_result_rejects_all_empty(
    repo: StorageRepository,
) -> None:
    await _settings(repo)
    service = EcosystemLifecycleService(repo, project_id="p1")
    with pytest.raises(ValueError):
        await service.apply_debate_result(
            "review-nonexistent",
            risks_md="",
            learnings_md="",
            integration_md="",
        )


# ============================================================
# Stage 3
# ============================================================


async def test_mark_as_reference_adds_tag_and_state(
    repo: StorageRepository,
) -> None:
    """mark_as_reference seeds the lifecycle:reference tag + flips state."""
    await _settings(repo)
    a = await _make_profile(repo, "owner/a", stars=5000)
    review_id = await _make_architecture_done_review(repo, a)

    service = EcosystemLifecycleService(repo, project_id="p1")
    review = await service.mark_as_reference(review_id, agent_id="leader")
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.REFERENCED
    assert review.stage3_completed_at is not None

    # Tag should now be applied with source=lifecycle.
    tag = await repo.get_tag_by_name(LIFECYCLE_TAG_REFERENCE)
    assert tag is not None
    repo_tags = await repo.list_repo_tags(repo_id=a, project_id="p1")
    matching = [rt for rt in repo_tags if rt.tag_id == tag.id]
    assert matching, "lifecycle:reference tag should be applied to the repo"
    assert matching[0].source == EcosystemTagSource.LIFECYCLE
    assert matching[0].agent_id == "leader"


async def test_start_integration_returns_task_intent(
    repo: StorageRepository,
) -> None:
    """start_integration returns TaskDispatchIntent + tags repo + advances state."""
    await _settings(repo)
    a = await _make_profile(
        repo, "owner/a", stars=5000, shallow_summary="一个高质量记忆库"
    )
    review_id = await _make_architecture_done_review(repo, a)

    service = EcosystemLifecycleService(repo, project_id="p1")
    intent = await service.start_integration(review_id)
    assert isinstance(intent, TaskDispatchIntent)
    assert intent.repo_full_name == "owner/a"
    assert intent.title.startswith("Integrate owner/a")
    assert "ecosystem-integration" in intent.tags
    assert any(t.startswith("repo:owner/a") for t in intent.tags)
    assert "## 集成目标" in intent.description

    # State machine + tag should have advanced.
    fresh = await repo.get_deep_review(review_id, project_id="p1")
    assert fresh is not None
    assert fresh.stage_status == EcosystemStageStatus.INTEGRATED

    integrated_tag = await repo.get_tag_by_name(LIFECYCLE_TAG_INTEGRATED)
    assert integrated_tag is not None
    repo_tags = await repo.list_repo_tags(repo_id=a, project_id="p1")
    assert any(rt.tag_id == integrated_tag.id for rt in repo_tags)


async def test_start_integration_rejects_pre_architecture(
    repo: StorageRepository,
) -> None:
    """A queued review (pre architecture_done) cannot be integrated."""
    await _settings(repo)
    a = await _make_profile(repo, "owner/a", stars=5000)
    review = EcosystemDeepReview(
        project_id="p1",
        repo_id=a,
        status=EcosystemDeepReviewStatus.QUEUED,
        stage_status=EcosystemStageStatus.QUEUED,
    )
    await repo.create_deep_review(review, project_id="p1")

    service = EcosystemLifecycleService(repo, project_id="p1")
    with pytest.raises(ValueError):
        await service.start_integration(review.id)


async def test_link_integration_task_writes_back(
    repo: StorageRepository,
) -> None:
    """link_integration_task writes integration_task_id."""
    await _settings(repo)
    a = await _make_profile(repo, "owner/a", stars=5000)
    review_id = await _make_architecture_done_review(repo, a)

    service = EcosystemLifecycleService(repo, project_id="p1")
    await service.start_integration(review_id)

    review = await service.link_integration_task(
        deep_review_id=review_id, task_id="task-xyz"
    )
    assert review is not None
    assert review.integration_task_id == "task-xyz"
    assert review.stage_status == EcosystemStageStatus.INTEGRATED
