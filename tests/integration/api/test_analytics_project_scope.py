"""Analytics query filters intersect the persisted project, team, and header scope."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from aiteam.api import deps
from aiteam.api.routes import analytics
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository


@pytest.fixture
async def analytics_client(tmp_path, monkeypatch):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'analytics.db'}"
    repo = StorageRepository(db_url=db_url)
    await repo.init_db()
    monkeypatch.setattr(deps, "_repository", repo)
    projects, teams, agents = [], [], []
    for index in range(2):
        project = await repo.create_project(f"project-{index}", root_path=str(tmp_path / f"project-{index}"))
        team = await repo.create_team(f"team-{index}", "coordinate", project_id=project.id)
        agent = await repo.create_agent(team.id, f"agent-{index}", "worker")
        await repo.update_agent(agent.id, project_id=project.id)
        await repo.create_activity(
            agent_id=agent.id, session_id=f"session-{index}", tool_name=f"Tool{index}", status="completed",
        )
        task = await repo.create_task(team.id, f"task-{index}", project_id=project.id)
        await repo.update_task(task.id, status="completed")
        projects.append(project.id)
        teams.append(team.id)
        agents.append(agent.id)
    app = FastAPI()
    app.include_router(analytics.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            yield client, projects, teams, agents
    finally:
        await get_engine(db_url).dispose()


@pytest.mark.parametrize("endpoint", ["tool-usage", "agent-productivity", "timeline", "efficiency"])
async def test_analytics_project_team_and_header_intersection(analytics_client, endpoint):
    client, projects, teams, agents = analytics_client

    async def check(expected, *, params=None, headers=None):
        response = await client.get(f"/api/analytics/{endpoint}", params=params, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["success"] is True
        data = response.json()["data"]
        if endpoint in {"tool-usage", "timeline"}:
            count = sum(item["count"] for item in data)
            if expected == 1 and endpoint == "tool-usage":
                assert {item["tool_name"] for item in data} == {"Tool0"}
        elif endpoint == "agent-productivity":
            count = sum(item["activity_count"] for item in data)
            if expected == 1:
                assert {item["agent_id"] for item in data} == {agents[0]}
        else:
            count = sum(item["activity_count"] for item in data["agent_utilization"])
            assert data["task_completion"]["total_tasks"] == expected
            assert data["task_completion"]["completed_tasks"] == expected
            if expected == 1:
                assert {item["agent_id"] for item in data["top_agents"]} == {agents[0]}
        assert count == expected, data

    await check(2)
    await check(1, params={"project_id": projects[0]})
    await check(1, params={"project_id": projects[0], "team_id": teams[0]})
    await check(0, params={"project_id": projects[0], "team_id": teams[1]})
    header = {"X-Project-Id": projects[0]}
    await check(1, headers=header)
    await check(1, params={"project_id": projects[0], "team_id": teams[0]}, headers=header)
    await check(0, params={"project_id": projects[1], "team_id": teams[1]}, headers=header)
    await check(0, params={"team_id": teams[1]}, headers=header)
