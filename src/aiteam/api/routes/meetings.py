"""AI Team OS — Meeting routes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api.deps import get_event_bus, get_memory_store, get_repository, get_scoped_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.exceptions import NotFoundError
from aiteam.api.schemas import (
    APIListResponse,
    APIResponse,
    MeetingConcludeBody,
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
    # Resolve team_id: accept both UUID and team name for backward compatibility
    resolved_team_id = team_id
    team = await repo.get_team(team_id)
    if team is None:
        team = await repo.get_team_by_name(team_id)
        if team is None:
            raise HTTPException(status_code=404, detail=f"团队 '{team_id}' 不存在")
        resolved_team_id = team.id

    try:
        meeting = await repo.create_meeting(
            team_id=resolved_team_id,
            topic=body.topic,
            participants=body.participants,
            meta_json=body.meta_json,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建会议失败: {exc}") from exc
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

    # Impersonation audit: flag when caller_agent_id is set and differs from agent_id
    msg_metadata: dict = {}
    caller = body.caller_agent_id.strip() if body.caller_agent_id else ""
    is_impersonation = bool(caller and caller != body.agent_id)
    if is_impersonation:
        msg_metadata = {
            "impersonation": True,
            "actual_author": caller,
        }
        await event_bus.emit(
            "meeting.impersonation",
            f"meeting:{meeting_id}",
            {
                "meeting_id": meeting_id,
                "claimed_agent_id": body.agent_id,
                "claimed_agent_name": body.agent_name,
                "actual_author": caller,
                "round_number": body.round_number,
            },
        )

    message = await repo.create_meeting_message(
        meeting_id=meeting_id,
        agent_id=body.agent_id,
        agent_name=body.agent_name,
        content=body.content,
        round_number=body.round_number,
        msg_metadata=msg_metadata,
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
            "impersonation": is_impersonation,
        },
    )
    resp_msg = "消息发送成功（已标记代打审计）" if is_impersonation else "消息发送成功"
    return APIResponse(data=message, message=resp_msg)


@router.put(
    "/api/meetings/{meeting_id}/conclude",
    response_model=APIResponse[Meeting],
)
async def conclude_meeting(
    meeting_id: str,
    body: MeetingConcludeBody = MeetingConcludeBody(),
    repo: StorageRepository = Depends(get_repository),
    event_bus: EventBus = Depends(get_event_bus),
    memory_store: MemoryStore = Depends(get_memory_store),
) -> APIResponse[Meeting]:
    """Conclude a meeting.

    validate_attendance=True (default): blocks conclude if expected participants haven't spoken.
    force=True: bypasses attendance check but records a warning event.
    """
    meeting = await repo.get_meeting(meeting_id)
    if meeting is None:
        msg = f"会议 '{meeting_id}' 不存在"
        raise NotFoundError(msg)

    # Attendance validation
    if body.validate_attendance:
        messages = await repo.list_meeting_messages(meeting_id)
        meta = getattr(meeting, "meta_json", None) or {}
        expected = meta.get("expected_participants", meeting.participants or [])
        spoken_ids = {m.agent_id for m in messages}
        spoken_names = {m.agent_name for m in messages}
        missing = [p for p in expected if p not in spoken_ids and p not in spoken_names]

        if missing and not body.force:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "参与者未全员发言，无法结束会议",
                    "missing": missing,
                    "spoken": list(spoken_names),
                    "hint": "设置 force=true 可强制结束，但会记录到事件日志",
                },
            )

        if missing and body.force:
            await event_bus.emit(
                "meeting.forced_conclude_with_missing",
                f"meeting:{meeting_id}",
                {
                    "meeting_id": meeting_id,
                    "missing_participants": missing,
                    "spoken_participants": list(spoken_names),
                    "topic": meeting.topic,
                },
            )

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
    all_messages = await repo.list_meeting_messages(meeting_id)
    if all_messages:
        conclusion_text = body.summary or all_messages[-1].content[:500]
        await memory_store.store(
            scope="team",
            scope_id=updated.team_id,
            content=f"[会议决策] {updated.topic}: {conclusion_text}",
            metadata={"meeting_id": meeting_id, "topic": updated.topic},
        )

    return APIResponse(data=updated, message="会议已结束，结论已保存到团队记忆")


async def attendance_check_logic(meeting_id: str, repo: StorageRepository) -> dict:
    """Core attendance check logic — extracted for testability."""
    meeting = await repo.get_meeting(meeting_id)
    if meeting is None:
        raise NotFoundError(f"会议 '{meeting_id}' 不存在")

    meta = meeting.meta_json or {}
    expected = meta.get("expected_participants", meeting.participants)
    messages = await repo.list_meeting_messages(meeting_id)
    current_round = max((m.round_number for m in messages), default=1)
    spoken = list({m.agent_name for m in messages if m.round_number == current_round})
    pending = [p for p in expected if p not in spoken]

    round_started_at = meta.get("round_started_at")
    timeout_seconds = 0
    if round_started_at:
        try:
            started = datetime.fromisoformat(round_started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            timeout_seconds = int((datetime.now(UTC) - started).total_seconds())
        except Exception:
            pass

    return {
        "success": True,
        "meeting_id": meeting_id,
        "round": current_round,
        "expected": expected,
        "spoken": spoken,
        "pending": pending,
        "timeout_in_seconds": timeout_seconds,
    }


@router.get(
    "/api/meetings/{meeting_id}/attendance",
)
async def meeting_attendance_check(
    meeting_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """Check which expected participants have spoken in the current round."""
    return await attendance_check_logic(meeting_id, repo)


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
