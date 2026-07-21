#!/usr/bin/env python3
"""PostToolUse hook — link a freshly-saved deep-review report to its row.

Fires on PostToolUse for ``mcp__ai-team-os__report_save``. When the saved
report is type ``deep-review`` and its body contains ``deep_review_id=<uuid>``
(plus matching ``repo_id=<uuid>``), the hook PATCHes the corresponding
EcosystemDeepReview row by calling
``POST /api/ecosystem/deep_reviews/{deep_review_id}/link_report``.

K5: hook also parses the 5-section markdown body into structured fields
(summary / architecture / learnings / risks / integration / demo) and ships
them in the link_report payload so the row reflects the actual report.

Stdlib only — runs in any Python the CC harness happens to use.
"""

import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

_PORT_FILE = os.path.join(
    os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt"
)
_API_TIMEOUT = 3

# Anchors emitted at the top of every deep-review report.
_DEEP_REVIEW_ID_RE = re.compile(r"deep_review_id\s*=\s*([0-9a-fA-F-]{8,})")
_REPO_ID_RE = re.compile(r"repo_id\s*=\s*([0-9a-fA-F-]{8,})")

_TARGET_TOOL_NAMES = {
    "mcp__ai-team-os__report_save",
    "report_save",
}

# 5-section template headings — match either Chinese template or plain numeric
# variants ("## 1.", "## 1 ", "## 1 真实定位"). The first capture group is the
# section number we keep.
_SECTION_RE = re.compile(
    r"^##\s*(\d)[\.\s].*$",
    re.MULTILINE,
)
# Demo metadata lines inside the trailing "## 元数据" block.
_DEMO_RESULT_RE = re.compile(
    r"^[-*]?\s*demo_result\s*[:：]\s*(\w+)",
    re.IGNORECASE | re.MULTILINE,
)
_DEMO_LOG_RE = re.compile(
    r"^[-*]?\s*demo_log_excerpt\s*[:：]\s*\|?\s*\n((?:[ \t]+.*\n?)+)",
    re.MULTILINE,
)
# 推荐动作 line inside section 5.
_RECOMMENDATION_RE = re.compile(
    r"推荐动作[^\n]*?[:：][^\n]*?\*{0,2}(integrate|reference|learn|skip)\*{0,2}",
    re.IGNORECASE,
)


def _get_api_url() -> str:
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


def _http_post(path: str, body: dict) -> dict | None:
    """POST JSON to the OS API. Returns parsed dict, or None on any error."""
    url = f"{_get_api_url()}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None


