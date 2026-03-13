"""AI Team OS — 统一事件总线."""

from __future__ import annotations

import logging

from aiteam.api.ws.manager import ws_manager
from aiteam.api.ws.protocol import WSEvent
from aiteam.storage.repository import StorageRepository
from aiteam.types import Event

logger = logging.getLogger(__name__)


class EventBus:
    """统一事件发射器 — 同时持久化到DB和通过WS广播."""

    def __init__(self, repo: StorageRepository) -> None:
        self._repo = repo

    async def emit(self, event_type: str, source: str, data: dict) -> Event:
        """发射事件: 1) 写入数据库 2) WS广播.

        Args:
            event_type: 事件类型（如 "team.created"）。
            source: 事件来源（如 "team:<id>"）。
            data: 事件附带数据。

        Returns:
            持久化后的 Event 对象。
        """
        # 持久化
        event = await self._repo.create_event(event_type, source, data)

        # WS广播
        try:
            ws_event = WSEvent(
                channel=event_type,
                event_type=event_type,
                data=data,
                timestamp=event.timestamp,
            )
            await ws_manager.broadcast_event(ws_event)
        except Exception:
            logger.warning("WS broadcast failed for %s", event_type)

        return event
