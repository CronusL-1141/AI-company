"""时间戳双制式 — 观测断链闭合批 ⑥ 的无争议修复。

取证结论:SQLAlchemy 的 SQLite 方言落库时把 aware datetime 的 offset 静默剥掉,
库里全是 naive 串,但**墙钟分两制**:核心域用本地墙钟(datetime.now(),24 列/18 表),
ecosystem 域用 UTC 墙钟(datetime.now(tz=UTC),19 列/16 表)。两制并存本身可以接受
(各自域内自洽),真正的 bug 是**跨制式比较**——拿一个域的 cutoff 去比另一个域的列,
静默错 8 小时且不抛异常。

本文件钉住其中一处正在造成错误结果的真 bug:aggregate_model_usage 的 cutoff 取
aware-UTC,而它比较的 workflow_agents.updated_at 是本地墙钟写入的,于是"近 N 天"
实际统计 N 天 + 8 小时。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

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

    updated_at 由 upsert_workflow_agent 以本地墙钟写入;若 cutoff 用 aware-UTC,
    在 UTC+8 上会把窗口多放宽 8 小时,这条 7 天 4 小时前的行就会被错误计入。
    """
    from sqlalchemy import select

    from aiteam.storage.connection import get_session
    from aiteam.storage.models import WorkflowAgentModel

    await repo.upsert_workflow_agent(
        WorkflowAgent(run_id="run-clock", wf_id="wf-clock", cc_agent_id="agent-stale", model="opus", tokens=999)
    )
    # 把水位推到窗口外，用与该列一致的本地墙钟
    async with get_session(repo._db_url) as session:
        row = (
            await session.execute(
                select(WorkflowAgentModel).where(WorkflowAgentModel.cc_agent_id == "agent-stale")
            )
        ).scalar_one()
        row.updated_at = datetime.now() - timedelta(days=7, hours=4)

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
