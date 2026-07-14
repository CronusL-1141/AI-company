"""Tests for CC-to-OS team mapping.

Tests two components:
1. send_event._resolve_cc_team_name — reads CC team configs to inject team name
2. hook_translator._resolve_cc_team — finds/creates OS team by name
"""

from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.hooks.send_event import _resolve_cc_team_name
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

# ============================================================
# send_event._resolve_cc_team_name tests
# ============================================================


class TestResolveCCTeamName:
    """Tests for _resolve_cc_team_name in send_event.py."""

    def test_empty_session_id_returns_none(self):
        assert _resolve_cc_team_name("") is None

    def test_matching_session_returns_team_name(self, tmp_path):
        """When a config.json has matching leadSessionId, return its name."""
        teams_dir = tmp_path / ".claude" / "teams" / "my-team"
        teams_dir.mkdir(parents=True)
        config = {
            "name": "My-Team",
            "leadSessionId": "abc-123",
            "members": [],
        }
        (teams_dir / "config.json").write_text(
            json.dumps(config),
            encoding="utf-8",
        )

        # Monkey-patch expanduser to use tmp_path
        original = os.path.expanduser

        def mock_expanduser(p):
            if p == "~":
                return str(tmp_path)
            return original(p)

        os.path.expanduser = mock_expanduser
        try:
            result = _resolve_cc_team_name("abc-123")
            assert result == "My-Team"
        finally:
            os.path.expanduser = original

    def test_no_matching_session_returns_none(self, tmp_path):
        """When no config matches the session_id, return None."""
        teams_dir = tmp_path / ".claude" / "teams" / "other-team"
        teams_dir.mkdir(parents=True)
        config = {
            "name": "Other-Team",
            "leadSessionId": "xyz-999",
            "members": [],
        }
        (teams_dir / "config.json").write_text(
            json.dumps(config),
            encoding="utf-8",
        )

        original = os.path.expanduser

        def mock_expanduser(p):
            if p == "~":
                return str(tmp_path)
            return original(p)

        os.path.expanduser = mock_expanduser
        try:
            result = _resolve_cc_team_name("abc-123")
            assert result is None
        finally:
            os.path.expanduser = original

    def test_no_teams_dir_returns_none(self, tmp_path):
        """When ~/.claude/teams doesn't exist, return None gracefully."""
        original = os.path.expanduser

        def mock_expanduser(p):
            if p == "~":
                return str(tmp_path)
            return original(p)

        os.path.expanduser = mock_expanduser
        try:
            result = _resolve_cc_team_name("abc-123")
            assert result is None
        finally:
            os.path.expanduser = original

    def test_corrupt_config_skipped(self, tmp_path):
        """Corrupt config.json files should be silently skipped."""
        teams_dir = tmp_path / ".claude" / "teams" / "bad-team"
        teams_dir.mkdir(parents=True)
        (teams_dir / "config.json").write_text("not valid json", encoding="utf-8")

        # Also add a valid one
        good_dir = tmp_path / ".claude" / "teams" / "good-team"
        good_dir.mkdir(parents=True)
        config = {
            "name": "Good-Team",
            "leadSessionId": "match-me",
            "members": [],
        }
        (good_dir / "config.json").write_text(
            json.dumps(config),
            encoding="utf-8",
        )

        original = os.path.expanduser

        def mock_expanduser(p):
            if p == "~":
                return str(tmp_path)
            return original(p)

        os.path.expanduser = mock_expanduser
        try:
            result = _resolve_cc_team_name("match-me")
            assert result == "Good-Team"
        finally:
            os.path.expanduser = original


# ============================================================
# hook_translator._resolve_cc_team + _on_subagent_start tests
# ============================================================


@pytest_asyncio.fixture()
async def translator():
    """Create a HookTranslator with in-memory DB."""
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    event_bus = EventBus(repo=repo)
    ht = HookTranslator(repo=repo, event_bus=event_bus)
    yield ht, repo
    await close_db()


