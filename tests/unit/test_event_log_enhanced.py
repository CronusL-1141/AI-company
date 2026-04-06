"""Unit tests for v0.9 enhanced event log — entity_id, entity_type, state_snapshot."""

from __future__ import annotations

from aiteam.storage.repository import StorageRepository

# ================================================================
# EventModel new field storage and retrieval
# ================================================================


async def test_create_event_with_entity_fields(db_repository: StorageRepository) -> None:
    """create_event stores entity_id, entity_type, and state_snapshot correctly."""
    event = await db_repository.create_event(
        event_type="task.updated",
        source="test",
        data={"task_id": "task-123"},
        entity_id="task-123",
        entity_type="task",
        state_snapshot={"status": "running", "assigned_to": "agent-1", "title": "Test task"},
    )

    assert event.entity_id == "task-123"
    assert event.entity_type == "task"
    assert event.state_snapshot == {"status": "running", "assigned_to": "agent-1", "title": "Test task"}


async def test_create_event_without_entity_fields(db_repository: StorageRepository) -> None:
    """create_event with no entity fields defaults to None — backward compat."""
    event = await db_repository.create_event(
        event_type="agent.created",
        source="test",
        data={"info": "hello"},
    )

    assert event.entity_id is None
    assert event.entity_type is None
    assert event.state_snapshot is None


async def test_event_entity_fields_persist(db_repository: StorageRepository) -> None:
    """Entity fields round-trip through the database correctly."""
    created = await db_repository.create_event(
        event_type="agent.updated",
        source="repository",
        data={"agent_id": "agent-abc"},
        entity_id="agent-abc",
        entity_type="agent",
        state_snapshot={"status": "busy", "name": "worker-1"},
    )

    events = await db_repository.list_events(entity_id="agent-abc")
    assert len(events) == 1
    fetched = events[0]
    assert fetched.id == created.id
    assert fetched.entity_id == "agent-abc"
    assert fetched.entity_type == "agent"
    assert fetched.state_snapshot == {"status": "busy", "name": "worker-1"}


# ================================================================
# update_task auto-generates snapshot event
# ================================================================


async def test_update_task_auto_emits_event(db_repository: StorageRepository) -> None:
    """update_task automatically creates a task.updated event with snapshot."""
    team = await db_repository.create_team("t1", "coordinate")
    task = await db_repository.create_task(team.id, "My task")

    await db_repository.update_task(task.id, status="running")

    events = await db_repository.list_events(entity_id=task.id)
    assert len(events) >= 1
    evt = events[0]
    assert evt.entity_id == task.id
    assert evt.entity_type == "task"
    assert evt.state_snapshot is not None
    assert "status" in evt.state_snapshot
    assert evt.state_snapshot["status"] == "running"
    assert "title" in evt.state_snapshot


async def test_update_task_snapshot_excludes_large_fields(db_repository: StorageRepository) -> None:
    """Task snapshot only stores {status, assigned_to, title} — no description or result."""
    team = await db_repository.create_team("t2", "coordinate")
    task = await db_repository.create_task(team.id, "Trim test", description="very long description " * 100)

    await db_repository.update_task(task.id, status="completed", result="big result " * 100)

    events = await db_repository.list_events(entity_id=task.id)
    assert len(events) >= 1
    snapshot = events[0].state_snapshot
    assert snapshot is not None
    assert "description" not in snapshot
    assert "result" not in snapshot
    assert set(snapshot.keys()) <= {"status", "assigned_to", "title"}


# ================================================================
# update_agent auto-generates snapshot event
# ================================================================


async def test_update_agent_auto_emits_event(db_repository: StorageRepository) -> None:
    """update_agent automatically creates an agent.updated event with snapshot."""
    team = await db_repository.create_team("t3", "coordinate")
    agent = await db_repository.create_agent(
        team_id=team.id,
        name="worker",
        role="dev",
        system_prompt="",
    )

    await db_repository.update_agent(agent.id, status="busy")

    events = await db_repository.list_events(entity_id=agent.id)
    assert len(events) >= 1
    evt = events[0]
    assert evt.entity_id == agent.id
    assert evt.entity_type == "agent"
    assert evt.state_snapshot is not None
    assert evt.state_snapshot["status"] == "busy"
    assert evt.state_snapshot["name"] == "worker"


async def test_update_agent_snapshot_excludes_system_prompt(db_repository: StorageRepository) -> None:
    """Agent snapshot only stores {status, name} — no system_prompt or config."""
    team = await db_repository.create_team("t4", "coordinate")
    agent = await db_repository.create_agent(
        team_id=team.id,
        name="prompter",
        role="dev",
        system_prompt="very long system prompt " * 200,
    )

    await db_repository.update_agent(agent.id, status="waiting")

    events = await db_repository.list_events(entity_id=agent.id)
    assert len(events) >= 1
    snapshot = events[0].state_snapshot
    assert snapshot is not None
    assert "system_prompt" not in snapshot
    assert "config" not in snapshot
    assert set(snapshot.keys()) <= {"status", "name"}


# ================================================================
# GET /api/events?entity_id= filtering
# ================================================================


async def test_list_events_filter_by_entity_id(db_repository: StorageRepository) -> None:
    """list_events with entity_id only returns matching events."""
    await db_repository.create_event("task.updated", "test", {}, entity_id="task-A", entity_type="task")
    await db_repository.create_event("task.updated", "test", {}, entity_id="task-B", entity_type="task")
    await db_repository.create_event("agent.updated", "test", {}, entity_id="task-A", entity_type="agent")

    results = await db_repository.list_events(entity_id="task-A")
    assert len(results) == 2
    assert all(e.entity_id == "task-A" for e in results)


async def test_list_events_entity_id_no_match(db_repository: StorageRepository) -> None:
    """list_events returns empty list when entity_id has no events."""
    await db_repository.create_event("task.updated", "test", {}, entity_id="task-X", entity_type="task")

    results = await db_repository.list_events(entity_id="nonexistent-id")
    assert results == []


async def test_list_events_without_entity_id_returns_all(db_repository: StorageRepository) -> None:
    """list_events without entity_id filter returns all events as before."""
    await db_repository.create_event("task.updated", "test", {}, entity_id="task-1", entity_type="task")
    await db_repository.create_event("agent.created", "test", {})

    results = await db_repository.list_events()
    assert len(results) == 2
