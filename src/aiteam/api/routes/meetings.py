"""AI Team OS — Meeting routes."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api.deps import get_event_bus, get_memory_store, get_repository, get_scoped_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.exceptions import NotFoundError
from aiteam.api.schemas import (
    APIListResponse,
    APIResponse,
    MeetingCreate,
    MeetingMessageCreate,
)
from aiteam.memory.store import MemoryStore
from aiteam.storage.repository import StorageRepository
from aiteam.types import Meeting, MeetingMessage, MeetingStatus

router = APIRouter(tags=["meetings"])


@router.post(
    "/api/teams/{team_id}/meetings",
    response_model=APIResponse[Meeting],
    status_code=201,
)
async def create_meeting(
    team_id: str,
    body: MeetingCreate,
    repo: StorageRepository = Depends(get_scoped_repository),
    event_bus: EventBus = Depends(get_event_bus),
) -> APIResponse[Meeting]:
    """Create a meeting."""
    meeting = await repo.create_meeting(
        team_id=team_id,
        topic=body.topic,
        participants=body.participants,
    )
    await event_bus.emit(
        "meeting.started",
        f"meeting:{meeting.id}",
        {
            "meeting_id": meeting.id,
            "team_id": team_id,
            "topic": body.topic,
            "participants": body.participants,
        },
    )
    guide = (
        f"会议创建成功。操作指引：\n"
        f"  发送消息: POST http://localhost:8000/api/meetings/{meeting.id}/messages\n"
        f"  读取消息: GET http://localhost:8000/api/meetings/{meeting.id}/messages\n"
        f"  结束会议: PUT http://localhost:8000/api/meetings/{meeting.id}/conclude\n"
        f"  讨论规则: R1各自观点 → R2+引用回应 → 最后汇总共识"
    )
    return APIResponse(data=meeting, message=guide)


@router.get(
    "/api/teams/{team_id}/meetings",
    response_model=APIListResponse[Meeting],
)
async def list_meetings(
    team_id: str,
    status: str | None = Query(None, description="按状态过滤: active / concluded"),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> APIListResponse[Meeting]:
    """List team meetings."""
    meeting_status = MeetingStatus(status) if status else None
    meetings = await repo.list_meetings(team_id, status=meeting_status)
    return APIListResponse(data=meetings, total=len(meetings))


@router.get(
    "/api/meetings/{meeting_id}",
    response_model=APIResponse[Meeting],
)
async def get_meeting(
    meeting_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Meeting]:
    """Get meeting details."""
    meeting = await repo.get_meeting(meeting_id)
    if meeting is None:
        msg = f"会议 '{meeting_id}' 不存在"
        raise NotFoundError(msg)
    return APIResponse(data=meeting)


@router.get(
    "/api/meetings/{meeting_id}/messages",
    response_model=APIListResponse[MeetingMessage],
)
async def list_meeting_messages(
    meeting_id: str,
    limit: int = Query(100, ge=1, le=500),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[MeetingMessage]:
    """Get meeting message list."""
    messages = await repo.list_meeting_messages(meeting_id, limit=limit)
    return APIListResponse(data=messages, total=len(messages))


@router.post(
    "/api/meetings/{meeting_id}/messages",
    response_model=APIResponse[MeetingMessage],
    status_code=201,
)
async def create_meeting_message(
    meeting_id: str,
    body: MeetingMessageCreate,
    repo: StorageRepository = Depends(get_repository),
    event_bus: EventBus = Depends(get_event_bus),
) -> APIResponse[MeetingMessage]:
    """Send a meeting message."""
    # Verify meeting exists
    meeting = await repo.get_meeting(meeting_id)
    if meeting is None:
        msg = f"会议 '{meeting_id}' 不存在"
        raise NotFoundError(msg)
    # A14: Concluded meetings cannot receive messages
    if meeting.status == MeetingStatus.CONCLUDED:
        raise HTTPException(400, "会议已结束，无法发送消息")
    # Auto-add speaker to participants list
    if body.agent_name not in (meeting.participants or []):
        updated_participants = list(meeting.participants or []) + [body.agent_name]
        await repo.update_meeting(meeting_id, participants=updated_participants)
    message = await repo.create_meeting_message(
        meeting_id=meeting_id,
        agent_id=body.agent_id,
        agent_name=body.agent_name,
        content=body.content,
        round_number=body.round_number,
    )
    await event_bus.emit(
        "meeting.message",
        f"meeting:{meeting_id}",
        {
            "meeting_id": meeting_id,
            "message_id": message.id,
            "agent_id": body.agent_id,
            "agent_name": body.agent_name,
            "content": body.content,
            "round_number": body.round_number,
        },
    )
    return APIResponse(data=message, message="消息发送成功")


@router.put(
    "/api/meetings/{meeting_id}/conclude",
    response_model=APIResponse[Meeting],
)
async def conclude_meeting(
    meeting_id: str,
    repo: StorageRepository = Depends(get_repository),
    event_bus: EventBus = Depends(get_event_bus),
    memory_store: MemoryStore = Depends(get_memory_store),
) -> APIResponse[Meeting]:
    """Conclude a meeting."""
    meeting = await repo.get_meeting(meeting_id)
    if meeting is None:
        msg = f"会议 '{meeting_id}' 不存在"
        raise NotFoundError(msg)
    updated = await repo.update_meeting(
        meeting_id,
        status=MeetingStatus.CONCLUDED,
        concluded_at=datetime.now(),
    )
    await event_bus.emit(
        "meeting.concluded",
        f"meeting:{meeting_id}",
        {
            "meeting_id": meeting_id,
            "team_id": updated.team_id,
            "topic": updated.topic,
        },
    )

    # Auto-save meeting conclusion to team memory
    messages = await repo.list_meeting_messages(meeting_id)
    if messages:
        last_msg = messages[-1]
        conclusion = last_msg.content[:500]
        await memory_store.store(
            scope="team",
            scope_id=updated.team_id,
            content=f"[会议决策] {updated.topic}: {conclusion}",
            metadata={"meeting_id": meeting_id, "topic": updated.topic},
        )

    return APIResponse(data=updated, message="会议已结束，结论已保存到团队记忆")


@router.put(
    "/api/meetings/{meeting_id}",
    response_model=APIResponse[Meeting],
)
async def update_meeting(
    meeting_id: str,
    body: dict,
    repo: StorageRepository = Depends(get_repository),
) -> APIResponse[Meeting]:
    """Update meeting fields (partial update — topic, participants, notes, etc.).

    Allows updating arbitrary meeting fields such as topic, participants, or notes.
    To conclude a meeting use the dedicated /conclude endpoint instead.

    Args:
        meeting_id: Meeting ID
        body: Fields to update (partial update)

    Returns:
        Updated meeting info
    """
    meeting = await repo.get_meeting(meeting_id)
    if meeting is None:
        from aiteam.api.exceptions import NotFoundError
        msg = f"会议 '{meeting_id}' 不存在"
        raise NotFoundError(msg)
    # Remove protected fields that should not be updated via this generic endpoint
    body.pop("id", None)
    body.pop("team_id", None)
    body.pop("status", None)
    body.pop("concluded_at", None)
    if not body:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="无更新字段")
    updated = await repo.update_meeting(meeting_id, **body)
    return APIResponse(data=updated, message="会议更新成功")
