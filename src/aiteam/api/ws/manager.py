"""AI Team OS — WebSocket连接管理器.

管理WebSocket连接的生命周期、频道订阅和事件广播。
"""

from __future__ import annotations

from fnmatch import fnmatch

from fastapi import WebSocket

from aiteam.api.ws.protocol import WSEvent


class ConnectionManager:
    """WebSocket连接管理器."""

    def __init__(self) -> None:
        # 连接ID → WebSocket实例
        self._connections: dict[str, WebSocket] = {}
        # 连接ID → 订阅的频道集合
        self._subscriptions: dict[str, set[str]] = {}
        # 频道 → 订阅该频道的连接ID集合（加速查找）
        self._channel_index: dict[str, set[str]] = {}

    @property
    def active_count(self) -> int:
        """当前活跃连接数."""
        return len(self._connections)

    async def connect(self, conn_id: str, websocket: WebSocket) -> None:
        """注册新的WebSocket连接."""
        await websocket.accept()
        self._connections[conn_id] = websocket
        self._subscriptions[conn_id] = set()

    def disconnect(self, conn_id: str) -> None:
        """注销WebSocket连接."""
        # 清理频道索引
        channels = self._subscriptions.pop(conn_id, set())
        for channel in channels:
            if channel in self._channel_index:
                self._channel_index[channel].discard(conn_id)
                if not self._channel_index[channel]:
                    del self._channel_index[channel]
        # 移除连接
        self._connections.pop(conn_id, None)

    def subscribe(self, conn_id: str, channel: str) -> None:
        """订阅频道."""
        if conn_id not in self._subscriptions:
            return
        self._subscriptions[conn_id].add(channel)
        if channel not in self._channel_index:
            self._channel_index[channel] = set()
        self._channel_index[channel].add(conn_id)

    def unsubscribe(self, conn_id: str, channel: str) -> None:
        """取消订阅频道."""
        if conn_id in self._subscriptions:
            self._subscriptions[conn_id].discard(channel)
        if channel in self._channel_index:
            self._channel_index[channel].discard(conn_id)
            if not self._channel_index[channel]:
                del self._channel_index[channel]

    async def broadcast_event(self, event: WSEvent) -> None:
        """根据频道模式匹配广播事件.

        使用fnmatch通配符匹配，例如 "team.*" 会匹配 "team.created" 频道。
        """
        target_conn_ids: set[str] = set()

        for channel, conn_ids in self._channel_index.items():
            # 支持通配符匹配：订阅的频道模式匹配事件频道
            if fnmatch(event.channel, channel):
                target_conn_ids.update(conn_ids)

        if not target_conn_ids:
            return

        message = event.model_dump_json()
        disconnected: list[str] = []

        for conn_id in target_conn_ids:
            ws = self._connections.get(conn_id)
            if ws is None:
                continue
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(conn_id)

        # 清理断开的连接
        for conn_id in disconnected:
            self.disconnect(conn_id)


# 全局单例
ws_manager = ConnectionManager()