class TestResolveCCTeam:
    """Tests for HookTranslator._resolve_cc_team."""

    @pytest.mark.asyncio
    async def test_existing_team_found_by_name(self, translator):
        """If OS already has a team with the same name, return it."""
        ht, repo = translator
        team = await repo.create_team(name="Sprint-1", mode="coordinate")
        result = await ht._resolve_cc_team("Sprint-1", "session-abc")
        assert result is not None
        assert result.id == team.id

    @pytest.mark.asyncio
    async def test_auto_create_team_when_not_found(self, translator):
        """If no OS team matches, auto-create one."""
        ht, repo = translator
        result = await ht._resolve_cc_team("New-Team", "session-abc")
        assert result is not None
        assert result.name == "New-Team"

        # Verify it's persisted
        teams = await repo.list_teams()
        assert any(t.name == "New-Team" for t in teams)

    @pytest.mark.asyncio
    async def test_empty_name_returns_none(self, translator):
        ht, repo = translator
        result = await ht._resolve_cc_team("", "session-abc")
        assert result is None


class TestSubagentStartTeamMapping:
    """Tests for _on_subagent_start with cc_team_name."""

    @pytest.mark.asyncio
    async def test_agent_registered_to_cc_team(self, translator):
        """Agent with cc_team_name should be registered to the named team."""
        ht, repo = translator

        # Pre-create an OS team matching the CC team name
        team = await repo.create_team(name="M6-sprint", mode="coordinate")

        payload = {
            "hook_event_name": "SubagentStart",
            "agent_id": "cc-agent-001",
            "agent_type": "dev-worker",
            "session_id": "session-leader-123",
            "cc_team_name": "M6-sprint",
        }
        result = await ht._on_subagent_start(payload)
        assert result["status"] == "created"

        # Verify agent is in the correct team
        agents = await repo.list_agents(team.id)
        assert any(a.name == "dev-worker" for a in agents)

    @pytest.mark.asyncio
    async def test_agent_auto_creates_os_team(self, translator):
        """When cc_team_name doesn't match any OS team, auto-create it."""
        ht, repo = translator

        payload = {
            "hook_event_name": "SubagentStart",
            "agent_id": "cc-agent-002",
            "agent_type": "qa-worker",
            "session_id": "session-leader-456",
            "cc_team_name": "Brand-New-Team",
        }
        result = await ht._on_subagent_start(payload)
        assert result["status"] == "created"

        # Verify team was created and agent is in it
        team = await repo.get_team_by_name("Brand-New-Team")
        assert team is not None
        agents = await repo.list_agents(team.id)
        assert any(a.name == "qa-worker" for a in agents)

    @pytest.mark.asyncio
    async def test_without_cc_team_name_falls_back(self, translator):
        """Without cc_team_name, should fall back to leader's team (original behavior)."""
        ht, repo = translator

        # Create a team with a leader
        team = await repo.create_team(name="Default-Team", mode="coordinate")
        leader = await repo.create_agent(
            team_id=team.id,
            name="Leader",
            role="leader",
            source="api",
            session_id="session-789",
        )
        await repo.update_team(team.id, leader_agent_id=leader.id)

        payload = {
            "hook_event_name": "SubagentStart",
            "agent_id": "cc-agent-003",
            "agent_type": "helper",
            "session_id": "session-789",
            # No cc_team_name
        }
        result = await ht._on_subagent_start(payload)
        assert result["status"] == "created"

        # Agent should be in the leader's team
        agents = await repo.list_agents(team.id)
        assert any(a.name == "helper" for a in agents)

    @pytest.mark.asyncio
    async def test_dedup_existing_agent_in_cc_team(self, translator):
        """If agent already exists in the CC team, update instead of create."""
        ht, repo = translator

        team = await repo.create_team(name="M6-sprint", mode="coordinate")
        existing = await repo.create_agent(
            team_id=team.id,
            name="dev-worker",
            role="dev-worker",
            source="hook",
        )

        payload = {
            "hook_event_name": "SubagentStart",
            "agent_id": "cc-agent-004",
            "agent_type": "dev-worker",
            "session_id": "session-dedup",
            "cc_team_name": "M6-sprint",
        }
        result = await ht._on_subagent_start(payload)
        assert result["status"] == "updated"
        assert result["agent_id"] == existing.id


