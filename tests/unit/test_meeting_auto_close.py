"""Regression test: meetings must NOT be auto-concluded when team CC config
disappears while the meeting still has recent messages.

Root cause: _check_team_liveness in StateReaper immediately closed OS teams
(and cascaded to conclude all active meetings) when the CC team directory
was missing — without checking if meetings were still actively in use.

Fix: guard against closing teams that have active meetings with messages
newer than MEETING_EXPIRY_MINUTES.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.state_reaper import StateReaper
from aiteam.storage.repository import StorageRepository
from aiteam.types import MeetingStatus


@pytest_asyncio.fixture()
async def repo():
    from aiteam.storage.connection import close_db

    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def event_bus(repo: StorageRepository):
    bus = EventBus(repo)
    bus.emit = AsyncMock()
    return bus


@pytest_asyncio.fixture()
async def team_with_active_meeting(repo: StorageRepository):
    """Create a team with an active meeting that has a recent message."""
    team = await repo.create_team(name="test-liveness-team", mode="coordinate")
    meeting = await repo.create_meeting(
        team_id=team.id,
        topic="active discussion",
        participants=["agent-a", "agent-b"],
    )
    await repo.create_meeting_message(
        meeting_id=meeting.id,
        agent_id="agent-a",
        agent_name="agent-a",
        content="This is a recent message",
        round_number=1,
    )
    return team, meeting


def _fake_teams_dir(tmp_path):
    """Create a real but empty ~/.claude/teams directory for testing."""
    teams_dir = tmp_path / ".claude" / "teams"
    teams_dir.mkdir(parents=True)
    return teams_dir


@pytest.mark.asyncio()
async def test_team_liveness_does_not_close_team_with_active_recent_meeting(
    repo: StorageRepository,
    event_bus: EventBus,
    team_with_active_meeting,
    tmp_path,
):
    """A team whose CC dir is missing but has an active meeting with recent
    messages must NOT be closed by _check_team_liveness."""
    team, meeting = team_with_active_meeting
    teams_dir = _fake_teams_dir(tmp_path)

    reaper = StateReaper(repo, event_bus)

    # Patch Path.home() to point to our tmp_path so teams_dir resolves there
    with patch("pathlib.Path.home", return_value=tmp_path):
        await reaper._check_team_liveness(repo)

    # Team should still be active (not closed)
    updated_team = await repo.get_team(team.id)
    assert updated_team.status == "active", (
        f"Team was closed despite having an active meeting with recent messages. "
        f"Status: {updated_team.status}"
    )

    # Meeting should still be active
    updated_meeting = await repo.get_meeting(meeting.id)
    assert updated_meeting.status == MeetingStatus.ACTIVE.value, (
        f"Meeting was auto-concluded despite having recent messages. "
        f"Status: {updated_meeting.status}"
    )


@pytest.mark.asyncio()
async def test_team_liveness_closes_team_with_no_active_meetings(
    repo: StorageRepository,
    event_bus: EventBus,
    tmp_path,
):
    """A team whose CC dir is missing and has NO active meetings should be closed."""
    team = await repo.create_team(name="abandoned-team", mode="coordinate")
    _fake_teams_dir(tmp_path)

    reaper = StateReaper(repo, event_bus)

    with patch("pathlib.Path.home", return_value=tmp_path):
        await reaper._check_team_liveness(repo)

    updated_team = await repo.get_team(team.id)
    assert updated_team.status == "completed"


@pytest.mark.asyncio()
async def test_team_liveness_closes_team_with_only_stale_meetings(
    repo: StorageRepository,
    event_bus: EventBus,
    tmp_path,
):
    """A team with active meetings but only OLD messages (beyond expiry) should be closed."""
    from aiteam.config.settings import MEETING_EXPIRY_MINUTES
    from sqlalchemy import text

    team = await repo.create_team(name="stale-meeting-team", mode="coordinate")
    meeting = await repo.create_meeting(
        team_id=team.id,
        topic="old discussion",
        participants=["agent-a"],
    )
    await repo.create_meeting_message(
        meeting_id=meeting.id,
        agent_id="agent-a",
        agent_name="agent-a",
        content="Old message",
        round_number=1,
    )
    # Backdate the message timestamp beyond the expiry window
    from aiteam.storage.connection import get_session

    old_time = datetime.now() - timedelta(minutes=MEETING_EXPIRY_MINUTES + 10)
    async with get_session(repo._db_url) as session:
        await session.execute(
            text("UPDATE meeting_messages SET timestamp = :ts WHERE meeting_id = :mid"),
            {"ts": old_time, "mid": meeting.id},
        )
        await session.commit()

    _fake_teams_dir(tmp_path)
    reaper = StateReaper(repo, event_bus)

    with patch("pathlib.Path.home", return_value=tmp_path):
        await reaper._check_team_liveness(repo)

    updated_team = await repo.get_team(team.id)
    assert updated_team.status == "completed", (
        "Team with only stale meetings should be closed"
    )

    updated_meeting = await repo.get_meeting(meeting.id)
    assert updated_meeting.status == MeetingStatus.CONCLUDED.value
