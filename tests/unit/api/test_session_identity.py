"""CC session identity must survive the session it belongs to.

Two defects found by live forensics on 2026-07-27, both in the "team looks dead
while the agent is alive" family:

1. ``_on_session_end`` wiped ``agents.session_id`` alongside setting the row
   offline. session_id is *identity*, not *state* — clearing it detaches the row
   from the session that owns it. The knock-on effect is worse than the NULL:
   ``_on_session_start`` reuses a leader by looking it up with
   ``find_agents_by_session``, so after a wipe the same session can no longer
   find its own leader and mints a fresh row on every resume. Production showed
   120 leader rows, all session_id NULL, 11 of them created in a single day,
   three of those inside one team.

2. ``_on_stop`` mode 2 swept *every team in the database* whenever the stopping
   session happened to own no busy agents, offlining live sub-agents belonging
   to entirely different sessions. This is the same cross-session clobber that
   7ae3b7cd fixed for SessionEnd; Stop kept its global sweep. Caught in the act:
   an agent of session 80d0cc5e was flipped offline at 17:27:10 with
   ``trigger=stop_global`` while it was actively running tools.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

OS_ROOT = "/Users/dev/Desktop/AI team OS"
SESSION_A = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
SESSION_B = "abff40af-58a1-4a7e-84ee-a68f07f72ed3"


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def translator(repo):
    yield HookTranslator(repo=repo, event_bus=EventBus(repo=repo))


async def _session_start(translator, session_id: str, cwd: str = OS_ROOT) -> dict:
    return await translator.handle_event(
        {"hook_event_name": "SessionStart", "session_id": session_id, "cwd": cwd}
    )


async def _leaders(repo, session_id: str | None = None) -> list:
    rows = [a for t in await repo.list_teams() for a in await repo.list_agents(t.id)]
    leaders = [a for a in rows if a.role == "leader"]
    if session_id is not None:
        leaders = [a for a in leaders if a.session_id == session_id]
    return leaders


class TestSessionIdSurvivesSessionEnd:
    @pytest.mark.asyncio
    async def test_session_end_offlines_but_keeps_identity(self, translator, repo):
        await repo.create_project(name="AI team OS", root_path=OS_ROOT)
        await _session_start(translator, SESSION_A)

        await translator.handle_event(
            {"hook_event_name": "SessionEnd", "session_id": SESSION_A}
        )

        leaders = await _leaders(repo)
        assert len(leaders) == 1
        assert leaders[0].status.value == "offline"
        # The row still knows which session it belonged to.
        assert leaders[0].session_id == SESSION_A

    @pytest.mark.asyncio
    async def test_resumed_session_reuses_its_leader_row(self, translator, repo):
        """Restart/compact-resume must not mint a second leader in the same team."""
        await repo.create_project(name="AI team OS", root_path=OS_ROOT)
        await _session_start(translator, SESSION_A)
        first = (await _leaders(repo))[0]

        await translator.handle_event(
            {"hook_event_name": "SessionEnd", "session_id": SESSION_A}
        )
        await _session_start(translator, SESSION_A)

        leaders = await _leaders(repo)
        assert [a.id for a in leaders] == [first.id]
        assert leaders[0].status.value == "busy"

    @pytest.mark.asyncio
    async def test_a_different_session_still_gets_its_own_leader(self, translator, repo):
        """Reuse is keyed on session_id — two sessions never share a leader row."""
        await repo.create_project(name="AI team OS", root_path=OS_ROOT)
        await _session_start(translator, SESSION_A)
        await _session_start(translator, SESSION_B)

        assert len(await _leaders(repo, SESSION_A)) == 1
        assert len(await _leaders(repo, SESSION_B)) == 1


class TestStopNeverClobbersAnotherSession:
    async def _busy_agent_in_session(self, repo, session_id: str):
        team = await repo.create_team(name=f"session-{session_id[:8]}", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id,
            name="worker",
            role="worker",
            source="hook",
            session_id=session_id,
        )
        # Both Stop modes skip agents born in the last 30s (an in-flight Stop must
        # not clobber an agent that just spawned). Age the row past that guard so
        # the tests exercise the real decision instead of the recency shortcut.
        await repo.update_agent(
            agent.id, status="busy", created_at=datetime.now() - timedelta(minutes=2)
        )
        return agent

    @pytest.mark.asyncio
    async def test_stop_from_an_idle_session_leaves_other_sessions_alone(
        self, translator, repo
    ):
        live = await self._busy_agent_in_session(repo, SESSION_A)

        # Session B ends a turn and owns no busy agents at all.
        await translator.handle_event(
            {"hook_event_name": "Stop", "session_id": SESSION_B}
        )

        assert (await repo.get_agent(live.id)).status.value == "busy"

    @pytest.mark.asyncio
    async def test_stop_still_touches_its_own_session_agents(self, translator, repo):
        mine = await self._busy_agent_in_session(repo, SESSION_A)
        before = (await repo.get_agent(mine.id)).last_active_at

        result = await translator.handle_event(
            {"hook_event_name": "Stop", "session_id": SESSION_A}
        )

        after = await repo.get_agent(mine.id)
        assert after.status.value == "busy"
        assert result["heartbeat_updates"] == [mine.id]
        assert before is None or after.last_active_at >= before

    @pytest.mark.asyncio
    async def test_stop_without_a_session_id_is_a_no_op(self, translator, repo):
        """A payload with no session cannot prove anything about anyone."""
        live = await self._busy_agent_in_session(repo, SESSION_A)

        await translator.handle_event({"hook_event_name": "Stop", "session_id": ""})

        assert (await repo.get_agent(live.id)).status.value == "busy"
