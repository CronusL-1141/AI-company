"""aggregate_model_usage 单测 — 编排宪章观测口径（按档位聚合 workflow agent 用量）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import WorkflowAgent


@pytest.fixture
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


async def _seed(repo: StorageRepository, model: str, tokens: int, wf: str, cc: str):
    await repo.upsert_workflow_agent(
        WorkflowAgent(
            run_id=wf,
            wf_id=wf,
            cc_agent_id=cc,
            label=f"a-{cc}",
            model=model,
            state="done",
            tokens=tokens,
        )
    )


class TestAggregateModelUsage:
    @pytest.mark.asyncio
    async def test_groups_and_sums_by_model(self, repo):
        await _seed(repo, "claude-fable-5", 1000, "wf_1", "c1")
        await _seed(repo, "claude-opus-4-8", 300, "wf_1", "c2")
        await _seed(repo, "claude-opus-4-8", 200, "wf_1", "c3")
        usage = await repo.aggregate_model_usage(days=7)
        by_model = {u["model"]: u for u in usage}
        assert by_model["claude-fable-5"] == {
            "model": "claude-fable-5",
            "agents": 1,
            "tokens": 1000,
        }
        assert by_model["claude-opus-4-8"]["agents"] == 2
        assert by_model["claude-opus-4-8"]["tokens"] == 500

    @pytest.mark.asyncio
    async def test_ordered_by_tokens_desc(self, repo):
        await _seed(repo, "claude-opus-4-8", 9000, "wf_2", "c1")
        await _seed(repo, "claude-fable-5", 100, "wf_2", "c2")
        usage = await repo.aggregate_model_usage(days=7)
        assert usage[0]["model"] == "claude-opus-4-8"

    @pytest.mark.asyncio
    async def test_window_excludes_old_rows(self, repo):
        await _seed(repo, "claude-opus-4-8", 500, "wf_3", "c1")
        # 手动把该行推老到窗口外
        from sqlalchemy import update

        from aiteam.storage.connection import get_session
        from aiteam.storage.models import WorkflowAgentModel

        async with get_session(repo._db_url) as session:
            await session.execute(
                update(WorkflowAgentModel).values(
                    updated_at=datetime.now(UTC) - timedelta(days=30)
                )
            )
            await session.commit()
        assert await repo.aggregate_model_usage(days=7) == []
        assert (await repo.aggregate_model_usage(days=90))[0]["tokens"] == 500

    @pytest.mark.asyncio
    async def test_empty_model_labelled(self, repo):
        await _seed(repo, "", 50, "wf_4", "c1")
        usage = await repo.aggregate_model_usage(days=7)
        assert usage[0]["model"] == "(未记录)"
