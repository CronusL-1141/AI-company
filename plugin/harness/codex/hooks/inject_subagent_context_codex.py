#!/usr/bin/env python3
"""Offer optional OS records only for an explicit, verified Codex task binding.

Captured native SubagentStart payloads do not carry task_id or project_id. They
therefore produce no output. The optional top-level pair below is an explicit
input contract, not a claim that the current host supplies it. Dispatch text
already visible to an agent remains usable without this hook; we never recover
it from transcripts, inherited environment identities, or OS discovery APIs.
"""

from __future__ import annotations

import json
import queue
import re
import sys
import threading
import urllib.request

MAX_INPUT_CHARS = 65_536
MAX_RESPONSE_BYTES = 65_536
HTTP_BUDGET_SECONDS = 1.5
UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not turn one precise task lookup into additional requests."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _binding(payload: object) -> tuple[str, str] | None:
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SubagentStart":
        return None
    values = (payload.get("task_id"), payload.get("project_id"))
    if any(not isinstance(value, str) or not UUID_PATTERN.fullmatch(value) for value in values):
        return None
    return values[0].lower(), values[1].lower()


def _verified(task_id: str, project_id: str) -> bool:
    # Defer even API URL discovery until the current event provides both IDs.
    # The shared stdlib selector preserves the existing dynamic API port setup.
    from hook_core import _get_api_url

    url = f"{_get_api_url().rstrip('/')}/api/tasks/{task_id}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(request, timeout=HTTP_BUDGET_SECONDS) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        return False
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("success") is not True:
        return False
    task = document.get("data")
    return (
        isinstance(task, dict)
        and task.get("id") == task_id
        and task.get("project_id") == project_id
    )


def main() -> None:
    """Fail silently and bound the whole lookup, including slow response bodies."""
    try:
        raw = sys.stdin.read(MAX_INPUT_CHARS + 1)
        if len(raw) > MAX_INPUT_CHARS:
            return
        binding = _binding(json.loads(raw))
        if binding is None:
            return
        task_id, project_id = binding
        result: queue.Queue[bool] = queue.Queue(maxsize=1)

        def verify() -> None:
            try:
                result.put(_verified(task_id, project_id))
            except Exception:
                result.put(False)

        # A daemon only bounds this invocation; it cannot outlive the hook.
        threading.Thread(target=verify, daemon=True).start()
        if not result.get(timeout=HTTP_BUDGET_SECONDS):
            return
        context = (
            f"[AI Team OS] 当前派单绑定已核对：task_id={task_id}，project_id={project_id}。"
            f"原派单已授权的记录操作可使用 task_memo_read(task_id=\"{task_id}\")、"
            f"task_memo_add(task_id=\"{task_id}\")。"
            "记录是否执行、范围与频率均以原派单为准；本绑定不增加操作权限。"
        )
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SubagentStart", "additionalContext": context,
        }}, ensure_ascii=False))
    except Exception:
        return


if __name__ == "__main__":
    main()
