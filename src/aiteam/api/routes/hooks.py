"""AI Team OS — Hooks bridge API routes.

Receives Claude Code Hook events and translates them into OS system operations via HookTranslator.
"""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from aiteam.api import compact_checkpoint
from aiteam.api.deps import get_event_bus, get_hook_translator, get_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/hooks", tags=["hooks"])

# ---------------------------------------------------------------------------
# Denial classification types
# ---------------------------------------------------------------------------

DenialCategory = Literal[
    "recoverable_with_retry",
    "recoverable_with_workaround",
    "needs_user_approval",
    "permanent_denial",
]

# Dangerous Bash patterns that need user approval
_DANGEROUS_BASH_PATTERNS = [
    r"rm\s+-[a-z]*r[a-z]*\s+[/~]",  # rm -rf / or rm -rf ~
    r"git\s+push\s+.*--force",        # git push --force
    r"git\s+push\s+.*-f\b",           # git push -f
    r"git\s+reset\s+--hard",          # git reset --hard
    r"git\s+checkout\s+\.",           # git checkout .
    r"rm\s+-rf\s+\.",                 # rm -rf .
    r":\s*>\s*/dev/",                 # redirect to /dev/ devices
    r"dd\s+if=",                      # dd block writes
    r"mkfs\.",                        # filesystem formatting
]

_TRANSIENT_PATTERNS = ["temporary", "rate limit", "transient", "try again"]
_PATH_OUTSIDE_PATTERNS = [
    "outside the project",
    "outside project",
    "not in allowed",
    "path not allowed",
    "additionaldirectories",
]
_REPORT_WRITE_PATTERNS = [
    r"\.claude[/\\]data[/\\]",
    r"reports?[/\\]\d",
]


def _matches_any_str(text: str, patterns: list[str]) -> bool:
    text_lower = text.lower()
    return any(p in text_lower for p in patterns)


def _matches_any_re(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _classify_denial(tool_name: str, tool_input: dict, reason: str) -> tuple[DenialCategory, str, str]:
    """Classify a permission denial into one of 4 action categories.

    Returns (category, hint, additional_context).
    """
    # Transient / rate-limit → retry once
    if _matches_any_str(reason, _TRANSIENT_PATTERNS):
        return (
            "recoverable_with_retry",
            "This looks like a transient denial — retrying automatically.",
            "",
        )

    # Write to .claude/data/... or report paths → use report_save instead
    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", tool_input.get("path", ""))
        if _matches_any_re(file_path, _REPORT_WRITE_PATTERNS):
            return (
                "recoverable_with_workaround",
                "Use the report_save MCP tool instead of writing directly to .claude/data/ or report paths.",
                (
                    "Direct writes to OS data/report directories are blocked. "
                    "Call report_save(author=..., topic=..., content=..., report_type=...) to save reports."
                ),
            )

    # Bash dangerous commands → needs user approval
    if tool_name == "Bash":
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if _matches_any_re(command, _DANGEROUS_BASH_PATTERNS):
            return (
                "needs_user_approval",
                "This command requires explicit user approval. Create a briefing to request it.",
                (
                    f"Dangerous Bash command blocked: {reason}. "
                    "Do NOT retry. If this action is necessary, create a briefing for the user."
                ),
            )
        # Non-dangerous Bash denial
        return (
            "needs_user_approval",
            "Bash command was denied. Consider a safer alternative or request user approval.",
            f"Bash denied: {reason}. Use a dedicated tool (Read/Write/Edit/Glob/Grep) if possible.",
        )

    # Path outside project → needs user approval
    if _matches_any_str(reason, _PATH_OUTSIDE_PATTERNS):
        return (
            "needs_user_approval",
            "Path is outside allowed project directories. Request user to add it to additionalDirectories.",
            (
                "Access denied: path is outside the project root. "
                "Leader has been notified. Ask the user to extend additionalDirectories if access is needed."
            ),
        )

    # Unknown / unclassified
    return (
        "permanent_denial",
        "",
        f"Permission denied with no known recovery path: {reason}",
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


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


class DiagnoseDenialRequest(BaseModel):
    """Payload for classifying a PermissionDenied event."""

    tool_name: str = Field(default="", max_length=100)
    tool_input: dict = Field(default_factory=dict)
    reason: str = Field(default="", max_length=2000)


class DiagnoseDenialResponse(BaseModel):
    """Classification result for a PermissionDenied event."""

    category: DenialCategory
    hint: str = ""
    additional_context: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


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


@router.post("/diagnose_denial", response_model=DiagnoseDenialResponse)
async def diagnose_denial(payload: DiagnoseDenialRequest) -> DiagnoseDenialResponse:
    """Classify a permission denial into one of 4 action categories.

    Categories:
    - recoverable_with_retry: transient issue, safe to retry once
    - recoverable_with_workaround: use an alternative tool/approach
    - needs_user_approval: requires explicit user decision
    - permanent_denial: no known recovery path, log and move on
    """
    category, hint, additional_context = _classify_denial(
        tool_name=payload.tool_name,
        tool_input=payload.tool_input,
        reason=payload.reason,
    )
    return DiagnoseDenialResponse(
        category=category,
        hint=hint,
        additional_context=additional_context,
    )


class CompactCheckpointRequest(BaseModel):
    """PreCompact 检查点写入请求。"""

    session_id: str
    trigger: str = ""
    cwd: str = ""


@router.post("/compact-checkpoint")
async def save_compact_checkpoint(
    payload: CompactCheckpointRequest,
    repo: StorageRepository = Depends(get_repository),
    bus: EventBus = Depends(get_event_bus),
) -> dict:
    """在上下文被压缩前，把这个会话的 OS 侧作战态定格成一条检查点事件。

    快照由服务端直接从库里取——hook 只报"我要压缩了"，不负责搜集内容。这样
    hook 保持纯 stdlib、单次 HTTP，也不会因为 hook 侧少查一样东西就把检查点
    做残。
    """
    snapshot = await compact_checkpoint.build_snapshot(repo, payload.session_id, payload.cwd)
    snapshot["trigger"] = payload.trigger
    await bus.emit(
        compact_checkpoint.CHECKPOINT_EVENT,
        f"session:{payload.session_id}",
        snapshot,
    )
    return {"success": True, "data": snapshot}


@router.get("/compact-checkpoint")
async def read_compact_checkpoint(
    session_id: str,
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """取该会话最近一条检查点，附一段可直接注入的渲染文本。

    没有检查点时 found=False、text 为空串——调用方（session_bootstrap）据此
    什么都不注入，绝不给用户的上下文塞占位噪声。
    """
    events = await repo.list_events(
        event_type=compact_checkpoint.CHECKPOINT_EVENT,
        source=f"session:{session_id}",
        limit=1,
    )
    if not events:
        return {"success": True, "found": False, "text": "", "data": None}
    snapshot = events[0].data or {}
    return {
        "success": True,
        "found": True,
        "text": compact_checkpoint.render(snapshot),
        "saved_at": events[0].timestamp.isoformat() if events[0].timestamp else None,
        "data": snapshot,
    }
