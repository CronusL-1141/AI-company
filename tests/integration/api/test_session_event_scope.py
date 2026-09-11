"""Project queries over raw session events use persisted ownership, not payload hints."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from aiteam.api.deps import get_hook_translator, get_scoped_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import events, hooks
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture
async def session_api(tmp_path):
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await repo.init_db()
    translator = HookTranslator(repo, EventBus(repo))
    app = FastAPI()
    app.include_router(events.router)
    app.include_router(hooks.router)
    app.dependency_overrides[get_scoped_repository] = lambda: repo
    app.dependency_overrides[get_hook_translator] = lambda: translator
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield repo, client
    finally:
        await close_db()


async def _project(repo, name):
    return await repo.create_project(name=name, root_path=f"/isolated-events/{name}")


async def _agent(repo, *, session_id, team_project=None, agent_project=None, name="default"):
    team = await repo.create_team(f"team-{name}", "coordinate", project_id=team_project)
    agent = await repo.create_agent(
        team.id, name, "default", source="hook", session_id=session_id,
        cc_tool_use_id=f"native-{name}",
    )
    if agent_project is not None:
        agent = await repo.update_agent(agent.id, project_id=agent_project)
    return agent


async def _feed(client, **params):
    response = await client.get("/api/events", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(body["data"])
    return body["data"]


@pytest.mark.parametrize("binding", ["agent", "team", "explicit-agent-wins"])
async def test_raw_native_tool_event_appears_in_its_persisted_project(session_api, binding):
    repo, client = session_api
    owner, other = await _project(repo, "owner"), await _project(repo, "other")
    sid = "opaque-native-session"
    agent = await _agent(
        repo, session_id=sid,
        team_project=owner.id if binding == "team" else other.id if binding == "explicit-agent-wins" else None,
        agent_project=owner.id if binding != "team" else None,
    )
    response = await client.post("/api/hooks/event", json={
        "hook_event_name": "PreToolUse", "session_id": sid,
        "agent_id": agent.cc_tool_use_id, "agent_type": "default",
        "tool_name": "Bash", "tool_input": {"command": "true"},
    })
    assert response.status_code == 200, response.text
    raw = await _feed(client, source=f"session:{sid}", type="cc.tool_use")
    assert len(raw) == 1 and raw[0]["entity_id"] is None
    assert await _feed(client, project_id=owner.id, type="cc.tool_use") == raw
    assert await _feed(client, project_id=other.id, type="cc.tool_use") == []


@pytest.mark.parametrize("entity_id", [None, ""])
async def test_empty_entity_session_event_uses_exact_non_uuid_registration(session_api, entity_id):
    repo, client = session_api
    owner = await _project(repo, "owner")
    await _agent(repo, session_id="not-a-uuid", agent_project=owner.id)
    event = await repo.create_event("cc.tool_complete", "session:not-a-uuid", {}, entity_id=entity_id)
    selected = await _feed(client, project_id=owner.id, type="cc.tool_complete")
    assert [item["id"] for item in selected] == [event.id]


async def test_unknown_or_other_session_is_not_inferred_from_payload(session_api):
    repo, client = session_api
    owner, other = await _project(repo, "owner"), await _project(repo, "other")
    await _agent(repo, session_id="owner-session", agent_project=owner.id, name="owner")
    await _agent(repo, session_id="other-session", agent_project=other.id, name="other")
    for source in ("session:other-session", "session:00000000-0000-4000-8000-000000000001", "session:"):
        await repo.create_event("cc.tool_use", source, {
            "project_id": owner.id, "session_id": "owner-session", "text": owner.id,
        })
    assert len(await _feed(client, type="cc.tool_use")) == 3
    assert await _feed(client, project_id=owner.id, type="cc.tool_use") == []
    assert len(await _feed(client, project_id=other.id, type="cc.tool_use")) == 1


async def test_conflicting_persisted_session_projects_are_not_guessed(session_api):
    repo, client = session_api
    first, second = await _project(repo, "first"), await _project(repo, "second")
    await _agent(repo, session_id="ambiguous", agent_project=first.id, name="first")
    await _agent(repo, session_id="ambiguous", agent_project=second.id, name="second")
    await repo.create_event("cc.tool_use", "session:ambiguous", {})
    assert await _feed(client, project_id=first.id, type="cc.tool_use") == []
    assert await _feed(client, project_id=second.id, type="cc.tool_use") == []


async def test_known_foreign_task_entity_cannot_reenter_via_owned_session(session_api):
    repo, client = session_api
    owner, other = await _project(repo, "owner"), await _project(repo, "other")
    agent = await _agent(repo, session_id="owner-session", agent_project=owner.id)
    task = await repo.create_task(agent.team_id, "Other project task", project_id=other.id)
    event = await repo.create_event("task.updated", "session:owner-session", {}, entity_id=task.id)
    assert await _feed(client, project_id=owner.id, type="task.updated") == []
    assert [item["id"] for item in await _feed(client, project_id=other.id, type="task.updated")] == [event.id]


async def test_explicit_foreign_agent_entity_is_not_overridden_by_session(session_api):
    repo, client = session_api
    owner, other = await _project(repo, "owner"), await _project(repo, "other")
    await _agent(repo, session_id="owner-session", agent_project=owner.id, name="owner")
    foreign = await _agent(repo, session_id="other-session", team_project=other.id, name="other")
    event = await repo.create_event("cc.tool_use", "session:owner-session", {}, entity_id=foreign.id)
    assert await _feed(client, project_id=owner.id, type="cc.tool_use") == []
    assert [item["id"] for item in await _feed(client, project_id=other.id, type="cc.tool_use")] == [event.id]


async def test_explicit_prefix_and_exact_type_contract(session_api):
    repo, client = session_api
    first = await repo.create_event("cc.tool_use", "session:any", {})
    second = await repo.create_event("cc.tool_complete", "session:any", {})
    await repo.create_event("task.updated", "repository", {})
    assert [item["id"] for item in await _feed(client, type_prefix="cc.")] == [second.id, first.id]
    assert await _feed(client, type="cc.") == []
    exact = await _feed(client, type="cc.tool_use", type_prefix="task.")
    assert [item["id"] for item in exact] == [first.id]


@pytest.mark.parametrize("prefix", ["cc_", "cc%"])
async def test_prefix_is_literal_not_an_sql_wildcard(session_api, prefix):
    repo, client = session_api
    await repo.create_event("cc.tool_use", "repository", {})
    await repo.create_event("cc.tool_complete", "repository", {})
    assert await _feed(client, type_prefix=prefix) == []


async def test_project_prefix_source_and_limit_intersect(session_api):
    repo, client = session_api
    owner, other = await _project(repo, "owner"), await _project(repo, "other")
    await _agent(repo, session_id="owner-session", agent_project=owner.id, name="owner")
    await _agent(repo, session_id="other-session", agent_project=other.id, name="other")
    await repo.create_event("cc.tool_use", "session:owner-session", {})
    latest = await repo.create_event("cc.tool_complete", "session:owner-session", {})
    for _ in range(3):
        await repo.create_event("cc.tool_use", "session:other-session", {})
    chosen = await _feed(client, project_id=owner.id, type_prefix="cc.", limit=1)
    assert [item["id"] for item in chosen] == [latest.id]
    assert await _feed(client, project_id=owner.id, type_prefix="cc.", source="session:other-session") == []