class TestWorkflowSubagentTracking:
    """Tests for _on_subagent_start workflow-subagent branch (one workflow = one team)."""

    def _wf_payload(self, wf_id: str, agent_id: str, session_id: str = "sess-leader-1") -> dict:
        tp = (
            rf"C:\Users\X\.claude\projects\P\{session_id}\subagents\workflows\{wf_id}\agent-{agent_id}.jsonl"
        )
        return {
            "hook_event_name": "SubagentStart",
            "agent_id": agent_id,
            "agent_type": "workflow-subagent",
            "session_id": session_id,
            "transcript_path": tp,
        }

    @pytest.mark.asyncio
    async def test_creates_team_per_run_without_active_team(self, translator):
        """A workflow subagent auto-creates its run team even with no pre-existing team."""
        ht, repo = translator
        result = await ht._on_subagent_start(self._wf_payload("wf_aaa111-b2", "agent-001"))
        assert result["status"] == "created"
        assert result["kind"] == "workflow"
        team = await repo.get_team_by_name("workflow-wf_aaa111-b2")
        assert team is not None
        agents = await repo.list_agents(team.id)
        assert len(agents) == 1
        assert agents[0].role == "workflow-subagent"

    @pytest.mark.asyncio
    async def test_same_run_two_agents_one_team_two_members(self, translator):
        """Two agents of the SAME workflow run -> one team, two distinct members (no collapse)."""
        ht, repo = translator
        await ht._on_subagent_start(self._wf_payload("wf_run9", "agent-AAA"))
        await ht._on_subagent_start(self._wf_payload("wf_run9", "agent-BBB"))
        team = await repo.get_team_by_name("workflow-wf_run9")
        agents = await repo.list_agents(team.id)
        assert len(agents) == 2, "16-agent collapse bug: members must not dedup by shared name"
        names = {a.name for a in agents}
        assert names == {"wf-agent-AAAA"[:13], "wf-agent-BBBB"[:13]} or len(names) == 2

    @pytest.mark.asyncio
    async def test_different_runs_separate_teams(self, translator):
        """Different workflow runs land in different teams (strict 1:1)."""
        ht, repo = translator
        await ht._on_subagent_start(self._wf_payload("wf_alpha", "agent-1"))
        await ht._on_subagent_start(self._wf_payload("wf_beta", "agent-2"))
        assert await repo.get_team_by_name("workflow-wf_alpha") is not None
        assert await repo.get_team_by_name("workflow-wf_beta") is not None

    @pytest.mark.asyncio
    async def test_dedup_same_cc_agent_id(self, translator):
        """Same cc_agent_id reported twice -> single member, second call updates."""
        ht, repo = translator
        r1 = await ht._on_subagent_start(self._wf_payload("wf_dd", "agent-same"))
        r2 = await ht._on_subagent_start(self._wf_payload("wf_dd", "agent-same"))
        assert r1["status"] == "created"
        assert r2["status"] == "updated"
        team = await repo.get_team_by_name("workflow-wf_dd")
        agents = await repo.list_agents(team.id)
        assert len(agents) == 1

    @pytest.mark.asyncio
    async def test_fallback_to_session_when_no_transcript(self, translator):
        """No transcript_path -> fall back to session-scoped workflow team."""
        ht, repo = translator
        payload = {
            "hook_event_name": "SubagentStart",
            "agent_id": "agent-x",
            "agent_type": "workflow-subagent",
            "session_id": "abcdef123456",
        }
        result = await ht._on_subagent_start(payload)
        assert result["status"] == "created"
        assert await repo.get_team_by_name("workflow-session-abcdef12") is not None


