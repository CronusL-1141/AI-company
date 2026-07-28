"""时间窗口口径 — "近 N 天"必须真的是 N 天。

史料:本库曾同时跑两个墙钟(核心域本地 / ecosystem 域 UTC),而 SQLite 把 aware
datetime 的 offset 静默剥掉,于是跨域比较错 8 小时**且不抛异常**。
``aggregate_model_usage`` 就是被咬中的一处:cutoff 取 UTC 而它比较的
``workflow_agents.updated_at`` 写的是本地,"近 N 天"实际统计了 N 天 + 8 小时。

全库统一 UTC 后(见 docs/utc-unification-design.md)两侧同源,这两条测试留下来当
回归网:窗口边界一旦再次被某个时钟差撑开或缩窄,它们立刻红。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import WorkflowAgent


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest.mark.asyncio()
async def test_usage_window_does_not_leak_eight_extra_hours(repo: StorageRepository) -> None:
    """刚过窗口的行必须落在窗口外。

    这条行龄 7 天 4 小时,只比窗口多出 4 小时——比任何一个时区偏移都小。所以只要
    cutoff 与列的口径再次错开哪怕一个偏移量,它就会被错误计入。
    """
    from sqlalchemy import select

    from aiteam.storage.connection import get_session
    from aiteam.storage.models import WorkflowAgentModel

    await repo.upsert_workflow_agent(
        WorkflowAgent(run_id="run-clock", wf_id="wf-clock", cc_agent_id="agent-stale", model="opus", tokens=999)
    )
    # 把水位推到窗口外（与该列同一时钟：全库唯一的 UTC）
    async with get_session(repo._db_url) as session:
        row = (
            await session.execute(
                select(WorkflowAgentModel).where(WorkflowAgentModel.cc_agent_id == "agent-stale")
            )
        ).scalar_one()
        row.updated_at = utc_now() - timedelta(days=7, hours=4)

    rows = await repo.aggregate_model_usage(days=7)
    total = sum(r.get("agents", 0) for r in rows)
    assert total == 0, f"窗口外的行被计入——cutoff 与列口径不一致: {rows}"


@pytest.mark.asyncio()
async def test_recent_row_still_counted(repo: StorageRepository) -> None:
    """修复不得把窗口内的行一起误伤。"""
    await repo.upsert_workflow_agent(
        WorkflowAgent(run_id="run-clock", wf_id="wf-clock", cc_agent_id="agent-fresh", model="opus", tokens=1234)
    )
    rows = await repo.aggregate_model_usage(days=7)
    assert sum(r.get("agents", 0) for r in rows) == 1
