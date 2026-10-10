"""真实隔离 SQLite 上验证父子创建、持久化读回和任务墙投影。"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.loop.task_wall_engine import TaskWallEngine
from aiteam.mcp.tools import task as task_tools
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'parents.sqlite'}"
    repo = StorageRepository(db_url=database_url)
    _run(repo.init_db())
    memory = MemoryStore(repository=repo)
    bus = EventBus(repo=repo)
    translator = HookTranslator(repo=repo, event_bus=bus)
    for name, value in {
        "_repository": repo, "_memory_store": memory, "_event_bus": bus,
        "_manager": TeamManager(repository=repo, memory=memory),
        "_task_wall_engine": TaskWallEngine(repo=repo), "_hook_translator": translator,
    }.items():
        monkeypatch.setattr(deps, name, value)
    app = create_app()

    @asynccontextmanager
    async def lifespan(_app):
        yield

    app.router.lifespan_context = lifespan
    with TestClient(app) as client:
        project = _run(repo.create_project("目标项目", root_path=str(tmp_path / "project")))
        other = _run(repo.create_project("其它项目", root_path=str(tmp_path / "other")))
        team = _run(repo.create_team("一队", "coordinate", project_id=project.id))
        other_team = _run(repo.create_team("二队", "coordinate", project_id=project.id))
        unbound = _run(repo.create_team("未绑定队", "coordinate"))

        def api_call(method, path, payload=None):
            # MCP transport替身只转发HTTP，不替代任何生产请求校验。
            response = client.request(method, path, json=payload)
            result = response.json()
            if response.status_code >= 400:
                # Exact native _api_call HTTP error envelope, not a friendlier stub.
                from aiteam.mcp import _base
                recovery = _base.get_http_recovery(response.status_code)
                category = _base.get_business_recovery(response.text) or recovery.get("category", "unknown")
                return {"success": False, "error": f"HTTP {response.status_code}: {response.reason_phrase}",
                        "detail": response.text,
                        "_error_category": category,
                        "_recovery": recovery.get("recovery", "")}
            return result

        monkeypatch.setattr(task_tools, "_api_call", api_call)
        monkeypatch.setattr(task_tools, "_resolve_project_id", lambda pid: pid or project.id)
        mcp = FastMCP("parent-creation-test")
        task_tools.register(mcp)
        yield {
            "repo": repo, "client": client, "pid": project.id, "other_pid": other.id,
            "team": team.id, "other_team": other_team.id, "unbound": unbound.id, "mcp": mcp,
        }
    _run(translator.drain(5))
    _run(get_engine(database_url).dispose())


def _mcp(world, name, **arguments):
    result = _run(world["mcp"].call_tool(name, arguments))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _create(world, entry, title="任务", **extra):
    if entry == "project-http":
        return world["client"].post(
            f"/api/projects/{world['pid']}/tasks", json={"title": title, **extra},
        )
    return world["client"].post(
        f"/api/teams/{world['team']}/tasks/run",
        json={"title": title, "description": "任务说明", **extra},
    )


def _rows(wall):
    return [row for bucket in wall["wall"].values() for row in bucket]


@pytest.mark.parametrize("entry", ["project-http", "team-http", "task_create", "task_run"])
def test_new_entry_persists_parent_and_reads_same_wall(world, entry):
    parent = _create(world, "project-http", "父任务").json()["data"]
    if entry in ("task_create", "task_run"):
        arguments = {"title": "子任务", "parent_id": parent["id"]}
        if entry == "task_run":
            arguments.update(team_id=world["team"], description="任务说明")
        child = _mcp(world, entry, **arguments)["data"]
    else:
        response = _create(world, entry, "子任务", parent_id=parent["id"])
        assert response.status_code == (201 if entry == "project-http" else 200), response.text
        child = response.json()["data"]
    assert (child["parent_id"], child["depth"], child["project_id"]) == (parent["id"], 1, world["pid"])
    # Separate requests read the persisted row, relationship endpoint and both views.
    detail = world["client"].get(f"/api/tasks/{child['id']}").json()["data"]
    assert detail["parent_id"] == parent["id"] and detail["depth"] == 1
    subtasks = world["client"].get(f"/api/tasks/{parent['id']}/subtasks").json()["data"]
    assert subtasks["total"] == 1 and subtasks["subtasks"][0]["id"] == child["id"]
    replay = world["client"].get(f"/api/tasks/{parent['id']}/replay").json()["data"]
    assert replay["stats"]["total_subtasks"] == 1
    assert {step["task_id"] for step in replay["timeline"] if step["source"] == "subtask"} == {child["id"]}
    wall = world["client"].get(f"/api/projects/{world['pid']}/task-wall").json()
    assert [row["id"] for row in _rows(wall)] == [parent["id"]]
    assert _rows(wall)[0]["subtasks"][0]["id"] == child["id"]
    assert _rows(wall)[0]["subtask_progress"] == {"completed": 0, "total": 1}
    assert _rows(_mcp(world, "task_list_project", project_id=world["pid"]))[0]["subtask_count"] == 1
    assert detail["depends_on"] == [] and detail["assigned_to"] is None and detail["status"] == "pending"
    assert world["client"].get(f"/api/tasks/{parent['id']}").json()["data"]["status"] == "pending"


@pytest.mark.parametrize("entry", ["project-http", "team-http", "task_create", "task_run"])
def test_default_request_remains_top_level(world, entry):
    if entry in ("task_create", "task_run"):
        args = {"title": "旧请求", "tags": ["parent:historical-tag"]}
        if entry == "task_run":
            args.update(team_id=world["team"], description="旧调用")
        data = _mcp(world, entry, **args)["data"]
    else:
        data = _create(world, entry, "旧请求", tags=["parent:historical-tag"]).json()["data"]
    detail = world["client"].get(f"/api/tasks/{data['id']}").json()["data"]
    assert detail["parent_id"] is None and detail["depth"] == 0
    assert detail["tags"] == ["parent:historical-tag"]
    assert detail["horizon"] == ("short" if entry in ("team-http", "task_run") else "mid")


@pytest.mark.parametrize("entry", ["project-http", "team-http"])
@pytest.mark.parametrize("invalid", ["missing", "other-project", "unowned", "child", "depth", "completed"])
def test_parent_validation_rejects_without_creating(world, entry, invalid):
    repo = world["repo"]
    if invalid == "missing":
        parent_id = "does-not-exist"
    else:
        kwargs = {"project_id": world["pid"]}
        if invalid == "other-project":
            kwargs["project_id"] = world["other_pid"]
        elif invalid == "unowned":
            kwargs["project_id"] = None
        elif invalid == "child":
            kwargs.update(parent_id="legacy-parent", depth=1)
        elif invalid == "depth":
            kwargs["depth"] = 1
        elif invalid == "completed":
            kwargs["status"] = "completed"
        parent_id = _run(repo.create_task(None, "不合法父任务", **kwargs)).id
    before = _run(repo.list_tasks_by_project(world["pid"]))
    response = _create(world, entry, parent_id=parent_id)
    assert response.status_code == 400, response.text
    assert [t.id for t in _run(repo.list_tasks_by_project(world["pid"]))] == [t.id for t in before]


def test_team_owner_wins_over_request_scope_and_unbound_cannot_adopt_parent(world):
    client = world["client"]
    parent = _create(world, "project-http", "父任务").json()["data"]
    wrong_parent = _run(world["repo"].create_task(None, "错误项目父", project_id=world["other_pid"]))
    response = client.post(
        f"/api/teams/{world['team']}/tasks/run", headers={"X-Project-Id": world["other_pid"]},
        json={"description": "跨作用域", "parent_id": wrong_parent.id},
    )
    assert response.status_code == 400
    response = client.post(
        f"/api/teams/{world['team']}/tasks/run", headers={"X-Project-Id": world["other_pid"]},
        json={"description": "结构归属", "parent_id": parent["id"]},
    )
    assert response.status_code == 200 and response.json()["data"]["project_id"] == world["pid"]
    response = client.post(
        f"/api/teams/{world['unbound']}/tasks/run", headers={"X-Project-Id": world["pid"]},
        json={"description": "无归属", "parent_id": parent["id"]},
    )
    assert response.status_code == 400
    # Existing unbound, parentless calls still use the original scope fallback.
    old = client.post(f"/api/teams/{world['unbound']}/tasks/run", json={"description": "旧无归属"})
    assert old.status_code == 200 and old.json()["data"]["project_id"] is None


def test_team_parent_includes_project_and_other_team_children(world):
    client = world["client"]
    parent = _create(world, "team-http", "团队父任务").json()["data"]
    project_child = _create(world, "project-http", "项目子任务", parent_id=parent["id"]).json()["data"]
    team_child = client.post(
        f"/api/teams/{world['other_team']}/tasks/run",
        json={"description": "跨队子任务", "parent_id": parent["id"]},
    ).json()["data"]
    _create(world, "project-http", "其它项目级顶层任务")
    expected = {project_child["id"], team_child["id"]}
    subtasks = client.get(f"/api/tasks/{parent['id']}/subtasks").json()["data"]
    assert {row["id"] for row in subtasks["subtasks"]} == expected
    replay = client.get(f"/api/tasks/{parent['id']}/replay").json()["data"]
    assert replay["stats"]["total_subtasks"] == 2
    assert {step["task_id"] for step in replay["timeline"] if step["source"] == "subtask"} == expected
    for path in (f"/api/projects/{world['pid']}/task-wall", f"/api/teams/{world['team']}/task-wall"):
        rows = _rows(client.get(path).json())
        row = next(r for r in rows if r["id"] == parent["id"])
        assert {c["id"] for c in row["subtasks"]} == expected
        if "/teams/" in path:
            assert [r["id"] for r in rows] == [parent["id"]]
    compact = _mcp(world, "task_list_project", team_id=world["team"])
    assert _rows(compact)[0]["subtask_count"] == 2


def test_status_filters_parent_cards_without_losing_other_status_children(world):
    parent = _create(world, "project-http", "父任务", status="running").json()["data"]
    pending = _create(world, "project-http", "待办子", parent_id=parent["id"]).json()["data"]
    closed = _create(world, "project-http", "已完成子", parent_id=parent["id"], status="completed").json()["data"]
    _create(world, "project-http", "另一待办父任务")
    client = world["client"]
    wall = client.get(f"/api/projects/{world['pid']}/task-wall", params={"status": "running"}).json()
    assert [row["id"] for row in _rows(wall)] == [parent["id"]]
    assert {row["id"] for row in _rows(wall)[0]["subtasks"]} == {pending["id"], closed["id"]}
    assert _rows(_mcp(world, "task_list_project", project_id=world["pid"], status="running"))[0]["subtask_count"] == 2
    assert wall["stats"]["total"] == 2
    # Completing the parent is still explicit; existing children remain readable.
    assert client.put(f"/api/tasks/{parent['id']}", json={"status": "completed"}).status_code == 409
    assert client.put(f"/api/tasks/{pending['id']}", json={"status": "completed"}).status_code == 200
    assert client.put(f"/api/tasks/{parent['id']}", json={"status": "completed"}).status_code == 200
    assert all(row["id"] != parent["id"] for row in _rows(client.get(f"/api/projects/{world['pid']}/task-wall").json()))
    completed = client.get(
        f"/api/projects/{world['pid']}/task-wall", params={"status": "completed", "include_completed": True},
    ).json()
    assert {row["id"] for row in completed["completed"][0]["subtasks"]} == {pending["id"], closed["id"]}


def test_mcp_new_parameter_is_last_and_old_positional_calls_work(world):
    create = world["mcp"]._tool_manager.get_tool("task_create").fn
    run = world["mcp"]._tool_manager.get_tool("task_run").fn
    assert list(inspect.signature(create).parameters)[8] == "parent_id"
    assert list(inspect.signature(run).parameters)[9] == "parent_id"
    old_project = create("旧位置调用", world["pid"], "说明", "high", "long", ["旧"], False, "")
    old_team = run(world["team"], "旧位置调用", "团队任务", None, [], "high", "long", ["旧"], "")
    assert old_project["data"]["parent_id"] is None and old_team["data"]["parent_id"] is None
    tools = _run(world["mcp"].list_tools())
    for tool in tools:
        if tool.name in ("task_create", "task_run"):
            assert "parent_id" in tool.inputSchema["properties"]
            assert "parent_id" not in tool.inputSchema.get("required", [])


def _get(world, task_id):
    return world["client"].get(f"/api/tasks/{task_id}").json()["data"]


def _update(world, task_id, **values):
    return world["client"].put(f"/api/tasks/{task_id}", json=values)


def _complete(world, task_id):
    return world["client"].put(f"/api/tasks/{task_id}/complete")


@pytest.mark.parametrize("entry", ["project-http", "team-http", "task_create", "task_run"])
def test_dependencies_auto_start_and_common_completion(world, entry):
    first = _create(world, "project-http", "前置一").json()["data"]["id"]
    second = _create(world, "project-http", "前置二").json()["data"]["id"]
    args = {"depends_on": [first, second], "auto_start": True}
    if entry in ("task_create", "task_run"):
        if entry == "task_run":
            args.update(team_id=world["team"], description="后置")
        data = _mcp(world, entry, title="后置", **args)["data"]
    else:
        response = _create(world, entry, "后置", **args)
        assert response.is_success, response.text
        data = response.json()["data"]
    assert data["status"] == "blocked" and data["block_reason"] == "dependency"
    assert _mcp(world, "task_update", task_id=first, status="completed")["success"]
    assert _get(world, data["id"])["status"] == "blocked"
    done = _complete(world, second)
    assert done.status_code == 200 and done.json()["unblocked_tasks"][0]["id"] == data["id"]
    assert _get(world, data["id"])["status"] == "pending"
    original_time = _get(world, second)["completed_at"]
    # Simulate a missed unlock while preserving a recorded dependency reason.
    from aiteam.storage.connection import get_session
    from aiteam.storage.models import TaskModel
    async def missed_unlock_seed():
        # Explicit fault injection; internal config writers cannot mutate management.
        async with get_session(world["repo"]._db_url) as session:
            row = await session.get(TaskModel, data["id"])
            row.status = "blocked"
            row.config = {**row.config, "task_management": {
                **row.config["task_management"], "block_reason": "dependency",
            }}
    _run(missed_unlock_seed())
    assert _complete(world, second).json()["unblocked_tasks"][0]["id"] == data["id"]
    assert _get(world, second)["completed_at"] == original_time


def test_manual_waiting_and_unknown_blockers_are_not_auto_unlocked(world):
    dep = _create(world, "project-http").json()["data"]["id"]
    manual = _create(world, "project-http", status="blocked", depends_on=[dep]).json()["data"]
    waiting = _create(world, "project-http", depends_on=[dep], waiting_on={
        "kind": "user", "reason": "请确认方案", "target_id": "owner",
    }).json()["data"]
    legacy = _run(world["repo"].create_task(None, "旧阻塞", project_id=world["pid"],
                                          depends_on=[dep], status="blocked"))
    assert manual["block_reason"] == "manual" and waiting["block_reason"] == "waiting"
    result = _complete(world, dep).json()
    assert result["unblocked_tasks"] == [] and any(legacy.id in text for text in result["warnings"])
    for task_id in (manual["id"], waiting["id"], legacy.id):
        assert _get(world, task_id)["status"] == "blocked"
    assert _update(world, waiting["id"], waiting_on=None).json()["data"]["status"] == "pending"
    assert _mcp(world, "task_update", task_id=manual["id"], status="pending")["data"]["status"] == "pending"
    assert _update(world, legacy.id, description="修复说明").json()["data"]["status"] == "blocked"


@pytest.mark.parametrize("relation", ["missing", "cross", "self", "cycle", "parent-cycle"])
def test_dependency_validation_is_atomic(world, relation):
    first = _create(world, "project-http", "一").json()["data"]
    second = _create(world, "project-http", "二").json()["data"]
    if relation == "missing":
        values = {"depends_on": ["missing"]}
    elif relation == "cross":
        other = _run(world["repo"].create_task(None, "跨项目", project_id=world["other_pid"]))
        values = {"depends_on": [other.id]}
    elif relation == "self":
        values = {"depends_on": [first["id"]]}
    elif relation == "cycle":
        assert _update(world, second["id"], depends_on=[first["id"]]).status_code == 200
        values = {"depends_on": [second["id"]]}
    else:
        assert _update(world, first["id"], parent_id=second["id"]).status_code == 200
        values = {"depends_on": [second["id"]]}
    before = _get(world, first["id"])
    response = _update(world, first["id"], description="不能写入", **values)
    assert response.status_code == 400, response.text
    assert _get(world, first["id"]) == before


def test_reparent_detach_and_completed_parent_repair(world):
    parent = _create(world, "project-http", "父").json()["data"]
    child = _create(world, "project-http", "子").json()["data"]
    assert _update(world, child["id"], parent_id=parent["id"]).json()["data"]["depth"] == 1
    assert _update(world, parent["id"], parent_id=child["id"]).status_code == 400
    assert _mcp(world, "task_update", task_id=child["id"], parent_id="")["data"]["parent_id"] is None
    assert _get(world, child["id"])["depth"] == 0
    assert _update(world, child["id"], parent_id=parent["id"]).status_code == 200
    # Historical inconsistent data can still be repaired; do not trap its child.
    _run(world["repo"].update_task(parent["id"], status="completed"))
    assert _update(world, child["id"], description="补充", depends_on=[]).status_code == 200
    assert _complete(world, child["id"]).status_code == 200
    assert _update(world, child["id"], parent_id=None).status_code == 200


def test_revision_conflict_and_clear_fields_preserve_frozen_memo(world):
    frozen = [{"content": "只此一份冻结记录", "timestamp": "2026-01-01T00:00:00", "author": "old"}]
    task = _run(world["repo"].create_task(None, "原题", project_id=world["pid"],
                                         config={"memo": frozen, "custom": "keep"}, description="描述"))
    before = _get(world, task.id)
    response = _update(world, task.id, expected_revision=before["revision"], title="新题", description="",
                       result="", assigned_to=None, horizon="long", status_note="新状态说明")
    assert response.status_code == 200, response.text
    after = _get(world, task.id)
    assert after["revision"] == before["revision"] + 1
    assert after["title"] == "新题" and after["description"] == after["result"] == ""
    assert after["horizon"] == "long" and after["status_note"] == "新状态说明"
    assert after["config"]["custom"] == "keep" and after["config"]["memo"] == frozen
    conflict = _update(world, task.id, expected_revision=before["revision"], title="覆盖")
    assert conflict.status_code == 409 and conflict.json()["detail"]["current_revision"] == after["revision"]
    assert _get(world, task.id)["title"] == "新题"


def test_concurrent_revision_allows_exactly_one_writer(world):
    task = _create(world, "project-http").json()["data"]
    def write(index):
        return _update(world, task["id"], title=f"写者{index}", expected_revision=task["revision"]).status_code
    with ThreadPoolExecutor(max_workers=48) as executor:
        outcomes = list(executor.map(write, range(48)))
    assert outcomes.count(200) == 1 and outcomes.count(409) == 47
    assert _get(world, task["id"])["revision"] == task["revision"] + 1


@pytest.mark.parametrize("kind", ["user", "agent"])
def test_waiting_and_on_demand_wall_metadata(world, kind):
    agent = _run(world["repo"].create_agent(world["team"], "worker", "dev", system_prompt=""))
    task = _create(world, "project-http", "等待任务", waiting_on={
        "kind": kind, "target_id": agent.id if kind == "agent" else "owner", "reason": "需要反馈",
    }, assigned_to=agent.id).json()["data"]
    assert task["status"] == "blocked" and task["waiting_on"]["kind"] == kind
    assert _complete(world, task["id"]).status_code == 409
    memo = world["client"].post(f"/api/tasks/{task['id']}/memo",
                                json={"content": "新进展", "author": "worker", "type": "progress"})
    assert memo.status_code == 200, memo.text
    _run(world["repo"].update_agent(agent.id, status="offline"))
    row = _rows(world["client"].get(f"/api/projects/{world['pid']}/task-wall").json())[0]
    assert row["latest_progress"]["content"] == "新进展"
    assert row["executor_stopped"] is True and row["waiting_user"] == (kind == "user")
    assert row["management_hints"] and _get(world, task["id"])["status"] == "blocked"
    digest = world["client"].get(f"/api/projects/{world['pid']}/task-wall/digest").json()
    assert [r["id"] for r in digest["waiting_user"]] == ([task["id"]] if kind == "user" else [])
    if kind == "user":
        assert digest["waiting_user"][0]["waiting_on"]["reason"] == "需要反馈"
        assert "等用户 1 条" in digest["text"]
    cleared = _mcp(world, "task_update", task_id=task["id"], waiting_on={})["data"]
    assert cleared["waiting_on"] is None and cleared["status"] == "pending"


def test_batch_references_replay_conflict_and_all_or_nothing(world):
    body = {"idempotency_key": "batch-1", "tasks": [
        {"key": "p", "title": "父"}, {"key": "c", "title": "子", "parent_id": "@p"},
        {"key": "d", "title": "后置", "depends_on": ["@c"], "auto_start": True},
    ]}
    path = f"/api/projects/{world['pid']}/tasks/batch"
    first = world["client"].post(path, json=body)
    assert first.status_code == 201, first.text
    data = first.json()["data"]
    assert data["tasks"][1]["parent_id"] == data["key_map"]["p"]
    assert data["tasks"][2]["status"] == "blocked"
    second = world["client"].post(path, json=body).json()["data"]
    assert second["replayed"] and second["key_map"] == data["key_map"]
    conflict = {**body, "tasks": [{"key": "p", "title": "不同参数"}]}
    assert world["client"].post(path, json=conflict).status_code == 409
    before = {t.id for t in _run(world["repo"].list_tasks_by_project(world["pid"]))}
    bad = {"idempotency_key": "bad", "tasks": [{"key": "ok", "title": "不能部分入库"},
           {"key": "bad", "title": "错误", "depends_on": ["@missing"]}]}
    assert world["client"].post(path, json=bad).status_code == 400
    assert {t.id for t in _run(world["repo"].list_tasks_by_project(world["pid"]))} == before
    bad["tasks"][1]["depends_on"] = []
    assert world["client"].post(path, json=bad).status_code == 201


@pytest.mark.parametrize("shape", ["dependency", "parent", "mixed", "grandchild"])
def test_batch_graph_rejects_cycles_and_grandchildren(world, shape):
    tasks = [{"key": "a", "title": "一"}, {"key": "b", "title": "二"}]
    if shape == "dependency":
        tasks[0]["depends_on"], tasks[1]["depends_on"] = ["@b"], ["@a"]
    elif shape == "parent":
        tasks[0]["parent_id"], tasks[1]["parent_id"] = "@b", "@a"
    elif shape == "mixed":
        tasks[1].update(parent_id="@a", depends_on=["@a"])
    else:
        tasks[1]["parent_id"] = "@a"
        tasks.append({"key": "c", "title": "孙", "parent_id": "@b"})
    result = _mcp(world, "task_create_batch", tasks=tasks, idempotency_key=f"bad-{shape}")
    assert result["success"] is False
    assert _run(world["repo"].list_tasks_by_project(world["pid"])) == []


def test_compact_writes_keep_status_relations_and_revision(world):
    parent = _mcp(world, "task_create", title="父")["data"]
    result = _mcp(world, "task_create", title="子", parent_id=parent["id"], fields="compact")
    assert result["view"] == "compact"
    assert result["data"]["parent_id"] == parent["id"] and result["data"]["revision"] == 1
    assert "config" not in result["data"]
    before = len(_run(world["repo"].list_tasks_by_project(world["pid"])))
    assert _mcp(world, "task_create", title="拒绝", fields="wrong")["success"] is False
    assert len(_run(world["repo"].list_tasks_by_project(world["pid"]))) == before


def test_running_and_completion_audit_are_visible_in_digest(world):
    task = _create(world, "project-http").json()["data"]
    first = _update(world, task["id"], status="running").json()["data"]
    assert _update(world, task["id"], status="running").json()["data"]["started_at"] == first["started_at"]
    events = _run(world["repo"].list_events(entity_id=task["id"], type_prefix="task.updated"))
    assert events and all(e.state_snapshot["status"] == "running" for e in events)
    digest = world["client"].get(f"/api/projects/{world['pid']}/task-wall/digest").json()
    assert digest["recent"][0]["activity_kind"] == "started"
    completed = _complete(world, task["id"]).json()["data"]
    assert _complete(world, task["id"]).json()["data"]["completed_at"] == completed["completed_at"]
    digest = world["client"].get(f"/api/projects/{world['pid']}/task-wall/digest").json()
    assert digest["closed_7d"] == 1


def test_internal_updates_invalidate_api_revision_and_preserve_other_fields(world):
    task = _create(world, "project-http", "内部更新").json()["data"]
    _run(world["repo"].update_task(task["id"], description="hook写入"))
    assert _update(world, task["id"], expected_revision=task["revision"], status_note="旧视图").status_code == 409
    async def updates():
        await asyncio.gather(
            world["repo"].update_task(task["id"], description="描述写者"),
            world["repo"].update_task(task["id"], result="结果写者"),
        )
    _run(updates())
    current = _get(world, task["id"])
    assert current["description"] == "描述写者" and current["result"] == "结果写者"
    assert current["revision"] == task["revision"] + 3


def test_batch_concurrent_retries_are_single_atomic_batch(world):
    body = {"idempotency_key": "concurrent-batch", "tasks": [
        {"key": "p", "title": "父"}, {"key": "c", "title": "子", "parent_id": "@p"},
    ]}
    def create(_):
        response = world["client"].post(f"/api/projects/{world['pid']}/tasks/batch", json=body)
        assert response.status_code == 201, response.text
        return response.json()["data"]
    with ThreadPoolExecutor(max_workers=24) as executor:
        replies = list(executor.map(create, range(24)))
    assert sum(not reply["replayed"] for reply in replies) == 1
    assert all(reply["key_map"] == replies[0]["key_map"] for reply in replies)
    assert len(_run(world["repo"].list_tasks_by_project(world["pid"]))) == 2


def test_waiting_clear_keeps_incomplete_dependency_and_invalid_agents_rejected(world):
    dependency = _create(world, "project-http").json()["data"]["id"]
    waiting = _create(world, "project-http", depends_on=[dependency], waiting_on={
        "kind": "user", "reason": "需要确认",
    }).json()["data"]
    updated = _update(world, waiting["id"], waiting_on=None).json()["data"]
    assert updated["status"] == "blocked" and updated["block_reason"] == "dependency"
    assert _complete(world, dependency).json()["unblocked_tasks"][0]["id"] == waiting["id"]
    before = _get(world, waiting["id"])
    wrong_team = _run(world["repo"].create_team("其它项目队", "coordinate", project_id=world["other_pid"]))
    wrong_agent = _run(world["repo"].create_agent(wrong_team.id, "foreign", "dev", system_prompt=""))
    for target in ("missing", wrong_agent.id):
        assert _update(world, waiting["id"], waiting_on={
            "kind": "agent", "target_id": target, "reason": "需要反馈",
        }).status_code == 400
    assert _get(world, waiting["id"]) == before


def test_batch_replay_refuses_deleted_members(world):
    body = {"idempotency_key": "deleted-member", "tasks": [{"key": "a", "title": "不能伪造回放"}]}
    path = f"/api/projects/{world['pid']}/tasks/batch"
    created = world["client"].post(path, json=body).json()["data"]
    from sqlalchemy import delete

    from aiteam.storage.connection import get_session
    from aiteam.storage.models import TaskModel
    async def remove_member():
        async with get_session(world["repo"]._db_url) as session:
            await session.execute(delete(TaskModel).where(TaskModel.id == created["tasks"][0]["id"]))
    _run(remove_member())
    response = world["client"].post(path, json=body)
    assert response.status_code == 409 and "被删除任务" in response.json()["detail"]


def test_audit_failure_rolls_back_completion_and_dependency_unlock(world, monkeypatch):
    source = _create(world, "project-http", "前置").json()["data"]
    downstream = _create(world, "project-http", "后置", depends_on=[source["id"]]).json()["data"]
    before_source, before_downstream = _get(world, source["id"]), _get(world, downstream["id"])
    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit failure injection")
    with monkeypatch.context() as context:
        context.setattr(world["repo"], "create_event", fail_audit)
        with pytest.raises(RuntimeError, match="audit failure injection"):
            _complete(world, source["id"])
    assert _get(world, source["id"]) == before_source
    assert _get(world, downstream["id"]) == before_downstream
    assert _complete(world, source["id"]).json()["unblocked_tasks"][0]["id"] == downstream["id"]


def test_completed_legacy_parent_retries_compensate_despite_open_child(world):
    parent = _create(world, "project-http", "旧完成父").json()["data"]
    child = _create(world, "project-http", "旧未关子", parent_id=parent["id"]).json()["data"]
    downstream = _create(world, "project-http", "下游", depends_on=[parent["id"]]).json()["data"]
    from aiteam.clock import utc_now
    _run(world["repo"].update_task(parent["id"], status="completed", completed_at=utc_now()))
    first_time = _get(world, parent["id"])["completed_at"]
    response = _complete(world, parent["id"])
    assert response.status_code == 200, response.text
    assert response.json()["unblocked_tasks"][0]["id"] == downstream["id"]
    assert response.json()["warnings"] and _get(world, parent["id"])["completed_at"] == first_time
    assert _get(world, child["id"])["status"] == "pending"


def test_completed_legacy_metadata_repair_does_not_reopen_or_retime(world):
    parent = _create(world, "project-http", "完成父").json()["data"]
    _create(world, "project-http", "仍待办子", parent_id=parent["id"])
    from aiteam.clock import utc_now
    _run(world["repo"].update_task(parent["id"], status="completed", completed_at=utc_now()))
    before = _get(world, parent["id"])
    response = _update(world, parent["id"], description="保留事实", result="补齐结果", status_note="历史修复")
    assert response.status_code == 200, response.text
    after = _get(world, parent["id"])
    assert after["status"] == "completed" and after["completed_at"] == before["completed_at"]
    assert after["description"] == "保留事实" and response.json()["warnings"]


@pytest.mark.parametrize("relation", ["dependency", "waiting"])
def test_completed_task_refuses_new_waiting_contradictions(world, relation):
    completed = _create(world, "project-http", "已完成", status="completed").json()["data"]
    dependency = _create(world, "project-http", "未完成依赖").json()["data"]
    patch = {"depends_on": [dependency["id"]]} if relation == "dependency" else {
        "waiting_on": {"kind": "user", "reason": "新的等待"},
    }
    before = _get(world, completed["id"])
    assert _update(world, completed["id"], **patch).status_code == 409
    assert _get(world, completed["id"]) == before


@pytest.mark.parametrize("source_waiting", [False, True])
def test_batch_final_intentions_are_independent_of_entry_order(world, source_waiting):
    outcomes = []
    for reverse in (False, True):
        tasks = [{"key": "d", "title": "后置", "depends_on": ["@s"], "auto_start": True},
                 {"key": "s", "title": "前置", "status": "completed", "auto_start": True}]
        if source_waiting:
            tasks[1]["waiting_on"] = {"kind": "user", "reason": "待确认"}
        if reverse:
            tasks.reverse()
        result = _mcp(world, "task_create_batch", tasks=tasks, idempotency_key=f"order-{source_waiting}-{reverse}")
        assert result["success"], result
        data = result["data"]
        outcomes.append({key: _get(world, task_id)["status"] for key, task_id in data["key_map"].items()})
    assert outcomes == [{"d": "blocked", "s": "blocked" if source_waiting else "running"}] * 2


def test_derived_waiting_and_dependency_status_are_audited_and_in_digest(world):
    dependency = _create(world, "project-http", "前置").json()["data"]
    task = _create(world, "project-http", "活动追踪").json()["data"]
    for patch, expected_kind in [({"waiting_on": {"kind": "user", "reason": "需回复"}}, "blocked"),
                                 ({"waiting_on": None}, "reopened"),
                                 ({"depends_on": [dependency["id"]]}, "blocked")]:
        assert _update(world, task["id"], **patch).status_code == 200
        events = _run(world["repo"].list_events(entity_id=task["id"], type_prefix="task.updated"))
        assert "status" in events[0].data["changes"]
        digest = world["client"].get(f"/api/projects/{world['pid']}/task-wall/digest").json()
        item = next(item for item in digest["recent"] if item["id"] == task["id"])
        assert item["activity_kind"] == expected_kind


def test_legacy_internal_update_initializes_revision_before_api_overwrite(world):
    task = _run(world["repo"].create_task(None, "旧任务", project_id=world["pid"], description="旧描述"))
    before = _get(world, task.id)
    assert before["revision"] == 0
    _run(world["repo"].update_task(task.id, description="新进展"))
    assert _update(world, task.id, description="旧覆盖", expected_revision=0).status_code == 409
    assert _get(world, task.id)["description"] == "新进展"


def test_editor_roundtrip_of_existing_completed_relationships_is_not_new_attachment(world):
    parent = _create(world, "project-http", "父").json()["data"]
    child = _create(world, "project-http", "子", parent_id=parent["id"]).json()["data"]
    _run(world["repo"].update_task(parent["id"], status="completed"))
    assert _update(world, child["id"], parent_id=parent["id"], description="只改描述").status_code == 200
    dependency = _create(world, "project-http", "旧未完依赖").json()["data"]
    from aiteam.clock import utc_now
    completed = _run(world["repo"].create_task(None, "旧完成矛盾", project_id=world["pid"], status="completed",
                                              depends_on=[dependency["id"]], completed_at=utc_now(), config={
        "task_management": {"waiting_on": {"kind": "user", "reason": "旧等待"}, "revision": 2},
    }))
    before = _get(world, completed.id)
    response = _update(world, completed.id, description="编辑器原关系回传", parent_id=before["parent_id"],
                       depends_on=before["depends_on"], waiting_on=before["waiting_on"],
                       expected_revision=before["revision"])
    assert response.status_code == 200, response.text
    assert response.json()["data"]["completed_at"] == before["completed_at"]


def test_batch_parent_auto_start_is_validated_by_final_intention(world):
    result = _mcp(world, "task_create_batch", tasks=[
        {"key": "child", "title": "子", "parent_id": "@parent"},
        {"key": "parent", "title": "父", "status": "completed", "auto_start": True},
    ], idempotency_key="parent-final-intention")
    assert result["success"], result
    data = result["data"]
    assert _get(world, data["key_map"]["parent"])["status"] == "running"
    assert _get(world, data["key_map"]["child"])["parent_id"] == data["key_map"]["parent"]


def test_wall_management_reads_only_displayed_active_parents(world, monkeypatch):
    running = _create(world, "team-http", "显示任务").json()["data"]["id"]
    _create(world, "project-http", "不显示期限", horizon="long")
    _create(world, "project-http", "隐藏完成", status="completed")
    calls = []
    original = StorageRepository.task_wall_management_metadata
    async def observed(self, ids):
        calls.append(set(ids))
        return await original(self, ids)
    monkeypatch.setattr(StorageRepository, "task_wall_management_metadata", observed)
    response = world["client"].get(f"/api/projects/{world['pid']}/task-wall", params={"horizon": "short"})
    assert response.status_code == 200 and calls[-1] == {running}
    response = world["client"].get(f"/api/teams/{world['team']}/task-wall")
    assert response.status_code == 200 and calls[-1] == {running}


@pytest.mark.parametrize("patch", [{"result": "继续执行"}, {"assigned_to": "worker-1"}, {"status": "running"}],
                         ids=["result", "assignment", "explicit-running"])
def test_running_fact_survives_reopened_dependency_and_unrelated_edits(world, patch):
    source = _create(world, "project-http", "前置", status="completed").json()["data"]
    task = _create(world, "project-http", "已执行", depends_on=[source["id"]], auto_start=True).json()["data"]
    assert _update(world, source["id"], status="pending").status_code == 200
    response = _update(world, task["id"], **patch)
    assert response.status_code == 200 and response.json()["warnings"]
    current = _get(world, task["id"])
    assert current["status"] == "running" and current["started_at"] == task["started_at"]
    assert _complete(world, source["id"]).json()["unblocked_tasks"] == []
    assert _get(world, task["id"])["status"] == "running"


def test_running_new_unfinished_dependency_requires_explicit_pause(world):
    source = _create(world, "project-http", "未完前置").json()["data"]
    task = _create(world, "project-http", "执行中", auto_start=True).json()["data"]
    before = _get(world, task["id"])
    response = _update(world, task["id"], depends_on=[source["id"]])
    assert response.status_code == 409 and _get(world, task["id"]) == before
    response = _update(world, task["id"], depends_on=[source["id"]], status="blocked")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "blocked" and response.json()["data"]["block_reason"] == "manual"
    assert _complete(world, source["id"]).json()["unblocked_tasks"] == []
    assert _get(world, task["id"])["status"] == "blocked"


def test_explicit_running_refuses_missing_dependencies_instead_of_silent_blocked(world):
    dependency = _create(world, "project-http").json()["data"]
    task = _create(world, "project-http", depends_on=[dependency["id"]]).json()["data"]
    before = _get(world, task["id"])
    result = _mcp(world, "task_update", task_id=task["id"], status="running")
    assert result["success"] is False and result["blocked_by"] == [dependency["id"]]
    assert _get(world, task["id"]) == before


@pytest.mark.parametrize("field", ["priority", "horizon", "title"])
@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_empty_identity_and_enums_are_rejected_without_database_change(world, field, transport):
    task = _create(world, "project-http", "不可清标题", priority="high", horizon="long").json()["data"]
    before = _get(world, task["id"])
    if transport == "http":
        assert _update(world, task["id"], **{field: ""}).status_code == 400
    else:
        assert _mcp(world, "task_update", task_id=task["id"], **{field: ""})["success"] is False
    assert _get(world, task["id"]) == before


def test_unconditional_48_writes_keep_every_revision(world):
    task = _create(world, "project-http", "无条件并发").json()["data"]
    barrier = threading.Barrier(48)
    def write(index):
        barrier.wait(timeout=20)
        response = _update(world, task["id"], status_note=f"写者{index}")
        assert response.status_code == 200, response.text
    with ThreadPoolExecutor(max_workers=48) as executor:
        list(executor.map(write, range(48)))
    assert _get(world, task["id"])["revision"] == task["revision"] + 48


def test_48_predecessors_complete_concurrently_and_unlock_once(world):
    predecessors = [_create(world, "project-http", f"前置{i}").json()["data"]["id"] for i in range(48)]
    task = _create(world, "project-http", "多前置后置", depends_on=predecessors).json()["data"]
    barrier = threading.Barrier(48)
    def complete(task_id):
        barrier.wait(timeout=20)
        response = _complete(world, task_id)
        assert response.status_code == 200, response.text
        return response.json()["unblocked_tasks"]
    with ThreadPoolExecutor(max_workers=48) as executor:
        results = list(executor.map(complete, predecessors))
    assert sum(len(result) for result in results) == 1
    after = _get(world, task["id"])
    assert after["status"] == "pending" and after["revision"] == task["revision"] + 1
    assert all(_get(world, task_id)["status"] == "completed" for task_id in predecessors)


def test_repository_48_writes_per_database_keep_revisions_and_isolate(world, tmp_path):
    second_url = f"sqlite+aiosqlite:///{tmp_path / 'second-writers.sqlite'}"
    second = StorageRepository(db_url=second_url)
    async def exercise():
        await second.init_db()
        try:
            project = await second.create_project("另一库", root_path=str(tmp_path / "second-project"))
            first_task = await world["repo"].create_managed_task(world["pid"], None, {"title": "一库"})
            second_task = await second.create_managed_task(project.id, None, {"title": "二库"})
            async def write(repo, task, index):
                await repo.update_task(task.id, description=f"{task.title}-写者-{index}")
            await asyncio.gather(*[write(repo, task, index)
                                  for repo, task in ((world["repo"], first_task), (second, second_task))
                                  for index in range(48)])
            for repo, task in ((world["repo"], first_task), (second, second_task)):
                current = await repo.get_task(task.id)
                assert current.revision == task.revision + 48
                assert current.description.startswith(task.title)
            assert await world["repo"].get_task(second_task.id) is None
            assert await second.get_task(first_task.id) is None
        finally:
            await get_engine(second_url).dispose()
    world["client"].portal.call(exercise)


@pytest.mark.parametrize("fields", ["all", "compact"])
def test_native_mcp_http_conflict_is_structured_without_shared_base_change(world, monkeypatch, fields):
    import io
    import urllib.error
    import urllib.parse

    from aiteam.mcp import _base
    task = _create(world, "project-http", "真实错误形状").json()["data"]
    assert _update(world, task["id"], description="新进展").status_code == 200
    def urlopen(request, timeout):
        parsed = urllib.parse.urlsplit(request.full_url)
        response = world["client"].request(request.method, parsed.path,
                                            json=json.loads(request.data) if request.data else None)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, response.status_code, response.reason_phrase,
                                         response.headers, io.BytesIO(response.content))
        stream = io.BytesIO(response.content)
        stream.headers, stream.status = response.headers, response.status_code
        stream.geturl = lambda: request.full_url
        return stream
    monkeypatch.setattr(_base, "_get_api_url", lambda: "http://isolated.invalid")
    monkeypatch.setattr(_base, "_current_project_id", lambda: "")
    monkeypatch.setattr(_base.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(task_tools, "_api_call", _base._api_call)
    result = _mcp(world, "task_update", task_id=task["id"], expected_revision=task["revision"],
                  description="旧覆盖", fields=fields)
    assert result["success"] is False and result["error"] == "HTTP 409: Conflict"
    assert isinstance(result["detail"], dict)
    assert result["current_revision"] == task["revision"] + 1
    assert result["current_task"]["id"] == task["id"]
    assert ("config" in result["current_task"]) == (fields == "all")
    assert _get(world, task["id"])["description"] == "新进展"


def test_child_waiting_user_is_visible_in_digest_briefing_and_parent_card(world):
    parent = _create(world, "project-http", "父任务").json()["data"]
    child = _create(world, "project-http", "等待拍板子任务", parent_id=parent["id"], waiting_on={
        "kind": "user", "reason": "需要确认子任务方案",
    }).json()["data"]
    client, pid = world["client"], world["pid"]
    digest = client.get(f"/api/projects/{pid}/task-wall/digest").json()
    assert digest["open_total"] == 1
    entry = digest["waiting_user"][0]
    assert (entry["id"], entry["parent_id"], entry["parent_title"]) == (child["id"], parent["id"], "父任务")
    assert entry["waiting_on"]["reason"] == "需要确认子任务方案"
    text = client.get(f"/api/projects/{pid}/task-wall/digest", params={"format": "text"}).text
    assert "等用户 1 条" in text
    for params in ({}, {"status": "pending"}):
        wall = client.get(f"/api/projects/{pid}/task-wall", params=params).json()
        row = _rows(wall)[0]
        assert row["waiting_user"] and row["waiting_on"] is None
        assert row["waiting_user_subtasks"][0]["id"] == child["id"]
        assert row["subtasks"][0]["waiting_on"]["kind"] == "user"
        assert wall["digest"]["waiting_user"][0]["id"] == child["id"]
    assert _update(world, child["id"], waiting_on=None).status_code == 200
    assert client.get(f"/api/projects/{pid}/task-wall/digest").json()["waiting_user"] == []


def test_old_completed_unknown_time_is_never_filled_by_edit_or_retry(world):
    task = _run(world["repo"].create_task(None, "旧完成", project_id=world["pid"], status="completed"))
    before = _get(world, task.id)
    assert before["completed_at"] is None
    edited = _update(world, task.id, description="补结果").json()
    retried = _complete(world, task.id).json()
    assert edited["data"]["completed_at"] is None and retried["data"]["completed_at"] is None
    assert edited["warnings"] and retried["warnings"]
    digest = world["client"].get(f"/api/projects/{world['pid']}/task-wall/digest").json()
    assert digest["completed_total"] == 1 and digest["closed_7d"] == 0


def test_stale_internal_config_cannot_roll_back_management(world):
    task = _create(world, "project-http", "陈旧内部写").json()["data"]
    stale = _get(world, task["id"])["config"]
    updated = _update(world, task["id"], waiting_on={"kind": "user", "reason": "新等待"},
                      status_note="新说明").json()["data"]
    _run(world["repo"].update_task(task["id"], config={**stale, "watchdog": "observation"}))
    after = _get(world, task["id"])
    assert after["waiting_on"] == updated["waiting_on"] and after["status_note"] == "新说明"
    assert after["block_reason"] == "waiting" and after["revision"] == updated["revision"] + 1
    assert after["config"]["watchdog"] == "observation"


def test_non_object_legacy_config_is_preserved_on_management_write(world):
    from sqlalchemy import update

    from aiteam.storage.connection import get_session
    from aiteam.storage.models import TaskModel
    task = _create(world, "project-http").json()["data"]
    async def legacy_shape():
        async with get_session(world["repo"]._db_url) as session:
            await session.execute(update(TaskModel).where(TaskModel.id == task["id"]).values(config=["legacy", 7]))
    _run(legacy_shape())
    response = _update(world, task["id"], description="仍可修复")
    assert response.status_code == 200, response.text
    assert _get(world, task["id"])["config"]["legacy_config_value"] == ["legacy", 7]


def test_idempotency_key_length_matches_orm_and_boundary(world):
    from aiteam.storage.models import TaskCreateBatchModel
    assert TaskCreateBatchModel.__table__.c.idempotency_key.type.length == 200
    body = {"tasks": [{"key": "a", "title": "边界"}], "idempotency_key": "k" * 200}
    path = f"/api/projects/{world['pid']}/tasks/batch"
    assert world["client"].post(path, json=body).status_code == 201
    body["idempotency_key"] += "x"
    assert world["client"].post(path, json=body).status_code == 422
    assert len(_run(world["repo"].list_tasks_by_project(world["pid"]))) == 1


def test_project_deletion_cleans_batch_receipts_without_erasing_measured_token_rows(world):
    from sqlalchemy import select

    from aiteam.clock import utc_now
    from aiteam.storage.connection import get_session
    from aiteam.storage.models import TaskCreateBatchModel
    agent = _run(world["repo"].create_agent(world["team"], "measured-worker", "dev", system_prompt=""))
    _run(world["repo"].update_agent(agent.id, input_tokens=0, output_tokens=0, tokens_measured_at=utc_now()))
    body = {"idempotency_key": "delete-project-receipt", "tasks": [{"key": "a", "title": "批次任务"}]}
    response = world["client"].post(f"/api/projects/{world['pid']}/tasks/batch", json=body)
    assert response.status_code == 201
    assert _run(world["repo"].delete_project(world["pid"])) is True
    async def read_receipts():
        async with get_session(world["repo"]._db_url) as session:
            return (await session.execute(select(TaskCreateBatchModel).where(
                TaskCreateBatchModel.project_id == world["pid"],
            ))).scalars().all()
    assert _run(read_receipts()) == []
    retained = _run(world["repo"].get_agent(agent.id))
    assert retained is not None and retained.input_tokens == 0 and retained.tokens_measured_at is not None


def test_failed_child_remains_unclosed_and_parent_refusal_explains_status(world):
    parent = _create(world, "project-http", "父").json()["data"]
    child = _create(world, "project-http", "失败子", parent_id=parent["id"], status="failed").json()["data"]
    response = _complete(world, parent["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["open_subtasks"] == [{"id": child["id"], "title": "失败子", "status": "failed"}]
    assert _get(world, child["id"])["status"] == "failed"
    assert _update(world, child["id"], parent_id=None).status_code == 200
    assert _complete(world, parent["id"]).status_code == 200
    assert _get(world, child["id"])["status"] == "failed"


def test_whitespace_title_refused_and_mixed_parent_wait_hint_is_correct(world):
    task = _create(world, "project-http", "不能空白").json()["data"]
    before = _get(world, task["id"])
    assert _update(world, task["id"], title="   ").status_code == 400
    assert _get(world, task["id"]) == before
    assert _create(world, "project-http", "   ").status_code == 400
    agent = _run(world["repo"].create_agent(world["team"], "target", "dev", system_prompt=""))
    assert _update(world, task["id"], waiting_on={"kind": "agent", "target_id": agent.id,
                                                "reason": "父等执行者"}).status_code == 200
    child = _create(world, "project-http", "子等用户", parent_id=task["id"], waiting_on={
        "kind": "user", "reason": "子需拍板",
    }).json()["data"]
    row = _rows(world["client"].get(f"/api/projects/{world['pid']}/task-wall").json())[0]
    assert row["waiting_user"] and row["waiting_user_subtasks"][0]["id"] == child["id"]
    assert "等agent：父等执行者" in row["management_hints"]
    assert "等用户：父等执行者" not in row["management_hints"]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_waiting_requires_clear_before_explicit_running_in_one_request(world, transport):
    task = _create(world, "project-http", "待确认", waiting_on={"kind": "user", "reason": "请确认"}).json()["data"]
    before = _get(world, task["id"])
    if transport == "http":
        refused = _update(world, task["id"], status="running")
        assert refused.status_code == 409 and refused.json()["detail"]["waiting_on"]["reason"] == "请确认"
    else:
        refused = _mcp(world, "task_update", task_id=task["id"], status="running")
        assert refused["success"] is False and refused["waiting_on"]["reason"] == "请确认"
    assert _get(world, task["id"]) == before
    if transport == "http":
        response = _update(world, task["id"], status="running", waiting_on=None)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
    else:
        response = _mcp(world, "task_update", task_id=task["id"], status="running", waiting_on={})
        assert response["success"], response
        data = response["data"]
    assert data["status"] == "running" and data["waiting_on"] is None and data["started_at"] is not None
    assert _get(world, task["id"])["status"] == "running"


def test_legacy_running_with_unchanged_waiting_keeps_execution_fact_until_explicit_pause(world):
    from aiteam.clock import utc_now
    task = _create(world, "project-http", "历史等待", waiting_on={
        "kind": "user", "reason": "历史确认",
    }).json()["data"]
    _run(world["repo"].update_task(task["id"], status="running", started_at=utc_now()))
    before = _get(world, task["id"])
    assert before["started_at"] is not None
    response = _update(world, task["id"], description="只补进展")
    assert response.status_code == 200 and response.json()["warnings"]
    after = _get(world, task["id"])
    assert after["status"] == "running" and after["started_at"] == before["started_at"]
    assert after["waiting_on"] == before["waiting_on"]
    echoed = _mcp(world, "task_update", task_id=task["id"], waiting_on=after["waiting_on"], status_note="原等待回传")
    assert echoed["success"] and echoed["warnings"] and echoed["data"]["status"] == "running"
    assert echoed["data"]["started_at"] == before["started_at"]
    changed = _update(world, task["id"], waiting_on={"kind": "user", "reason": "新暂停意图"})
    assert changed.status_code == 200 and changed.json()["data"]["status"] == "blocked"


@pytest.mark.parametrize("obstacle", ["dependency", "waiting"])
@pytest.mark.parametrize("auto_start", [False, True], ids=["explicit-only", "explicit-with-auto-start"])
def test_explicit_create_running_refuses_unmet_condition_atomically(world, obstacle, auto_start):
    values = {"status": "running", "auto_start": auto_start}
    if obstacle == "dependency":
        dependency = _create(world, "project-http", "未完成依赖").json()["data"]["id"]
        values["depends_on"] = [dependency]
    else:
        values["waiting_on"] = {"kind": "user", "reason": "创建前需确认"}
    before = [task.model_dump(mode="json") for task in _run(world["repo"].list_tasks_by_project(world["pid"]))]
    response = _create(world, "project-http", "不能隐式阻塞的明确开始", **values)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["blocked_by"] == values.get("depends_on", [])
    assert bool(detail["waiting_on"]) == (obstacle == "waiting")
    after = [task.model_dump(mode="json") for task in _run(world["repo"].list_tasks_by_project(world["pid"]))]
    assert after == before


@pytest.mark.parametrize("obstacle", ["dependency", "waiting"])
def test_auto_start_only_creation_still_derives_blocked(world, obstacle):
    values = {"auto_start": True}
    if obstacle == "dependency":
        dependency = _create(world, "project-http", "未完成依赖").json()["data"]["id"]
        values["depends_on"] = [dependency]
    else:
        values["waiting_on"] = {"kind": "user", "reason": "等待确认"}
    response = _create(world, "project-http", "按条件开始", **values)
    assert response.status_code == 201, response.text
    task = response.json()["data"]
    assert task["status"] == "blocked" and task["started_at"] is None
    assert task["block_reason"] == ("dependency" if obstacle == "dependency" else "waiting")
    assert _get(world, task["id"])["status"] == "blocked"


@pytest.mark.parametrize("transport", ["http", "mcp"])
@pytest.mark.parametrize("obstacle", ["dependency", "waiting"])
def test_batch_explicit_running_refusal_rolls_back_tasks_and_receipt(world, transport, obstacle):

    from aiteam.storage.connection import get_session
    from aiteam.storage.models import TaskCreateBatchModel
    entries = [{"key": "source", "title": "前置"}, {"key": "running", "title": "明确开始", "status": "running"}]
    if obstacle == "dependency":
        entries[1]["depends_on"] = ["@source"]
    else:
        entries[1]["waiting_on"] = {"kind": "user", "reason": "需先确认"}
    key = f"explicit-refusal-{transport}-{obstacle}"
    body = {"tasks": entries, "idempotency_key": key}
    path = f"/api/projects/{world['pid']}/tasks/batch"
    if transport == "http":
        refused = world["client"].post(path, json=body)
        assert refused.status_code == 409, refused.text
    else:
        refused = _mcp(world, "task_create_batch", **body)
        assert refused["success"] is False and refused["error"] == "HTTP 409: Conflict"
    assert _run(world["repo"].list_tasks_by_project(world["pid"])) == []
    async def read_receipt():
        async with get_session(world["repo"]._db_url) as session:
            return await session.get(TaskCreateBatchModel, (world["pid"], key))
    assert _run(read_receipt()) is None
    entries[1].pop("status")
    entries[1]["auto_start"] = True
    retry = world["client"].post(path, json=body)
    assert retry.status_code == 201, retry.text
    assert retry.json()["data"]["tasks"][1]["status"] == "blocked"


def test_explicit_create_running_succeeds_when_dependency_complete_and_no_waiting(world):
    source = _create(world, "project-http", "完成前置", status="completed").json()["data"]["id"]
    response = _create(world, "project-http", "可真实开始", status="running", depends_on=[source])
    assert response.status_code == 201, response.text
    task = response.json()["data"]
    assert task["status"] == "running" and task["started_at"] is not None
    assert _get(world, task["id"])["started_at"] == task["started_at"]


def test_batch_creation_keeps_dependency_snapshot_until_running_insertion(world, monkeypatch):
    from aiteam.clock import utc_now
    from aiteam.task_management import TaskManagement

    source = _create(world, "project-http", "完成前置", status="completed").json()["data"]
    original = TaskManagement._rows
    original_event = world["repo"].create_event
    async def exercise():
        read_complete, continue_creation = asyncio.Event(), asyncio.Event()
        reopened_commit_observed = asyncio.Event()
        reopened_commit_at = None
        async def observed_read(self, session, project_id, referenced):
            rows = await original(self, session, project_id, referenced)
            if source["id"] in referenced:
                read_complete.set()
                await continue_creation.wait()
            return rows
        monkeypatch.setattr(TaskManagement, "_rows", observed_read)
        async def observed_event(*args, **kwargs):
            nonlocal reopened_commit_at
            if (kwargs.get("entity_id") == source["id"] and
                    (kwargs.get("state_snapshot") or {}).get("status") == "pending"):
                # update_task emits this event only after its state transaction
                # commits. Re-read the actual persisted state before recording
                # the boundary; event completion time is too late for this test.
                assert (await world["repo"].get_task(source["id"])).status.value == "pending"
                reopened_commit_at = utc_now()
                reopened_commit_observed.set()
            return await original_event(*args, **kwargs)
        monkeypatch.setattr(world["repo"], "create_event", observed_event)
        async def reopen():
            await world["repo"].update_task(source["id"], status="pending")
            assert reopened_commit_at is not None
            return reopened_commit_at
        creating = asyncio.create_task(world["repo"].create_managed_task_batch(
            world["pid"], [{"key": "running", "title": "明确开始", "status": "running",
                            "depends_on": [source["id"]]}], "dependency-snapshot-race",
        ))
        await asyncio.wait_for(read_complete.wait(), timeout=10)
        reopening = asyncio.create_task(reopen())
        # Hold a real read/write window, with no mocked task state. BEGIN must
        # serialize the dependency edit until validated insertion commits.
        try:
            await asyncio.wait_for(reopened_commit_observed.wait(), timeout=0.5)
        except TimeoutError:
            pass  # With BEGIN, the dependency writer cannot commit until release.
        finally:
            continue_creation.set()
        batch, reopened_at = await asyncio.gather(creating, reopening)
        task = batch["tasks"][0]
        assert task.status.value == "running"
        assert task.started_at <= reopened_at, "dependency was reopened before a stale snapshot began execution"
        persisted = await world["repo"].get_task(task.id)
        assert persisted.started_at == task.started_at and persisted.depends_on == [source["id"]]
        assert (await world["repo"].get_task(source["id"])).status.value == "pending"
    world["client"].portal.call(exercise)