class TestWorkflowStrict1to1AndStep4:
    """strict 1:1 (re-key by wf_id) + Step 4 (parse plan from script)."""

    def _wf_payload(self, wf_id, agent_id, session_id="sess-x", event="SubagentStop"):
        # SubagentStop / subagent PreToolUse carry agent_transcript_path with the wf dir
        atp = rf"C:\Users\X\.claude\projects\P\{session_id}\subagents\workflows\{wf_id}\agent-{agent_id}.jsonl"
        return {"hook_event_name": event, "agent_id": agent_id,
                "agent_type": "workflow-subagent", "session_id": session_id,
                "agent_transcript_path": atp, "transcript_path": f"C:/p/{session_id}.jsonl"}

    @pytest.mark.asyncio
    async def test_subagent_stop_promotes_to_per_run_team(self, translator):
        """Agent registered in session-fallback team migrates to workflow-<wf_id> on Stop."""
        ht, repo = translator
        # 1. SubagentStart (no wf_id) -> session fallback team
        start = {"hook_event_name": "SubagentStart", "agent_id": "ag-1",
                 "agent_type": "workflow-subagent", "session_id": "sess-x"}
        await ht._on_subagent_start(start)
        fallback = await repo.get_team_by_name("workflow-session-sess-x")
        assert fallback is not None
        assert len(await repo.list_agents(fallback.id)) == 1
        # 2. SubagentStop carries wf_id -> migrate to workflow-wf_run1
        await ht._on_subagent_stop(self._wf_payload("wf_run1", "ag-1"))
        run_team = await repo.get_team_by_name("workflow-wf_run1")
        assert run_team is not None, "strict 1:1: per-run team must be created"
        assert len(await repo.list_agents(run_team.id)) == 1, "agent migrated to run team"
        assert len(await repo.list_agents(fallback.id)) == 0, "agent left the fallback team"

    @pytest.mark.asyncio
    async def test_two_runs_split_into_two_teams(self, translator):
        """Two agents from two different runs end up in two distinct per-run teams."""
        ht, repo = translator
        for wf, ag in [("wf_aa", "x1"), ("wf_bb", "x2")]:
            await ht._on_subagent_start({"hook_event_name": "SubagentStart", "agent_id": ag,
                                         "agent_type": "workflow-subagent", "session_id": "s"})
            await ht._on_subagent_stop(self._wf_payload(wf, ag, session_id="s"))
        assert await repo.get_team_by_name("workflow-wf_aa") is not None
        assert await repo.get_team_by_name("workflow-wf_bb") is not None

    def test_parse_workflow_plan_extracts_phases_and_agents(self, translator):
        """Step 4: static parse yields phases (declarative) + literal agent count + dynamic nodes."""
        ht, _ = translator
        script = """
        export const meta = { name: 'demo-x', phases: [
          { title: 'Audit' }, { title: 'Synthesize' }, { title: 'Verify' } ] }
        phase('Audit')
        const a = await agent('do A', {label:'a'})
        const b = await parallel([() => agent('B'), () => agent('C')])
        const v = await parallel(items.map(x => () => agent('verify '+x)))
        """
        plan = ht._parse_workflow_plan(script)
        assert plan["name"] == "demo-x"
        assert plan["phases"] == ["Audit", "Synthesize", "Verify"]
        assert plan["literal_agent_count"] == 4  # A, B, C, verify
        assert plan["dynamic_nodes"] >= 1        # items.map(...)


