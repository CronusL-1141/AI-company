"""Memory reconcile guards end to end: MCP tools -> real uvicorn -> SQLite file.

Before 2026-09-28 a session bound to project X could, through the stock MCP tools,
invalidate, merge into and score project Y's task memos, and invalidate global,
user and project-Y direction entries. Separately, sessions reconciling the same
project at once each wrote their own summary of the same memos (2 sessions gave
2 summaries, 16 gave 4).

The harness is production code end to end: the MCP tools called through FastMCP
with the session bound to project X (as the MCP server binds it at startup), a
real uvicorn with the full middleware stack on a temporary SQLite file, and every
assertion read back over a separate sqlite3 connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from aiteam.api import app as app_module
from aiteam.api import debug_log, deps
from aiteam.api import event_bus as event_bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.ws.manager import ConnectionManager
from aiteam.mcp import _base as mcp_base
from aiteam.mcp.tools import memory as memory_tools
from aiteam.mcp.tools import project as project_tools
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository
from tests.unit.api.test_hook_ingest_preread import hook_server  # noqa: F401 - pytest fixture

SESSIONS = 48  # the depth at which the lock-free read-modify-write reliably breaks


def _http(api: str, method: str, path: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(api + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def _call(mcp: FastMCP, name: str, args: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, args))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _rows(database, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _project(api: str, name: str) -> tuple[str, str, list[str]]:
    """A project with one task and two near-duplicate memos (one candidate group)."""
    _, project = _http(api, "POST", "/api/projects", {"name": name, "root_path": f"/tmp/guard-e2e-{name}"})
    pid = project["data"]["id"]
    _, task = _http(api, "POST", f"/api/projects/{pid}/tasks", {"title": f"{name} task"})
    tid = task["data"]["id"]
    memos = []
    for text in (f"{name} 部署 API 到生产环境使用 docker compose 命令",
                 f"{name} 生产环境部署 API 用 docker compose 命令启动"):
        _, memo = _http(api, "POST", f"/api/tasks/{tid}/memo", {"content": text}, {"X-Project-Id": pid})
        memos.append(memo["data"]["id"])
    return pid, tid, memos


@pytest.fixture()
def world(hook_server, monkeypatch):  # noqa: F811
    port, database, _ = hook_server
    api = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("AITEAM_API_URL", api)
    x, _, x_memos = _project(api, "X")
    y, y_task, y_memos = _project(api, "Y")
    direction = {}
    for scope, content in (("global", "守卫 e2e 全局纪律：所有输出使用中文"),
                           ("user", "守卫 e2e 用户偏好：先给结论"),
                           ("project", "守卫 e2e Y 项目方向：只读生产库")):
        _, body = _http(api, "POST", "/api/memories",
                        {"content": content, "kind": "constraint", "scope": scope}, {"X-Project-Id": y})
        direction[scope] = body["data"]["id"]
    # The session under test is bound to project X, the way the MCP server binds itself.
    monkeypatch.setattr(mcp_base, "PROJECT_DIR", "")
    monkeypatch.setattr(mcp_base, "_session_project_id", x)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "guard-e2e-session-x")
    mcp = FastMCP("reconcile-guards")
    memory_tools.register(mcp)
    return {"api": api, "database": database, "mcp": mcp, "x": x, "x_memos": x_memos,
            "y": y, "y_task": y_task, "y_memos": y_memos, "direction": direction}


def test_a_session_in_one_project_cannot_touch_another_projects_memory(world):
    mcp, database = world["mcp"], world["database"]
    y0, y1 = world["y_memos"]

    candidates = _call(mcp, "memory_reconcile_candidates", {})
    assert candidates["success"] is True, candidates
    applied = _call(mcp, "memory_reconcile_apply", {"operations": [
        {"op": "invalidate", "memo_ids": [y0]},
        {"op": "merge", "content": "X 会话写出的 Y 摘要", "memo_ids": [y1]},
        {"op": "score", "memo_id": y1, "quality_score": 1, "reason": "from X"},
    ]})
    by_match = _call(mcp, "memory_invalidate", {"content_match": "守卫 e2e 全局纪律"})
    user_by_id = _call(mcp, "memory_invalidate", {"memory_id": world["direction"]["user"]})
    y_entry_by_id = _call(mcp, "memory_invalidate", {"memory_id": world["direction"]["project"],
                                                     "confirm_shared_scope": True})

    # Damage first: read the file back on a connection of our own.
    y_rows = _rows(database, "SELECT id, invalid_at, quality_score, author FROM task_memos "
                             "WHERE task_id = ? ORDER BY created_at", (world["y_task"],))
    assert [(r[0], r[1], r[2], r[3]) for r in y_rows] == [
        (y0, None, None, "leader"), (y1, None, None, "leader")], y_rows
    invalid = _rows(database, "SELECT scope FROM memories WHERE invalid_at IS NOT NULL")
    assert invalid == [], f"direction entries invalidated from project X: {invalid}"

    assert [r["status"] for r in applied["data"]["results"]] == ["error"] * 3, applied
    assert by_match["success"] is False and by_match["requires_confirmation"] is True
    assert user_by_id["success"] is False and user_by_id["requires_confirmation"] is True
    assert y_entry_by_id["success"] is False and "HTTP 404" in y_entry_by_id["error"]


def test_confirmed_shared_invalidation_and_own_project_reconcile_go_through(world):
    mcp, database = world["mcp"], world["database"]

    confirmed = _call(mcp, "memory_invalidate", {"content_match": "守卫 e2e 全局纪律",
                                                 "confirm_shared_scope": True})
    assert confirmed["success"] is True, confirmed
    assert _rows(database, "SELECT invalid_at IS NOT NULL FROM memories WHERE id = ?",
                 (world["direction"]["global"],)) == [(1,)]

    lease = _call(mcp, "memory_reconcile_candidates", {})["data"]["reconcile_lease"]
    merged = _call(mcp, "memory_reconcile_apply", {
        "operations": [{"op": "merge", "content": "X 部署摘要", "memo_ids": world["x_memos"]}],
        "lease_id": lease["lease_id"],
    })
    assert merged["data"]["results"][0]["status"] == "applied", merged
    assert merged["data"]["reconcile_lease"] == {"status": "released"}
    summary = merged["data"]["results"][0]["new_memo_id"]
    assert _rows(database, "SELECT project_id, invalid_at FROM task_memos WHERE id = ?",
                 (summary,)) == [(world["x"], None)]
    placeholders = ",".join("?" * len(world["x_memos"]))
    assert _rows(database, f"SELECT invalidated_by FROM task_memos WHERE id IN ({placeholders})",
                 tuple(world["x_memos"])) == [(summary,), (summary,)]
    lease_left = _rows(database, "SELECT json_extract(config, '$.memory.reconcile_lease') "
                                 "FROM projects WHERE id = ?", (world["x"],))
    assert lease_left == [(None,)]


def test_superseding_a_global_entry_through_memory_add_needs_confirmation(world):
    """memory_add(supersedes=<global id>) swaps text every project inherits: same gate as invalidate."""
    mcp, database = world["mcp"], world["database"]
    old_id = world["direction"]["global"]

    refused = _call(mcp, "memory_add", {"content": "放宽版全局纪律", "kind": "constraint",
                                        "scope": "global", "supersedes": old_id})
    assert _rows(database, "SELECT invalid_at FROM memories WHERE id = ?", (old_id,)) == [(None,)]
    assert _rows(database, "SELECT count(*) FROM memories WHERE content = ?", ("放宽版全局纪律",)) == [(0,)]
    assert refused["success"] is False and refused["requires_confirmation"] is True, refused
    assert refused["target"]["id"] == old_id and refused["replacement"] == "放宽版全局纪律"

    confirmed = _call(mcp, "memory_add", {"content": "放宽版全局纪律", "kind": "constraint",
                                          "scope": "global", "supersedes": old_id,
                                          "confirm_shared_scope": True})
    assert confirmed["success"] is True, confirmed
    new_id = confirmed["data"]["id"]
    assert _rows(database, "SELECT invalidated_by FROM memories WHERE id = ?", (old_id,)) == [(new_id,)]
    assert _rows(database, "SELECT invalid_at FROM memories WHERE id = ?", (new_id,)) == [(None,)]


def test_a_session_without_cc_session_id_reconciles_by_lease_id(world, monkeypatch):
    """A host that sends no session header (Codex) is recognised by the lease_id alone."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    mcp, database = world["mcp"], world["database"]
    first = _call(mcp, "memory_reconcile_candidates", {})["data"]["reconcile_lease"]
    ops = [{"op": "invalidate", "memo_ids": world["x_memos"][:1]}]

    assert _call(mcp, "memory_reconcile_candidates", {})["success"] is False
    renewed = _call(mcp, "memory_reconcile_candidates", {"lease_id": first["lease_id"]})
    assert renewed["data"]["reconcile_lease"]["status"] == "renewed"
    refused = _call(mcp, "memory_reconcile_apply", {"operations": ops})
    assert refused["success"] is False
    assert _rows(database, "SELECT invalid_at FROM task_memos WHERE id = ?", (world["x_memos"][0],)) == [(None,)]

    applied = _call(mcp, "memory_reconcile_apply", {"operations": ops, "lease_id": first["lease_id"]})
    assert applied["data"]["results"][0]["status"] == "applied", applied
    assert _rows(database, "SELECT invalid_at IS NOT NULL FROM task_memos WHERE id = ?",
                 (world["x_memos"][0],)) == [(1,)]


