"""v1.5.0-C — End-to-end stage progression tests.

Covers the funnel state machine described in
``docs/v1.5.0-progressive-deep-review-design.md`` §2:

    queued → shallow_done → architecture_done → debated
                                              → referenced (Stage 3 reference path)
                                              → integrated  (Stage 3 integrate path)

Each test walks one or more transitions and asserts the resulting
``stage_status`` + the timestamp field that the helper writes (so we
catch regressions in ``update_deep_review_stage`` mapping logic).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from aiteam.services.ecosystem_lifecycle import (
    LIFECYCLE_TAG_INTEGRATED,
    LIFECYCLE_TAG_REFERENCE,
    EcosystemLifecycleService,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemProjectSettings,
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemStageStatus,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
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


async def _bootstrap(
    repo: StorageRepository,
    *,
    project_id: str = "p1",
    full_name: str = "owner/x",
    tag_name: str = "memory_system",
) -> tuple[str, str]:
    """Seed settings + profile + capability tag, return (project_id, repo_id)."""
    await repo.upsert_ecosystem_project_settings(
        EcosystemProjectSettings(
            project_id=project_id, min_stars=1000, top_n=50, shallow_concurrency=5
        )
    )
    profile = EcosystemRepoProfile(
        project_id=project_id,
        repo_full_name=full_name,
        name=full_name.split("/")[-1],
        owner=full_name.split("/")[0],
        stars=5000,
        is_active=True,
        shallow_summary="高质量记忆库",
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile, project_id=project_id)
    fetched = await repo.get_ecosystem_profile(full_name, project_id=project_id)
    assert fetched is not None
    repo_id = fetched.id

    cap_tag = EcosystemTag(name=tag_name, category=EcosystemTagCategory.CAPABILITY)
    await repo.upsert_tag(cap_tag)
    seeded = await repo.get_tag_by_name(tag_name)
    assert seeded is not None
    await repo.add_repo_tag(
        EcosystemRepoTag(
            project_id=project_id,
            repo_id=repo_id,
            tag_id=seeded.id,
            confidence=0.9,
            source=EcosystemTagSource.MANUAL,
        ),
        project_id=project_id,
    )
    return project_id, repo_id


# ============================================================
# Full happy-path progressions
# ============================================================


async def test_full_funnel_reference_path(repo: StorageRepository) -> None:
    """queued → shallow_done → architecture_done → debated → referenced."""
    project_id, repo_id = await _bootstrap(repo)
    service = EcosystemLifecycleService(repo, project_id=project_id)

    # Stage 1 dispatch
    [intent] = await service.request_deep_review_batch(
        tags=["memory_system"], research_goal="升级记忆"
    )
    review_id = intent.deep_review_id
    review = await repo.get_deep_review(review_id, project_id=project_id)
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.SHALLOW_DONE
    assert review.shallow_completed_at is None  # writer didn't set it

    # Stage 1 writeback
    review = await service.apply_architecture_md(
        review_id, architecture_md="## 架构\n核心模块 A / B / C"
    )
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.ARCHITECTURE_DONE
    assert review.architecture_completed_at is not None

    # Stage 2 trigger + link + writeback
    debate_intent = await service.trigger_debate(
        repo_ids=[repo_id], research_goal="升级记忆"
    )
    assert review_id in debate_intent.review_ids

    linked = await service.link_debate_meeting(
        review_ids=[review_id], meeting_id="meet-abc"
    )
    assert linked == 1

    review = await service.apply_debate_result(
        review_id,
        risks_md="风险 X",
        learnings_md="借鉴 Y",
        integration_md="集成方案 Z",
        integration_recommendation="reference",
    )
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.DEBATED
    assert review.debated_at is not None
    assert review.debate_meeting_id == "meet-abc"

    # Stage 3 reference path
    review = await service.mark_as_reference(review_id, agent_id="leader")
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.REFERENCED
    assert review.stage3_completed_at is not None

    # Tag check
    ref_tag = await repo.get_tag_by_name(LIFECYCLE_TAG_REFERENCE)
    assert ref_tag is not None
    repo_tags = await repo.list_repo_tags(repo_id=repo_id, project_id=project_id)
    assert any(rt.tag_id == ref_tag.id for rt in repo_tags)


async def test_full_funnel_integrate_path(repo: StorageRepository) -> None:
    """queued → ... → debated → integrated (with task linking)."""
    project_id, repo_id = await _bootstrap(repo, full_name="owner/intgr")
    service = EcosystemLifecycleService(repo, project_id=project_id)

    [intent] = await service.request_deep_review_batch(tags=["memory_system"])
    review_id = intent.deep_review_id

    await service.apply_architecture_md(review_id, architecture_md="## 架构 A")
    await service.apply_debate_result(
        review_id,
        risks_md="r",
        learnings_md="l",
        integration_md="i",
        integration_recommendation="integrate",
    )

    task_intent = await service.start_integration(review_id, priority="critical")
    assert task_intent.priority == "critical"
    assert task_intent.review_id == review_id
    assert task_intent.repo_id == repo_id
    assert task_intent.repo_full_name == "owner/intgr"

    # State should now be INTEGRATED (link_integration_task is optional follow-up).
    review = await repo.get_deep_review(review_id, project_id=project_id)
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.INTEGRATED

    # Linked task id is empty until link_integration_task is called.
    assert review.integration_task_id is None

    review = await service.link_integration_task(
        deep_review_id=review_id, task_id="task-final"
    )
    assert review is not None
    assert review.integration_task_id == "task-final"
    assert review.stage_status == EcosystemStageStatus.INTEGRATED  # idempotent

    # Lifecycle tag applied.
    integ_tag = await repo.get_tag_by_name(LIFECYCLE_TAG_INTEGRATED)
    assert integ_tag is not None
    repo_tags = await repo.list_repo_tags(repo_id=repo_id, project_id=project_id)
    assert any(rt.tag_id == integ_tag.id for rt in repo_tags)


async def test_skip_stage2_integrate_directly(repo: StorageRepository) -> None:
    """architecture_done → integrated (Stage 2 skipped — fast path).

    Per service contract, ``start_integration`` accepts architecture_done
    too (not just debated). This is what we want for trivial integrations
    where a debate is overkill.
    """
    project_id, repo_id = await _bootstrap(repo, full_name="owner/quick")
    service = EcosystemLifecycleService(repo, project_id=project_id)

    [intent] = await service.request_deep_review_batch(tags=["memory_system"])
    review_id = intent.deep_review_id
    await service.apply_architecture_md(review_id, architecture_md="## 简短架构")

    task_intent = await service.start_integration(review_id)
    assert task_intent.repo_id == repo_id

    review = await repo.get_deep_review(review_id, project_id=project_id)
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.INTEGRATED


# ============================================================
# Stage_status timestamp mapping (regression guard)
# ============================================================


async def test_each_stage_sets_correct_timestamp(repo: StorageRepository) -> None:
    """Each stage advance must populate exactly one timestamp field."""
    project_id, repo_id = await _bootstrap(repo, full_name="owner/ts")
    service = EcosystemLifecycleService(repo, project_id=project_id)

    [intent] = await service.request_deep_review_batch(tags=["memory_system"])
    review_id = intent.deep_review_id

    # architecture_done → architecture_completed_at
    await service.apply_architecture_md(review_id, architecture_md="## arch")
    r = await repo.get_deep_review(review_id, project_id=project_id)
    assert r is not None
    assert r.architecture_completed_at is not None
    assert r.debated_at is None
    assert r.stage3_completed_at is None

    # debated → debated_at
    await service.apply_debate_result(
        review_id,
        risks_md="x",
        learnings_md="y",
        integration_md="z",
    )
    r = await repo.get_deep_review(review_id, project_id=project_id)
    assert r is not None
    assert r.debated_at is not None
    assert r.stage3_completed_at is None

    # referenced → stage3_completed_at
    await service.mark_as_reference(review_id)
    r = await repo.get_deep_review(review_id, project_id=project_id)
    assert r is not None
    assert r.stage3_completed_at is not None
