"""Unit tests for the v1.5.0-B EcosystemShallowQueueWorker — dispatch path."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from aiteam.services.ecosystem_shallow_queue import (
    STAGE0_TIMEOUT_SECONDS,
    DispatchIntent,
    EcosystemShallowQueueWorker,
)
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReviewStatus,
    EcosystemProjectSettings,
    EcosystemRepoProfile,
    EcosystemStageStatus,
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


async def _make_profile(
    repo: StorageRepository,
    full_name: str,
    *,
    stars: int = 5000,
    project_id: str = "proj-test",
    is_active: bool = True,
    shallow_summary: str = "",
    last_shallow_refreshed_at: datetime | None = None,
) -> str:
    """Insert a profile and return its id."""
    profile = EcosystemRepoProfile(
        project_id=project_id,
        repo_full_name=full_name,
        name=full_name.split("/")[-1],
        owner=full_name.split("/")[0],
        stars=stars,
        is_active=is_active,
        shallow_summary=shallow_summary,
        # Bug 2 修复后：有 summary 且 pushed_at <= last_shallow_refreshed_at 才跳过。
        # 测试中若要模拟"已完成刷新"，需要传一个比 pushed_at 更新的 last_shallow_refreshed_at。
        last_shallow_refreshed_at=last_shallow_refreshed_at,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile, project_id=project_id)
    fetched = await repo.get_ecosystem_profile(full_name, project_id=project_id)
    assert fetched is not None
    return fetched.id


async def _seed_settings(
    repo: StorageRepository,
    project_id: str = "proj-test",
    *,
    min_stars: int = 1000,
    top_n: int = 50,
    concurrency: int = 5,
) -> None:
    settings = EcosystemProjectSettings(
        project_id=project_id,
        min_stars=min_stars,
        top_n=top_n,
        shallow_concurrency=concurrency,
    )
    await repo.upsert_ecosystem_project_settings(settings)


# ============================================================
# Dispatch path
# ============================================================


async def test_tick_dispatches_active_profiles_missing_summary(
    repo: StorageRepository,
) -> None:
    """tick() picks active profiles with empty shallow_summary and dispatches.

    Bug 2 修复后语义：
    - owner/a, owner/b: 无 summary → 候选
    - owner/c: 有 summary 且 last_shallow_refreshed_at 设为刚刚（> pushed_at=None），
      _has_new_push 返回 False → 跳过
    """
    await _seed_settings(repo)
    repo_a = await _make_profile(repo, "owner/a", stars=10000)
    repo_b = await _make_profile(repo, "owner/b", stars=5000)
    # 已有总结且刷新时间戳最新 → 没有新 push → 跳过
    await _make_profile(
        repo,
        "owner/c",
        stars=4000,
        shallow_summary="既有总结",
        last_shallow_refreshed_at=datetime.now(tz=UTC),
    )

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()

    assert result.dispatched == 2
    assert result.queued == 2  # only candidates, owner/c was filtered
    dispatched_ids = {i.repo_id for i in result.intents}
    assert dispatched_ids == {repo_a, repo_b}

    # Each dispatch should have created an in-flight deep_review row —
    # D5: stage=queued + claimed_by='tick:*'; status derives to 'queued'.
    for intent in result.intents:
        assert intent.repo_full_name in {"owner/a", "owner/b"}
        assert intent.timeout_seconds == STAGE0_TIMEOUT_SECONDS
        review = await repo.get_deep_review(
            intent.deep_review_id, project_id="proj-test"
        )
        assert review is not None
        assert review.status == EcosystemDeepReviewStatus.QUEUED
        assert review.claimed_by is not None
        assert review.claimed_by.startswith("tick:")
        assert review.dispatch_prompt
        assert intent.repo_full_name in review.dispatch_prompt


async def test_tick_dispatches_all_candidates_no_budget_cap(
    repo: StorageRepository,
) -> None:
    """Bug 3 修复：tick() 一次性 dispatch 全部候选，不再受 concurrency 上限限制。

    concurrency 设置保留，但节流移至 worker claim 阶段，dispatch 阶段无上限。
    """
    await _seed_settings(repo, concurrency=2)
    for i in range(5):
        await _make_profile(repo, f"owner/r{i}", stars=2000 + i)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()
    # 所有 5 个候选全部入队，不再只取前 concurrency=2 个
    assert result.dispatched == 5
    assert result.queued == 5


async def test_tick_skips_inflight_repo(repo: StorageRepository) -> None:
    """Re-running tick does not duplicate dispatch for in-flight repos."""
    await _seed_settings(repo)
    _ = await _make_profile(repo, "owner/x", stars=8000)
    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    first = await worker.tick()
    assert first.dispatched == 1

    second = await worker.tick()
    assert second.dispatched == 0
    assert second.skipped_inflight == 1


async def test_tick_skips_inactive_or_failed_profiles(
    repo: StorageRepository,
) -> None:
    """is_active=False / is_deleted / is_private_now repos are excluded."""
    await _seed_settings(repo)
    inactive = await _make_profile(
        repo, "owner/inactive", stars=2000, is_active=False
    )
    deleted = await _make_profile(repo, "owner/deleted", stars=2000)
    await repo.mark_profile_deleted(deleted, project_id="proj-test")
    private = await _make_profile(repo, "owner/private", stars=2000)
    await repo.mark_profile_private(private, project_id="proj-test")
    alive = await _make_profile(repo, "owner/alive", stars=2000)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()
    assert result.dispatched == 1
    assert result.intents[0].repo_id == alive
    assert inactive not in {i.repo_id for i in result.intents}


async def test_enqueue_repo_dispatches_one(repo: StorageRepository) -> None:
    """Scanner hook entry point dispatches a single repo."""
    await _seed_settings(repo)
    rid = await _make_profile(repo, "owner/new", stars=3000)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    intent = await worker.enqueue_repo(rid)
    assert isinstance(intent, DispatchIntent)
    assert intent.repo_id == rid
    assert "owner/new" in intent.prompt


async def test_enqueue_repo_skips_already_summarized(
    repo: StorageRepository,
) -> None:
    """enqueue_repo no-ops on profiles that already have a summary."""
    await _seed_settings(repo)
    rid = await _make_profile(
        repo, "owner/done", stars=3000, shallow_summary="已扫描"
    )

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    intent = await worker.enqueue_repo(rid)
    assert intent is None


async def test_prompt_injects_lessons_from_pattern_searcher(
    repo: StorageRepository,
) -> None:
    """When pattern_searcher returns failure patterns they are injected."""
    await _seed_settings(repo)
    await _make_profile(repo, "owner/repo", stars=4000)

    async def fake_pattern_searcher(query: str, top_k: int = 3):
        return [
            {
                "type": "failure",
                "error": "description too short",
                "lesson": "fall back to README",
            }
        ]

    worker = EcosystemShallowQueueWorker(
        repo,
        project_id="proj-test",
        pattern_searcher=fake_pattern_searcher,
    )
    result = await worker.tick()
    assert result.dispatched == 1
    prompt = result.intents[0].prompt
    assert "fall back to README" in prompt
    assert "description too short" in prompt


async def test_settings_auto_created_when_missing(
    repo: StorageRepository,
) -> None:
    """Worker auto-creates default settings when none exist."""
    await _make_profile(repo, "owner/r", stars=1500)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()
    # Default settings (min_stars=1000, top_n=100) so the 1500-star repo qualifies.
    assert result.dispatched == 1
    settings = await repo.get_ecosystem_project_settings("proj-test")
    assert settings is not None
    assert settings.min_stars == 1000


async def test_queue_status_returns_metrics(repo: StorageRepository) -> None:
    """queue_status() reports accurate counts from DR table (Bug 4 修复).

    owner/a: 无 summary → _find_candidates 收入 → tick() 后 DR 行 stage=queued → pending=1
    owner/b: 有 summary + last_shallow_refreshed_at 最新 → 跳过 → DR 行 0 → pending 不变
    owner/del: 被删 → 计入 deleted，不参与 DR 计数
    """
    await _seed_settings(repo)
    await _make_profile(repo, "owner/a", stars=2000)  # 无 summary → 候选
    await _make_profile(
        repo,
        "owner/b",
        stars=2000,
        shallow_summary="done",
        last_shallow_refreshed_at=datetime.now(tz=UTC),  # 最新时间戳 → 跳过
    )
    deleted = await _make_profile(repo, "owner/del", stars=2000)
    await repo.mark_profile_deleted(deleted, project_id="proj-test")

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")

    # 先跑一次 tick 让 owner/a 进入 DR 表（stage=queued）
    await worker.tick()

    status = await worker.queue_status()
    # Bug 4 修复后：pending 来自 DR 行 stage_status='queued' AND claimed_by IS NULL。
    # D5 收敛后 tick 建行原子携带 claimed_by → 派遣行直接计入 in_progress
    # （语义正确化：已派遣 ≠ 待认领），pending 归 0。
    assert status["pending"] == 0
    assert status["in_progress"] == 1
    assert status["pending_shallow"] == 0  # 向后兼容别名
    assert status["in_flight"] == 1  # 向后兼容别名
    assert status["deleted"] == 1
    assert status["concurrency"] == 5


async def test_no_project_id_returns_empty_tick(repo: StorageRepository) -> None:
    """Worker without project_id has no settings -> no work."""
    await _make_profile(repo, "owner/a", stars=2000, project_id="proj-x")

    worker = EcosystemShallowQueueWorker(repo, project_id="")
    result = await worker.tick()
    assert result.dispatched == 0
    assert result.queued == 0


# ============================================================
# Bug 2 二次修复: stage_status 判定测试 (v1.6.1)
# ============================================================


async def test_tick_dispatches_shallow_done_repo_with_new_push(
    repo: StorageRepository,
) -> None:
    """Bug 2 二次修复：stage='shallow_done' + 有新 push 的库应该被重新派遣。

    模拟场景：profile 有 shallow_summary，pushed_at 比 last_shallow_refreshed_at 新
    （即 _has_new_push 返回 True）。tick 应创建新 DR 行并 dispatch=1, skipped=0。
    现有的 shallow_done DR 行不应阻止新一轮入队。
    """
    from datetime import timedelta

    await _seed_settings(repo)
    # pushed_at 为 now，last_shallow_refreshed_at 为过去（模拟有新 push）
    old_refresh_time = datetime.now(tz=UTC) - timedelta(days=7)
    rid = await _make_profile(
        repo,
        "owner/old-with-new-push",
        stars=3000,
        shallow_summary="旧总结",
        last_shallow_refreshed_at=old_refresh_time,
    )
    # 手动设置 pushed_at 比 last_shallow_refreshed_at 更新（模拟 GitHub 新推送）
    # 直接更新 profile pushed_at 为 now (比 last_shallow_refreshed_at 更新)
    from aiteam.types import EcosystemDeepReview, EcosystemDeepReviewStatus, EcosystemStageStatus

    # 先创建一个历史 shallow_done DR 行。D5 后 create_deep_review 强制派生
    # status，历史脏数据 (status='running') 需经通用 setter 直写构造。
    history_dr = EcosystemDeepReview(
        project_id="proj-test",
        repo_id=rid,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,  # 已完成阶段
    )
    await repo.create_deep_review(history_dr, project_id="proj-test")
    await repo.update_deep_review(
        history_dr.id,
        _project_id="proj-test",
        status=EcosystemDeepReviewStatus.RUNNING,  # 历史脏数据
    )

    # 更新 profile 的 pushed_at 为比 last_shallow_refreshed_at 更新的时间
    profile = await repo.get_ecosystem_profile_by_id(rid, project_id="proj-test")
    assert profile is not None
    new_pushed_at = datetime.now(tz=UTC)  # 新 push 时间
    await repo.update_profile_shallow_summary(
        rid,
        shallow_summary=profile.shallow_summary or "旧总结",
        refreshed_at=old_refresh_time,
        project_id="proj-test",
    )
    # 直接设置 pushed_at 为新时间
    from aiteam.storage.connection import get_session
    from aiteam.storage.models import EcosystemRepoProfileModel
    async with get_session(repo._db_url) as session:
        from sqlalchemy import update as sa_update
        stmt = sa_update(EcosystemRepoProfileModel).where(
            EcosystemRepoProfileModel.id == rid
        ).values(pushed_at=new_pushed_at)
        await session.execute(stmt)
        await session.flush()

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()

    # 有新 push 的老库（stage=shallow_done）应该被重新派遣，不被跳过
    assert result.dispatched == 1, (
        f"expected dispatched=1, got dispatched={result.dispatched}, "
        f"skipped={result.skipped_inflight}"
    )
    assert result.skipped_inflight == 0


async def test_tick_skips_queued_stage_dr_row(repo: StorageRepository) -> None:
    """Bug 2 二次修复：stage='queued' 的 DR 行应被跳过（真正 in-flight）。

    模拟场景：profile 没有 summary，tick 创建 DR 行后 stage=queued。
    第二次 tick 应识别 stage=queued 的 DR 行并 skip，不重复 dispatch。
    """
    await _seed_settings(repo)
    await _make_profile(repo, "owner/inflight", stars=4000)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    first = await worker.tick()
    assert first.dispatched == 1  # 首次正常入队

    # 将刚创建的 DR 行 stage_status 保持为 queued（默认就是 queued），模拟未完成
    second = await worker.tick()
    assert second.dispatched == 0
    assert second.skipped_inflight == 1  # stage=queued 被正确跳过


async def test_backfill_shallow_done_status_completed(repo: StorageRepository) -> None:
    """backfill 方法将 stage='shallow_done' + status='running' 的行修正为 'completed'。

    1. 创建 stage=shallow_done + status=running 的脏数据行（历史场景）
    2. 创建 stage=shallow_done + status=completed 的干净行（不应被改动）
    3. 运行 backfill
    4. 验证脏数据行变为 completed，干净行不变
    """
    from aiteam.types import EcosystemDeepReview, EcosystemDeepReviewStatus, EcosystemStageStatus

    await _seed_settings(repo)
    rid = await _make_profile(repo, "owner/backfill-test", stars=2000)

    # 创建脏数据行：stage=shallow_done + status=running。
    # D5 后 create_deep_review 强制派生 status，历史脏数据需经通用 setter 直写构造。
    dirty_dr = EcosystemDeepReview(
        project_id="proj-test",
        repo_id=rid,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,
    )
    await repo.create_deep_review(dirty_dr, project_id="proj-test")
    await repo.update_deep_review(
        dirty_dr.id,
        _project_id="proj-test",
        status=EcosystemDeepReviewStatus.RUNNING,
    )

    # 创建干净行：stage=shallow_done + status=completed (不同 repo_id 以便区分)
    rid2 = await _make_profile(repo, "owner/backfill-clean", stars=2000)
    clean_dr = EcosystemDeepReview(
        project_id="proj-test",
        repo_id=rid2,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,  # create 派生 status=completed
    )
    await repo.create_deep_review(clean_dr, project_id="proj-test")

    # 运行 backfill
    fixed_count = await repo.backfill_shallow_done_status_completed(project_id="proj-test")
    assert fixed_count == 1, f"expected 1 row fixed, got {fixed_count}"

    # 验证脏数据行已修正
    dirty_dr_updated = await repo.get_deep_review(dirty_dr.id, project_id="proj-test")
    assert dirty_dr_updated is not None
    assert dirty_dr_updated.status == EcosystemDeepReviewStatus.COMPLETED

    # 验证干净行未被二次修改（仍是 completed，rowcount 不计入）
    clean_dr_check = await repo.get_deep_review(clean_dr.id, project_id="proj-test")
    assert clean_dr_check is not None
    assert clean_dr_check.status == EcosystemDeepReviewStatus.COMPLETED


# ============================================================
# D5 收敛: tick/claim 双认领缺口回归 (v1.6.2)
# ============================================================


async def test_tick_dispatched_row_cannot_be_double_claimed(
    repo: StorageRepository,
) -> None:
    """D5 认领缺口回归：tick 派遣行建行即原子携带 claimed_by，竞争窗口=0。

    历史缺口：INSERT(stage=queued, claimed_by=NULL) 后该行永久满足
    claim_next_shallow_repo 候选条件，tick 派遣与 claim 认领可双抓同行。
    修复后 claim 的候选 SELECT (queued, unclaimed) 在任何时刻都看不到
    tick 建的行 → 返回 None。
    """
    await _seed_settings(repo)
    await _make_profile(repo, "owner/no-double-claim", stars=8000)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()
    assert result.dispatched == 1
    dr_id = result.intents[0].deep_review_id

    review = await repo.get_deep_review(dr_id, project_id="proj-test")
    assert review is not None
    assert review.stage_status == EcosystemStageStatus.QUEUED
    assert review.claimed_by is not None and review.claimed_by.startswith("tick:")

    # claim worker 此刻绝不能抢到 tick 已派遣的行
    claimed = await repo.claim_next_shallow_repo(
        worker_id="rival-worker", project_id="proj-test"
    )
    assert claimed is None


async def test_stage_advance_releases_tick_claim(
    repo: StorageRepository,
) -> None:
    """D5 配套释放：stage 推进（SHALLOW_DONE）统一清 claimed_by/claimed_at，
    并派生 status=completed + 补 completed_at，防止悬挂占住。"""
    await _seed_settings(repo)
    await _make_profile(repo, "owner/release-on-advance", stars=8000)

    worker = EcosystemShallowQueueWorker(repo, project_id="proj-test")
    result = await worker.tick()
    dr_id = result.intents[0].deep_review_id

    advanced = await repo.update_deep_review_stage(
        dr_id, EcosystemStageStatus.SHALLOW_DONE, project_id="proj-test"
    )
    assert advanced is not None
    assert advanced.claimed_by is None
    assert advanced.claimed_at is None
    assert advanced.status == EcosystemDeepReviewStatus.COMPLETED  # 派生
    assert advanced.completed_at is not None  # choke point 补齐
