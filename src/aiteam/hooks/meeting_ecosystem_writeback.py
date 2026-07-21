#!/usr/bin/env python3
"""PostToolUse hook — remind Leader to write back ecosystem-debate conclusions.

Fires on PostToolUse for ``mcp__ai-team-os__meeting_conclude`` (and the
underlying API call). When the concluded meeting:

1. Has a ``debate_meeting_id`` link back to one or more EcosystemDeepReview
   rows, OR
2. Carries an ecosystem-related keyword in its topic (e.g. "ecosystem",
   "生态库", "深扫", "deep review"),

the hook prints a short reminder so Leader knows to dispatch a follow-up
agent that calls ``ecosystem_apply_debate_result`` for each linked review,
plus optionally ``ecosystem_mark_as_reference`` /
``ecosystem_start_integration`` per the debate verdict.

The hook NEVER blocks the tool call — it always exits 0. Output is written
to stdout (visible to Leader) and the hook makes a best-effort GET to the
OS API to enrich the reminder with the actual review_ids.

Stdlib only — runs in any Python the CC harness happens to use.
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_PORT_FILE = os.path.join(
    os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt"
)
_API_TIMEOUT = 3

# Tool names that signal a meeting was concluded.
_TARGET_TOOL_NAMES = {
    "mcp__ai-team-os__meeting_conclude",
    "meeting_conclude",
}

# Keywords (lowercase) that hint a meeting topic is ecosystem-related.
# Mirror of ``services.ecosystem_lifecycle.ECOSYSTEM_KEYWORDS``.
_ECOSYSTEM_KEYWORDS: tuple[str, ...] = (
    "ecosystem",
    "生态",
    "生态库",
    "生态仓",
    "deep review",
    "深扫",
    "deep_review",
)


# ============================================================
# I/O helpers
# ============================================================


def _get_api_url() -> str:
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


def _http_get(path: str) -> dict | list | None:
    """GET JSON from the OS API. Returns parsed body, or None on any error."""
    url = f"{_get_api_url()}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None


def _read_stdin_json() -> dict[str, Any]:
    """Best-effort read of stdin as JSON. Empty dict on any error."""
    try:
        raw = sys.stdin.read()
    except (UnicodeDecodeError, OSError):
        try:
            raw = io.TextIOWrapper(
                sys.stdin.buffer, encoding="utf-8", errors="replace"
            ).read()
        except Exception:
            return {}
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


# ============================================================
# Detection logic
# ============================================================


def topic_matches_ecosystem(topic: str) -> list[str]:
    """Return the matched ecosystem keywords (lowercase) found in ``topic``.

    Matching is case-insensitive and substring-based. Empty list = no match.
    Exposed as a pure helper so unit tests can exercise it without the
    hook plumbing.
    """
    if not topic:
        return []
    lower = topic.lower()
    return [kw for kw in _ECOSYSTEM_KEYWORDS if kw.lower() in lower]


def extract_meeting_id(payload: dict[str, Any]) -> str:
    """Pull the meeting_id out of a PostToolUse payload (tool_input + response)."""
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _TARGET_TOOL_NAMES:
        return ""
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, dict):
        meeting_id = tool_input.get("meeting_id") or ""
        if meeting_id:
            return str(meeting_id)
    tool_response = payload.get("tool_response") or payload.get("tool_result") or {}
    if isinstance(tool_response, dict):
        data = tool_response.get("data") or {}
        if isinstance(data, dict):
            mid = data.get("id") or data.get("meeting_id") or ""
            if mid:
                return str(mid)
    return ""


def fetch_meeting_topic(meeting_id: str) -> str:
    """Fetch the meeting's topic via the OS API. Empty string on any error."""
    if not meeting_id:
        return ""
    safe = urllib.parse.quote(meeting_id)
    body = _http_get(f"/api/meetings/{safe}")
    if not isinstance(body, dict):
        return ""
    data = body.get("data") if "data" in body else body
    if isinstance(data, dict):
        return str(data.get("topic") or "")
    return ""


def fetch_linked_reviews(meeting_id: str) -> list[dict[str, Any]]:
    """Find deep_review rows whose debate_meeting_id == meeting_id.

    Lists recent deep reviews via the OS API and filters client-side.
    Capped at 200 reviews — anything older is unlikely to still be open.
    """
    body = _http_get("/api/ecosystem/deep_reviews?limit=200")
    if not isinstance(body, dict):
        return []
    rows = body.get("reviews") or body.get("data") or []
    if not isinstance(rows, list):
        return []
    matched: list[dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict) and r.get("debate_meeting_id") == meeting_id:
            matched.append(r)
    return matched


# ============================================================
# Reminder assembly
# ============================================================


def build_reminder(
    *,
    meeting_id: str,
    topic: str,
    matched_keywords: list[str],
    linked_reviews: list[dict[str, Any]],
) -> str:
    """Render the human-readable reminder for Leader.

    Pure function — exposed so unit tests can compare output without
    invoking the hook end-to-end.
    """
    lines = [
        "[ecosystem writeback hint] meeting concluded with ecosystem signals.",
        f"  meeting_id: {meeting_id}",
        f"  topic: {topic[:200]}",
    ]
    if matched_keywords:
        lines.append(f"  matched keywords: {', '.join(matched_keywords)}")
    if linked_reviews:
        ids = [r.get("id", "?") for r in linked_reviews[:10]]
        lines.append(f"  linked deep_reviews: {', '.join(ids)}")
        lines.append("")
        lines.append("  Suggested follow-up:")
        lines.append(
            "  1. Spawn an agent to read the meeting messages + concluded summary."
        )
        lines.append(
            "  2. For each linked review, call "
            "ecosystem_apply_debate_result(deep_review_id=..., risks_md=..., "
            "learnings_md=..., integration_md=..., integration_recommendation=...)."
        )
        lines.append(
            "  3. Per verdict, call ecosystem_mark_as_reference(deep_review_id=...) "
            "or ecosystem_start_integration(deep_review_id=...)."
        )
    else:
        lines.append("  (no review row currently links debate_meeting_id; the meeting")
        lines.append(
            "   may have been a free-form ecosystem discussion. If you want the "
            "conclusions"
        )
        lines.append(
            "   logged onto a specific repo, run ecosystem_trigger_debate first to"
        )
        lines.append("   establish the link before concluding the meeting.)")
    return "\n".join(lines)


# ============================================================
# Entrypoint
# ============================================================


def main() -> int:
    """Hook entrypoint. Always exits 0 (non-blocking)."""
    payload = _read_stdin_json()
    meeting_id = extract_meeting_id(payload)
    if not meeting_id:
        return 0

    # Pull topic (from response if present, else fetch).
    topic = ""
    response = payload.get("tool_response") or payload.get("tool_result") or {}
    if isinstance(response, dict):
        data = response.get("data") if "data" in response else response
        if isinstance(data, dict):
            topic = str(data.get("topic") or "")
    if not topic:
        topic = fetch_meeting_topic(meeting_id)

    matched = topic_matches_ecosystem(topic)
    linked = fetch_linked_reviews(meeting_id)

    # Suppress noise: only emit reminder when topic matches OR we have
    # explicit linked reviews.
    if not matched and not linked:
        return 0

    reminder = build_reminder(
        meeting_id=meeting_id,
        topic=topic,
        matched_keywords=matched,
        linked_reviews=linked,
    )
    print(reminder, file=sys.stdout)
    return 0


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


if __name__ == "__main__":  # pragma: no cover — entrypoint
    _yield_if_superseded()
    sys.exit(main())
