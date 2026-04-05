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
from aiteam.api.schemas import APIListResponse, APIResponse, ChannelMessageCreate
from aiteam.storage.repository import StorageRepository
from aiteam.types import ChannelMessage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/channels", tags=["channels"])

# Valid channel formats: team:<name>, project:<id>, global
_CHANNEL_PATTERN = re.compile(r"^(team:[a-zA-Z0-9_\-]+|project:[a-zA-Z0-9_\-]+|global)$")


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
    msg = await repo.create_channel_message(
        channel=channel,
        sender=payload.sender,
        content=payload.content,
        mentions=payload.mentions,
        metadata=payload.metadata,
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
