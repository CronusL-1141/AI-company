"""Bounded rankings across persisted SQLite, HTTP, and intersected scope filters."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event

from aiteam.api import deps
from aiteam.api.routes import analytics
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository


async def test_ranking_sql_pagination_and_global_metrics(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'rankings.db'}"
    repo = StorageRepository(db_url=db_url)
    await repo.init_db()
    monkeypatch.setattr(deps, "_repository", repo)
    projects, teams, expected = [], [], []
    for scope, size in enumerate((107, 2)):
        project = await repo.create_project(f"scope-{scope}", root_path=str(tmp_path / str(scope)))
        team = await repo.create_team(f"team-{scope}", "coordinate", project_id=project.id)
        projects.append(project.id)
        teams.append(team.id)
        rows = []
        for index in range(size):
            agent = await repo.create_agent(team.id, "same-name", "worker")
            await repo.update_agent(agent.id, project_id=project.id)
            count = 3 if index == 0 else 1
            for _ in range(count):
                await repo.create_activity(agent.id, "fixture", "Read", status="completed")
            rows.append((agent.id, count))
        expected.append(sorted(rows, key=lambda row: (-row[1], row[0])))
        # Agents with no observed activity do not belong in this ranking's total.
        await repo.create_agent(team.id, "unobserved", "worker")
        task = await repo.create_task(team.id, "done", project_id=project.id)
        await repo.update_task(task.id, status="completed")
    app = FastAPI()
    app.include_router(analytics.router)
    engine = get_engine(db_url)
    queries = []

    def capture(_connection, _cursor, statement, parameters, _context, _many):
        if "GROUP BY agent_activities.agent_id" in statement:
            queries.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:

            async def page(**params):
                response = await client.get("/api/analytics/agent-productivity", params=params)
                assert response.status_code == 200, response.text
                return response.json()

            first = await page(project_id=projects[0])
            assert first["total"] == 107
            assert first["total_activities"] == 109
            assert first["limit"] == 20 and first["offset"] == 0
            assert len(first["data"]) == 20
            assert queries and "LIMIT" in queries[-1][0] and queries[-1][1][-2:] == (20, 0)
            assert [row["agent_id"] for row in first["data"]] == [row[0] for row in expected[0][:20]]
            ids = []
            for offset in range(0, 107, 20):
                result = await page(project_id=projects[0], limit=20, offset=offset)
                assert result["total"] == 107 and result["total_activities"] == 109
                ids.extend(row["agent_id"] for row in result["data"])
            assert ids == [row[0] for row in expected[0]]
            assert len(set(ids)) == 107
            last = await page(project_id=projects[0], offset=100)
            assert len(last["data"]) == 7
            assert (await page(project_id=projects[0], offset=107))["data"] == []
            assert (await page(project_id=projects[0], offset=10000))["total"] == 107
            assert (await page(project_id=projects[0], offset=2**63 - 1))["data"] == []
            assert len((await page(project_id=projects[0], limit=100))["data"]) == 100
            assert len((await page(project_id=projects[0], limit=1))["data"]) == 1
            assert (await page())["total"] == 109
            assert (await page(team_id=teams[1]))["total"] == 2
            assert (await page(project_id=projects[0], team_id=teams[1]))["total"] == 0
            scoped = await client.get("/api/analytics/agent-productivity", headers={"X-Project-Id": projects[0]})
            assert scoped.json()["total"] == 107
            mismatch = await client.get(
                "/api/analytics/agent-productivity",
                params={"project_id": projects[1]},
                headers={"X-Project-Id": projects[0]},
            )
            assert mismatch.json()["total"] == 0 and mismatch.json()["total_activities"] == 0
            for params in (
                {"limit": 0},
                {"limit": -1},
                {"limit": 101},
                {"offset": -1},
                {"offset": 2**63},
                {"limit": "nan"},
                {"offset": "1.5"},
            ):
                response = await client.get("/api/analytics/agent-productivity", params=params)
                assert response.status_code == 422, response.text
            efficiency = await client.get("/api/analytics/efficiency", params={"project_id": projects[0]})
            assert efficiency.json()["data"]["avg_tools_per_task"] == 109
            overview = await client.get("/api/analytics/team-overview", params={"team_id": teams[0]})
            assert overview.json()["data"]["total_activities"] == 109
            assert len(overview.json()["data"]["agent_productivity"]) == 107
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        await engine.dispose()


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 2**63}],
    ids=["zero-limit", "oversized-limit", "negative-offset", "overflow-offset"],
)
async def test_repository_rejects_invalid_ranking_window(kwargs):
    with pytest.raises(ValueError):
        await StorageRepository(db_url="sqlite+aiosqlite://").get_agent_productivity(**kwargs)
