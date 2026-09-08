#!/usr/bin/env python3
"""Observe Codex events without changing the host's tool outcome.

Counters contain only state names and integers; invocation rows add sanitized
identifiers, timing and reason codes. Each invocation is committed before
reading stdin, so a killed sender remains visible in the denominator.
Set AITEAM_CODEX_STATE_DIR to isolate diagnostics from user-level counters.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

_STATES = ("invoked", "inert_dropped", "posted", "post_unreachable", "error")
_TOOL_EVENTS = ("PreToolUse", "PostToolUse")
_OS_PREFIXES = ("mcp__ai_team_os__", "mcp__ai-team-os__")


def _counter_path() -> Path:
    override = os.environ.get("AITEAM_CODEX_STATE_DIR")
    if override:
        return Path(override) / "hook-counts.sqlite3"
    root = os.environ.get("XDG_STATE_HOME")
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base / "ai-team-os" / "codex" / "hook-counts.sqlite3"


def _label(value: object) -> str:
    """Keep identifiers only, never arbitrary payload text or filesystem paths."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        return value
    return ""


def _record(state: str, call_id: str, started: float, *, event: str = "",
            payload: dict | None = None, reason: str = "", exception: str = "") -> None:
    """Use short atomic transactions; never reset an unreadable counter file."""
    try:
        sys.stderr.write(f"[aiteam-codex-hook] {state}\n")
    except Exception:
        pass
    try:
        path = _counter_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.closing(sqlite3.connect(path, timeout=0.25)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS counters "
                    "(state TEXT PRIMARY KEY, count INTEGER NOT NULL CHECK(count >= 0))"
                )
                rows = connection.execute("SELECT state, count FROM counters").fetchall()
                if any(
                    name not in _STATES or type(count) is not int or count < 0
                    for name, count in rows
                ) or (rows and {name for name, _ in rows} != set(_STATES)):
                    raise ValueError("Invalid counter contents")
                connection.executemany(
                    "INSERT OR IGNORE INTO counters(state, count) VALUES (?, 0)",
                    [(name,) for name in _STATES],
                )
                connection.execute(
                    "UPDATE counters SET count = count + 1 WHERE state = ?", (state,)
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS invocations ("
                    "call_id TEXT PRIMARY KEY, event TEXT NOT NULL, session_id TEXT NOT NULL, "
                    "agent_id TEXT NOT NULL, tool_name TEXT NOT NULL, status TEXT NOT NULL, "
                    "started_at TEXT NOT NULL, finished_at TEXT, elapsed_ms INTEGER, "
                    "reason_code TEXT NOT NULL, exception_class TEXT NOT NULL)"
                )
                now = datetime.now(UTC).isoformat()
                if state == "invoked":
                    connection.execute(
                        "INSERT INTO invocations VALUES (?, ?, '', '', '', 'pending', "
                        "?, NULL, NULL, '', '')", (call_id, _label(event), now),
                    )
                else:
                    metadata = payload or {}
                    connection.execute(
                        "UPDATE invocations SET event=?, session_id=?, agent_id=?, tool_name=?, "
                        "status=?, finished_at=?, elapsed_ms=?, reason_code=?, exception_class=? "
                        "WHERE call_id=?",
                        (_label(event), _label(metadata.get("session_id")),
                         _label(metadata.get("agent_id")), _label(metadata.get("tool_name")),
                         state, now, max(0, round((time.monotonic() - started) * 1000)),
                         reason, _label(exception), call_id),
                    )
    except Exception as error:
        try:
            # Do not log exceptions' messages: they may include user paths.
            sys.stderr.write(
                f"[aiteam-codex-hook] counter_write_failed {type(error).__name__}\n"
            )
        except Exception:
            pass


def _is_inert(event: str, payload: dict) -> bool:
    if event not in _TOOL_EVENTS:
        return False
    name = payload.get("tool_name", "")
    if not isinstance(name, str) or name.startswith(_OS_PREFIXES):
        return False
    return name == "update_plan" or name.startswith("mcp__codex_app__")


def main() -> None:
    call_id = uuid.uuid4().hex
    started = time.monotonic()
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    _record("invoked", call_id, started, event=event)
    state = "error"
    reason = "invalid_payload"
    exception = ""
    payload = None
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = None
            raise ValueError("Expected an object")
        reason = "invalid_event"
        event = sys.argv[1] if len(sys.argv) > 1 else payload.get("hook_event_name")
        if not isinstance(event, str) or not event.strip():
            raise ValueError("Expected an event name")
        payload["hook_event_name"] = event
        if _is_inert(event, payload):
            state = "inert_dropped"
            reason = "inert_tool"
        else:
            # The shared core logs payload-derived details. Keep this observer's
            # diagnostics state-only and guarantee no host-facing stdout.
            reason = "sender_exception"
            stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                import hook_core

                state = str(hook_core.post_event(
                    hook_core._trim_payload(payload), hook_core._get_api_url()
                ))
            if state not in _STATES[2:]:
                state = "error"
                reason = "invalid_core_state"
            elif state == "posted":
                reason = "posted"
            else:
                # Inspect only known terminal error suffixes. Never persist core
                # logs: they may contain payload-derived details and local paths.
                detail = stderr.getvalue().rstrip()
                timed_out = detail.endswith((": error - timed out",
                                            ": API unreachable - <urlopen error timed out>"))
                reason = "post_timeout" if timed_out else "post_other"
                http_error = re.search(
                    re.escape(f"[aiteam-hook] {event}: API unreachable - HTTP Error ")
                    + r"([45][0-9]{2}): [^\r\n]*$", detail,
                )
                if state == "post_unreachable" and http_error:
                    reason = f"http_{http_error.group(1)}"
    except BaseException as error:
        state = "error"
        exception = type(error).__name__
    finally:
        _record(state, call_id, started, event=event, payload=payload,
                reason=reason, exception=exception)


if __name__ == "__main__":
    main()