def _strings(value) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


def test_a_blocked_session_cannot_lift_the_lease_from_project_list(world, monkeypatch):
    """Review 1f62451a: a refused session read lease_id from project_list and applied as the holder."""
    mcp, database = world["mcp"], world["database"]
    lease = _call(mcp, "memory_reconcile_candidates", {})["data"]["reconcile_lease"]
    assert lease["status"] == "acquired" and lease["lease_id"]

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "guard-e2e-intruder")
    tools = FastMCP("reconcile-guards-projects")
    project_tools.register(tools)
    assert _call(mcp, "memory_reconcile_candidates", {})["success"] is False
    listed = _call(tools, "project_list", {})
    dumped = json.dumps(listed, ensure_ascii=False)
    assert lease["lease_id"] not in dumped
    assert "guard-e2e-session-x" not in dumped

    ops = [{"op": "invalidate", "memo_ids": world["x_memos"]}]
    for candidate in {s for s in _strings(listed) if s}:
        forged = _call(mcp, "memory_reconcile_apply", {"operations": ops, "lease_id": candidate})
        assert forged["success"] is False, candidate
    placeholders = ",".join("?" * len(world["x_memos"]))
    assert _rows(database, f"SELECT count(*) FROM task_memos WHERE id IN ({placeholders}) "
                           "AND invalid_at IS NULL", tuple(world["x_memos"])) == [(len(world["x_memos"]),)]
    stored = _rows(database, "SELECT json_extract(config, '$.memory.reconcile_lease') FROM projects WHERE id = ?",
                   (world["x"],))[0][0]
    assert lease["lease_id"] not in stored and "guard-e2e-session-x" not in stored