class TestFleetIdentityP1:
    """fleet-layer P1: session_id as a first-class identity key (03fe7cae churn fix).

    Verifies the three drift sources are closed: no cross-session leader fallback,
    no cross-session leader reuse at SessionStart, and per-session container teams
    tagged with an ownership key.
    """

    @pytest.mark.asyncio
    async def test_find_leader_no_cross_session_fallback(self, translator):
        """_find_leader must NOT return or rebind another session's leader."""
        ht, repo = translator
        team = await repo.create_team(
            name="session-aaaaaaaa", mode="coordinate",
            config={"kind": "session", "owner_session_id": "sess-A"},
        )
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader",
            source="hook", session_id="sess-A",
        )
        # Looking up a DIFFERENT session must return None (no cross-session guess),
        # and the sess-A leader's session_id must stay untouched (no self-heal rebind).
        result = await ht._find_leader("sess-B-unknown")
        assert result is None
        after = await repo.get_agent(leader.id)
        assert after.session_id == "sess-A", "must not rebind another session's leader"

    @pytest.mark.asyncio
    async def test_find_leader_same_session_hit(self, translator):
        ht, repo = translator
        team = await repo.create_team(name="session-bbbbbbbb", mode="coordinate")
        leader = await repo.create_agent(
            team_id=team.id, name="Leader", role="leader",
            source="hook", session_id="sess-here",
        )
        result = await ht._find_leader("sess-here")
        assert result is not None and result.id == leader.id

    @pytest.mark.asyncio
    async def test_session_start_creates_owned_container(self, translator):
        """SessionStart in a registered project creates a per-session container team
        tagged kind=session + owner_session_id, and a leader bound to that session."""
        ht, repo = translator
        proj = await repo.create_project(name="P", root_path="/tmp/fleet-p1")
        await ht._on_session_start({
            "hook_event_name": "SessionStart",
            "session_id": "sess-start-1",
            "cwd": "/tmp/fleet-p1",
        })
        leaders = [
            a for a in await repo.find_agents_by_session("sess-start-1")
            if a.role == "leader"
        ]
        assert len(leaders) == 1
        leader = leaders[0]
        assert leader.project_id == proj.id
        team = await repo.get_team(leader.team_id)
        assert (team.config or {}).get("kind") == "session"
        assert (team.config or {}).get("owner_session_id") == "sess-start-1"
        assert team.leader_agent_id == leader.id

    @pytest.mark.asyncio
    async def test_session_start_does_not_reuse_another_sessions_leader(self, translator):
        """Two sessions in the same project get TWO distinct leader rows (no reuse)."""
        ht, repo = translator
        await repo.create_project(name="P2", root_path="/tmp/fleet-p2")
        for sid in ("sess-x1", "sess-x2"):
            await ht._on_session_start({
                "hook_event_name": "SessionStart",
                "session_id": sid,
                "cwd": "/tmp/fleet-p2",
            })
        l1 = [a for a in await repo.find_agents_by_session("sess-x1") if a.role == "leader"]
        l2 = [a for a in await repo.find_agents_by_session("sess-x2") if a.role == "leader"]
        assert len(l1) == 1 and len(l2) == 1
        assert l1[0].id != l2[0].id, "each session must own its own leader row"

    def test_team_owned_by_session_helper(self, translator):
        from types import SimpleNamespace

        ht, _ = translator
        # owner_session_id match is authoritative
        t1 = SimpleNamespace(config={"owner_session_id": "s1"}, name="whatever")
        assert ht._team_owned_by_session(t1, "s1") is True
        assert ht._team_owned_by_session(t1, "s2") is False
        # legacy fallback: container name encodes the session id8
        t2 = SimpleNamespace(config={}, name="session-abcd1234")
        assert ht._team_owned_by_session(t2, "abcd1234ffff") is True
        # unknown ownership -> conservative False (never cross-session clobber)
        t3 = SimpleNamespace(config={}, name="random-team")
        assert ht._team_owned_by_session(t3, "s1") is False
        assert ht._team_owned_by_session(t3, "") is False
