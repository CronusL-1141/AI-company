"""Confirm from a Claude Code transcript whether a hook line was really displayed.

Claude Code drops SessionStart output on some resumes and on ``/clear``
(docs/user-notice-design.md §3). When a line was displayed, the transcript
holds an attachment record::

    {"type": "attachment", "timestamp": "...Z",
     "attachment": {"type": "hook_system_message", "hookEvent": "SessionStart",
                    "hookName": "SessionStart:resume", "content": "<systemMessage>"}}

On resume the record and the screen go together. After ``/clear`` the record
is always written but only the fullscreen renderer paints the line (CC 2.1.281,
0 of 17 on the default renderer), so a ``/clear`` record counts as shown only
when that renderer is on (``clear_lines_visible``).

This module reads only the tail of the file, only inside the Claude Code
projects folder, and always in a worker thread.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from aiteam.clock import parse_utc
from aiteam.services.notices.render import strip_ansi

TAIL_BYTES = 512 * 1024
# Records a little older than the claim still count (hook and CC clocks are the
# same machine, but the hook writes before CC stamps the record).
CLOCK_SLACK = timedelta(seconds=120)


def projects_root() -> Path:
    """``$CLAUDE_CONFIG_DIR/projects``, else ``~/.claude/projects``."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    return base / "projects"


def _allowed(path: Path) -> Path | None:
    try:
        resolved = path.expanduser().resolve(strict=True)
        root = projects_root().resolve()
    except (OSError, RuntimeError):
        return None
    if root not in resolved.parents or not resolved.is_file():
        return None
    return resolved


def _read_displayed(path_text: str, since: datetime, event: str) -> list[str] | None:
    path = _allowed(Path(path_text)) if path_text else None
    if path is None:
        return None
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - TAIL_BYTES)
            handle.seek(start)
            data = handle.read()
    except OSError:
        return None
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line is cut in the middle
    shown: list[str] = []
    for raw in lines:
        if b"hook_system_message" not in raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        attachment = record.get("attachment") if isinstance(record, dict) else None
        if not isinstance(attachment, dict) or attachment.get("type") != "hook_system_message":
            continue
        if event and attachment.get("hookEvent") != event:
            continue
        try:
            stamp = parse_utc(str(record.get("timestamp") or ""))
        except (TypeError, ValueError):
            continue
        if stamp is None or stamp < since - CLOCK_SLACK:
            continue
        content = attachment.get("content")
        if isinstance(content, str):
            shown.append(strip_ansi(content))
    return shown


async def displayed_messages(
    transcript_path: str, *, since: datetime, event: str = "SessionStart",
) -> list[str] | None:
    """Plain texts of hook messages displayed for ``event`` since ``since``.

    ``None`` means the transcript could not be read (missing path, outside the
    projects folder, unreadable); callers must treat that as "unknown", not as
    "not displayed".
    """
    return await asyncio.to_thread(_read_displayed, transcript_path, since, event)


def was_displayed(plain_line: str, messages: list[str]) -> bool:
    """True when a displayed hook message contains the plain notice line."""
    return any(plain_line in message.splitlines() or plain_line in message for message in messages)


def _fullscreen(cwd: str, tui_env: str) -> bool:
    from aiteam.api.language import cc_setting

    if cc_setting(cwd, "axScreenReader") is True:
        return False  # screen-reader mode keeps the classic renderer
    if tui_env:
        return tui_env == "fullscreen"
    return cc_setting(cwd, "tui", lambda value: isinstance(value, str) and bool(value)) == "fullscreen"


async def clear_lines_visible(cwd: str, tui_env: str = "") -> bool:
    """Does this session paint the lines a ``/clear`` start writes?

    Only the fullscreen renderer does: the ``tui`` setting merged over the local,
    project and user settings, unless screen-reader mode is on or the hook
    reports a renderer the session side forces (``tui_env``: the environment
    overrides, or Claude Code's fullscreen crash latch in its global config,
    which the API cannot read). Anything else, an unset ``tui`` included,
    counts as the default renderer.
    """
    return await asyncio.to_thread(_fullscreen, cwd, tui_env)
