"""Meeting writes that fail must leave nothing behind and tell the caller nothing internal.

- A message that fails to insert must not have added its speaker to the meeting:
  the participant update and the insert are one transaction.
- A failed meeting creation answers a generic 500; the exception text stays in the log.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.exc import IntegrityError
from testlib import make_team

from aiteam.storage.models import MeetingMessageModel
from tests.unit.test_meeting_security import _make_client, _teardown


def test_a_message_that_fails_to_insert_adds_no_participant(monkeypatch):
    client, repo, _ = _make_client()
    try:
        team_id = make_team({"name": "atomic-team", "mode": "coordinate"})["id"]
        meeting_id = client.post(f"/api/teams/{team_id}/meetings", json={"topic": "t"}).json()["data"]["id"]
        first = client.post(f"/api/meetings/{meeting_id}/messages",
                            json={"agent_id": "a1", "agent_name": "alice", "content": "hi"})
        assert first.status_code == 201
        taken = first.json()["data"]["id"]

        # The next insert reuses alice's primary key, so it fails inside the database.
        original = MeetingMessageModel.from_pydantic

        def clashing(message):
            row = original(message)
            row.id = taken
            return row

        monkeypatch.setattr(MeetingMessageModel, "from_pydantic", staticmethod(clashing))
        try:  # the test client re-raises what the server's 500 handler answered
            failed = client.post(f"/api/meetings/{meeting_id}/messages",
                                 json={"agent_id": "b1", "agent_name": "bob", "content": "hi"})
            assert failed.status_code >= 500
        except IntegrityError:
            pass
        monkeypatch.setattr(MeetingMessageModel, "from_pydantic", staticmethod(original))

        meeting = asyncio.get_event_loop().run_until_complete(repo.get_meeting(meeting_id))
        assert meeting.participants == ["alice"], f"bob joined without a message: {meeting.participants}"
    finally:
        _teardown()


def test_a_failed_meeting_creation_does_not_echo_the_exception(monkeypatch):
    client, repo, _ = _make_client()
    try:
        team_id = make_team({"name": "leak-team", "mode": "coordinate"})["id"]

        async def boom(**kwargs):
            raise RuntimeError("secret /Users/someone/internal/path.db codec detail")

        monkeypatch.setattr(repo, "create_meeting", boom)
        resp = client.post(f"/api/teams/{team_id}/meetings", json={"topic": "t"})
        assert resp.status_code == 500
        assert "secret" not in resp.text and "path.db" not in resp.text, resp.text
        assert "创建会议失败" in resp.json()["detail"]
    finally:
        _teardown()


def test_concurrent_new_speakers_all_join(tmp_path):
    """Read-modify-write of participants under concurrency must not drop anyone."""
    from aiteam.storage.connection import get_engine
    from aiteam.storage.repository import StorageRepository

    url = f"sqlite+aiosqlite:///{tmp_path / 'race.db'}"
    speakers = [f"speaker-{i:02d}" for i in range(48)]

    async def scenario() -> list[str]:
        repo = StorageRepository(db_url=url)
        await repo.init_db()
        team = await repo.create_team(name="race-team", mode="coordinate")
        meeting = await repo.create_meeting(team_id=team.id, topic="race", participants=[])
        await asyncio.gather(*(
            repo.create_meeting_message(meeting_id=meeting.id, agent_id=name, agent_name=name,
                                        content="hi", join_participants=True)
            for name in speakers
        ))
        joined = (await repo.get_meeting(meeting.id)).participants
        await get_engine(url).dispose()
        return joined

    joined = asyncio.run(scenario())
    missing = sorted(set(speakers) - set(joined))
    assert not missing, f"{len(missing)} speakers lost from participants: {missing[:5]}"
