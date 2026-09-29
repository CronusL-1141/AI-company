"""The task-wall digest across the persistence boundary, through every outlet that shows it.

Tasks, memos and status changes go in over HTTP to a real uvicorn on a temporary
SQLite file; then separate requests read the digest endpoint (JSON and text), the
wall, the project summary, the task_list_project MCP tool (FastMCP, over HTTP as a
host calls it) and the SessionStart hook (plugin/hooks/session_bootstrap.py as a
subprocess). They must report one wall: the same counts, the same top of the
backlog, and the briefing must carry the server's text byte for byte.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP
from sqlalchemy.dialects import sqlite

from aiteam.mcp.tools import task as task_tools
from aiteam.storage.repository import task_status_events_stmt

ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = ROOT / "plugin" / "hooks" / "session_bootstrap.py"


def _http(api: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(api + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read().decode()
        if response.headers.get_content_type() == "application/json":
            return json.loads(raw)
        return raw


def _mcp_wall(**args) -> dict:
    mcp = FastMCP("digest-e2e")
    task_tools.register(mcp)
    result = asyncio.run(mcp.call_tool("task_list_project", args))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _bootstrap(api: str, home: Path, work: Path) -> str:
    env = {**os.environ, "AITEAM_API_URL": api, "HOME": str(home)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    proc = subprocess.run(
        [sys.executable, str(BOOTSTRAP)], input=json.dumps({"session_id": "digest-e2e", "source": "startup"}),
        capture_output=True, text=True, timeout=60, env=env, cwd=str(work),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture()
def world(hook_server, tmp_path, monkeypatch):
    """A project with 9 pending, 2 running, 1 blocked and 1 closed task, one decision memo."""
    port, database, _ = hook_server
    api = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("AITEAM_API_URL", api)
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    pid = _http(api, "POST", "/api/projects", {"name": "digest-e2e", "root_path": str(work)})["data"]["id"]

    def add(title: str, **extra) -> str:
        return _http(api, "POST", f"/api/projects/{pid}/tasks", {"title": title, **extra})["data"]["id"]

    for i, (priority, horizon) in enumerate([("high", "short"), ("high", "mid"), ("medium", "long"),
                                             ("medium", "short"), ("low", "mid"), ("low", "short"),
                                             ("medium", "mid"), ("high", "long"), ("low", "long")]):
        add(f"backlog {i}", priority=priority, horizon=horizon)
    running = add("running with a decision", status="running", horizon="mid")
    add("running quietly", status="running")
    add("blocked on a vendor", status="blocked", horizon="mid")
    closed = add("shipped today")
    _http(api, "PUT", f"/api/tasks/{closed}", {"status": "completed"})
    _http(api, "POST", f"/api/tasks/{running}/memo",
          {"content": "chose option B", "type": "decision", "author": "lead"})
    return {"api": api, "database": database, "home": home, "work": work, "pid": pid,
            "running": running, "closed": closed, "add": add}


def test_every_outlet_reports_one_wall(world):
    api, pid = world["api"], world["pid"]
    digest = _http(api, "GET", f"/api/projects/{pid}/task-wall/digest")
    assert digest["open_total"] == 12
    assert digest["by_status"] == {"pending": 9, "running": 2, "blocked": 1, "failed": 0}
    assert digest["completed_total"] == 1 and digest["closed_7d"] == 1
    top_ids = [item["id"] for item in digest["top"]]
    recent = [(item["id"], item["activity_kind"]) for item in digest["recent"][:2]]
    assert recent == [(world["running"], "memo"), (world["closed"], "closed")]

    # The wall, paged to 3 pending: stats and the embedded digest still cover the whole wall.
    wall = _http(api, "GET", f"/api/projects/{pid}/task-wall?limit=3")
    rows = [row for bucket in wall["wall"].values() for row in bucket]
    assert sorted(row["status"] for row in rows) == ["blocked", "pending", "pending", "pending",
                                                     "running", "running"]
    assert wall["not_shown"] == {"pending": 6}
    assert wall["stats"]["total"] == 12 and wall["stats"]["by_status"] == digest["by_status"]
    assert wall["stats"]["completed_count"] == 1
    assert [item["id"] for item in wall["digest"]["top"]] == top_ids
    running_row = next(row for row in rows if row["id"] == world["running"])
    assert (running_row["last_activity_kind"], running_row["last_activity_by"]) == ("memo", "lead")

    # The MCP tool leads with the same text and the same counts.
    mcp = _mcp_wall(project_id=pid, limit=3)
    assert list(mcp)[0] == "digest"
    assert mcp["digest"].split("\n")[:3] == digest["text"].split("\n")[:3]
    assert mcp["stats"]["by_status"] == digest["by_status"] and mcp["not_shown"] == {"pending": 6}

    # The project summary counts and ranks from the digest too.
    summary = _http(api, "GET", f"/api/projects/{pid}/summary")
    assert (summary["pending_tasks"], summary["running_tasks"]) == (9, 2)
    assert [task["id"] for task in summary["top_tasks"]] == top_ids[:3]


def test_text_format_is_the_json_text(world):
    api, pid = world["api"], world["pid"]
    text = _http(api, "GET", f"/api/projects/{pid}/task-wall/digest?format=text")
    assert isinstance(text, str) and text.startswith("=== 任务墙：未关 12 条")
    assert text == _http(api, "GET", f"/api/projects/{pid}/task-wall/digest?format=json")["text"]


def test_briefing_carries_the_server_text_byte_for_byte(world):
    api, pid = world["api"], world["pid"]
    before = _http(api, "GET", f"/api/projects/{pid}/task-wall/digest?format=text")
    context = _bootstrap(api, world["home"], world["work"])
    after = _http(api, "GET", f"/api/projects/{pid}/task-wall/digest?format=text")
    assert before in context or after in context, context
    for retired in ("任务墙Top5", "统计: 总", "=== 进行中任务 ==="):
        assert retired not in context


def test_only_status_changes_and_work_memos_move_a_task(world):
    """Persisted through the API, read back by a fresh request: title edits and the
    entity-less status_changed stream do not count; a status change and a memo do."""
    api, pid, database = world["api"], world["pid"], world["database"]
    task = world["add"]("aged task")
    con = sqlite3.connect(database)
    with con:
        con.execute("UPDATE tasks SET created_at = datetime('now', '-10 days') WHERE id = ?", (task,))
        # task.status_changed carries no entity id in production; even with one it is not a source.
        con.execute(
            "INSERT INTO events (id, type, source, data, timestamp, entity_id, entity_type, state_snapshot) "
            "VALUES ('evt-sc', 'task.status_changed', 'test', '{}', datetime('now'), ?, 'task', ?)",
            (task, json.dumps({"status": "running"})),
        )
    con.close()

    def row() -> dict:
        wall = _http(api, "GET", f"/api/projects/{pid}/task-wall?limit=100")
        return next(r for bucket in wall["wall"].values() for r in bucket if r["id"] == task)

    _http(api, "PUT", f"/api/tasks/{task}", {"title": "aged task, renamed"})
    renamed = row()
    assert renamed["last_activity_kind"] == "created" and round(renamed["idle_days"]) == 10

    _http(api, "PUT", f"/api/tasks/{task}", {"status": "running"})
    started = row()
    assert started["last_activity_kind"] == "started" and started["idle_days"] < 0.01

    _http(api, "POST", f"/api/tasks/{task}/memo", {"content": "found it", "type": "issue", "author": "w"})
    noted = row()
    assert (noted["last_activity_kind"], noted["last_activity_memo_type"]) == ("memo", "issue")


def test_status_event_lookup_uses_the_entity_index(world):
    closed_since = datetime.now(UTC) - timedelta(days=7)
    statement = task_status_events_stmt(world["pid"], closed_since).compile(
        dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
    con = sqlite3.connect(world["database"])
    try:
        plan = " | ".join(row[-1] for row in con.execute(f"EXPLAIN QUERY PLAN {statement}"))
    finally:
        con.close()
    assert "ix_events_entity_id" in plan, plan
    assert "ix_events_type" not in plan, plan

