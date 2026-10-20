"""Task prefixes resolve only within a confirmed project, on real file SQLite."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.mcp.tools import task as task_tools
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import get_engine, get_session
from aiteam.storage.models import TaskModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import Task

UNIQUE = "12345678-1234-4567-8901-123456789abc"
COLLISION = "abcdef01-1234-4567-8901-123456789abc"
COLLISION_2 = "abcdef01-1234-4567-8901-123456789abd"
FOREIGN = "12345678-1234-4567-8901-123456789abd"
FOREIGN_ONLY = "87654321-1234-4567-8901-123456789abc"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'prefix.sqlite'}"
    repo = StorageRepository(db_url=url)
    run(repo.init_db())
    project = run(repo.create_project("当前项目", root_path=str(tmp_path / "project")))
    other = run(repo.create_project("其它项目", root_path=str(tmp_path / "other")))
    memory = MemoryStore(repository=repo)
    for name, value in {
        "_repository": repo, "_memory_store": memory, "_event_bus": EventBus(repo=repo),
        "_manager": TeamManager(repository=repo, memory=memory),
        "_project_dir_cache": {project.root_path.lower(): project.id},
    }.items():
        monkeypatch.setattr(deps, name, value)

    async def insert(task_id, project_id, title="测试任务"):
        async with get_session(url) as session:
            session.add(TaskModel.from_pydantic(Task(id=task_id, title=title, project_id=project_id)))

    for tid, pid in [(UNIQUE, project.id), (COLLISION, project.id), (COLLISION_2, project.id),
                     (FOREIGN, other.id), (FOREIGN_ONLY, other.id),
                     ("abcdef01-1234-4567-8901-123456789abe", other.id)]:
        run(insert(tid, pid))
    app = create_app()

    @asynccontextmanager
    async def lifespan(_app):
        yield

    app.router.lifespan_context = lifespan
    with TestClient(app) as client:
        headers = {"X-Project-Id": project.id}

        def api_call(method, path, payload=None):
            response = client.request(method, path, json=payload, headers=headers)
            return response.json()

        monkeypatch.setattr(task_tools, "_api_call", api_call)
        mcp = FastMCP("task-prefix-test")
        task_tools.register(mcp)
        yield {"client": client, "headers": headers, "repo": repo, "pid": project.id,
               "insert": insert, "project_dir": project.root_path, "url": url, "mcp": mcp}
    run(get_engine(url).dispose())


def mcp_call(world, name, **args):
    result = run(world["mcp"].call_tool(name, args))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


# Each target endpoint must reject the same collision before its business logic.
ENDPOINTS = [
    ("GET", "/api/tasks/{tid}", None),
    ("PUT", "/api/tasks/{tid}", {"status_note": "更新"}),
    ("PUT", "/api/tasks/{tid}/complete", {}),
    ("GET", "/api/tasks/{tid}/memo", None),
    ("POST", "/api/tasks/{tid}/memo", {"content": "记录"}),
    ("GET", "/api/tasks/{tid}/subtasks", None),
    ("GET", "/api/tasks/{tid}/execution-trace", None),
    ("GET", "/api/tasks/{tid}/replay", None),
    ("GET", "/api/tasks/{tid}/what-if", None),
    ("POST", "/api/tasks/{tid}/diagnose", None),
    ("POST", "/api/watchdog/verify/{tid}", None),
    ("GET", "/api/tasks/compare?task_id_1={tid}&task_id_2=" + UNIQUE, None),
]


@pytest.mark.parametrize("method,path,payload", ENDPOINTS,
                         ids=[path.split("?")[0] + ":" + method for method, path, _ in ENDPOINTS])
def test_all_target_endpoints_share_collision_rule(world, method, path, payload):
    response = world["client"].request(method, path.format(tid="abcdef01"),
                                        json=payload, headers=world["headers"])
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert {row["id"] for row in detail["candidates"]} == {COLLISION, COLLISION_2}
    assert detail["truncated"] is False


def test_unique_update_revision_and_memo_persist_across_requests(world):
    client, headers = world["client"], world["headers"]
    initial = client.get("/api/tasks/12345678", headers=headers)
    assert initial.status_code == 200, initial.text
    revision = initial.json()["data"]["revision"]
    assert initial.json()["data"]["id"] == UNIQUE
    changed = client.put("/api/tasks/12345678", headers=headers,
                         json={"status_note": "已持久化", "expected_revision": revision})
    assert changed.status_code == 200, changed.text
    reread = client.get(f"/api/tasks/{UNIQUE}").json()["data"]
    assert (reread["status_note"], reread["revision"]) == ("已持久化", revision + 1)
    stale = client.put("/api/tasks/12345678", headers=headers,
                       json={"title": "不应写入", "expected_revision": revision})
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_task"]["id"] == UNIQUE
    memo = client.post("/api/tasks/12345678/memo", headers=headers, json={"content": "跨请求memo"})
    assert memo.status_code == 200, memo.text
    assert [m["content"] for m in client.get(f"/api/tasks/{UNIQUE}/memo").json()["data"]] == ["跨请求memo"]
    # Fresh repository read, not an in-memory response object.
    fresh = StorageRepository(db_url=world["url"])
    assert run(fresh.get_task(UNIQUE)).title == "测试任务"
    assert [m.content for m in run(fresh.list_task_memos(UNIQUE))] == ["跨请求memo"]
    assert run(fresh.list_task_memos(FOREIGN)) == []
    trace = client.get("/api/tasks/12345678/execution-trace", headers=headers).json()["data"]
    assert trace["task"]["id"] == UNIQUE
    assert any(step["content"] == "跨请求memo" for step in trace["timeline"])
    assert client.get(f"/api/tasks/{FOREIGN}").json()["data"]["status_note"] == ""


def test_project_intersection_and_full_id_compatibility(world):
    client, headers = world["client"], world["headers"]
    assert client.get("/api/tasks/12345678", headers=headers).json()["data"]["id"] == UNIQUE
    assert client.get("/api/tasks/87654321", headers=headers).status_code == 404
    # Full-ID lookup retains its historical global behavior, even with another scope.
    assert client.get(f"/api/tasks/{FOREIGN_ONLY}", headers=headers).json()["data"]["id"] == FOREIGN_ONLY
    assert client.get("/api/tasks/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/tasks/12345678").status_code == 400
    assert client.get("/api/tasks/12345678", headers={"X-Project-Dir": "/unregistered"}).status_code == 400
    assert client.get("/api/tasks/12345678", headers={"X-Project-Id": "missing"}).status_code == 400
    directory = {"X-Project-Dir": world["project_dir"] + "/.worktrees/test"}
    assert client.get("/api/tasks/12345678", headers=directory).json()["data"]["id"] == UNIQUE
    assert client.get("/api/tasks/ABCDEF01-1234-4567-8901-123456789ABC", headers=headers).status_code == 404
    assert client.get("/api/tasks/ABCDEF01-1234-4567-8901-123456789ab", headers=headers).status_code == 409
    assert client.get("/api/tasks/12345678-", headers=headers).json()["data"]["id"] == UNIQUE


@pytest.mark.parametrize("bad", ["1234567", "abcdef0_", "abcdef0%", "abcdef0g", "123456781", "12345678--", "1" * 37],
                         ids=["too-short", "underscore", "wildcard", "non-hex",
                              "missing-hyphen", "extra-hyphen", "too-long"])
def test_invalid_prefixes_refused_without_candidate_query(world, monkeypatch, bad):
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid prefix must not reach the query")
    monkeypatch.setattr(StorageRepository, "find_task_prefix_candidates", forbidden)
    response = world["client"].get(f"/api/tasks/{bad}", headers=world["headers"])
    assert response.status_code == 400, response.text


def test_bounded_candidates_never_include_foreign_tasks(world):
    for index in range(25):
        run(world["insert"](f"aaaaaaaa-0000-0000-0000-{index:012x}", world["pid"]))
    response = world["client"].get("/api/tasks/aaaaaaaa", headers=world["headers"])
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert len(detail["candidates"]) == 20 and detail["truncated"] is True
    assert all(row["id"].startswith("aaaaaaaa-") for row in detail["candidates"])
    unscoped = world["repo"]
    with pytest.raises(ValueError, match="项目上下文"):
        run(unscoped.find_task_prefix_candidates("aaaaaaaa"))


def test_mcp_status_update_and_memo_use_same_http_resolution(world):
    assert mcp_call(world, "task_status", task_id="12345678")["data"]["id"] == UNIQUE
    changed = mcp_call(world, "task_update", task_id="12345678", status_note="MCP持久化")
    assert changed["data"]["id"] == UNIQUE
    assert mcp_call(world, "task_memo_add", task_id="12345678", content="MCP memo")["success"] is True
    assert mcp_call(world, "task_memo_read", task_id="12345678")["data"][0]["content"] == "MCP memo"
    assert world["client"].get(f"/api/tasks/{UNIQUE}").json()["data"]["status_note"] == "MCP持久化"


@pytest.mark.parametrize("method,path,payload", ENDPOINTS,
                         ids=[path.split("?")[0] + ":" + method for method, path, _ in ENDPOINTS])
def test_all_target_endpoints_accept_unique_prefix_and_reject_unscoped(world, method, path, payload):
    client = world["client"]
    target = path.format(tid="12345678")
    assert client.request(method, target, json=payload).status_code == 400
    response = client.request(method, target, json=payload, headers=world["headers"])
    # What-if needs a team, independently of task-ID resolution.
    expected = 400 if "/what-if" in target else 200
    assert response.status_code == expected, response.text
    if "/what-if" in target:
        assert "team_id" in response.json()["detail"]


def test_prefix_repository_query_is_bounded_and_uses_id_index(world):
    from sqlalchemy import event, text

    engine = get_engine(world["url"])
    statements = []

    def record(_connection, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        scoped = StorageRepository(db_url=world["url"], project_scope=world["pid"])
        candidates = run(scoped.find_task_prefix_candidates("12345678"))
        assert [item["id"] for item in candidates] == [UNIQUE]
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert len(statements) == 1
    statement, parameters = statements[0]
    assert "LIMIT" in statement.upper() and 21 in parameters

    async def explain():
        async with engine.connect() as connection:
            result = await connection.execute(text(
                "EXPLAIN QUERY PLAN SELECT id, title FROM tasks "
                "WHERE project_id=:pid AND id>=:lo AND id<:hi ORDER BY id LIMIT 21"
            ), {"pid": world["pid"], "lo": "12345678", "hi": "12345679"})
            return result.all()

    assert any("SEARCH tasks USING INDEX" in row[3] for row in run(explain()))
