"""agent → session 这一跳必须活得下来（token 归因 v1 阶段1）。

设计取证时 ``agents.session_id`` 全表 2,568 行只有 2 行非空，于是归因链上
"session → agent" 这一跳在库里等于不存在。根因是**三处同族**的"把身份当状态清"：

1. ``_on_session_end`` 抹 session_id —— 已由 91e07ad 修掉；
2. ``_on_stop`` mode 2 全局兜底 —— 同一次修掉；
3. ``deps._startup_reconciliation`` 每次 API 启动把全库 session_id 置 None ——
   **本文件修掉的这一处**，是三处里最后一处，也是最狠的一处：前两处按会话生效，
   这一处一次抹全库，且 API 会被活着的 CC 会话反复重启。

生产实证：events 里 2026-07-29 01:54:54 一秒内五行 ``agent.updated``，busy 行的
changes 是 ``["status","current_task","session_id"]``、其余是 ``["session_id"]``，
与该函数的两种写法逐字对上。

另一半是写入面：SubagentStop 顺手按 transcript 路径把 session_id 定回去，这样
即使某一跳漏写，只要 agent 跑完一轮就能自愈。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from aiteam.api.deps import _startup_reconciliation
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
OTHER_SESSION = "abff40af-58a1-4a7e-84ee-a68f07f72ed3"


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def translator(repo):
    yield HookTranslator(repo=repo, event_bus=EventBus(repo=repo))


def _subagent_transcript(session_id: str, cc_id: str, wf_id: str | None = None) -> str:
    mid = f"workflows/{wf_id}/" if wf_id else ""
    return (
        f"/Users/dev/.claude/projects/-Users-dev-Desktop-AI-team-OS/"
        f"{session_id}/subagents/{mid}agent-{cc_id}.jsonl"
    )


class TestStartupReconciliationKeepsIdentity:
    @pytest.mark.asyncio
    async def test_restart_resets_status_but_not_session_id(self, repo):
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="worker", role="worker",
            source="hook", session_id=SESSION,
        )
        await repo.update_agent(agent.id, status="busy", current_task="digging")

        await _startup_reconciliation(repo)

        after = await repo.get_agent(agent.id)
        assert after.status.value == "waiting"
        assert after.current_task is None
        # 身份不随进程重启消失
        assert after.session_id == SESSION

    @pytest.mark.asyncio
    async def test_idle_rows_keep_their_session_too(self, repo):
        """非 busy 行以前也会被单独抹一次（changes=["session_id"]）。"""
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="idle-one", role="worker",
            source="hook", session_id=SESSION,
        )
        await repo.update_agent(agent.id, status="waiting")

        await _startup_reconciliation(repo)

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_stale_waiting_agent_still_goes_offline_with_identity_intact(self, repo):
        """状态清理照旧生效——修的是身份列，不是自愈能力。"""
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="stale", role="worker",
            source="hook", session_id=SESSION,
        )
        await repo.update_agent(
            agent.id, status="waiting", last_active_at=utc_now() - timedelta(hours=3)
        )

        await _startup_reconciliation(repo)

        after = await repo.get_agent(agent.id)
        assert after.status.value == "offline"
        assert after.session_id == SESSION

    @pytest.mark.asyncio
    async def test_repeated_restarts_never_erode_identity(self, repo):
        """API 一天可能被重启多次——多跑几次也不该把身份磨掉。"""
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="worker", role="worker",
            source="hook", session_id=SESSION,
        )
        await repo.update_agent(agent.id, status="busy")

        for _ in range(3):
            await _startup_reconciliation(repo)

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_leader_reuse_survives_a_restart(self, repo, translator):
        """回归 91e07ad 的知识点：抹了身份，同一会话恢复时会再造一个 Leader。"""
        await repo.create_project(name="AI team OS", root_path="/Users/dev/Desktop/AI team OS")
        await translator.handle_event({
            "hook_event_name": "SessionStart",
            "session_id": SESSION,
            "cwd": "/Users/dev/Desktop/AI team OS",
        })

        await _startup_reconciliation(repo)
        await translator.handle_event({
            "hook_event_name": "SessionStart",
            "session_id": SESSION,
            "cwd": "/Users/dev/Desktop/AI team OS",
        })

        leaders = [
            a for t in await repo.list_teams()
            for a in await repo.list_agents(t.id)
            if a.role == "leader"
        ]
        assert len(leaders) == 1
        assert leaders[0].session_id == SESSION


class TestSubagentStopWritesSessionId:
    async def _registered_agent(self, repo, cc_id: str):
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id, name="worker", role="worker",
            source="hook", cc_tool_use_id=cc_id,
        )
        await repo.update_agent(agent.id, status="busy")
        return agent

    @pytest.mark.asyncio
    async def test_stop_derives_session_from_transcript_path(self, repo, translator):
        agent = await self._registered_agent(repo, "a1b2c3")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "a1b2c3",
            "agent_type": "worker",
            # payload 的 session_id 缺失时，路径仍然定得出来
            "session_id": "",
            "agent_transcript_path": _subagent_transcript(SESSION, "a1b2c3"),
        })

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_workflow_subagent_path_resolves_the_same_session(self, repo, translator):
        agent = await self._registered_agent(repo, "af2d08")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "af2d08",
            "agent_type": "worker",
            "session_id": "",
            "agent_transcript_path": _subagent_transcript(
                SESSION, "af2d08", wf_id="wf_8cd4fced-95a"
            ),
        })

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_file_truth_beats_a_disagreeing_payload(self, repo, translator):
        """路径是文件真相，payload 只是转述——冲突时以路径为准。"""
        agent = await self._registered_agent(repo, "a1b2c3")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "a1b2c3",
            "agent_type": "worker",
            "session_id": OTHER_SESSION,
            "agent_transcript_path": _subagent_transcript(SESSION, "a1b2c3"),
        })

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_payload_session_is_the_fallback_when_path_is_unparseable(
        self, repo, translator
    ):
        agent = await self._registered_agent(repo, "a1b2c3")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "a1b2c3",
            "agent_type": "worker",
            "session_id": SESSION,
            "agent_transcript_path": "/tmp/not-a-transcript.jsonl",
        })

        assert (await repo.get_agent(agent.id)).session_id == SESSION

    @pytest.mark.asyncio
    async def test_no_evidence_means_no_write_never_a_guess(self, repo, translator):
        agent = await self._registered_agent(repo, "a1b2c3")

        await translator.handle_event({
            "hook_event_name": "SubagentStop",
            "agent_id": "a1b2c3",
            "agent_type": "worker",
            "session_id": "",
            "agent_transcript_path": "",
        })

        assert (await repo.get_agent(agent.id)).session_id is None