def test_concurrent_sessions_get_one_lease_and_write_one_summary(world):
    api, database, pid = world["api"], world["database"], world["x"]
    sessions = [f"guard-concurrent-{i:02d}" for i in range(SESSIONS)]
    looked: dict[str, dict] = {}
    applied: dict[str, dict] = {}
    look_gate, apply_gate = threading.Barrier(SESSIONS), threading.Barrier(SESSIONS)

    warm_gate = threading.Barrier(SESSIONS)

    def session(sid: str) -> None:
        headers = {"X-Project-Id": pid, "X-CC-Session-Id": sid}
        # A running API has a warm connection pool; a cold one lets the first claimant
        # commit before anyone else has a connection, and the race never opens.
        warm_gate.wait()
        _http(api, "GET", "/api/memories", headers=headers)
        look_gate.wait()
        looked[sid] = _http(api, "GET", "/api/memory/reconcile/candidates", headers=headers)[1]
        data = looked[sid].get("data") or {}
        ids = [m["id"] for g in data.get("candidate_groups", []) for m in g["members"]] or world["x_memos"]
        apply_gate.wait()
        applied[sid] = _http(api, "POST", "/api/memory/reconcile/apply",
                             {"operations": [{"op": "merge", "content": f"{sid} 的摘要", "memo_ids": ids}]},
                             headers)[1]

    threads = [threading.Thread(target=session, args=(sid,)) for sid in sessions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)

    summaries = _rows(database, "SELECT content FROM task_memos WHERE author = 'reconcile' "
                                "AND project_id = ? AND invalid_at IS NULL", (pid,))
    assert len(summaries) == 1, f"{len(summaries)} competing summaries: {summaries[:4]}"
    granted = [sid for sid, body in looked.items() if body.get("success")]
    assert len(granted) == 1, f"{len(granted)} sessions were handed the lease at once"
    refused = [body for sid, body in applied.items() if sid not in granted]
    assert all(body.get("success") is False for body in refused)
    assert applied[granted[0]]["data"]["reconcile_lease"] == {"status": "released"}


