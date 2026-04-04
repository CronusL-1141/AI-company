"""AI Team OS — Unified event bus."""

from __future__ import annotations

import logging

from aiteam.api.ws.manager import ws_manager
from aiteam.api.ws.protocol import WSEvent
from aiteam.config import settings as cfg
from aiteam.integrations.notifier import send_webhook
from aiteam.storage.repository import StorageRepository
from aiteam.types import Event

logger = logging.getLogger(__name__)


class EventBus:
    """Unified event emitter — persists to DB and broadcasts via WS simultaneously."""

    def __init__(self, repo: StorageRepository) -> None:
        self._repo = repo

    async def emit(
        self,
        event_type: str,
        source: str,
        data: dict,
        entity_id: str | None = None,
        entity_type: str | None = None,
        state_snapshot: dict | None = None,
    ) -> Event:
        """Emit an event: 1) write to database 2) broadcast via WS.

        Args:
            event_type: Event type (e.g. "team.created").
            source: Event source (e.g. "team:<id>").
            data: Event payload data.
            entity_id: ID of the primary entity involved (task/agent/team/meeting).
            entity_type: Entity type label: "task" / "agent" / "team" / "meeting".
            state_snapshot: Trimmed key fields of entity state at event time.
                            Keep small — include only id, status, title, assigned_to etc.

        Returns:
            The persisted Event object.
        """
        # Persist
        event = await self._repo.create_event(
            event_type, source, data,
            entity_id=entity_id,
            entity_type=entity_type,
            state_snapshot=state_snapshot,
        )

        # WS broadcast
        try:
            ws_event = WSEvent(
                channel=event_type,
                event_type=event_type,
                data=data,
                timestamp=event.timestamp,
            )
            await ws_manager.broadcast_event(ws_event)
        except Exception:
            logger.warning("WS broadcast failed for %s", event_type, exc_info=True)

        # Slack/webhook notification
        webhook_url = cfg.SLACK_WEBHOOK_URL
        if webhook_url and event_type in cfg.NOTIFICATION_EVENTS:
            try:
                message = f"[{event_type}] {source}: {data}"
                await send_webhook(
                    webhook_url,
                    message,
                    metadata={"event_type": event_type, "source": source},
                )
            except Exception:
                logger.warning("Notification dispatch failed for %s", event_type, exc_info=True)

        return event
