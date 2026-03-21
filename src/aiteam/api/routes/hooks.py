"""AI Team OS — Hooks bridge API routes.

Receives Claude Code Hook events and translates them into OS system operations via HookTranslator.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from aiteam.api.deps import get_hook_translator
from aiteam.api.hook_translator import HookTranslator

router = APIRouter(prefix="/api/hooks", tags=["hooks"])


class HookEventPayload(BaseModel):
    """Claude Code Hook event payload schema."""

    hook_event_name: str = Field(default="", max_length=50)
    session_id: str = Field(default="", max_length=200)
    agent_id: str = Field(default="", max_length=200)
    agent_type: str = Field(default="", max_length=200)
    tool_name: str = Field(default="", max_length=100)
    tool_input: dict = Field(default_factory=dict)
    tool_output: dict = Field(default_factory=dict)
    cwd: str = Field(default="", max_length=500)
    cc_team_name: str = Field(default="", max_length=200)

    model_config = ConfigDict(extra="allow")


@router.post("/event")
async def receive_hook_event(
    payload: HookEventPayload,
    translator: HookTranslator = Depends(get_hook_translator),
) -> dict:
    """Unified receiver for Claude Code hook events.

    Receives various CC hook event payloads and auto-syncs to OS system:
    - SubagentStart/Stop: Agent status sync
    - PreToolUse/PostToolUse: Tool usage tracking
    - SessionStart/End: Session lifecycle management and reconciliation
    """
    return await translator.handle_event(payload.model_dump())
