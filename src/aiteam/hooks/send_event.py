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

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")


def _get_api_url() -> str:
    """Return current API URL. AITEAM_API_URL env var takes highest priority."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


API_URL = _get_api_url()

# ---------------------------------------------------------------------------
# Inert-tool early exit (cost guard for the `*` PreToolUse/PostToolUse matcher)
# ---------------------------------------------------------------------------
# send_event is registered at matcher "*" on purpose: the OS keys agent liveness,
# IDLE→BUSY self-heal, project-binding heal and per-tool activity spans off tool
# events, so narrowing the matcher blinds the observability layer (2026-07 "队死
# 人活" incident). The cost guard is therefore *inside* the hook: tool calls the
# OS provably ignores are dropped before the HTTP POST.
#
# Two rules that must not be broken when editing this set:
#   1. NEVER add a tool the OS reacts to — Read/Edit/Write/Bash are hook_translator
#      _INTENT_TOOLS; Agent/Workflow drive team + workflow ingestion; mcp__* calls
#      carry the OS's own writes. Dropping any of them regresses observability.
#   2. The drop is SYMMETRIC (both PreToolUse and PostToolUse). Dropping only the
#      PostToolUse half would strand the PreToolUse span in status="running"
#      forever, which is worse than not recording it at all.
_INERT_TOOLS = frozenset({
    "TodoWrite",        # local checklist bookkeeping, never consumed by the OS
    "ToolSearch",       # deferred-tool schema lookup
    "BashOutput",       # polling a background shell started by an earlier Bash
    "KillShell",
    "ListMcpResourcesTool",
    "ReadMcpResourceTool",
})
_TOOL_EVENTS = ("PreToolUse", "PostToolUse")


def _is_inert(event_name: str, payload: dict) -> bool:
    """True when this tool event carries no signal the OS consumes."""
    return event_name in _TOOL_EVENTS and payload.get("tool_name", "") in _INERT_TOOLS


# Large field truncation limit (prevent timeouts from oversized SubagentStop payloads)
MAX_FIELD_LEN = 500
MAX_PAYLOAD_BYTES = 32_768  # Overall payload limit 32KB; exceeding drops non-essential fields
LARGE_FIELDS = {"last_assistant_message", "agent_transcript_path", "transcript_path"}
# Fields that must be preserved (not dropped even if payload exceeds limit)
ESSENTIAL_FIELDS = {
    "hook_event_name",
    "session_id",
    "tool_name",
    "tool_input",
    "cc_team_name",
    # 路径字段短（LARGE_FIELDS 已截 500 字符）且承载 wf_id 提取——超限剥离会
    # 让 SubagentStop 丢失 wf_id、per-run 建队/迁移失败（2026-07-07 D1 实录）
    "transcript_path",
    "agent_transcript_path",
    # PostCompact 的两个字段：压缩摘要动辄几万字，整体载荷必然超 32KB 闸，
    # 于是 trigger 与摘要长度双双被剥掉，落库成 {trigger:"", summary_chars:0}
    # ——生产实测就是这条形状。摘要正文本来就刻意不存，改为在 hook 侧算好长度
    # 再把正文扔掉（见 _trim_payload），长度与 trigger 一起进必留字段。
    "trigger",
    "compact_summary_chars",
}


def _trim_payload(payload: dict) -> dict:
    """Truncate oversized fields to prevent HTTP timeouts.

    Two-level protection:
    1. Known large fields truncated to MAX_FIELD_LEN (500 chars)
    2. If overall exceeds 50KB, all string fields truncated to 200 chars
    """
    trimmed = {}
    for k, v in payload.items():
        if k == "compact_summary":
            # Measure here and drop the body: the OS deliberately never stores the
            # summary text (it is back in the model's context after the compact),
            # so shipping tens of KB over HTTP only serves to trip the size gate
            # and lose the one number we actually wanted.
            trimmed["compact_summary_chars"] = len(v) if isinstance(v, str) else 0
            continue
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


def _resolve_cc_team_name(session_id: str) -> str | None:
    """Look up the CC team owned by this session (leadSessionId is authoritative).

    leadSessionId is written by CC itself into ~/.claude/teams/<dir>/config.json,
    so it is the only same-source key linking a hook payload to a CC team.

    The previous "strategy 1" matched payload.agent_type (a *template* name such as
    testing-bug-fixer) against config members[].name (the *dispatch-time custom*
    member name): different sources, so it almost never matched — and when it did
    match by coincidence it attributed the event to another session's team, which is
    worse than a miss. Removed (2026-07-27 batch 3 wrong-object fixes).

    Uses only standard library; silently handles all exceptions.
    """
    if not session_id:
        return None
    teams_dir = os.path.join(os.path.expanduser("~"), ".claude", "teams")
    try:
        config_files = glob.glob(os.path.join(teams_dir, "*", "config.json"))
    except OSError:
        return None

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

        event_name = payload.get("hook_event_name", "")

        # Cost guard for the "*" tool matcher — drop events the OS ignores.
        if _is_inert(event_name, payload):
            return

        # SubagentStart/SubagentStop: inject CC team name
        if event_name in ("SubagentStart", "SubagentStop") and "cc_team_name" not in payload:
            cc_team = _resolve_cc_team_name(payload.get("session_id", ""))
            if cc_team:
                payload["cc_team_name"] = cc_team

        # Inject cwd for project matching (hook_translator needs this)
        if "cwd" not in payload:
            payload["cwd"] = os.getcwd()

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


def _yield_if_superseded() -> None:
    """Backup-chain yield: exit 0 if the source-install main chain covers this hook.

    When AI Team OS is present both as a marketplace plugin and via the source
    installer, CC fires two byte-identical copies of every hook. To keep exactly
    one chain speaking, the plugin-mode copy exits silently iff ~/.claude/settings.json
    already registers this same script under ~/.claude/hooks/ai-team-os/. Hooks the
    main chain does not register keep running from the plugin (out-of-box backup —
    no coverage gap). Only the plugin-mode copy ever yields: CLAUDE_PLUGIN_ROOT is
    set by CC for plugin hooks only and __file__ lives under it; the runtime
    main-chain copy and any direct/repo/test run lack that and never yield. Pure
    stdlib, one small file read; any error falls through and runs (fail-safe).
    """
    import os
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return
    try:
        import json
        from pathlib import Path
        here = Path(__file__).resolve()
        if Path(plugin_root).resolve() not in here.parents:
            return
        settings = Path.home() / ".claude" / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except Exception:
        return
    name = os.path.basename(__file__)
    for groups in registered.values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "ai-team-os" in cmd and name in cmd:
                    raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    main()
