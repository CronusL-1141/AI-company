"""Pipeline storage layer unit tests — PipelineState + StageTransition (append-only)."""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.pipeline.clock import FakeClock
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite repository for pipeline storage tests."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def task_id(repo: StorageRepository) -> str:
    """Create a minimal task and return its ID."""
    team = await repo.create_team("test-team", "coordinate")
    task = await repo.create_task(team_id=team.id, title="test task")
    return task.id


@pytest_asyncio.fixture()
async def another_task_id(repo: StorageRepository) -> str:
    """Create a second task for isolation tests."""
    team = await repo.create_team("test-team-2", "coordinate")
    task = await repo.create_task(team_id=team.id, title="another task")
    return task.id


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


async def test_get_pipeline_state_returns_none_when_no_config(
    repo: StorageRepository, task_id: str
) -> None:
    """task 无 config['pipeline'] 时，get_pipeline_state 返回 None 而非空对象。"""
    result = await repo.get_pipeline_state(task_id)
    assert result is None


async def test_set_and_get_pipeline_state_roundtrip(
    repo: StorageRepository, task_id: str
) -> None:
    """set_pipeline_state 写入后 get_pipeline_state 读出值一致。"""
    clock = FakeClock()
    await repo.set_pipeline_state(
        task_id,
        clock=clock,
        template="feature",
        current_stage="implement",
        current_stage_class="Execute",
        autopilot_active=False,
    )
    state = await repo.get_pipeline_state(task_id)
    assert state is not None
    assert state.template == "feature"
    assert state.current_stage == "implement"
    assert state.current_stage_class == "Execute"
    assert state.autopilot_active is False


async def test_set_pipeline_state_merges_not_overwrites(
    repo: StorageRepository, task_id: str
) -> None:
    """多次调用 set_pipeline_state 是 merge 语义，不覆盖已有字段。

    先写 autopilot_active=True，再只改 current_stage，autopilot_active 应保留。
    """
    await repo.set_pipeline_state(task_id, autopilot_active=True, current_stage="research")
    await repo.set_pipeline_state(task_id, current_stage="implement")

    state = await repo.get_pipeline_state(task_id)
    assert state is not None
    assert state.current_stage == "implement"
    assert state.autopilot_active is True  # must survive the second call


async def test_set_pipeline_state_preserves_other_config_fields(
    repo: StorageRepository, task_id: str
) -> None:
    """set_pipeline_state 不影响 task.config 中其他非 pipeline 字段。"""
    from sqlalchemy import select

    from aiteam.storage.connection import get_session
    from aiteam.storage.models import TaskModel

    # Directly set a non-pipeline config field
    async with get_session(repo._db_url) as session:
        result = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        row = result.scalar_one()
        row.config = {"custom_key": "custom_value"}

    await repo.set_pipeline_state(task_id, current_stage="test")

    async with get_session(repo._db_url) as session:
        result = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
        row = result.scalar_one()
        assert row.config.get("custom_key") == "custom_value"
        assert row.config.get("pipeline", {}).get("current_stage") == "test"


async def test_append_and_read_stage_history(
    repo: StorageRepository, task_id: str
) -> None:
    """append_stage_history 写入后 read_stage_history 按升序返回记录。"""
    clock = FakeClock()

    await repo.append_stage_history(
        task_id, from_stage=None, to_stage="research", triggered_by="manual", clock=clock
    )
    clock.advance(seconds=60)
    await repo.append_stage_history(
        task_id, from_stage="research", to_stage="implement", triggered_by="auto", clock=clock
    )

    history = await repo.read_stage_history(task_id)
    assert len(history) == 2
    assert history[0].from_stage is None
    assert history[0].to_stage == "research"
    assert history[1].from_stage == "research"
    assert history[1].to_stage == "implement"
    # ascending order: first entry must be earlier
    assert history[0].transitioned_at < history[1].transitioned_at


async def test_read_stage_history_task_isolation(
    repo: StorageRepository, task_id: str, another_task_id: str
) -> None:
    """read_stage_history 只返回指定 task_id 的记录，不跨任务泄漏。"""
    await repo.append_stage_history(task_id, from_stage=None, to_stage="research")
    await repo.append_stage_history(another_task_id, from_stage=None, to_stage="diagnose")

    history_a = await repo.read_stage_history(task_id)
    history_b = await repo.read_stage_history(another_task_id)

    assert len(history_a) == 1
    assert history_a[0].to_stage == "research"
    assert len(history_b) == 1
    assert history_b[0].to_stage == "diagnose"


async def test_append_stage_history_no_update_interface(
    repo: StorageRepository, task_id: str
) -> None:
    """StorageRepository 不暴露 update_stage_history 方法（append-only 保证）。"""
    assert not hasattr(repo, "update_stage_history")
    assert not hasattr(repo, "delete_stage_history")


async def test_stage_transition_triggered_by_valid_values(
    repo: StorageRepository, task_id: str
) -> None:
    """triggered_by 只接受 manual/auto/force/system 四个有效值；无效值 Pydantic 应拒绝。"""
    from pydantic import ValidationError

    from aiteam.types import StageTransition

    for valid in ("manual", "auto", "force", "system"):
        t = StageTransition(task_id=task_id, to_stage="implement", triggered_by=valid)
        assert t.triggered_by == valid

    with pytest.raises(ValidationError):
        StageTransition(task_id=task_id, to_stage="implement", triggered_by="invalid_value")


async def test_read_stage_history_default_limit_50(
    repo: StorageRepository, task_id: str
) -> None:
    """read_stage_history 默认 limit=50，超过 50 条时只返回前 50 条（按升序）。"""
    clock = FakeClock()
    for i in range(60):
        await repo.append_stage_history(
            task_id, from_stage=None, to_stage=f"stage-{i}", clock=clock
        )
        clock.advance(seconds=1)

    history = await repo.read_stage_history(task_id)
    assert len(history) == 50
    # Verify ascending order: stage-0 comes first
    assert history[0].to_stage == "stage-0"
