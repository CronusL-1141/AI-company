"""Leader liveness touch: a burst past the 60s mark runs the touch once, not once per event."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture
async def translator():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    yield HookTranslator(repo=repo, event_bus=EventBus(repo=repo))
    await close_db()


def _gate_lookups(translator, monkeypatch):
    """Count leader lookups and hold them until released, so a burst overlaps."""
    calls = []
    release = asyncio.Event()
    original = translator.repo.find_session_leaders

    async def gated(session_id):
        calls.append(session_id)
        await release.wait()
        return await original(session_id)

    monkeypatch.setattr(translator.repo, "find_session_leaders", gated)
    return calls, release


@pytest.mark.asyncio
async def test_concurrent_touches_share_one_lookup(translator, monkeypatch):
    team = await translator.repo.create_team("touch-team", "coordinate")
    leader = await translator.repo.create_agent(team.id, "leader", "leader", session_id="sess-touch")
    calls, release = _gate_lookups(translator, monkeypatch)

    burst = [asyncio.create_task(translator._touch_session_leader("sess-touch")) for _ in range(10)]
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(*burst)

    assert calls == ["sess-touch"]
    touched = await translator.repo.get_agent(leader.id)
    assert touched.status == "busy" and touched.last_active_at is not None
    assert "sess-touch" in translator._leader_touch


@pytest.mark.asyncio
async def test_slot_is_handed_back_when_nothing_was_touched(translator, monkeypatch):
    calls, release = _gate_lookups(translator, monkeypatch)
    release.set()

    await translator._touch_session_leader("sess-no-leader")
    assert "sess-no-leader" not in translator._leader_touch  # the next event retries
    await translator._touch_session_leader("sess-no-leader")
    assert calls == ["sess-no-leader", "sess-no-leader"]


@pytest.mark.asyncio
async def test_failed_lookup_hands_the_slot_back(translator, monkeypatch):
    async def broken(session_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(translator.repo, "find_session_leaders", broken)
    await translator._touch_session_leader("sess-broken")
    assert "sess-broken" not in translator._leader_touch
