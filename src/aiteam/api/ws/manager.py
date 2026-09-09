"""AI Team OS — WebSocket connection manager.

Manages WebSocket connection lifecycle, channel subscriptions, and event broadcasting.
"""

from __future__ import annotations

import asyncio
import logging
from fnmatch import fnmatch
from math import isfinite

from fastapi import WebSocket

from aiteam.api.ws.protocol import WSEvent

logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket connection manager."""

    def __init__(self, *, send_timeout: float = 0.25) -> None:
        # Server-side push budget, not a measured network latency threshold.
        if not isfinite(send_timeout) or send_timeout <= 0:
            raise ValueError("send_timeout must be finite and positive")
        self._send_timeout = send_timeout
        # Connection ID -> WebSocket instance
        self._connections: dict[str, WebSocket] = {}
        # Connection ID -> subscribed channel set
        self._subscriptions: dict[str, set[str]] = {}
        # Channel -> set of connection IDs subscribed to it (accelerated lookup)
        self._channel_index: dict[str, set[str]] = {}
        # Some transports finish their closing handshake after cancellation.
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    @property
    def active_count(self) -> int:
        """Current active connection count."""
        return len(self._connections)

    async def connect(self, conn_id: str, websocket: WebSocket) -> None:
        """Register a new WebSocket connection."""
        await websocket.accept()
        self.disconnect(conn_id)
        self._connections[conn_id] = websocket
        self._subscriptions[conn_id] = set()

    def disconnect(self, conn_id: str) -> None:
        """Unregister a WebSocket connection."""
        # Clean up channel index
        channels = self._subscriptions.pop(conn_id, set())
        for channel in channels:
            if channel in self._channel_index:
                self._channel_index[channel].discard(conn_id)
                if not self._channel_index[channel]:
                    del self._channel_index[channel]
        # Remove connection
        self._connections.pop(conn_id, None)

    def subscribe(self, conn_id: str, channel: str) -> None:
        """Subscribe to a channel."""
        if conn_id not in self._subscriptions:
            return
        self._subscriptions[conn_id].add(channel)
        if channel not in self._channel_index:
            self._channel_index[channel] = set()
        self._channel_index[channel].add(conn_id)

    def unsubscribe(self, conn_id: str, channel: str) -> None:
        """Unsubscribe from a channel."""
        if conn_id in self._subscriptions:
            self._subscriptions[conn_id].discard(channel)
        if channel in self._channel_index:
            self._channel_index[channel].discard(conn_id)
            if not self._channel_index[channel]:
                del self._channel_index[channel]

    async def broadcast_event(self, event: WSEvent) -> None:
        """Broadcast events by channel pattern matching.

        Uses fnmatch wildcard matching, e.g. "team.*" matches "team.created" channel.
        """
        target_conn_ids: set[str] = set()

        for channel, conn_ids in self._channel_index.items():
            # Wildcard matching: subscription channel pattern matches event channel
            if fnmatch(event.channel, channel):
                target_conn_ids.update(conn_ids)

        if not target_conn_ids:
            return

        message = event.model_dump_json()
        # Snapshot identities before yielding; a reconnect may reuse a connection ID.
        targets = [
            (conn_id, self._connections[conn_id])
            for conn_id in target_conn_ids
            if conn_id in self._connections
        ]
        await asyncio.gather(*(
            self._send_event(conn_id, ws, message) for conn_id, ws in targets
        ))

    async def _send_event(self, conn_id: str, ws: WebSocket, message: str) -> None:
        """Bound sends and cleanup independently, at most two push budgets per peer."""
        try:
            await asyncio.wait_for(ws.send_text(message), timeout=self._send_timeout)
        except Exception as exc:
            logger.warning("WS send failed for %s: %s", conn_id, type(exc).__name__)
            if self._connections.get(conn_id) is ws:
                self.disconnect(conn_id)
            # Eviction must also close the old transport, so clients can reconnect.
            # Bound our wait, not cancellation acknowledgement: legacy backends
            # can swallow cancellation while finishing the closing handshake.
            cleanup = asyncio.create_task(ws.close(code=1013))
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_finished)
            try:
                await asyncio.wait({cleanup}, timeout=self._send_timeout)
            finally:
                if not cleanup.done():
                    cleanup.cancel()

    def _cleanup_finished(self, task: asyncio.Task[None]) -> None:
        self._cleanup_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("WS close failed", exc_info=True)


# Global singleton
ws_manager = ConnectionManager()
