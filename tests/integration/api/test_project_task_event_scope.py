"""Project event feeds must include teamless tasks without leaking other projects."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aiteam.storage.repository import StorageRepository


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name, "root_path": f"/event-scope/{name}"})
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _task(client: TestClient, project_id: str, title: str) -> str:
    response = client.post(f"/api/projects/{project_id}/tasks", json={"title": title})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["team_id"] is None
    return response.json()["data"]["id"]


def _events(client: TestClient, **params: Any) -> dict:
    response = client.get("/api/events", params=params)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == len(payload["data"])
    return payload


def _event_ids(payload: dict) -> list[str]:
    return [event["id"] for event in payload["data"]]


@pytest.mark.parametrize("has_team", [False, True])
def test_api_task_update_is_visible_with_project_filter(repo_and_client, has_team):
    """Reproduce the actual create -> update -> entity query -> project query path."""
    repo, client = repo_and_client
    project_id = _project(client, "owner")
    if has_team:
        _run(repo.create_team("owner-team", "coordinate", project_id=project_id))
    task_id = _task(client, project_id, "Teamless task")
    response = client.put(f"/api/tasks/{task_id}", json={"status": "running"})
    assert response.status_code == 200, response.text

    # The baseline stores this event correctly; it only loses it in the project view.
    entity_feed = _events(client, entity_id=task_id, type="task.updated")
    assert entity_feed["total"] == 1
    project_feed = _events(
        client, entity_id=task_id, type="task.updated", project_id=project_id,
    )
    assert _event_ids(project_feed) == _event_ids(entity_feed)
    # The status-change emitter only carries task_id in data, not entity_id.
    unscoped = _events(client)
    assert {event["type"] for event in unscoped["data"]} == {"task.updated", "task.status_changed"}
    assert _event_ids(_events(client, project_id=project_id)) == _event_ids(unscoped)


def test_teamless_task_does_not_leak_to_other_or_unknown_project(repo_and_client):
    repo, client = repo_and_client
    owner = _project(client, "owner")
    other = _project(client, "other")
    task_id = _task(client, owner, "Private task")
    _run(repo.update_task(task_id, title="Updated private task"))

    assert _events(client, entity_id=task_id)["total"] == 1
    assert _events(client, entity_id=task_id, project_id=other)["data"] == []
    assert _events(client, entity_id=task_id, project_id="missing-project")["data"] == []
    assert _events(client, entity_id=task_id, project_id="")["total"] == 1


@pytest.mark.parametrize("event_source", ["repository", "team", "agent"])
def test_explicit_task_project_wins_over_team_and_event_source(repo_and_client, event_source):
    """A task explicitly moved to B must not appear in both A and B feeds."""
    repo, client = repo_and_client
    team_project = _project(client, "team-project")
    task_project = _project(client, "task-project")
    team = _run(repo.create_team("team", "coordinate", project_id=team_project))
    agent = _run(repo.create_agent(team.id, "worker", "dev", system_prompt=""))
    task = _run(repo.create_task(team.id, "Explicit owner", project_id=task_project))
    source = {"repository": "repository", "team": f"team:{team.id}", "agent": f"agent:{agent.id}"}[
        event_source
    ]
    event = _run(repo.create_event("task.updated", source, {}, entity_id=task.id, entity_type="task"))

    assert _events(client, entity_id=task.id, project_id=team_project)["data"] == []
    assert _event_ids(_events(client, entity_id=task.id, project_id=task_project)) == [event.id]
    # Team selection remains a structural membership view, not a project alias.
    team_events = _run(repo.list_events(team_ids=[team.id], entity_id=task.id))
    assert [item.id for item in team_events] == [event.id]


@pytest.mark.parametrize("legacy_project", [None, ""])
def test_legacy_task_without_project_falls_back_to_team(repo_and_client, legacy_project):
    repo, client = repo_and_client
    project_id = _project(client, "owner")
    other = _project(client, "other")
    team = _run(repo.create_team("legacy-team", "coordinate"))
    # Seed the historical shape before the team acquired a project binding.
    task = _run(repo.create_task(team.id, "Legacy task", project_id=legacy_project))
    _run(repo.update_team(team.id, project_id=project_id))
    event = _run(repo.create_event("task.updated", "repository", {}, entity_id=task.id))

    assert _event_ids(_events(client, entity_id=task.id, project_id=project_id)) == [event.id]
    assert _events(client, entity_id=task.id, project_id=other)["data"] == []


def test_api_status_event_payload_does_not_leak_through_conflicting_team(repo_and_client):
    repo, client = repo_and_client
    team_project = _project(client, "team-project")
    task_project = _project(client, "task-project")
    team = _run(repo.create_team("team", "coordinate", project_id=team_project))
    task = _run(repo.create_task(team.id, "Explicit owner", project_id=task_project))
    response = client.put(f"/api/tasks/{task.id}", json={"status": "running"})
    assert response.status_code == 200, response.text

    unscoped = _events(client)
    assert unscoped["total"] == 2
    assert _events(client, project_id=team_project)["data"] == []
    assert _event_ids(_events(client, project_id=task_project)) == _event_ids(unscoped)


def test_non_task_payload_task_id_does_not_override_the_event_owner(repo_and_client):
    repo, client = repo_and_client
    owner = _project(client, "owner")
    other = _project(client, "other")
    team = _run(repo.create_team("team", "coordinate", project_id=owner))
    other_task = _task(client, other, "Referenced task")
    event = _run(repo.create_event("agent.updated", f"team:{team.id}", {"task_id": other_task}))

    assert _event_ids(_events(client, project_id=owner)) == [event.id]
    assert _events(client, project_id=other)["data"] == []


def test_explicit_event_entity_wins_over_payload_task_reference(repo_and_client):
    repo, client = repo_and_client
    owner = _project(client, "owner")
    other = _project(client, "other")
    owned_task = _task(client, owner, "Primary task")
    referenced_task = _task(client, other, "Referenced task")
    event = _run(repo.create_event(
        "task.updated", "repository", {"task_id": referenced_task}, entity_id=owned_task,
    ))

    assert _event_ids(_events(client, project_id=owner)) == [event.id]
    assert _events(client, project_id=other)["data"] == []


def test_project_filter_preserves_existing_team_and_agent_event_paths(repo_and_client):
    repo, client = repo_and_client
    owner = _project(client, "owner")
    other = _project(client, "other")
    team = _run(repo.create_team("owner-team", "coordinate", project_id=owner))
    agent = _run(repo.create_agent(team.id, "worker", "dev", system_prompt=""))
    expected = [
        _run(repo.create_event("team.created", f"team:{team.id}", {})),
        _run(repo.create_event("team.created", "repository", {}, entity_id=team.id)),
        _run(repo.create_event("agent.status_changed", f"agent:{agent.id}", {})),
        _run(repo.create_event("agent.updated", "repository", {}, entity_id=agent.id)),
    ]
    assert set(_event_ids(_events(client, project_id=owner))) == {item.id for item in expected}
    assert _events(client, project_id=other)["data"] == []


def test_project_scope_is_applied_before_limit_and_total_matches_page(repo_and_client):
    repo, client = repo_and_client
    owner = _project(client, "owner")
    other = _project(client, "other")
    team = _run(repo.create_team("owner-team", "coordinate", project_id=owner))
    task_id = _task(client, owner, "Teamless task")
    other_task = _task(client, other, "Other task")
    first = _run(repo.create_event("task.updated", "repository", {}, entity_id=task_id))
    second = _run(repo.create_event("team.created", f"team:{team.id}", {}))
    third = _run(repo.create_event("task.updated", "repository", {}, entity_id=task_id))
    for _ in range(3):
        _run(repo.create_event("task.updated", "repository", {}, entity_id=other_task))

    assert _event_ids(_events(client, project_id=owner, limit=2)) == [third.id, second.id]
    assert _event_ids(_events(client, project_id=owner, limit=1)) == [third.id]
    assert _event_ids(_events(client, project_id=owner, limit=200)) == [third.id, second.id, first.id]


@pytest.mark.parametrize(
    ("event_type", "source", "expected_count"),
    [("task.updated", "repository", 1), ("task.created", "repository", 0),
     ("task.updated", "different", 0)],
)
def test_project_scope_intersects_existing_api_filters(repo_and_client, event_type, source, expected_count):
    repo, client = repo_and_client
    owner = _project(client, "owner")
    task_id = _task(client, owner, "Teamless task")
    _run(repo.create_event("task.updated", "repository", {}, entity_id=task_id))
    payload = _events(
        client, project_id=owner, entity_id=task_id, type=event_type, source=source,
    )
    assert payload["total"] == expected_count


def test_explicit_team_scope_does_not_gain_teamless_tasks(repo_and_client):
    repo, client = repo_and_client
    owner = _project(client, "owner")
    other = _project(client, "other")
    team = _run(repo.create_team("owner-team", "coordinate", project_id=owner))
    owned = _run(repo.create_task(team.id, "Team task"))
    conflicting = _run(repo.create_task(team.id, "Other project task", project_id=other))
    teamless = _task(client, owner, "Teamless task")
    events = {}
    for task_id in [owned.id, conflicting.id, teamless]:
        events[task_id] = _run(repo.create_event("task.updated", "repository", {}, entity_id=task_id))

    team_events = _run(repo.list_events(team_ids=[team.id]))
    assert {item.id for item in team_events} == {events[owned.id].id, events[conflicting.id].id}
    assert _run(repo.list_events(team_ids=[])) == []
    assert _run(repo.list_events(team_ids=["missing-team"])) == []


def test_repository_project_and_team_filters_intersect(repo_and_client):
    repo, client = repo_and_client
    assert isinstance(repo, StorageRepository)
    owner = _project(client, "owner")
    other = _project(client, "other")
    team = _run(repo.create_team("owner-team", "coordinate", project_id=owner))
    owned = _run(repo.create_task(team.id, "Team task"))
    conflicting = _run(repo.create_task(team.id, "Other project task", project_id=other))
    teamless = _task(client, owner, "Teamless task")
    events = {}
    for task_id in [owned.id, conflicting.id, teamless]:
        events[task_id] = _run(repo.create_event("task.updated", "repository", {}, entity_id=task_id))

    selected = _run(repo.list_events(project_id=owner, team_ids=[team.id], type_prefix="task."))
    assert [item.id for item in selected] == [events[owned.id].id]
    assert _run(repo.list_events(project_id=owner, team_ids=[])) == []
    assert _run(repo.list_events(project_id=owner, team_ids=["missing-team"])) == []
