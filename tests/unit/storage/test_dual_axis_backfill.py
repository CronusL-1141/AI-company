"""D5 双轴收敛测试 — 派生映射真源 / choke point / R1-F3 回填矩阵与幂等。

覆盖（对应 D5 测试计划 T1-T6）：
- T1: types.STAGE_TO_STATUS 全量覆盖 + derive_status_from_stage 纯函数契约；
- T2: create_deep_review choke point 强制派生（忽略调用方传入 status）；
- T3: update_deep_review_stage choke point 派生 + 清认领 + completed_at
      COALESCE（已有值绝不覆盖，历史不可改写）；
- T4: backfill_deep_review_dual_axis 真实组合矩阵（R1/R2/F1/F2/F3 各规则
      rowcount 与终态），内嵌顺序论证（R1 先于 F3，(completed,queued)
      老行绝不被误重置回 queued）；
- T5: 回填幂等 — 两跑第二遍 rowcount 恒 0、行零变化；
- T6: project 隔离 — 显式 project_id 只触本项目行（红线3 现范式）。

认领缺口并发回归（tick 建行原子携带 claimed_by）见
tests/unit/services/test_ecosystem_shallow_queue.py::test_tick_dispatched_row_cannot_be_double_claimed。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    STAGE_TO_STATUS,
    EcosystemDeepReview,
    EcosystemDeepReviewStatus,
    EcosystemRepoProfile,
    EcosystemStageStatus,
    derive_status_from_stage,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """内存 SQLite 仓库用于测试。"""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def sample_repo_id(repo: StorageRepository) -> str:
    """先建一个 EcosystemRepoProfile 返回其 id 用作 FK。"""
    profile = EcosystemRepoProfile(
        repo_full_name="anthropics/claude-code",
        name="claude-code",
        owner="anthropics",
        stars=50000,
        last_scanned_at=datetime.now(tz=UTC),
    )
    await repo.upsert_ecosystem_profile(profile)
    fetched = await repo.get_ecosystem_profile("anthropics/claude-code")
    assert fetched is not None
    return fetched.id


async def _make_legacy_row(
    repo: StorageRepository,
    repo_id: str,
    *,
    status: EcosystemDeepReviewStatus,
    stage: EcosystemStageStatus = EcosystemStageStatus.QUEUED,
    project_id: str | None = None,
    **extra_fields,
) -> EcosystemDeepReview:
    """构造历史脏数据行：先经 create（会强制派生），再用通用 setter 直写脏 status。

    D5 后 create_deep_review 是 choke point，脏组合只能经低层 setter 复现 —
    与真实历史行的落库路径等价（老 reviewer 当年正是直写 status 列）。
    """
    row = EcosystemDeepReview(
        project_id=project_id,
        repo_id=repo_id,
        stage_status=stage,
    )
    await repo.create_deep_review(row, project_id=project_id)
    updated = await repo.update_deep_review(
        row.id,
        _project_id=project_id,
        status=status,
        **extra_fields,
    )
    assert updated is not None
    return updated


# ---------------------------------------------------------------------------
# T1: 映射真源 — STAGE_TO_STATUS / derive_status_from_stage
# ---------------------------------------------------------------------------


def test_stage_to_status_covers_every_stage() -> None:
    """STAGE_TO_STATUS 必须覆盖 EcosystemStageStatus 全部成员（唯一真源无死角）。"""
    assert set(STAGE_TO_STATUS.keys()) == set(EcosystemStageStatus)


def test_stage_to_status_mapping_values() -> None:
    """规格钉死的映射：queued→queued；5 完成态→completed；3 失败态→failed。"""
    expected = {
        EcosystemStageStatus.QUEUED: EcosystemDeepReviewStatus.QUEUED,
        EcosystemStageStatus.SHALLOW_DONE: EcosystemDeepReviewStatus.COMPLETED,
        EcosystemStageStatus.ARCHITECTURE_DONE: EcosystemDeepReviewStatus.COMPLETED,
        EcosystemStageStatus.DEBATED: EcosystemDeepReviewStatus.COMPLETED,
        EcosystemStageStatus.REFERENCED: EcosystemDeepReviewStatus.COMPLETED,
        EcosystemStageStatus.INTEGRATED: EcosystemDeepReviewStatus.COMPLETED,
        EcosystemStageStatus.SHALLOW_FAILED: EcosystemDeepReviewStatus.FAILED,
        EcosystemStageStatus.ARCHITECTURE_FAILED: EcosystemDeepReviewStatus.FAILED,
        EcosystemStageStatus.DEBATED_FAILED: EcosystemDeepReviewStatus.FAILED,
    }
    assert STAGE_TO_STATUS == expected


def test_derive_status_accepts_enum_and_str() -> None:
    """derive_status_from_stage 接受 enum 与字符串（先归一）。"""
    assert (
        derive_status_from_stage(EcosystemStageStatus.DEBATED)
        is EcosystemDeepReviewStatus.COMPLETED
    )
    assert (
        derive_status_from_stage("shallow_failed")
        is EcosystemDeepReviewStatus.FAILED
    )
    assert derive_status_from_stage("queued") is EcosystemDeepReviewStatus.QUEUED


def test_derive_status_rejects_unknown_stage() -> None:
    """未知 stage 字符串抛 ValueError（与 EcosystemStageStatus() 同契约）。"""
    with pytest.raises(ValueError):
        derive_status_from_stage("not-a-stage")


# ---------------------------------------------------------------------------
# T2: create_deep_review choke point — 强制派生，忽略调用方 status
# ---------------------------------------------------------------------------


async def test_create_forces_derived_status(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """建行时 status 被强制派生，调用方传入的 RUNNING 被忽略。"""
    row = EcosystemDeepReview(
        repo_id=sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,  # 调用方伪造，应被忽略
        stage_status=EcosystemStageStatus.QUEUED,
    )
    await repo.create_deep_review(row)

    fetched = await repo.get_deep_review(row.id)
    assert fetched is not None
    assert fetched.status == EcosystemDeepReviewStatus.QUEUED  # 派生自 stage


async def test_create_derives_completed_for_shallow_done(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """lifecycle 建行范式：stage=shallow_done → status 派生 completed。"""
    row = EcosystemDeepReview(
        repo_id=sample_repo_id,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,
    )
    await repo.create_deep_review(row)

    fetched = await repo.get_deep_review(row.id)
    assert fetched is not None
    assert fetched.status == EcosystemDeepReviewStatus.COMPLETED


# ---------------------------------------------------------------------------
# T3: update_deep_review_stage choke point — 派生 + 清认领 + COALESCE 补时间戳
# ---------------------------------------------------------------------------


async def test_stage_advance_derives_failed_and_fills_completed_at(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """推 SHALLOW_FAILED：status 派生 failed、认领释放、completed_at 补齐。"""
    row = EcosystemDeepReview(
        repo_id=sample_repo_id,
        stage_status=EcosystemStageStatus.QUEUED,
        claimed_by="tick:deadbeef",
        claimed_at=datetime.now(tz=UTC),
    )
    await repo.create_deep_review(row)

    advanced = await repo.update_deep_review_stage(
        row.id, EcosystemStageStatus.SHALLOW_FAILED
    )
    assert advanced is not None
    assert advanced.status == EcosystemDeepReviewStatus.FAILED
    assert advanced.claimed_by is None
    assert advanced.claimed_at is None
    assert advanced.completed_at is not None


async def test_stage_advance_never_overwrites_completed_at(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """COALESCE 语义：行已有 completed_at 时 stage 推进绝不覆盖（历史不可改写）。"""
    historic_ts = datetime(2025, 1, 1, tzinfo=UTC)
    row = EcosystemDeepReview(
        repo_id=sample_repo_id,
        stage_status=EcosystemStageStatus.SHALLOW_DONE,
    )
    await repo.create_deep_review(row)
    await repo.update_deep_review(row.id, completed_at=historic_ts)

    advanced = await repo.update_deep_review_stage(
        row.id, EcosystemStageStatus.ARCHITECTURE_DONE
    )
    assert advanced is not None
    assert advanced.status == EcosystemDeepReviewStatus.COMPLETED
    # SQLite 落库剥 tzinfo，按 naive wall time 比较：原值保留、未被覆盖为 now
    assert advanced.completed_at is not None
    assert advanced.completed_at.replace(tzinfo=None) == historic_ts.replace(
        tzinfo=None
    )


# ---------------------------------------------------------------------------
# T4: 回填矩阵 — 真实组合一一有归属，分规则 rowcount 可对账
# ---------------------------------------------------------------------------


async def test_backfill_matrix_covers_real_combinations(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """8 类真实历史组合单跑一次回填：R1=1 R2=1 F1=2 F2=1 F3=2，干净行零触碰。"""
    ts = datetime(2025, 6, 1, tzinfo=UTC)

    # R1: 老 reviewer 完成行 (completed, queued) → stage 推到 shallow_done
    r1_row = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        stage=EcosystemStageStatus.QUEUED,
        completed_at=ts,
    )
    # R2: 老 reviewer 失败行 (failed, queued) 带派遣证据 → shallow_failed
    r2_row = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.FAILED,
        stage=EcosystemStageStatus.QUEUED,
        started_at=ts,
        dispatch_prompt="dispatched once",
    )
    # F3(b): (failed, queued) 无任何派遣证据 → 不过 R2 gate，重置回真排队
    f3_no_evidence = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.FAILED,
        stage=EcosystemStageStatus.QUEUED,
    )
    # F1(a): v1.5.2 主病灶 (running, shallow_done) → status=completed
    f1_running = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.SHALLOW_DONE,
    )
    # F1(b): (queued, architecture_done) — 只推 stage 从不碰 status 的行
    f1_queued = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.QUEUED,
        stage=EcosystemStageStatus.ARCHITECTURE_DONE,
    )
    # F2: (running, shallow_failed) — report_failure 只推 stage 的行
    f2_row = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.SHALLOW_FAILED,
    )
    # F3(a): (running, queued) — 漂移出生点在飞/死行
    f3_running = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.QUEUED,
    )
    # 干净行 (queued, queued) 双默认 → 全链 no-op
    clean = EcosystemDeepReview(
        repo_id=sample_repo_id, stage_status=EcosystemStageStatus.QUEUED
    )
    await repo.create_deep_review(clean)

    counts = await repo.backfill_deep_review_dual_axis()
    assert counts == {"R1": 1, "R2": 1, "F1": 2, "F2": 1, "F3": 2}

    # R1 终态：stage 推离 queued，shallow_completed_at COALESCE 自 completed_at
    r1_after = await repo.get_deep_review(r1_row.id)
    assert r1_after is not None
    assert r1_after.stage_status == EcosystemStageStatus.SHALLOW_DONE
    assert r1_after.status == EcosystemDeepReviewStatus.COMPLETED  # F1 no-op
    # SQLite 落库剥 tzinfo，按 naive wall time 比较 COALESCE 结果
    assert r1_after.shallow_completed_at is not None
    assert r1_after.shallow_completed_at.replace(tzinfo=None) == ts.replace(
        tzinfo=None
    )

    # R2 终态：stage=shallow_failed，status 保持 failed（F2 no-op）
    r2_after = await repo.get_deep_review(r2_row.id)
    assert r2_after is not None
    assert r2_after.stage_status == EcosystemStageStatus.SHALLOW_FAILED
    assert r2_after.status == EcosystemDeepReviewStatus.FAILED

    # F1 终态：status=completed + completed_at 补齐（COALESCE created_at）
    for fixed in (f1_running, f1_queued):
        after = await repo.get_deep_review(fixed.id)
        assert after is not None
        assert after.status == EcosystemDeepReviewStatus.COMPLETED
        assert after.completed_at is not None

    # F2 终态：status=failed
    f2_after = await repo.get_deep_review(f2_row.id)
    assert f2_after is not None
    assert f2_after.status == EcosystemDeepReviewStatus.FAILED
    assert f2_after.completed_at is not None

    # F3 终态：status 重置 queued，stage 仍 queued（可被重新 claim）
    for reset in (f3_running, f3_no_evidence):
        after = await repo.get_deep_review(reset.id)
        assert after is not None
        assert after.status == EcosystemDeepReviewStatus.QUEUED
        assert after.stage_status == EcosystemStageStatus.QUEUED

    # 干净行零触碰
    clean_after = await repo.get_deep_review(clean.id)
    assert clean_after is not None
    assert clean_after.status == EcosystemDeepReviewStatus.QUEUED
    assert clean_after.stage_status == EcosystemStageStatus.QUEUED


async def test_backfill_order_r1_before_f3_protects_completed_rows(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """顺序铁律：(completed, queued) 老行必须被 R1 推离 queued，绝不落入 F3
    被误重置为 (queued, queued) —— 整链最高危陷阱的直接回归。"""
    legacy = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        stage=EcosystemStageStatus.QUEUED,
        completed_at=datetime(2025, 3, 1, tzinfo=UTC),
    )

    await repo.backfill_deep_review_dual_axis()

    after = await repo.get_deep_review(legacy.id)
    assert after is not None
    # 若顺序颠倒（F3 先跑），status 会被重置为 queued —— 此断言必红
    assert after.status == EcosystemDeepReviewStatus.COMPLETED
    assert after.stage_status == EcosystemStageStatus.SHALLOW_DONE


async def test_backfill_r2_gate_requires_dispatch_evidence(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """R2 gate：report_id 单证据也可推 shallow_failed；三证据全空走 F3。"""
    with_report = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.FAILED,
        stage=EcosystemStageStatus.QUEUED,
        report_id="rep-1",
    )
    no_evidence = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.FAILED,
        stage=EcosystemStageStatus.QUEUED,
    )

    counts = await repo.backfill_deep_review_dual_axis()
    assert counts["R2"] == 1
    assert counts["F3"] == 1

    a = await repo.get_deep_review(with_report.id)
    b = await repo.get_deep_review(no_evidence.id)
    assert a is not None and b is not None
    assert a.stage_status == EcosystemStageStatus.SHALLOW_FAILED
    assert a.status == EcosystemDeepReviewStatus.FAILED
    assert b.stage_status == EcosystemStageStatus.QUEUED
    assert b.status == EcosystemDeepReviewStatus.QUEUED


# ---------------------------------------------------------------------------
# T5: 幂等 — 两跑第二遍 rowcount 恒 0
# ---------------------------------------------------------------------------


async def test_backfill_is_idempotent(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """每条 WHERE 都排除自身写后状态：第二跑全规则 rowcount=0、行零变化。"""
    await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.COMPLETED,
        stage=EcosystemStageStatus.QUEUED,
    )
    await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.SHALLOW_DONE,
    )
    await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.QUEUED,
        started_at=datetime.now(tz=UTC) - timedelta(days=30),
    )

    first = await repo.backfill_deep_review_dual_axis()
    assert sum(first.values()) == 3

    snapshot = {
        r.id: (r.status, r.stage_status, r.completed_at, r.shallow_completed_at)
        for r in await repo.list_deep_reviews(limit=100)
    }

    second = await repo.backfill_deep_review_dual_axis()
    assert second == {"R1": 0, "R2": 0, "F1": 0, "F2": 0, "F3": 0}

    resnap = {
        r.id: (r.status, r.stage_status, r.completed_at, r.shallow_completed_at)
        for r in await repo.list_deep_reviews(limit=100)
    }
    assert resnap == snapshot


async def test_backfill_empty_table_returns_zero(repo: StorageRepository) -> None:
    """空库（本地 Win→Mac 未迁数据场景）回填安全返回全 0。"""
    counts = await repo.backfill_deep_review_dual_axis()
    assert counts == {"R1": 0, "R2": 0, "F1": 0, "F2": 0, "F3": 0}


# ---------------------------------------------------------------------------
# T6: project 隔离 — 显式 project_id 只触本项目行（红线3 现范式）
# ---------------------------------------------------------------------------


async def test_backfill_respects_project_scope(
    repo: StorageRepository, sample_repo_id: str
) -> None:
    """backfill(project_id='p1') 只修 p1 的脏行；全局跑再收 p2。"""
    dirty_p1 = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.SHALLOW_DONE,
        project_id="p1",
    )
    dirty_p2 = await _make_legacy_row(
        repo, sample_repo_id,
        status=EcosystemDeepReviewStatus.RUNNING,
        stage=EcosystemStageStatus.SHALLOW_DONE,
        project_id="p2",
    )

    counts = await repo.backfill_deep_review_dual_axis(project_id="p1")
    assert counts["F1"] == 1

    p1_after = await repo.get_deep_review(dirty_p1.id, project_id="p1")
    p2_after = await repo.get_deep_review(dirty_p2.id, project_id="p2")
    assert p1_after is not None and p2_after is not None
    assert p1_after.status == EcosystemDeepReviewStatus.COMPLETED
    assert p2_after.status == EcosystemDeepReviewStatus.RUNNING  # 未触碰

    global_counts = await repo.backfill_deep_review_dual_axis()
    assert global_counts["F1"] == 1
    p2_final = await repo.get_deep_review(dirty_p2.id, project_id="p2")
    assert p2_final is not None
    assert p2_final.status == EcosystemDeepReviewStatus.COMPLETED