def _read_stdin_json() -> dict:
    """Best-effort read of stdin as JSON. Empty dict on any error."""
    try:
        raw = sys.stdin.read()
    except (UnicodeDecodeError, OSError):
        try:
            raw = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace").read()
        except Exception:
            return {}
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def _extract_anchors(payload: dict) -> tuple[str | None, str | None, str | None, str]:
    """Pull (deep_review_id, repo_id, report_id, content) out of a PostToolUse payload.

    Looks at ``tool_input`` for the report content, and at ``tool_response``
    for the freshly-minted report id. Returns ``(None, None, None, "")`` if any
    piece is missing or the tool wasn't a report_save.
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _TARGET_TOOL_NAMES:
        return None, None, None, ""

    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None, None, None, ""

    if (tool_input.get("report_type") or "").strip() != "deep-review":
        return None, None, None, ""

    content = tool_input.get("content") or ""
    if not isinstance(content, str) or not content.strip():
        return None, None, None, ""

    deep_match = _DEEP_REVIEW_ID_RE.search(content)
    repo_match = _REPO_ID_RE.search(content)
    if deep_match is None or repo_match is None:
        return None, None, None, ""

    tool_response = payload.get("tool_response") or {}
    report_id = ""
    if isinstance(tool_response, dict):
        report_id = (
            tool_response.get("id")
            or tool_response.get("report_id")
            or ""
        )
    if not report_id:
        return None, None, None, ""

    return deep_match.group(1), repo_match.group(1), report_id, content


def _split_sections(content: str) -> dict[str, str]:
    """Split a 5-section deep-review markdown into a dict keyed by section number.

    Returns ``{"1": "...", "2": "...", ...}`` containing the body of each
    ``## N.`` section (heading line excluded). Missing sections are absent
    from the dict — callers must tolerate that.

    The trailing ``## 元数据`` (or any non-numeric ``##`` section after the 5
    sections) is dropped from the section bodies, so demo metadata never
    bleeds into section 5.
    """
    matches = list(_SECTION_RE.finditer(content))
    if not matches:
        return {}

    # Find the first non-numeric ## heading after the last numeric one — that
    # marks where 元数据 / appendix begins.
    appendix_start = len(content)
    last_numeric_end = matches[-1].end()
    appendix_re = re.compile(r"^##\s+(?!\d[\.\s])\S", re.MULTILINE)
    appendix_match = appendix_re.search(content, last_numeric_end)
    if appendix_match is not None:
        appendix_start = appendix_match.start()

    sections: dict[str, str] = {}
    for idx, m in enumerate(matches):
        num = m.group(1)
        body_start = m.end()
        body_end = (
            matches[idx + 1].start() if idx + 1 < len(matches) else appendix_start
        )
        body = content[body_start:body_end].strip("\n")
        # Strip "---" rules separating appendix.
        body = re.sub(r"\n---+\s*$", "", body).rstrip()
        sections[num] = body
    return sections


def _parse_demo_metadata(content: str) -> tuple[str | None, str | None]:
    """Extract demo_result + demo_log_excerpt from the 元数据 block.

    Returns (demo_result, demo_log_excerpt). Either or both may be ``None``
    when the report didn't include them.
    """
    demo_result: str | None = None
    demo_log: str | None = None

    m = _DEMO_RESULT_RE.search(content)
    if m is not None:
        token = m.group(1).strip().lower()
        if token in {"success", "fail", "skipped"}:
            demo_result = token

    log_match = _DEMO_LOG_RE.search(content)
    if log_match is not None:
        block = log_match.group(1)
        # Dedent the indented block (YAML literal style — strip leading
        # whitespace common to all non-empty lines).
        lines = block.splitlines()
        non_empty = [ln for ln in lines if ln.strip()]
        if non_empty:
            common = min(len(ln) - len(ln.lstrip()) for ln in non_empty)
            dedented = "\n".join(ln[common:] if len(ln) >= common else ln for ln in lines)
            demo_log = dedented.strip()
    return demo_result, demo_log


def _parse_recommendation(integration_md: str) -> str | None:
    """Pull integrate/reference/learn/skip out of section 5 body."""
    if not integration_md:
        return None
    m = _RECOMMENDATION_RE.search(integration_md)
    return m.group(1).lower() if m is not None else None


def _build_link_payload(report_id: str, content: str) -> dict[str, object]:
    """Assemble the link_report POST body with parsed 5-section fields."""
    sections = _split_sections(content)
    summary_md = sections.get("1", "")
    architecture_md = sections.get("2", "")
    learnings_md = sections.get("3", "")
    risks_md = sections.get("4", "")
    integration_md = sections.get("5", "")

    demo_result, demo_log_excerpt = _parse_demo_metadata(content)
    recommendation = _parse_recommendation(integration_md)

    payload: dict[str, object] = {"report_id": report_id}
    if summary_md:
        payload["summary_md"] = summary_md
    if architecture_md:
        payload["architecture_md"] = architecture_md
    if learnings_md:
        payload["learnings_md"] = learnings_md
    if risks_md:
        payload["risks_md"] = risks_md
    if integration_md:
        payload["integration_md"] = integration_md
    if demo_result is not None:
        payload["demo_result"] = demo_result
    if demo_log_excerpt:
        payload["demo_log_excerpt"] = demo_log_excerpt
    if recommendation is not None:
        payload["integration_recommendation"] = recommendation
    return payload


def main() -> None:
    payload = _read_stdin_json()
    deep_review_id, _repo_id, report_id, content = _extract_anchors(payload)
    if not deep_review_id or not report_id:
        # Not a deep-review report_save — silently exit.
        sys.stdout.write("{}")
        return

    body = _build_link_payload(report_id, content)
    _http_post(
        f"/api/ecosystem/deep_reviews/{deep_review_id}/link_report",
        body,
    )
    sys.stdout.write("{}")


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
