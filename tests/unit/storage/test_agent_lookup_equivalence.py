"""The narrowed agent lookups pick exactly what the hydrate-everything versions did.

``find_session_leaders`` / ``find_session_primary_agent`` replace Python scans over
``find_agents_by_session`` on the hook path, and ``list_busy_agents_by_team``
replaces ``list_agents`` per team in the reaper. Each is checked against the old
algorithm, written out here verbatim, over seeded random sessions and teams.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import timedelta

import pytest
import pytest_asyncio

from aiteam.clock import utc_now
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

ROLES = ("leader", "member", "general-purpose")
SOURCES = ("api", "hook")
STATUSES = ("busy", "waiting", "offline")


@pytest_asyncio.fixture
async def seeded(tmp_path):
    database = tmp_path / "lookup.sqlite"
    url = f"sqlite+aiosqlite:///{database}"
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    rng = random.Random(20260925)
    base = utc_now() - timedelta(days=1)
    teams = [await repo.create_team(f"team-{i}", "coordinate") for i in range(12)]
    sessions = [f"sess-{i}" for i in range(40)]
    agent_ids = []
    for index in range(400):
        team = rng.choice(teams)
        tier = rng.randrange(len(sessions))
        # Sessions 0-19 may have leaders; 20-29 have none, so the api tier decides;
        # 30-39 have neither, so the busy-first fallback decides.
        role = rng.choice(ROLES) if tier < 20 else rng.choice(ROLES[1:])
        source = rng.choice(SOURCES) if tier < 30 else "hook"
        agent = await repo.create_agent(
            team.id, f"agent-{index}", role, source=source, session_id=sessions[tier],
        )
        agent_ids.append(agent.id)
    con = sqlite3.connect(database)
    try:
        for agent_id in agent_ids:
            con.execute(
                "UPDATE agents SET status = ?, created_at = ?, project_id = ? WHERE id = ?",
                (rng.choice(STATUSES), (base + timedelta(seconds=rng.randrange(80000))).isoformat(),
                 rng.choice(("proj-a", "proj-b", None)), agent_id),
            )
        for team in teams:
            con.execute("UPDATE teams SET project_id = ? WHERE id = ?",
                        (rng.choice(("proj-a", "proj-b", None)), team.id))
        # Rows whose team is gone must stay out, as list_teams never yields them.
        con.execute("DELETE FROM teams WHERE id = ?", (teams[0].id,))
        con.commit()
    finally:
        con.close()
    try:
        yield repo, url, sessions
    finally:
        await get_engine(url).dispose()


def _old_find_leader(agents):
    """hook_translator._find_leader before the change, verbatim."""
    if not agents:
        return None
    leaders = [a for a in agents if a.role == "leader"]
    if leaders:
        return leaders[0]
    api_matches = [a for a in agents if a.source == "api"]
    if api_matches:
        return api_matches[0]
    agents.sort(key=lambda a: 0 if a.status == "busy" else 1)
    return agents[0]


@pytest.mark.asyncio
async def test_session_lookups_match_the_python_scan(seeded):
    repo, _, sessions = seeded
    for session_id in [*sessions, "sess-missing"]:
        everything = await repo.find_agents_by_session(session_id)
        old = _old_find_leader(list(everything))
        new = await repo.find_session_primary_agent(session_id)
        assert (new.id if new else None) == (old.id if old else None), session_id
        leaders = [a.id for a in everything if a.role == "leader"]
        assert [a.id for a in await repo.find_session_leaders(session_id)] == leaders


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["", "proj-a"], ids=["unscoped", "project-scoped"])
async def test_busy_agents_by_team_match_list_agents_per_team(seeded, scope):
    _, url, _ = seeded
    repo = StorageRepository(db_url=url, project_scope=scope)
    old: list[str] = []
    for team in await repo.list_teams():
        old.extend(a.id for a in await repo.list_agents(team.id) if a.status == "busy")
    grouped = await repo.list_busy_agents_by_team()
    new = [a.id for team in await repo.list_teams() for a in grouped.get(team.id, [])]
    assert new == old
    assert old  # the seed must exercise the comparison
