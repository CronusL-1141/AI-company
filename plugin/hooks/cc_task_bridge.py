#!/usr/bin/env python3
"""CC TaskCreated hook bridge — mirrors CC tasks to OS task wall.

Fires when CC's TaskCreate tool is used. Silently creates a mirrored task
in the OS task wall so the Dashboard can track it.
Uses stdlib only (no aiteam package dependency).
"""

import json
import os
import sys
import time
import urllib.request

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")
_API_TIMEOUT = 3
_PROJECT_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "supervisor-state.json")
_PROJECT_CACHE_TTL = 300


def _get_api_url() -> str:
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


def _load_state() -> dict:
    try:
        with open(_PROJECT_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_PROJECT_CACHE_FILE), exist_ok=True)
        with open(_PROJECT_CACHE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def _resolve_project_id(cwd: str) -> str | None:
    """Resolve project ID from cwd, with file-based cache (TTL 5 min)."""
    state = _load_state()
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
        project_id = data.get("project_id") or data.get("project", {}).get("id")
        if project_id:
            state["cached_project_id"] = project_id
            state["cached_project_id_at"] = time.time()
            _save_state(state)
        return project_id
    except Exception:
        return None


def _create_task(project_id: str, title: str, description: str, owner: str | None) -> None:
    api_url = _get_api_url()
    tags = ["cc-task"]
    payload: dict = {
        "title": title,
        "description": description,
        "priority": "medium",
        "horizon": "short",
        "tags": tags,
    }
    if owner:
        payload["assigned_to"] = owner

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{api_url}/api/projects/{project_id}/tasks",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
        resp.read()


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    title = payload.get("task_subject", "").strip()
    description = payload.get("task_description", "") or ""
    owner = payload.get("teammate_name") or None
    cwd = payload.get("cwd", os.getcwd())

    # Silent failure: if no title or cannot resolve, just exit cleanly
    if not title:
        print(json.dumps({}))
        return

    try:
        project_id = _resolve_project_id(cwd)
        if project_id:
            _create_task(project_id, title, description, owner)
    except Exception:
        pass  # Never block CC workflow

    print(json.dumps({}))


if __name__ == "__main__":
    main()
