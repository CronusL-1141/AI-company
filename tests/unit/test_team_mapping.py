"""Tests for CC-to-OS team mapping.

Tests two components:
1. send_event._resolve_cc_team_name — reads CC team configs to inject team name
2. hook_translator._resolve_cc_team — finds/creates OS team by name
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import pytest_asyncio

from aiteam.hooks.send_event import _resolve_cc_team_name
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
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
