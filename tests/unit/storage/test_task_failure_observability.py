"""失败观测写入侧回归测试 — 观测断链闭合批 ①。

断链实录（2026-07-28 取证）：``EventType.TASK_FAILED`` 自声明起就没有任何**存活**
写入方 ——

* ``config/settings.py`` 的通知事件白名单引用了 ``task.failed``；
* ``api/routes/settings.py`` 的默认通知配置也列了它；
* ``orchestrator/team_manager.py`` 里确实有一处 ``_emit("task.failed", ...)``，
  但它挂在 ``event_bus`` 上且该模块全仓无导入方 —— 是退役编排路径的死码。

于是「任务失败」这件事在事件流里从来没出现过，失败观测全靠 agent 自己汇报；
agent 卡死 / 被杀 / 静默转 failed 时，系统对失败一无所知。

本组测试把 ``task.failed`` 钉死在**存储层状态机的唯一入口**
``StorageRepository.update_task`` 上：只要状态被改成 failed，无论调用方是 API、
MCP 工具、reaper 回收还是直接调 repo，事件都必须落库，并且带得走前状态与失败
上下文——不依赖任何调用方的自觉。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    """In-memory SQLite repo for isolation."""
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


async def _make_task(repo: StorageRepository, title: str = "观测断链闭合验证任务"):
    """Create a team + task and return the task."""
    team = await repo.create_team(name="d1-observability", mode="coordinate")
    return await repo.create_task(team.id, title=title, description="失败观测写入侧验证")


async def _failed_events(repo: StorageRepository, task_id: str) -> list:
    """Fetch all task.failed events for one task."""
    events = await repo.list_events(event_type="task.failed", limit=100)
    return [e for e in events if e.entity_id == task_id]


# ---------------------------------------------------------------------------
# 核心：状态机转 failed 必须落事件
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_status_to_failed_emits_task_failed(repo: StorageRepository) -> None:
    """状态改成 failed → 存储层必须自动落一条 task.failed（不靠调用方自报）。"""
    task = await _make_task(repo)
    await repo.update_task(task.id, status=TaskStatus.RUNNING.value)

    await repo.update_task(task.id, status="failed", result="worker 进程被杀，无自报")

    hits = await _failed_events(repo, task.id)
    assert len(hits) == 1, "转 failed 未落 task.failed 事件——观测断链"
    assert hits[0].entity_type == "task"
    assert hits[0].source == "repository"


@pytest.mark.asyncio()
async def test_task_failed_carries_prior_status_and_context(repo: StorageRepository) -> None:
    """事件必须带前状态与失败上下文，否则事后无法归因。"""
    task = await _make_task(repo)
    await repo.update_task(task.id, status=TaskStatus.RUNNING.value)
    await repo.update_task(task.id, status="failed", result="依赖的 API 连续 3 次超时")

    hits = await _failed_events(repo, task.id)
    assert len(hits) == 1
    data = hits[0].data

    assert data["task_id"] == task.id
    # 前状态：失败是从哪个状态掉下来的，是归因的第一手信息
    assert data["from_status"] == "running", f"前状态丢失: {data}"
    assert data["to_status"] == "failed"
    # 失败上下文
    assert "超时" in (data.get("failure_context") or ""), f"失败上下文丢失: {data}"
    assert data["team_id"] == task.team_id
    # 快照仍按 update_task 的既有约定给出
    assert hits[0].state_snapshot["status"] == "failed"


@pytest.mark.asyncio()
async def test_enum_status_input_also_emits(repo: StorageRepository) -> None:
    """调用方传 TaskStatus 枚举（team_manager 老写法）时同样要落事件。"""
    task = await _make_task(repo)
    await repo.update_task(task.id, status=TaskStatus.FAILED, result="枚举入参路径")

    hits = await _failed_events(repo, task.id)
    assert len(hits) == 1, "枚举入参路径漏采"


@pytest.mark.asyncio()
async def test_repeated_failed_update_does_not_duplicate(repo: StorageRepository) -> None:
    """只在「转入」失败态时发一次；反复写 failed 不刷屏（事件流是账本不是日志）。"""
    task = await _make_task(repo)
    await repo.update_task(task.id, status="failed", result="第一次失败")
    await repo.update_task(task.id, result="补一句说明")  # 不改状态
    await repo.update_task(task.id, status="failed", result="重复写同状态")

    hits = await _failed_events(repo, task.id)
    assert len(hits) == 1, f"重复转 failed 造成事件刷屏: {len(hits)} 条"


@pytest.mark.asyncio()
async def test_non_failure_transition_emits_nothing(repo: StorageRepository) -> None:
    """正常流转不得误报失败。"""
    task = await _make_task(repo)
    await repo.update_task(task.id, status=TaskStatus.RUNNING.value)
    await repo.update_task(task.id, status=TaskStatus.COMPLETED.value, result="顺利完成")

    assert await _failed_events(repo, task.id) == []


@pytest.mark.asyncio()
async def test_recovery_then_fail_again_emits_second_event(repo: StorageRepository) -> None:
    """failed → 重试 running → 再 failed：第二次失败是新事实，必须再落一条。"""
    task = await _make_task(repo)
    await repo.update_task(task.id, status="failed", result="第一次失败")
    await repo.update_task(task.id, status=TaskStatus.RUNNING.value)  # 重试
    await repo.update_task(task.id, status="failed", result="第二次失败")

    hits = await _failed_events(repo, task.id)
    assert len(hits) == 2, f"重试后再次失败漏采: {len(hits)} 条"
    assert {h.data["from_status"] for h in hits} == {"pending", "running"}


# ---------------------------------------------------------------------------
# 诊断结果同样要落事件：分析做过了但没留痕，等于没做
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_failure_analysis_lands_event(repo: StorageRepository) -> None:
    """failure_analysis（失败炼金）跑完必须落 task.failure_analyzed。"""
    from aiteam.loop.failure_alchemy import FailureAlchemist

    task = await _make_task(repo)
    await repo.update_task(task.id, status="failed", result="连接被拒绝")

    await FailureAlchemist(repo).process_failure(task.id, task.team_id)

    events = await repo.list_events(event_type="task.failure_analyzed", limit=50)
    hits = [e for e in events if e.entity_id == task.id]
    assert len(hits) == 1, "失败炼金结果未落事件"
    assert hits[0].data["task_id"] == task.id
    # 产物摘要要能证明分析真的产出了东西
    assert hits[0].data["artifacts"] == ["antibody", "vaccine", "catalyst"]


@pytest.mark.asyncio()
async def test_diagnose_failure_lands_event(repo: StorageRepository) -> None:
    """diagnose_task_failure 跑完必须落 task.failure_diagnosed，且带根因。"""
    from aiteam.loop.failure_alchemy import FailureAlchemist

    task = await _make_task(repo)
    await repo.update_task(task.id, status="failed", result="数据库连接池耗尽")

    await FailureAlchemist(repo).diagnose_failure(task.id)

    events = await repo.list_events(event_type="task.failure_diagnosed", limit=50)
    hits = [e for e in events if e.entity_id == task.id]
    assert len(hits) == 1, "失败诊断结果未落事件"
    assert "连接池" in hits[0].data["root_cause"]


@pytest.mark.asyncio()
async def test_diagnosis_on_missing_task_lands_nothing(repo: StorageRepository) -> None:
    """任务不存在时不得落空事件。"""
    from aiteam.loop.failure_alchemy import FailureAlchemist

    await FailureAlchemist(repo).diagnose_failure("no-such-task")
    assert await repo.list_events(event_type="task.failure_diagnosed", limit=50) == []
