"""AI Team OS — Channel messaging routes (v1.0 P1-6).

Provides cross-team channel communication with @mention semantics.
Channel formats: "team:<name>" / "project:<id>" / "global"
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api.deps import get_event_bus, get_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.schemas import (
    APIListResponse,
    APIResponse,
    ChannelCursorAdvance,
    ChannelMessageCreate,
)
from aiteam.storage.repository import StorageRepository
from aiteam.types import ChannelMessage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels", tags=["channels"])

# Valid channel formats: team:<name>, project:<id>, global
_CHANNEL_PATTERN = re.compile(r"^(team:[a-zA-Z0-9_\-]+|project:[a-zA-Z0-9_\-]+|global)$")
# 读者是角色标识，形如 leader-cc / leader-codex
_READER_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,100}$")


def _validate_channel(channel: str) -> None:
    """Validate channel name format."""
    if not _CHANNEL_PATTERN.match(channel):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid channel format '{channel}'. "
                "Expected: 'team:<name>', 'project:<id>', or 'global'"
            ),
        )


def _validate_reader(reader: str) -> None:
    """读者是角色标识（leader-cc / leader-codex），不是 session_id、不是自由文本。"""
    if not _READER_PATTERN.match(reader or ""):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid reader '{reader}'. 期望角色标识，如 'leader-cc'"
                "（字母/数字/连字符/下划线，1-100 字符）"
            ),
        )


def _infer_project_id(channel: str, explicit: str | None) -> str | None:
    """定出消息归属的项目：显式优先，其次从 "project:<id>" 频道名解析，都没有则 None。

    **刻意不在这里拒收**。曾经写成"定不出就 400"，理由是留空的消息在按项目的未读查询
    里谁都看不到；但那样会把这条约束加在整个 channel API 上，连从不使用未读功能的历史
    调用方一起打死（实测当场红 9 个既有用例）。

    折中：发送侧宽容、判定侧严格。project_id 为空的消息照发照存，只是不进任何项目的
    未读——它仍能被 channel_read 读到，只是不触发徽章。真正防"发了没人收得到"的闸放在
    MCP 工具层：那里按 cwd 自动填项目归属（与 task_memo / report 同一套归属模式），
    调用方不必手填也不会漏。响应里始终原样回报 project_id，为空是看得见的，不是静默。
    """
    if explicit:
        return explicit
    if channel.startswith("project:"):
        return channel.split(":", 1)[1]
    return None


# 未读查询放在最前注册：它是单段路径，与两段的 /{channel}/messages 不同形，但注册在
# 前可以从根上排除被 {channel} 通配吃掉的可能——一旦被吃掉，_validate_channel 会把它
# 判成非法频道返回 400，而 hook 侧对失败是静默降级的，最终表现为"徽章从来不亮"，
# 没有任何日志指向真正的原因。
@router.get("/unread", response_model=APIResponse[dict])
async def get_channel_unread(
    reader: str = Query(description="读者角色标识，如 leader-cc"),
    project_id: str = Query(description="归属项目 id；不支持不传查全部"),
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[dict]:
    """某读者在某项目下的逐频道未读计数。

    **纯读**：不写库、不 emit 事件。缺水位按 epoch 计（全部算未读）且不补写水位行——
    取摘要绝不等于已读，只有显式调 read-cursor 才推进。
    """
    _validate_reader(reader)
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id 必填，不支持不传查全部")

    channels, truncated = await repo.count_channel_unread(reader, project_id)
    return APIResponse(
        data={
            "reader": reader,
            "project_id": project_id,
            "total": sum(c["count"] for c in channels),
            "channels": channels,
            # 命中扫描上限时如实上报：少算的未读与"没有未读"在界面上分不开
            "truncated": truncated,
        }
    )


@router.post("/{channel}/read-cursor", response_model=APIResponse[dict])
async def advance_channel_cursor(
    channel: str,
    payload: ChannelCursorAdvance,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[dict]:
    """推进已读水位到调用方实际读到的那一条。

    幂等且单调：传入时间早于或等于现有水位时不动，返回 advanced=false。水位回退会让
    已读消息重新变未读，比不推进更糟。
    """
    _validate_channel(channel)
    _validate_reader(payload.reader)
    if not payload.project_id:
        raise HTTPException(status_code=400, detail="project_id 必填")

    cursor, advanced = await repo.set_channel_cursor(
        reader=payload.reader,
        channel=channel,
        project_id=payload.project_id,
        last_read_at=payload.last_read_at,
    )
    return APIResponse(
        data={
            "reader": cursor.reader,
            "channel": cursor.channel,
            "project_id": cursor.project_id,
            "last_read_at": cursor.last_read_at,
            "advanced": advanced,
        }
    )


@router.post("/{channel}/messages", response_model=APIResponse[ChannelMessage], status_code=201)
async def send_channel_message(
    channel: str,
    payload: ChannelMessageCreate,
    repo: StorageRepository = Depends(get_repository),
    event_bus: EventBus = Depends(get_event_bus),
) -> APIResponse[ChannelMessage]:
    """Send a message to a channel.

    Channel formats:
    - team:<name>: Send to a specific team channel
    - project:<id>: Send to a project-wide channel
    - global: Broadcast to all teams
    """
    _validate_channel(channel)
    project_id = _infer_project_id(channel, payload.project_id)
    msg = await repo.create_channel_message(
        channel=channel,
        sender=payload.sender,
        content=payload.content,
        mentions=payload.mentions,
        metadata=payload.metadata,
        project_id=project_id,
    )
    # Broadcast via EventBus so Dashboard receives it in real-time
    try:
        await event_bus.emit(
            event_type="channel.message",
            source=f"channel:{channel}",
            data={
                "id": msg.id,
                "channel": channel,
                "sender": payload.sender,
                "content": payload.content,
                "mentions": payload.mentions,
            },
        )
    except Exception:
        logger.warning("EventBus broadcast failed for channel message", exc_info=True)

    logger.info("Channel message sent to '%s' by '%s'", channel, payload.sender)
    return APIResponse(data=msg, message="Message sent")


@router.get("/{channel}/messages", response_model=APIListResponse[ChannelMessage])
async def read_channel_messages(
    channel: str,
    since: datetime | None = Query(default=None, description="Return messages after this timestamp (ISO 8601)"),
    limit: int = Query(default=50, ge=1, le=200),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[ChannelMessage]:
    """Read messages from a channel with optional incremental pull via 'since' parameter."""
    _validate_channel(channel)
    messages = await repo.list_channel_messages(channel=channel, since=since, limit=limit)
    return APIListResponse(data=messages, total=len(messages))


@router.get("/mentions/{agent_name}", response_model=APIListResponse[ChannelMessage])
async def get_mentions(
    agent_name: str,
    limit: int = Query(default=50, ge=1, le=200),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[ChannelMessage]:
    """Get channel messages that @mention a specific agent."""
    messages = await repo.list_channel_mentions(agent_name=agent_name, limit=limit)
    return APIListResponse(data=messages, total=len(messages))
