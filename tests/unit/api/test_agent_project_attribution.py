"""Agent/team project attribution must never come from cross-session global cwd.

Reproduces the 2026-07-27 incident: a sub-agent working in the "AI Team OS"
project was registered with project_id of the *Wenge* project, and its
system_prompt was rendered with Wenge's root_path — while the Leader row of the
very same team carried the correct project.

Root cause chain:
1. ``deps._auto_create_projects`` (runs on every API start) bulk-bound *every*
   orphan team to one project resolved from the **API process** ``os.getcwd()``,
   with ``existing_projects[0]`` as an ultimate fallback. The API process cwd is
   shared across all CC sessions, so whichever directory the server happened to
   be launched from claimed every unbound team.
2. ``_on_subagent_start`` then copied that polluted ``team.project_id`` onto the
   new agent row and into the rendered prompt template.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from aiteam.api import deps
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

OS_ROOT = "/Users/dev/Desktop/AI team OS"
WENGE_ROOT = "/Volumes/external/Wenge"


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def translator(repo):
    yield HookTranslator(repo=repo, event_bus=EventBus(repo=repo)), repo


async def _two_projects(repo):
    """Wenge is created first so ``existing_projects[0]`` ordering is exercised."""
    wenge = await repo.create_project(name="Wenge", root_path=WENGE_ROOT)
    os_proj = await repo.create_project(name="AI team OS", root_path=OS_ROOT)
    return wenge, os_proj


class TestStartupBackfillAttribution:
    """deps._auto_create_projects must use per-team authority, not process cwd."""

    @pytest.mark.asyncio
    async def test_orphan_team_binds_to_its_own_leader_project(self, repo, monkeypatch):
        """A session container team follows its Leader row, whatever the API cwd is."""
        wenge, os_proj = await _two_projects(repo)
        team = await repo.create_team(name="session-80d0cc5e", mode="coordinate")
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader", source="hook"
        )
        await repo.update_agent(leader.id, project_id=os_proj.id)

        # API server happens to have been started from the Wenge checkout.
        monkeypatch.setattr(os, "getcwd", lambda: WENGE_ROOT)
        await deps._auto_create_projects(repo)

        refreshed = await repo.get_team(team.id)
        assert refreshed.project_id == os_proj.id, (
            "team must follow its own Leader, not the API process cwd"
        )

    @pytest.mark.asyncio
    async def test_orphan_team_without_authority_stays_unbound(self, repo, monkeypatch):
        """No leader, no member project -> leave empty rather than guess."""
        wenge, os_proj = await _two_projects(repo)
        team = await repo.create_team(name="mystery-team", mode="coordinate")

        monkeypatch.setattr(os, "getcwd", lambda: "/somewhere/unrelated")
        await deps._auto_create_projects(repo)

        refreshed = await repo.get_team(team.id)
        assert refreshed.project_id is None, (
            "unknown ownership must stay unbound; existing_projects[0] fallback "
            "is exactly how 116 teams got claimed by Wenge"
        )

    @pytest.mark.asyncio
    async def test_two_sessions_do_not_cross_contaminate(self, repo, monkeypatch):
        """Interleaved sessions from two projects each keep their own project."""
        wenge, os_proj = await _two_projects(repo)
        os_team = await repo.create_team(name="session-aaaaaaaa", mode="coordinate")
        os_leader = await repo.create_agent(
            team_id=os_team.id, name="Leader", role="leader", source="hook"
        )
        await repo.update_agent(os_leader.id, project_id=os_proj.id)

        wenge_team = await repo.create_team(name="session-bbbbbbbb", mode="coordinate")
        wenge_leader = await repo.create_agent(
            team_id=wenge_team.id, name="Leader", role="leader", source="hook"
        )
        await repo.update_agent(wenge_leader.id, project_id=wenge.id)

        monkeypatch.setattr(os, "getcwd", lambda: WENGE_ROOT)
        await deps._auto_create_projects(repo)

        assert (await repo.get_team(os_team.id)).project_id == os_proj.id
        assert (await repo.get_team(wenge_team.id)).project_id == wenge.id

    @pytest.mark.asyncio
    async def test_workflow_team_still_exempt(self, repo, monkeypatch):
        """kind=workflow teams keep their pre-existing exemption (2026-07-07 rule)."""
        wenge, os_proj = await _two_projects(repo)
        team = await repo.create_team(
            name="workflow-wf_x", mode="coordinate", config={"kind": "workflow"}
        )
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader", source="hook"
        )
        await repo.update_agent(leader.id, project_id=os_proj.id)

        monkeypatch.setattr(os, "getcwd", lambda: OS_ROOT)
        await deps._auto_create_projects(repo)

        assert (await repo.get_team(team.id)).project_id is None


class TestSubagentRegistrationAttribution:
    """_on_subagent_start must prefer Leader authority over a polluted team row."""

    @pytest.mark.asyncio
    async def test_agent_follows_leader_not_polluted_team(self, translator):
        """The reported incident: team bound to Wenge, Leader bound to AI Team OS."""
        ht, repo = translator
        wenge, os_proj = await _two_projects(repo)
        team = await repo.create_team(
            name="session-80d0cc5e",
            mode="coordinate",
            config={"kind": "session", "owner_session_id": "80d0cc5e"},
            project_id=wenge.id,  # polluted by the startup backfill
        )
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader", source="hook"
        )
        await repo.update_agent(leader.id, project_id=os_proj.id)

        result = await ht._on_subagent_start(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "ac22ede93ef5e12a8",
                "agent_type": "general-purpose",
                "session_id": "80d0cc5e",
                "cc_team_name": "session-80d0cc5e",
                "cwd": OS_ROOT,
            }
        )
        assert result["status"] == "created"

        agent = await repo.get_agent(result["agent_id"])
        assert agent.project_id == os_proj.id, "agent must inherit Leader's project"
        assert WENGE_ROOT not in (agent.system_prompt or ""), (
            "rendered prompt must not carry another project's path"
        )

    @pytest.mark.asyncio
    async def test_agent_project_left_empty_when_unresolvable(self, translator):
        """No leader and no team binding -> leave empty, never guess."""
        ht, repo = translator
        await _two_projects(repo)
        await repo.create_team(name="orphan-team", mode="coordinate")

        result = await ht._on_subagent_start(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "cc-unknown-1",
                "agent_type": "worker",
                "session_id": "sid-unknown",
                "cc_team_name": "orphan-team",
                "cwd": "/nowhere/at/all",
            }
        )
        assert result["status"] == "created"
        agent = await repo.get_agent(result["agent_id"])
        assert agent.project_id is None

    @pytest.mark.asyncio
    async def test_repair_realigns_team_and_members(self, repo):
        """The bookkeeping companion heals rows written before the fix."""
        wenge, os_proj = await _two_projects(repo)
        team = await repo.create_team(
            name="session-80d0cc5e", mode="coordinate", project_id=wenge.id
        )
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader", source="hook"
        )
        await repo.update_agent(leader.id, project_id=os_proj.id)
        worker = await repo.create_agent(
            team_id=team.id, name="general-purpose", role="general-purpose", source="hook"
        )
        await repo.update_agent(worker.id, project_id=wenge.id)

        preview = await repo.repair_team_project_attribution(dry_run=True)
        assert len(preview) == 1
        assert preview[0]["from_project_id"] == wenge.id
        assert preview[0]["to_project_id"] == os_proj.id
        assert preview[0]["agents_fixed"] == 1
        assert (await repo.get_team(team.id)).project_id == wenge.id, "dry-run must not write"

        await repo.repair_team_project_attribution(dry_run=False)
        assert (await repo.get_team(team.id)).project_id == os_proj.id
        assert (await repo.get_agent(worker.id)).project_id == os_proj.id
        assert await repo.repair_team_project_attribution(dry_run=True) == []

    @pytest.mark.asyncio
    async def test_repair_leaves_unprovable_rows_alone(self, repo):
        """No Leader authority, or a workflow team -> never touched."""
        wenge, os_proj = await _two_projects(repo)
        await repo.create_team(name="no-leader", mode="coordinate", project_id=wenge.id)

        wf = await repo.create_team(
            name="workflow-wf_y",
            mode="coordinate",
            config={"kind": "workflow"},
            project_id=wenge.id,
        )
        wf_leader = await repo.create_agent(
            team_id=wf.id, name="Leader", role="leader", source="hook"
        )
        await repo.update_agent(wf_leader.id, project_id=os_proj.id)

        assert await repo.repair_team_project_attribution(dry_run=True) == []

    @pytest.mark.asyncio
    async def test_agent_falls_back_to_team_when_no_leader(self, translator):
        """Team binding remains the second authority when the team has no Leader."""
        ht, repo = translator
        _wenge, os_proj = await _two_projects(repo)
        await repo.create_team(
            name="bound-team", mode="coordinate", project_id=os_proj.id
        )

        result = await ht._on_subagent_start(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "cc-bound-1",
                "agent_type": "worker",
                "session_id": "sid-bound",
                "cc_team_name": "bound-team",
                "cwd": OS_ROOT,
            }
        )
        agent = await repo.get_agent(result["agent_id"])
        assert agent.project_id == os_proj.id
        assert OS_ROOT in (agent.system_prompt or "")
