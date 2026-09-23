#!/usr/bin/env python3
"""CC TaskCompleted hook bridge — mirrors *finished, shared* CC tasks onto the OS wall.

Q1 ruling (2026-07-27, options C + B): a completion-time ledger that mirrors
only tasks carrying an owner or a dependency link. CC's task list is a
session-local checklist — the OS wall is the cross-session account. Mirroring
every TaskCreate would flood the wall with somebody's private to-dos.

So two filters, both evidence-driven:

* **When** — TaskCompleted, not TaskCreated. A task that never finished says
  nothing about the project; recording it on creation means the wall fills with
  rows nobody will ever close.
* **What** — only tasks with ``teammate_name`` (someone else owns it) or with a
  non-empty ``blocks``/``blockedBy`` in CC's own task file (it is part of a
  chain). A solo, dependency-free checklist item stays inside CC.

Payload (CC v2.1.219, verified against the binary):
``{task_id, task_subject, task_description?, teammate_name?, team_name?}``
plus the common base (session_id, cwd, agent_id, …). Note there is **no**
dependency data in the payload — that has to come off disk, from
``~/.claude/tasks/<team_name>/<task_id>.json``.

Uses stdlib only (no aiteam package dependency).
"""

import contextlib
import json
import os
import random
import sys
import time
import urllib.request

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")
_API_TIMEOUT = 3
_PROJECT_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "supervisor-state.json")
_PROJECT_CACHE_TTL = 300
_SAVE_ATTEMPTS = 12
_SAVE_BACKOFF_S = 0.002  # random backoff of 0 .. n * this before retry n
_TASKS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "tasks")


def _get_api_url() -> str:
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


def _read_state_file() -> dict | None:
    """Parsed state file; {} when absent, None when present but unreadable."""
    return _read_state_snapshot()[0]


