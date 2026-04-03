#!/usr/bin/env python3
"""AI Team OS — Claude Code Hook event sender.

Executed when a CC hook fires; forwards events to the OS API.
Usage: python -m aiteam.hooks.send_event <EventType> (reads JSON from stdin)

Note: This script uses only Python standard library, no third-party packages,
since it may be called directly by CC in any Python environment.
"""

import glob
import json
import os
import sys
import urllib.error
import urllib.request

API_URL = os.environ.get("AITEAM_API_URL", "http://localhost:8000")

# Large field truncation limit (prevent timeouts from oversized SubagentStop payloads)
MAX_FIELD_LEN = 500
MAX_PAYLOAD_BYTES = 32_768  # Overall payload limit 32KB; exceeding drops non-essential fields
LARGE_FIELDS = {"last_assistant_message", "agent_transcript_path", "transcript_path"}
# Fields that must be preserved (not dropped even if payload exceeds limit)
ESSENTIAL_FIELDS = {"hook_event_name", "session_id", "tool_name", "tool_input", "cc_team_name"}


def _trim_payload(payload: dict) -> dict:
    """Truncate oversized fields to prevent HTTP timeouts.

    Two-level protection:
    1. Known large fields truncated to MAX_FIELD_LEN (500 chars)
    2. If overall exceeds 50KB, all string fields truncated to 200 chars
    """
    trimmed = {}
    for k, v in payload.items():
        if k in LARGE_FIELDS:
            if isinstance(v, str) and len(v) > MAX_FIELD_LEN:
                trimmed[k] = v[:MAX_FIELD_LEN] + "...(truncated)"
            elif isinstance(v, dict):
                trimmed[k] = str(v)[:MAX_FIELD_LEN] + "...(truncated)"
            else:
                trimmed[k] = v
        elif k == "tool_response" and isinstance(v, dict):
            # Truncate tool output but preserve structure
            tr = {}
            for rk, rv in v.items():
                if isinstance(rv, str) and len(rv) > MAX_FIELD_LEN:
                    tr[rk] = rv[:MAX_FIELD_LEN] + "...(truncated)"
                else:
                    tr[rk] = rv
            trimmed[k] = tr
        else:
            trimmed[k] = v

    # Overall size check: if exceeds 50KB, truncate all string fields recursively
    payload_str = json.dumps(trimmed)
    if len(payload_str) > 50_000:
        for k, v in trimmed.items():
            if isinstance(v, str) and len(v) > 200:
                trimmed[k] = v[:200] + "...(truncated)"

    return trimmed


def _resolve_cc_team_name(session_id: str, agent_name: str = "") -> str | None:
    """Look up team name in CC team config by agent_name.

    Strategy 1: Exact match by members.name (session-independent, reliable across sessions)
    Strategy 2: Fallback match by leadSessionId
    Uses only standard library; silently handles all exceptions.
    """
    teams_dir = os.path.join(os.path.expanduser("~"), ".claude", "teams")
    try:
        config_files = glob.glob(os.path.join(teams_dir, "*", "config.json"))
    except OSError:
        return None

    # Strategy 1: Look up agent_name in members list (reliable across sessions)
    if agent_name:
        for config_path in config_files:
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                for m in config.get("members", []):
                    if m.get("name", "") == agent_name:
                        return config.get("name")
            except (json.JSONDecodeError, OSError, KeyError):
                continue

    # Strategy 2: Fallback by leadSessionId
    if session_id:
        for config_path in config_files:
            try:
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                if config.get("leadSessionId") == session_id:
                    return config.get("name")
            except (json.JSONDecodeError, OSError, KeyError):
                continue

    return None


def main() -> None:
    # Force UTF-8 output on Windows (default is gbk, causes garbled Chinese)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        # On Windows stdin defaults to GBK; CC sends UTF-8, so force buffer read
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            return

        payload = json.loads(raw)

        # CC hook payload doesn't include event type name; inject via CLI arg
        if len(sys.argv) > 1 and "hook_event_name" not in payload:
            payload["hook_event_name"] = sys.argv[1]

        # SubagentStart/SubagentStop: inject CC team name
        event_name = payload.get("hook_event_name", "")
        if event_name in ("SubagentStart", "SubagentStop") and "cc_team_name" not in payload:
            session_id = payload.get("session_id", "")
            agent_name = payload.get("agent_type", "")
            cc_team = _resolve_cc_team_name(session_id, agent_name)
            if cc_team:
                payload["cc_team_name"] = cc_team

        # Truncate large fields
        payload = _trim_payload(payload)

        # Overall payload size check: if exceeds limit, keep only essential fields
        data = json.dumps(payload).encode("utf-8")
        if len(data) > MAX_PAYLOAD_BYTES:
            stripped = {k: v for k, v in payload.items() if k in ESSENTIAL_FIELDS}
            stripped["_stripped"] = True
            stripped["_original_size"] = len(data)
            event_name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
            sys.stderr.write(
                f"[aiteam-hook] {event_name}: payload too large "
                f"({len(data)} bytes > {MAX_PAYLOAD_BYTES}), stripped to essentials\n"
            )
            data = json.dumps(stripped).encode("utf-8")
        req = urllib.request.Request(
            f"{API_URL}/api/hooks/event",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=1.5) as resp:
            resp.read()  # Consume response without output — decisions handled by workflow_reminder.py

    except urllib.error.URLError as e:
        # OS service not running; output to stderr for debugging (doesn't block CC)
        event_name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
        sys.stderr.write(f"[aiteam-hook] {event_name}: API unreachable - {e}\n")
    except Exception as e:
        # Log other errors to stderr as well
        event_name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
        sys.stderr.write(f"[aiteam-hook] {event_name}: error - {e}\n")


if __name__ == "__main__":
    main()
