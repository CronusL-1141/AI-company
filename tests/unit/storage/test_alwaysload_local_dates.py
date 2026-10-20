"""Count rotation days in each host zone, using only isolated in-memory DBs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("zone", "first", "second", "distinct_days"),
    [
        ("UTC", "2026-07-14T23:59:00+00:00", "2026-07-15T00:00:00+00:00", 2),
        ("America/Los_Angeles", "2026-07-14T23:59:00+00:00", "2026-07-15T00:00:00+00:00", 1),
        ("America/Los_Angeles", "2026-07-15T06:59:00+00:00", "2026-07-15T07:00:00+00:00", 2),
        ("America/Los_Angeles", "2026-01-15T07:59:00+00:00", "2026-01-15T08:00:00+00:00", 2),
        ("America/Los_Angeles", "2026-11-01T08:30:00+00:00", "2026-11-01T09:30:00+00:00", 1),
    ],
    ids=["utc-midnight", "pacific-utc-midnight", "pacific-summer-midnight", "pacific-winter-midnight", "dst-fold"],
)
def test_rotation_distinct_days_follow_host_calendar(
    zone: str, first: str, second: str, distinct_days: int,
) -> None:
    source = Path(__file__).resolve().parents[3] / "src"
    env = {**os.environ, "TZ": zone, "PYTHONPATH": str(source)}
    script = """
import asyncio
import json
import sys
from datetime import datetime, timedelta
from unittest.mock import patch
from sqlalchemy import select
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import AgentActivityModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentActivity

async def main():
    moments = [datetime.fromisoformat(value) for value in sys.argv[1:]]
    repo = StorageRepository(db_url='sqlite+aiosqlite://')
    await repo.init_db()
    name = 'mcp__ai-team-os__local_day'
    try:
        async with get_session(repo._db_url) as session:
            for moment in moments:
                session.add(AgentActivityModel.from_pydantic(AgentActivity(
                    agent_id='test-agent', session_id='test-session', tool_name=name,
                    timestamp=moment,
                )))
        with patch('aiteam.storage.repository.utc_now', return_value=moments[-1] + timedelta(hours=1)):
            rows = await repo.alwaysload_tool_frequencies(min_distinct_days=1)
            eligible = await repo.alwaysload_tool_frequencies(min_distinct_days=2)
        async with get_session(repo._db_url) as session:
            stored = (await session.execute(select(AgentActivityModel.timestamp)
                      .order_by(AgentActivityModel.timestamp))).scalars().all()
        print(json.dumps({'rows': rows, 'eligible': eligible,
                          'stored': [value.isoformat() for value in stored]}))
    finally:
        await close_db()
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, first, second],
        env=env, capture_output=True, text=True, timeout=30, check=True,
    )
    data = json.loads(result.stdout)
    expected = [["mcp__ai-team-os__local_day", 2, distinct_days]]
    assert data["rows"] == expected
    assert data["eligible"] == (expected if distinct_days >= 2 else [])
    assert data["stored"] == [first, second]
