#!/usr/bin/env python3
"""Autopilot auto-stop hook (UserPromptSubmit).

When the user sends a message, this hook detects any tasks with
autopilot_active=True in the current project and disables them.
This implements the "user returns → autopilot stops" behaviour.

Design: silent fail-open — any error must not block the user prompt.
Exit codes:
    0 — always (hook failure must never block the user)
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

_PORT_FILE = Path.home() / ".claude" / "data" / "ai-team-os" / "api_port.txt"
_SUPERVISOR_STATE_FILE = (
    Path.home() / ".claude" / "data" / "ai-team-os" / "supervisor-state.json"
)
_API_TIMEOUT = 2
_PROJECT_CACHE_TTL = 300


def _get_api_url() -> str:
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(_PORT_FILE.read_text().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


def _load_supervisor_state() -> dict:
    try:
        return json.loads(_SUPERVISOR_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _resolve_project_id(cwd: str) -> str | None:
    state = _load_supervisor_state()
    cached = state.get("cached_project_id")
    cached_at = state.get("cached_project_id_at", 0)
    if cached and (time.time() - cached_at) < _PROJECT_CACHE_TTL:
        return cached

    api_url = _get_api_url()
    try:
        req = urllib.request.Request(
            f"{api_url}/api/context/resolve",
            data=json.dumps({"cwd": cwd, "auto_create": False}).encode(),  # 归属铁律：绝不自动立项
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("project_id") or (data.get("project") or {}).get("id")
    except Exception:
        return None


def _find_autopilot_tasks(project_id: str) -> list[dict]:
    api_url = _get_api_url()
    try:
        req = urllib.request.Request(
            f"{api_url}/api/projects/{project_id}/task-wall",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tasks = data.get("data") or data.get("items") or data.get("tasks") or []
        result = []
        for task in tasks:
            config = task.get("config") or {}
            pipeline = config.get("pipeline") or {}
            if pipeline.get("autopilot_active"):
                result.append(task)
        return result
    except Exception:
        return []


def _stop_autopilot(task_id: str) -> bool:
    api_url = _get_api_url()
    try:
        payload = json.dumps({"active": False}).encode()
        req = urllib.request.Request(
            f"{api_url}/api/tasks/{task_id}/pipeline/v2/autopilot",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("success"))
    except Exception:
        return False


def _add_task_memo(task_id: str) -> None:
    api_url = _get_api_url()
    try:
        payload = json.dumps({
            "content": "用户返回，autopilot 自动停止。",
            "memo_type": "progress",
            "author": "autopilot_auto_stop",
        }).encode()
        req = urllib.request.Request(
            f"{api_url}/api/tasks/{task_id}/memos",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            resp.read()
    except Exception:
        pass  # memo failure is non-critical


def main() -> None:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    cwd = payload.get("cwd") or os.getcwd()

    project_id = _resolve_project_id(cwd)
    if not project_id:
        sys.exit(0)

    tasks = _find_autopilot_tasks(project_id)
    if not tasks:
        sys.exit(0)

    for task in tasks:
        task_id = task.get("id", "")
        if not task_id:
            continue
        ok = _stop_autopilot(task_id)
        if ok:
            _add_task_memo(task_id)
            sys.stderr.write(
                f"[OS AUTOPILOT] 任务 {task_id} autopilot 已自动停止（用户返回）\n"
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
