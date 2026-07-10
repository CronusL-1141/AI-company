"""Tests for meeting_attendance_check API endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


class TestMeetingAttendanceCheck:
    """Tests for the attendance_check_logic pure-async function."""

    def _make_meeting(self, expected_participants=None):
        from aiteam.types import Meeting, MeetingStatus
        meta = {}
        if expected_participants is not None:
            meta["expected_participants"] = expected_participants
        return Meeting(
            id="mtg-test-1",
            team_id="team-1",
            topic="Test Meeting",
            status=MeetingStatus.ACTIVE,
            participants=expected_participants or [],
            meta_json=meta,
        )

    def _make_message(self, agent_name: str, round_number: int = 1):
        from aiteam.types import MeetingMessage
        return MeetingMessage(
            id=f"msg-{agent_name}-r{round_number}",
            meeting_id="mtg-test-1",
            agent_id=agent_name,
            agent_name=agent_name,
            content="Test message",
            round_number=round_number,
        )

    def _mock_repo(self, meeting, messages):
        repo = AsyncMock()
        repo.get_meeting.return_value = meeting
        repo.list_meeting_messages.return_value = messages
        return repo

    @pytest.mark.asyncio
    async def test_one_of_two_spoken_gives_correct_pending(self):
        from aiteam.api.routes.meetings import attendance_check_logic

        meeting = self._make_meeting(["arch-lead", "backend-arch"])
        repo = self._mock_repo(meeting, [self._make_message("arch-lead")])

        result = await attendance_check_logic("mtg-test-1", repo)

        assert result["round"] == 1
        assert "arch-lead" in result["spoken"]
        assert result["pending"] == ["backend-arch"]

    @pytest.mark.asyncio
    async def test_all_spoken_gives_empty_pending(self):
        from aiteam.api.routes.meetings import attendance_check_logic

        meeting = self._make_meeting(["arch-lead", "backend-arch"])
        messages = [self._make_message("arch-lead"), self._make_message("backend-arch")]
        repo = self._mock_repo(meeting, messages)

        result = await attendance_check_logic("mtg-test-1", repo)

        assert result["pending"] == []
        assert set(result["spoken"]) == {"arch-lead", "backend-arch"}

    @pytest.mark.asyncio
    async def test_nonexistent_meeting_raises_not_found(self):
        from aiteam.api.exceptions import NotFoundError
        from aiteam.api.routes.meetings import attendance_check_logic

        repo = AsyncMock()
        repo.get_meeting.return_value = None

        with pytest.raises(NotFoundError):
            await attendance_check_logic("does-not-exist", repo)

    @pytest.mark.asyncio
    async def test_no_messages_gives_full_pending(self):
        from aiteam.api.routes.meetings import attendance_check_logic

        meeting = self._make_meeting(["arch-lead", "backend-arch"])
        repo = self._mock_repo(meeting, [])

        result = await attendance_check_logic("mtg-test-1", repo)

        assert set(result["pending"]) == {"arch-lead", "backend-arch"}
        assert result["spoken"] == []

    @pytest.mark.asyncio
    async def test_round_detection_from_messages(self):
        from aiteam.api.routes.meetings import attendance_check_logic

        meeting = self._make_meeting(["arch-lead"])
        messages = [
            self._make_message("arch-lead", round_number=1),
            self._make_message("arch-lead", round_number=2),
        ]
        repo = self._mock_repo(meeting, messages)

        result = await attendance_check_logic("mtg-test-1", repo)

        assert result["round"] == 2

    @pytest.mark.asyncio
    async def test_falls_back_to_participants_when_no_meta(self):
        from aiteam.api.routes.meetings import attendance_check_logic

        meeting = self._make_meeting(expected_participants=None)
        meeting.participants = ["arch-lead", "backend-arch"]
        meeting.meta_json = {}
        repo = self._mock_repo(meeting, [])

        result = await attendance_check_logic("mtg-test-1", repo)

        assert set(result["expected"]) == {"arch-lead", "backend-arch"}

    @pytest.mark.asyncio
    async def test_result_has_required_keys(self):
        from aiteam.api.routes.meetings import attendance_check_logic

        meeting = self._make_meeting(["arch-lead"])
        repo = self._mock_repo(meeting, [])

        result = await attendance_check_logic("mtg-test-1", repo)

        for key in ("success", "meeting_id", "round", "expected", "spoken", "pending", "timeout_in_seconds"):
            assert key in result, f"Missing key: {key}"
