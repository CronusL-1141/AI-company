#!/usr/bin/env python3
"""AI Team OS — harness-neutral hook core shared by every harness entry script.

Every block below is a **verbatim** extraction from ``plugin/hooks/send_event.py``
(the frozen Claude Code entry): ``_PORT_FILE`` / ``_get_api_url`` (send_event.py
:18, :21-30), the size-guard constants (:68-94) and ``_trim_payload`` (:97-139).
``post_event`` is the POST-and-record tail of ``main()`` (:213-241) lifted into a
function that takes its API URL instead of reading a module global.

WHY THE DUPLICATION IS DELIBERATE — DO NOT "DE-DUPLICATE"
--------------------------------------------------------
``send_event.py`` does **not** import this module, and must not be changed to.
Two decisions force that:

1. **The CC entry is frozen.** ``scripts/hook_entry_freeze.json`` pins the sha256
   of ``plugin/hooks/send_event.py``; a machine check fails if it moves. Adding an
   ``import`` changes the file, so "just import hook_core" is exactly the edit the
   freeze exists to stop. The freeze is what lets the Codex work claim CC is
   untouched — a claim no diff review can make as cheaply.
2. **The Codex adapter needs the same core.** Its own entry script reuses these
   blocks, so they have to live somewhere importable that is not the frozen file.

Equivalence between the two copies is not left to good intentions: the tests in
``tests/unit/hooks/test_hook_core_equivalence.py`` push the same stdin through the
``send_event.py`` subprocess and through this module and require the POST bodies to
be byte-identical, and they assert each block's source text still occurs verbatim
inside ``send_event.py``. Change one side and they go red.

This file is also kept byte-identical across its copies (``plugin/hooks/`` and
``src/aiteam/hooks/``, machine-checked by I1 in ``scripts/check_invariants.sh``),
the same rule that governs every other hook script in this repository.

Not extracted on purpose (harness-specific, stays in each entry script):
``_INERT_TOOLS`` / ``_is_inert`` (the tool set differs per harness),
``_resolve_cc_team_name`` and ``_yield_if_superseded`` (both Claude Code only).

Note: standard library only, no third-party packages — a harness may invoke this
from any Python environment.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from enum import StrEnum

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
    # cwd 同样短，且是**唯一**能反查主会话 transcript 的兜底键：
    # leader_usage.locate_main_transcript 在路径不可用时走 <slug(cwd)>/<session_id>.jsonl，
    # 而那条兜底的 docstring 写明兜的就是"超大载荷被剥离到必留字段"这一档——
    # cwd 不在必留里，兜底就在它专为之而生的场景里必然失效（2026-08-03 排查发现）。
    # 项目归属匹配（_resolve_project_id_by_cwd）也吃同一个键。
    "cwd",
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


class HookPostState(StrEnum):
    """Outcome of one hook invocation, from the entry script's point of view.

    Five states, only three of which ``post_event`` can return; ``INVOKED`` and
    ``INERT_DROPPED`` describe what happened *before* the POST and are reported by
    the entry script itself. They live here so every harness names the same
    outcomes. Nothing consumes these values yet: this release only writes them to
    stderr, exactly as the pre-existing code did, so that the CC differential
    stays empty. Queueing or re-delivery of a lost event is a later decision.
    """

    INVOKED = "invoked"                    # entry script ran, before any decision
    INERT_DROPPED = "inert_dropped"        # dropped by the harness inert-tool guard
    POSTED = "posted"                      # API accepted the event
    POST_UNREACHABLE = "post_unreachable"  # OS service down / network refused
    ERROR = "error"                        # anything else (encode, HTTP error, ...)


def post_event(payload: dict, api_url: str) -> HookPostState:
    """Serialize, size-guard and POST one hook payload. Never raises.

    Byte-for-byte the same request body as ``send_event.py`` main() :213-241: the
    oversize path keeps only ESSENTIAL_FIELDS and appends ``_stripped`` /
    ``_original_size`` in that order. Failures are written to stderr and nothing
    else — a hook must never block or slow down its host.

    The stderr label comes from the payload's ``hook_event_name`` rather than
    ``sys.argv[1]``; every entry script sets that key before calling in, and the
    request body (the thing under test) is unaffected either way.
    """
    event_name = payload.get("hook_event_name") or "unknown"
    try:
        data = json.dumps(payload).encode("utf-8")
        if len(data) > MAX_PAYLOAD_BYTES:
            stripped = {k: v for k, v in payload.items() if k in ESSENTIAL_FIELDS}
            stripped["_stripped"] = True
            stripped["_original_size"] = len(data)
            sys.stderr.write(
                f"[aiteam-hook] {event_name}: payload too large "
                f"({len(data)} bytes > {MAX_PAYLOAD_BYTES}), stripped to essentials\n"
            )
            data = json.dumps(stripped).encode("utf-8")
        req = urllib.request.Request(
            f"{api_url}/api/hooks/event",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=1.5) as resp:
            resp.read()  # Consume response without output — decisions handled by workflow_reminder.py
        return HookPostState.POSTED

    except urllib.error.URLError as e:
        # OS service not running; output to stderr for debugging (doesn't block the host)
        sys.stderr.write(f"[aiteam-hook] {event_name}: API unreachable - {e}\n")
        return HookPostState.POST_UNREACHABLE
    except Exception as e:
        # Log other errors to stderr as well
        sys.stderr.write(f"[aiteam-hook] {event_name}: error - {e}\n")
        return HookPostState.ERROR
