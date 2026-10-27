#!/usr/bin/env python3
"""PermissionDenied hook — auto-retry logic using diagnose_denial classifier.

Reads the denial payload from stdin, calls POST /api/hooks/diagnose_denial to
classify the denial, then decides how to respond:

  - recoverable_with_retry    → retry=True (once per tool_use_id)
  - recoverable_with_workaround → retry=False
  - needs_user_approval       → retry=False
  - permanent_denial          → retry=False

Every denial is recorded as an event (fire-and-forget). A denial is not a
decision waiting on the user, so this hook files no pending briefing: pending
items are only created when the user is away and a real choice is needed.

The only output Claude Code reads from a PermissionDenied hook is
``hookSpecificOutput.retry``; any other field (additionalContext included) is
discarded, so the hook emits nothing else.

Falls back to keyword-matching (legacy logic) when the API is unreachable.

Usage: python permission_denied_recovery.py  (reads JSON from stdin)
stdlib only — no third-party packages.
"""

import importlib.util
import io
import json
import os
import sys
import time
import urllib.request

_API_TIMEOUT = 2

def _hook_core():
    """Load the shared hook core next to this file (its path section); None if it cannot load."""
    module = sys.modules.get("hook_core")
    if module is not None:
        return module
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook_core.py")
        spec = importlib.util.spec_from_file_location("hook_core", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["hook_core"] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop("hook_core", None)
        return None


def _get_api_url() -> str:
    """Return current API URL. AITEAM_API_URL env var takes highest priority.

    Same resolution order as every other hook (hook_core): env override, the port
    the API actually bound to (api_port.txt in the data directory), 8000.
    Hardcoding 8000 made this the only hook that went deaf whenever the API
    started on a fallback port.
    """
    core = _hook_core()
    if core is not None:
        return core._get_api_url()
    return os.environ.get("AITEAM_API_URL") or "http://localhost:8000"


def _retry_state_file() -> str:
    """permission_denied_retry.json in the OS data directory; FileNotFoundError when there is none."""
    core = _hook_core()
    if core is None:
        raise FileNotFoundError("hook_core.py could not be loaded")
    return str(core.os_data_dir() / "permission_denied_retry.json")


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
        with open(_retry_state_file(), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_retry_state(state: dict) -> None:
    try:
        path = _retry_state_file()
        now = time.time()
        pruned = {k: v for k, v in state.items() if now - v.get("ts", 0) < 3600}
        with open(path, "w", encoding="utf-8") as f:
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


def _hook_delivery():
    """Load hook_delivery.py from next to this file; None if it cannot load."""
    module = sys.modules.get("hook_delivery")
    if module is not None:
        return module
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook_delivery.py")
        spec = importlib.util.spec_from_file_location("hook_delivery", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["hook_delivery"] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop("hook_delivery", None)
        return None


def _post_event(payload: dict) -> None:
    """Record the denial through the shared delivery path (classified, ledgered; I25)."""
    delivery = _hook_delivery()
    if delivery is None:
        sys.stderr.write("[permission-denied-recovery] hook_delivery.py unavailable: event not recorded\n")
        return
    delivery.post_body(
        json.dumps(payload).encode("utf-8"), _API_BASE, "PermissionDenied",
        session_id=payload.get("session_id") or "",
        tool_use_id=payload.get("tool_use_id") or "",
        timeout=_API_TIMEOUT,
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
        }
    if _matches_any(reason, _PATH_OUTSIDE_PATTERNS):
        return {
            "category": "needs_user_approval",
            "hint": "Path is outside allowed directories. Request user to extend additionalDirectories.",
        }
    if _matches_any(reason, _TRANSIENT_PATTERNS):
        return {
            "category": "recoverable_with_retry",
            "hint": "Transient denial — retrying automatically.",
        }
    return {"category": "permanent_denial", "hint": ""}


# ---------------------------------------------------------------------------
# Output builders
# ---------------------------------------------------------------------------


def _output(retry: bool) -> None:
    result = {"hookSpecificOutput": {"hookEventName": "PermissionDenied", "retry": retry}}
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

    # --- Act on classification ---

    if category == "recoverable_with_retry":
        if tool_use_id and not _already_retried(tool_use_id):
            _mark_retried(tool_use_id)
            _output(retry=True)
        else:
            # Already retried — downgrade to permanent
            _output(retry=False)
        sys.exit(0)

    if category == "recoverable_with_workaround":
        _output(retry=False)
        sys.exit(0)

    if category == "needs_user_approval":
        # Recorded in the event log above; a denial is not a decision to queue for the user.
        _output(retry=False)
        sys.exit(0)

    # permanent_denial (or unknown category)
    _output(retry=False)
    sys.exit(0)


def _main_chain_commands() -> list[str]:
    """The commands this session's main chain runs this script with, when this is the plugin's copy.

    With AI Team OS both a marketplace plugin and a source install, CC fires two
    byte-identical copies of every hook; only the plugin copy defers (CLAUDE_PLUGIN_ROOT
    is set for plugin hooks only, and __file__ lives under it). The main chain is what
    the settings.json of this session's config folder registers under hooks/ai-team-os/;
    the folder comes from hook_core's cc_config_dir, the only name taken from it. Any
    error means no commands, so the hook runs (fail-safe).
    """
    import importlib.util
    import json
    import os
    import sys
    from pathlib import Path
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return []
    try:
        if Path(plugin_root).resolve() not in Path(__file__).resolve().parents:
            return []
        _core = sys.modules.get("hook_core")
        if _core is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook_core.py")
            spec = importlib.util.spec_from_file_location("hook_core", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _core = sys.modules["hook_core"] = module
        settings = _core.cc_config_dir() / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
        commands = [str(hook.get("command", "")) for groups in registered.values() for group in groups
                    for hook in group.get("hooks", [])]
    except Exception:
        return []
    name = os.path.basename(__file__)
    return [command for command in commands if "ai-team-os" in command and name in command]


def _yield_if_superseded() -> None:
    """Exit 0 when the main chain covers this hook; a hook it does not register keeps running here."""
    if _main_chain_commands():
        raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    main()
