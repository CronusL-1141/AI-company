#!/usr/bin/env python3
"""PermissionDenied hook — auto-retry logic using diagnose_denial classifier.

Reads the denial payload from stdin, calls POST /api/hooks/diagnose_denial to
classify the denial, then decides how to respond:

  - recoverable_with_retry    → retry=True (once per tool_use_id), inject hint
  - recoverable_with_workaround → retry=False, explain alternative in context
  - needs_user_approval       → retry=False, fire-and-forget briefing to Leader
  - permanent_denial          → retry=False, log event only

Falls back to keyword-matching (legacy logic) when the API is unreachable.

Usage: python permission_denied_recovery.py  (reads JSON from stdin)
stdlib only — no third-party packages.
"""

import io
import json
import os
import sys
import time
import urllib.request

_API_TIMEOUT = 2

_STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os")
_RETRY_STATE_FILE = os.path.join(_STATE_DIR, "permission_denied_retry.json")
_PORT_FILE = os.path.join(_STATE_DIR, "api_port.txt")


def _get_api_url() -> str:
    """Return current API URL. AITEAM_API_URL env var takes highest priority.

    Same resolution order as every other hook: env override → the port the API
    actually bound to (api_port.txt) → 8000. Hardcoding 8000 made this the only
    hook that went deaf whenever the API started on a fallback port.
    """
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


_API_BASE = _get_api_url()

# ---------------------------------------------------------------------------
# Fallback keyword lists (used when API is unreachable)
# ---------------------------------------------------------------------------

_TRANSIENT_PATTERNS = ["temporary", "rate limit", "transient", "try again"]
_PATH_OUTSIDE_PATTERNS = [
    "outside the project",
    "outside project",
    "not in allowed",
    "path not allowed",
    "additionaldirectories",
]


# ---------------------------------------------------------------------------
# Retry state helpers
# ---------------------------------------------------------------------------


def _load_retry_state() -> dict:
    try:
        with open(_RETRY_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_retry_state(state: dict) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        now = time.time()
        pruned = {k: v for k, v in state.items() if now - v.get("ts", 0) < 3600}
        with open(_RETRY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(pruned, f)
    except Exception:
        pass


def _already_retried(tool_use_id: str) -> bool:
    return tool_use_id in _load_retry_state()


def _mark_retried(tool_use_id: str) -> None:
    state = _load_retry_state()
    state[tool_use_id] = {"ts": time.time(), "retried": True}
    _save_retry_state(state)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: dict, timeout: float = _API_TIMEOUT) -> dict | None:
    """POST JSON to url. Returns parsed response or None on failure."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _post_event(payload: dict) -> None:
    _post_json(f"{_API_BASE}/api/hooks/event", payload)


def _post_briefing_async(title: str, description: str, session_id: str) -> None:
    """Fire-and-forget briefing — silently ignores failures."""
    _post_json(
        f"{_API_BASE}/api/leader-briefings",
        {
            "title": title,
            "description": description,
            "urgency": "medium",
        },
    )


# ---------------------------------------------------------------------------
# Diagnose via API
# ---------------------------------------------------------------------------


def _call_diagnose(tool_name: str, tool_input: dict, reason: str) -> dict | None:
    """Call POST /api/hooks/diagnose_denial. Returns parsed dict or None."""
    return _post_json(
        f"{_API_BASE}/api/hooks/diagnose_denial",
        {"tool_name": tool_name, "tool_input": tool_input, "reason": reason},
    )


# ---------------------------------------------------------------------------
# Fallback classifier (API unreachable)
# ---------------------------------------------------------------------------


def _matches_any(text: str, patterns: list) -> bool:
    text_lower = text.lower()
    return any(p in text_lower for p in patterns)


def _fallback_classify(tool_name: str, tool_input: dict, reason: str) -> dict:
    """Keyword-based fallback when API is unreachable."""
    if tool_name == "Bash":
        return {
            "category": "needs_user_approval",
            "hint": "Bash command denied. Consider a safer alternative or ask the user.",
            "additional_context": (
                f"Bash denied: {reason}. Use dedicated tools (Read/Write/Edit/Glob/Grep) if possible."
            ),
        }
    if _matches_any(reason, _PATH_OUTSIDE_PATTERNS):
        return {
            "category": "needs_user_approval",
            "hint": "Path is outside allowed directories. Request user to extend additionalDirectories.",
            "additional_context": (
                "Access denied: path outside project root. "
                "Ask the user to add it to additionalDirectories if access is needed."
            ),
        }
    if _matches_any(reason, _TRANSIENT_PATTERNS):
        return {
            "category": "recoverable_with_retry",
            "hint": "Transient denial — retrying automatically.",
            "additional_context": "",
        }
    return {
        "category": "permanent_denial",
        "hint": "",
        "additional_context": f"Permission denied with no known recovery path: {reason}",
    }


# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------


def _output(retry: bool, additional_context: str = "") -> None:
    result: dict = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionDenied",
            "retry": retry,
        }
    }
    if additional_context:
        result["hookSpecificOutput"]["additionalContext"] = additional_context
    sys.stdout.write(json.dumps(result))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, io.UnsupportedOperation):
        pass

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            sys.exit(0)
        payload = json.loads(raw)
    except Exception as e:
        sys.stderr.write(f"[permission-denied-recovery] parse error: {e}\n")
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    reason = payload.get("reason", "")
    tool_use_id = payload.get("tool_use_id", "")
    session_id = payload.get("session_id", "")

    # Record event to OS (fire-and-forget)
    _post_event({
        "hook_event_name": "PermissionDenied",
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "denial_reason": reason,
        "tool_use_id": tool_use_id,
        "cwd": payload.get("cwd", os.getcwd()),
    })

    # Classify via API; fall back to keyword matching if API is unavailable
    classification = _call_diagnose(tool_name, tool_input, reason)
    if not classification or "category" not in classification:
        classification = _fallback_classify(tool_name, tool_input, reason)

    category = classification.get("category", "permanent_denial")
    hint = classification.get("hint", "")
    additional_context = classification.get("additional_context", "")

    # --- Act on classification ---

    if category == "recoverable_with_retry":
        if tool_use_id and not _already_retried(tool_use_id):
            _mark_retried(tool_use_id)
            _output(retry=True, additional_context=hint)
        else:
            # Already retried — downgrade to permanent
            _output(
                retry=False,
                additional_context="Transient denial retry already attempted for this tool call.",
            )
        sys.exit(0)

    if category == "recoverable_with_workaround":
        _output(retry=False, additional_context=additional_context or hint)
        sys.exit(0)

    if category == "needs_user_approval":
        path_hint = tool_input.get("file_path", tool_input.get("path", tool_input.get("command", "")))
        _post_briefing_async(
            title=f"Agent denied: {tool_name} — needs approval",
            description=(
                f"Tool `{tool_name}` was denied (session {session_id}): {reason}\n"
                f"Detail: {path_hint or 'n/a'}\n"
                f"Hint: {hint}"
            ),
            session_id=session_id,
        )
        _output(retry=False, additional_context=additional_context or hint)
        sys.exit(0)

    # permanent_denial (or unknown category)
    _output(retry=False, additional_context=additional_context)
    sys.exit(0)


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
