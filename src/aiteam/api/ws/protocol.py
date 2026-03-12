"""AI Team OS — WebSocket消息协议.

定义服务端与客户端之间的消息格式。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ============================================================
# 服务端 → 客户端
# ============================================================


class WSEvent(BaseModel):
    """服务端推送的事件消息."""

    type: str = "event"
    channel: str
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)


class WSPong(BaseModel):
    """心跳响应."""

    type: str = "pong"


class WSAck(BaseModel):
    """操作确认."""

    type: str = "ack"
    action: str
    detail: str = ""


class WSError(BaseModel):
    """错误消息."""

    type: str = "error"
    message: str


# ============================================================
# 客户端 → 服务端
# ============================================================


class WSSubscribe(BaseModel):
    """订阅频道."""

    type: str = "subscribe"
    channel: str


class WSUnsubscribe(BaseModel):
    """取消订阅频道."""

    type: str = "unsubscribe"
    channel: str


class WSPing(BaseModel):
    """心跳请求."""

    type: str = "ping"