@pytest.fixture()
def logged_api(tmp_path, monkeypatch):
    """A real uvicorn at log level info with production's debug.log wiring.

    hook_server runs at warning with debug.log switched off, so access lines never
    appear there. Here uvicorn writes its access lines and setup_debug_log() routes
    them into ~/.claude/data/ai-team-os/debug.log (a temporary HOME under pytest),
    the way the API does in production.
    """
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "0")
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    monkeypatch.setattr(event_bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(event_bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'logged.sqlite'}"
    app = app_module.create_app()

    @asynccontextmanager
    async def lifespan(_app):
        repo = StorageRepository(db_url=database_url)
        await repo.init_db()
        bus = EventBus(repo=repo)
        translator = HookTranslator(repo=repo, event_bus=bus)
        app.dependency_overrides.update({
            deps.get_repository: lambda: repo,
            deps.get_event_bus: lambda: bus,
            deps.get_hook_translator: lambda: translator,
        })
        monkeypatch.setattr(deps, "_repository", repo)
        monkeypatch.setattr(deps, "_event_bus", bus)
        monkeypatch.setattr(deps, "_hook_translator", translator)
        try:
            yield
        finally:
            await translator.drain(5)
            await get_engine(database_url).dispose()

    app.router.lifespan_context = lifespan
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, http="httptools", log_level="info", lifespan="on",
    ))
    # After Config: its dictConfig resets the uvicorn loggers' handlers, as it does
    # before the API factory runs setup_debug_log() in production.
    log_file = debug_log.setup_debug_log()
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}", Path(log_file)
    finally:
        server.should_exit = True
        thread.join(10)


def test_a_lease_id_passed_back_never_reaches_the_access_log(logged_api, monkeypatch):
    """Review fc2f61c6: a lease_id in the candidates query string was written to debug.log."""
    api, log_file = logged_api
    monkeypatch.setenv("AITEAM_API_URL", api)
    _, project = _http(api, "POST", "/api/projects", {"name": "logged", "root_path": "/tmp/guard-logged"})
    pid = project["data"]["id"]
    # A caller with neither a CC session nor an HTTP MCP connection: the lease_id is its only proof.
    monkeypatch.setattr(mcp_base, "PROJECT_DIR", "")
    monkeypatch.setattr(mcp_base, "_session_project_id", pid)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    mcp = FastMCP("reconcile-guards-logged")
    memory_tools.register(mcp)

    lease_id = _call(mcp, "memory_reconcile_candidates", {})["data"]["reconcile_lease"]["lease_id"]
    marker = f"/marker-{uuid.uuid4().hex}"
    renewed = _call(mcp, "memory_reconcile_candidates", {"lease_id": lease_id, "scope_path": marker})
    assert renewed["data"]["reconcile_lease"]["status"] == "renewed", renewed
    released = _call(mcp, "memory_reconcile_apply", {"operations": [], "lease_id": lease_id})
    assert released["data"]["reconcile_lease"] == {"status": "released"}, released

    for handler in logging.getLogger("uvicorn.access").handlers:
        handler.flush()
    logged = "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(log_file.parent.glob(log_file.name + "*"))
    )
    # The renewal's access line is there (so the log really records query strings) ...
    assert marker.replace("/", "%2F") in logged or marker in logged, "access line for the renewal missing"
    # ... and the credential is not.
    assert lease_id not in logged
