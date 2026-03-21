"""AI Team OS — Decision event query routes (TOP2 cockpit Phase 1 & 2b)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.api.schemas import APIListResponse
from aiteam.storage.repository import StorageRepository
from aiteam.types import Event

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


@router.get("", response_model=APIListResponse[Event])
async def list_decisions(
    team_id: str | None = Query(None, description="Filter by source team_id (source prefix match)"),
    type: str | None = Query(
        None,
        description=(
            "Event type prefix or exact type, e.g. 'decision.' matches all decision events, "
            "'decision.task_assigned' exact matches task assignment decisions"
        ),
    ),
    limit: int = Query(50, ge=1, le=200, description="Return count limit"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Event]:
    """Query decision event list, returned in reverse chronological order.

    Supports filtering by event type prefix:
    - `decision.*` — all decision events (task assignment, plan selection, Agent selection)
    - `knowledge.*` — lessons learned events
    - `intent.*` — Agent intent events
    """
    # Check if prefix filter (contains wildcard * or ends with .)
    type_prefix: str | None = None
    exact_type: str | None = None

    if type is not None:
        if type.endswith("*") or type.endswith("."):
            # Prefix match: strip trailing * or keep . prefix
            type_prefix = type.rstrip("*")
        elif "." in type and not any(
            type == f"{ns}.{sub}"
            for ns in ("decision", "knowledge", "intent", "agent", "task", "meeting", "cc", "file", "system", "memory")
            for sub in type.split(".", 1)[1:]
            if sub
        ):
            # Namespace without sub-name (e.g. "decision"）treated as prefix
            type_prefix = type + "."
        else:
            # Exact type or prefix with sub-name
            if type.endswith("."):
                type_prefix = type
            else:
                # Attempt to determine: if type has no specific sub-name after dot, treat as prefix
                # Simple strategy: exact match first, let repository handle
                exact_type = type
    else:
        # Without type param, defaults to returning only decision.*/knowledge.*/intent.* events
        type_prefix = None  # No restriction, handled by namespace logic below

    # Build query
    if type is None:
        # Default: return all decision-related events (decision. + knowledge. + intent.)
        # Merge three query result sets
        decision_events = await repo.list_events(type_prefix="decision.", limit=limit)
        knowledge_events = await repo.list_events(type_prefix="knowledge.", limit=limit)
        intent_events = await repo.list_events(type_prefix="intent.", limit=limit)

        all_events = decision_events + knowledge_events + intent_events
        # Merge in reverse chronological order, take limit entries
        all_events.sort(key=lambda e: e.timestamp, reverse=True)
        events = all_events[:limit]

        # Filter by team_id (source format is "team:{team_id}"）
        if team_id:
            events = [
                e for e in events
                if e.source == f"team:{team_id}" or team_id in e.source
            ]
    else:
        events = await repo.list_events(
            event_type=exact_type,
            type_prefix=type_prefix,
            limit=limit,
        )
        if team_id:
            events = [
                e for e in events
                if e.source == f"team:{team_id}" or team_id in e.source
            ]

    return APIListResponse(data=events, total=len(events))
