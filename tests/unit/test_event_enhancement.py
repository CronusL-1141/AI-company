"""Unit tests for Event Log Enhancement (v0.9).

Covers:
- EventModel: entity_id, entity_type, state_snapshot nullable columns
- Event Pydantic model: new optional fields
- repository.create_event: new parameters stored correctly
- repository.list_events: entity_id filter
- EventBus.emit: new parameters forwarded to repository
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aiteam.storage.repository import StorageRepository
from aiteam.types import Event, EventType

# ============================================================
# Event Pydantic model
# ============================================================


class TestEventModel:
    """Test Event Pydantic model new fields."""

    def test_event_new_fields_default_none(self) -> None:
        """New fields default to None for backward compatibility."""
        event = Event(type=EventType.TASK_CREATED, source="test")
        assert event.entity_id is None
        assert event.entity_type is None
        assert event.state_snapshot is None

    def test_event_new_fields_set(self) -> None:
        """New fields can be set explicitly."""
        snapshot = {"id": "t1", "status": "pending", "title": "Do something"}
        event = Event(
            type=EventType.TASK_CREATED,
            source="task:t1",
            entity_id="t1",
            entity_type="task",
            state_snapshot=snapshot,
        )
        assert event.entity_id == "t1"
        assert event.entity_type == "task"
        assert event.state_snapshot == snapshot

    def test_event_snapshot_stores_dict(self) -> None:
        """state_snapshot accepts arbitrary dict."""
        event = Event(
            type=EventType.AGENT_STATUS_CHANGED,
            source="agent:a1",
            entity_id="a1",
            entity_type="agent",
            state_snapshot={"id": "a1", "status": "busy", "role": "developer"},
        )
        assert event.state_snapshot["status"] == "busy"


# ============================================================
# Repository: create_event with new fields
# ============================================================


class TestCreateEventEnhanced:
    """Test create_event stores new fields."""

    @pytest.mark.asyncio
    async def test_create_event_without_new_fields(
        self, db_repository: StorageRepository
    ) -> None:
        """create_event still works without new fields (backward compat)."""
        event = await db_repository.create_event(
            "task.created", "task:t1", {"task_id": "t1"}
        )
        assert event.entity_id is None
        assert event.entity_type is None
        assert event.state_snapshot is None

    @pytest.mark.asyncio
    async def test_create_event_with_entity_id(
        self, db_repository: StorageRepository
    ) -> None:
        """create_event stores entity_id and entity_type."""
        event = await db_repository.create_event(
            "task.created",
            "task:t1",
            {"task_id": "t1"},
            entity_id="t1",
            entity_type="task",
        )
        assert event.entity_id == "t1"
        assert event.entity_type == "task"

    @pytest.mark.asyncio
    async def test_create_event_with_state_snapshot(
        self, db_repository: StorageRepository
    ) -> None:
        """create_event stores state_snapshot."""
        snapshot = {"id": "t1", "status": "completed", "title": "Write tests"}
        event = await db_repository.create_event(
            "task.completed",
            "task:t1",
            {},
            entity_id="t1",
            entity_type="task",
            state_snapshot=snapshot,
        )
        assert event.state_snapshot == snapshot

    @pytest.mark.asyncio
    async def test_create_event_persisted_and_retrievable(
        self, db_repository: StorageRepository
    ) -> None:
        """Event with new fields is persisted and retrievable via list_events."""
        await db_repository.create_event(
            "agent.status_changed",
            "agent:a1",
            {"status": "busy"},
            entity_id="a1",
            entity_type="agent",
            state_snapshot={"id": "a1", "status": "busy"},
        )

        events = await db_repository.list_events(event_type="agent.status_changed")
        assert len(events) == 1
        e = events[0]
        assert e.entity_id == "a1"
        assert e.entity_type == "agent"
        assert e.state_snapshot == {"id": "a1", "status": "busy"}


# ============================================================
# Repository: list_events entity_id filter
# ============================================================


class TestListEventsEntityFilter:
    """Test list_events entity_id filter."""

    @pytest.mark.asyncio
    async def test_filter_by_entity_id(self, db_repository: StorageRepository) -> None:
        """entity_id filter returns only events for that entity."""
        await db_repository.create_event(
            "task.created", "task:t1", {}, entity_id="t1", entity_type="task"
        )
        await db_repository.create_event(
            "task.started", "task:t1", {}, entity_id="t1", entity_type="task"
        )
        await db_repository.create_event(
            "task.created", "task:t2", {}, entity_id="t2", entity_type="task"
        )

        events = await db_repository.list_events(entity_id="t1")
        assert len(events) == 2
        assert all(e.entity_id == "t1" for e in events)

    @pytest.mark.asyncio
    async def test_entity_id_filter_no_match(
        self, db_repository: StorageRepository
    ) -> None:
        """entity_id filter returns empty list when no match."""
        await db_repository.create_event("task.created", "task:t1", {}, entity_id="t1")
        events = await db_repository.list_events(entity_id="nonexistent-id")
        assert events == []

    @pytest.mark.asyncio
    async def test_entity_id_filter_combined_with_type(
        self, db_repository: StorageRepository
    ) -> None:
        """entity_id and event_type filters can be combined."""
        await db_repository.create_event(
            "task.created", "task:t1", {}, entity_id="t1", entity_type="task"
        )
        await db_repository.create_event(
            "task.completed", "task:t1", {}, entity_id="t1", entity_type="task"
        )

        events = await db_repository.list_events(
            event_type="task.completed", entity_id="t1"
        )
        assert len(events) == 1
        assert events[0].type == EventType.TASK_COMPLETED

    @pytest.mark.asyncio
    async def test_list_events_without_entity_id_returns_all(
        self, db_repository: StorageRepository
    ) -> None:
        """list_events without entity_id returns all events (backward compat)."""
        await db_repository.create_event("task.created", "task:t1", {}, entity_id="t1")
        await db_repository.create_event("task.created", "task:t2", {})  # no entity_id

        events = await db_repository.list_events(event_type="task.created")
        assert len(events) == 2


# ============================================================
# EventBus.emit new parameters
# ============================================================


class TestEventBusEmitEnhanced:
    """Test EventBus.emit forwards new parameters to repository."""

    @pytest.mark.asyncio
    async def test_emit_passes_entity_id_to_repo(self) -> None:
        """EventBus.emit forwards entity_id, entity_type, state_snapshot to create_event."""
        from aiteam.api.event_bus import EventBus

        mock_repo = MagicMock()
        mock_event = Event(
            type=EventType.TASK_CREATED,
            source="task:t1",
            entity_id="t1",
            entity_type="task",
            state_snapshot={"id": "t1", "status": "pending"},
        )
        mock_repo.create_event = AsyncMock(return_value=mock_event)

        bus = EventBus(repo=mock_repo)
        result = await bus.emit(
            "task.created",
            "task:t1",
            {"title": "Test task"},
            entity_id="t1",
            entity_type="task",
            state_snapshot={"id": "t1", "status": "pending"},
        )

        mock_repo.create_event.assert_called_once_with(
            "task.created",
            "task:t1",
            {"title": "Test task"},
            entity_id="t1",
            entity_type="task",
            state_snapshot={"id": "t1", "status": "pending"},
        )
        assert result.entity_id == "t1"

    @pytest.mark.asyncio
    async def test_emit_backward_compat_no_new_params(self) -> None:
        """EventBus.emit works without new params (backward compat)."""
        from aiteam.api.event_bus import EventBus

        mock_repo = MagicMock()
        mock_event = Event(type=EventType.TEAM_CREATED, source="team:t1")
        mock_repo.create_event = AsyncMock(return_value=mock_event)

        bus = EventBus(repo=mock_repo)
        await bus.emit("team.created", "team:t1", {"name": "my-team"})

        mock_repo.create_event.assert_called_once_with(
            "team.created",
            "team:t1",
            {"name": "my-team"},
            entity_id=None,
            entity_type=None,
            state_snapshot=None,
        )


# ============================================================
# Snapshot trimming pattern (utility function)
# ============================================================


class TestSnapshotTrimming:
    """Verify snapshot only stores key fields (pattern test)."""

    def test_snapshot_should_be_small(self) -> None:
        """Snapshot should contain only key fields, not full entity."""
        # Simulate what callers should do when building a snapshot
        full_task_data = {
            "id": "t1",
            "title": "Write tests",
            "description": "Very long description..." * 100,
            "status": "completed",
            "assigned_to": "agent-1",
            "result": "Done" * 500,
            "tags": ["test", "unit"],
            "config": {"memo": [{"content": "x" * 1000}]},
        }
        # Key fields only
        snapshot = {
            k: full_task_data[k]
            for k in ("id", "title", "status", "assigned_to")
            if k in full_task_data
        }
        # Snapshot is much smaller
        import json

        assert len(json.dumps(snapshot)) < len(json.dumps(full_task_data))
        assert "description" not in snapshot
        assert "result" not in snapshot
        assert snapshot["status"] == "completed"