def _file_identity(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _read_state_snapshot() -> tuple[dict | None, tuple[int, int, int] | None]:
    """(parsed state, identity of the very file parsed; None when there was none)."""
    try:
        f = open(_PROJECT_CACHE_FILE, encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except Exception:
        return None, None
    with f:
        try:
            identity = _file_identity(os.fstat(f.fileno()))
            data = json.load(f)
        except Exception:
            return None, None
    return (data if isinstance(data, dict) else None), identity


def _path_identity(path: str) -> tuple[int, int, int] | None:
    try:
        return _file_identity(os.stat(path))
    except FileNotFoundError:
        return None


def _load_state() -> dict:
    return _read_state_file() or {}


def _save_state(state: dict) -> None:
    """Merge this hook's project-cache entries into the shared state file.

    supervisor-state.json is mostly workflow_reminder's (counters, session
    throttles, S5 branch claims); this hook only owns ``project_id_by_cwd``.
    Its copy was loaded before an HTTP call of up to 3s, so writing the whole
    copy back erased whatever other sessions recorded meanwhile, and the old
    truncate-then-write let concurrent readers see half a file. Now, the same
    way workflow_reminder saves (no lock file): re-read the file, take only
    cache entries newer than what is there, write a temp file, and replace only
    if the file is still the one just read - otherwise re-read and merge again,
    giving up after a few tries. An unreadable file is left alone - losing a
    cache entry costs one extra resolve, overwriting the file could cost the rest.
    """
    mine = state.get("project_id_by_cwd")
    if not isinstance(mine, dict):
        return
    try:
        directory = os.path.dirname(_PROJECT_CACHE_FILE)
        os.makedirs(directory, exist_ok=True)
        for attempt in range(_SAVE_ATTEMPTS):
            if attempt:
                time.sleep(random.uniform(0, _SAVE_BACKOFF_S * attempt))
            current, identity = _read_state_snapshot()
            if current is None:
                return
            theirs = current.get("project_id_by_cwd")
            if not isinstance(theirs, dict):
                theirs = {}
            for key, entry in mine.items():
                old = theirs.get(key)
                if isinstance(entry, dict) and (
                    not isinstance(old, dict) or old.get("at", 0) < entry.get("at", 0)
                ):
                    theirs[key] = entry
            current["project_id_by_cwd"] = theirs
            tmp = f"{_PROJECT_CACHE_FILE}.{os.getpid()}.{time.monotonic_ns()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(current, f, ensure_ascii=False)
                if _path_identity(_PROJECT_CACHE_FILE) != identity:
                    os.unlink(tmp)
                    continue
                os.replace(tmp, _PROJECT_CACHE_FILE)
                return
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
    except Exception:
        pass


def _resolve_project_id(cwd: str) -> str | None:
    """Resolve project ID from cwd, cached **per cwd** (TTL 5 min).

    The cache used to be a single global ``cached_project_id``: whichever
    session resolved first won, and every other project's tasks landed in it
    for the next five minutes. Several CC sessions across different projects
    run concurrently on this machine, so that was a guaranteed cross-project
    mix-up rather than a rare race.

    Shared with workflow_reminder, which resolves on every tool call: same
    ``project_id_by_cwd`` map, same realpath(cwd) key, same ``{id, at}`` entry.
    Only a hit is trusted here. workflow_reminder also caches "no project here"
    as ``""``, but that entry can predate the directory's registration by up to
    5 minutes, and TaskCompleted fires once: a completion skipped on a stale
    ``""`` is never mirrored at all. So a cached ``""`` is asked again, and this
    hook only ever writes hits back.
    """
    state = _load_state()
    by_cwd = state.get("project_id_by_cwd")
    if not isinstance(by_cwd, dict):
        by_cwd = {}
    key = os.path.realpath(cwd)
    entry = by_cwd.get(key)
    if (
        isinstance(entry, dict)
        and entry.get("id")
        and 0 <= time.time() - entry.get("at", 0) < _PROJECT_CACHE_TTL
    ):
        return entry["id"]

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
        project_id = data.get("project_id") or (data.get("project") or {}).get("id") or ""
        if project_id:
            by_cwd[key] = {"id": project_id, "at": time.time()}
            state["project_id_by_cwd"] = by_cwd
            _save_state(state)
        return project_id or None
    except Exception:
        return None


def _read_cc_task(team_name: str, task_id: str) -> dict:
    """Read CC's own task file, which is where the dependency links live.

    CC keeps them at ~/.claude/tasks/<team>/<id>.json as
    ``{id, subject, description, activeForm, status, blocks, blockedBy}``.
    Any failure returns an empty dict — a missing file must never block the
    hook, it only means "no dependency evidence".
    """
    if not team_name or not task_id:
        return {}
    path = os.path.join(_TASKS_DIR, team_name, f"{task_id}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _should_mirror(owner: str, cc_task: dict) -> bool:
    """Owner or dependency link — otherwise it is a private checklist item."""
    if owner:
        return True
    return bool(cc_task.get("blocks")) or bool(cc_task.get("blockedBy"))


def _mirror(project_id: str, payload: dict, cc_task: dict) -> None:
    api_url = _get_api_url()
    blocked_by = cc_task.get("blockedBy")
    body = {
        "title": payload["title"],
        "description": payload["description"],
        "priority": "medium",
        "horizon": "short",
        "tags": ["cc-task"],
        "status": "completed",
        "cc_task_id": payload["task_id"],
        "cc_blocked_by": [str(i) for i in blocked_by] if isinstance(blocked_by, list) else [],
    }
    if payload["owner"]:
        body["assigned_to"] = payload["owner"]

    req = urllib.request.Request(
        f"{api_url}/api/projects/{project_id}/tasks",
        data=json.dumps(body).encode(),
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

    title = (payload.get("task_subject") or "").strip()
    task_id = str(payload.get("task_id") or "")
    owner = payload.get("teammate_name") or ""
    team_name = payload.get("team_name") or ""
    cwd = payload.get("cwd", os.getcwd())

    # Silent failure: if no title or cannot resolve, just exit cleanly
    if not title or not task_id:
        print(json.dumps({}))
        return

    try:
        cc_task = _read_cc_task(team_name, task_id)
        if _should_mirror(owner, cc_task):
            project_id = _resolve_project_id(cwd)
            if project_id:
                _mirror(
                    project_id,
                    {
                        "title": title,
                        "description": payload.get("task_description", "") or "",
                        "task_id": task_id,
                        "owner": owner,
                    },
                    cc_task,
                )
    except Exception:
        pass  # Never block CC workflow

    print(json.dumps({}))


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
