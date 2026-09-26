"""补投标记的解析，与按 tool_use_id 配对在并发下的正确性（仓储层，真 SQLite 文件）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from aiteam.api.hook_translator import HOOK_REPLAY_MAX_AGE, parse_hook_replay
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "origin", "valid", "attempt"),
    [
        ({"origin_at": "2026-09-26T11:59:00+00:00", "attempt": 1}, NOW - timedelta(minutes=1), True, 1),
        ({"origin_at": "2026-09-26T11:59:00Z", "attempt": 3}, NOW - timedelta(minutes=1), True, 3),
        ({"origin_at": "2026-09-26T19:59:00+08:00"}, NOW - timedelta(minutes=1), True, None),
        ({"origin_at": "2026-09-26T13:00:00+00:00"}, NOW, False, None),  # future
        ({"origin_at": "2026-09-26T11:59:00"}, NOW, False, None),  # no timezone
        ({"origin_at": "yesterday"}, NOW, False, None),
        ({"origin_at": 1758888000}, NOW, False, None),
        ({"origin_at": "x" * 65}, NOW, False, None),
        ({}, NOW, False, None),
        ({"origin_at": "2026-09-26T11:59:00Z", "attempt": True}, NOW - timedelta(minutes=1), True, None),
        ({"origin_at": "2026-09-26T11:59:00Z", "attempt": 0}, NOW - timedelta(minutes=1), True, None),
    ],
    ids=["utc", "zulu", "offset", "future", "naive", "garbage", "number", "too-long", "empty",
         "bool-attempt", "zero-attempt"],
)
def test_marker_time_is_validated(value, origin, valid, attempt):
    replay = parse_hook_replay(value, NOW)
    assert (replay.origin_at, replay.origin_valid, replay.attempt) == (origin, valid, attempt)


def test_too_old_falls_back_to_now():
    old = (NOW - HOOK_REPLAY_MAX_AGE - timedelta(seconds=1)).isoformat()
    assert parse_hook_replay({"origin_at": old}, NOW).origin_at == NOW
    edge = (NOW - HOOK_REPLAY_MAX_AGE).isoformat()
    assert parse_hook_replay({"origin_at": edge}, NOW).origin_valid is True


@pytest.mark.parametrize("value", [None, "2026-09-26T11:59:00Z", ["origin_at"], 1])
def test_no_marker_unless_it_is_a_dict(value):
    assert parse_hook_replay(value, NOW) is None


CALLS = 48  # parallel pairs: enough to crush a dev machine, per the repo's concurrency rule


def test_start_and_completion_racing_each_other_end_in_one_finished_row(tmp_path):
    """Pre and Post of 48 calls all at once, half of them Post first: one finished row each."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'race.sqlite'}"

    async def run() -> list:
        repo = StorageRepository(db_url=url)
        await repo.init_db()
        try:
            start = datetime.now(UTC)

            async def call(i: int):
                phases = [
                    repo.record_cc_tool_activity(
                        agent_id="agent-1", session_id="s-race", tool_use_id=f"toolu_{i}",
                        tool_name="Bash", phase="start", at=start, input_summary=f"call {i}",
                    ),
                    repo.record_cc_tool_activity(
                        agent_id="agent-1", session_id="s-race", tool_use_id=f"toolu_{i}",
                        tool_name="Bash", phase="complete", at=start + timedelta(seconds=1),
                        input_summary=f"call {i}", output_summary=f"out {i}",
                    ),
                ]
                await asyncio.gather(*(phases if i % 2 else reversed(phases)))

            await asyncio.gather(*(call(i) for i in range(CALLS)))
            return await repo.list_activities("agent-1", limit=CALLS * 2)
        finally:
            await get_engine(url).dispose()

    rows = asyncio.run(run())
    assert len(rows) == CALLS
    assert {r.status for r in rows} == {"completed"}
    assert sorted(r.output_summary for r in rows) == sorted(f"out {i}" for i in range(CALLS))
    assert {r.duration_ms for r in rows} == {1000}


@pytest.mark.parametrize("completion_first", [True, False], ids=["completion-first", "in-order"])
def test_the_row_spans_start_to_completion_in_either_order(tmp_path, completion_first):
    """Start at +1s, completion at +5s with a 1000ms host duration: one row from +1s to +5s.

    The host duration only places the start while no PreToolUse has been seen; a late
    start moves the start back and keeps the end (it used to keep the duration, which
    put the end a delivery delay too early).
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'span.sqlite'}"
    t0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    async def run():
        repo = StorageRepository(db_url=url)
        await repo.init_db()
        try:
            start = dict(phase="start", at=t0 + timedelta(seconds=1))
            done = dict(phase="complete", at=t0 + timedelta(seconds=5), duration_ms=1000)
            phases = [done, start] if completion_first else [start, done]
            for kwargs in phases:
                last = await repo.record_cc_tool_activity(
                    agent_id="a", session_id="s", tool_use_id="toolu_span", tool_name="Bash",
                    input_summary="x", **kwargs,
                )
            return last
        finally:
            await get_engine(url).dispose()

    row = asyncio.run(run())
    assert row.status == "completed"
    assert row.timestamp.replace(tzinfo=UTC) == t0 + timedelta(seconds=1)
    assert row.duration_ms == 4000
