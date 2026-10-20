"""REST 方向层写入归属：真实 uvicorn + 临时 SQLite 文件，独立连接读回。"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from aiteam.memory.scoping import dir_bucket_scope_id


@pytest.fixture()
def memory_api(hook_server):
    port, database, _ = hook_server
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
        projects = {}
        for name in ("X", "Y"):
            response = client.post("/api/projects", json={
                "name": f"memory-write-{name}", "root_path": f"/tmp/memory-write-{name}",
            })
            assert response.status_code == 201, response.text
            projects[name] = response.json()["data"]["id"]
        yield client, database, projects


def _write(client, content, *, scope="project", headers=None, **fields):
    return client.post("/api/memories", headers=headers or {}, json={
        "content": content, "kind": "constraint", "scope": scope, **fields,
    })


def _rows(database):
    # 单独打开磁盘库，断言不依赖响应对象或服务端 repository 的缓存。
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        return connection.execute(
            "SELECT id, scope, scope_id, content, invalid_at, invalidated_by "
            "FROM memories ORDER BY id"
        ).fetchall()


def _visible(client, headers):
    response = client.get("/api/memories", headers=headers)
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()["data"]}


@pytest.mark.parametrize("replace", [False, True], ids=["add", "supersedes"])
@pytest.mark.parametrize("confirm", [False, True], ids=["unconfirmed", "confirmed"])
def test_other_project_write_refused_without_any_mutation(memory_api, replace, confirm):
    client, database, projects = memory_api
    headers = {name: {"X-Project-Id": pid} for name, pid in projects.items()}
    originals = {}
    for name in ("X", "Y"):
        response = _write(client, f"{name} 原始项目纪律", headers=headers[name])
        assert response.json()["success"] is True, response.text
        originals[name] = response.json()["data"]["id"]
    before = _rows(database)
    fields = {"scope_id": projects["Y"], "confirm_shared_scope": confirm}
    if replace:
        fields["supersedes"] = originals["Y"]
    response = _write(client, "来自 X 的越界条目", headers=headers["X"], **fields)
    after = _rows(database)
    visible = {name: _visible(client, headers[name]) for name in ("X", "Y")}
    evidence = {"status": response.status_code, "body": response.json(),
                "before": before, "after": after, "visible": visible}
    assert response.status_code == 403, json.dumps(evidence, ensure_ascii=False, indent=2)
    assert "scope_id" in response.json()["detail"]
    assert after == before
    assert set(visible["X"]) == {originals["X"]}
    assert set(visible["Y"]) == {originals["Y"]}


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
def test_same_project_add_and_supersedes_persist(memory_api, explicit):
    client, database, projects = memory_api
    headers = {"X-Project-Id": projects["X"]}
    fields = {"scope_id": projects["X"]} if explicit else {}
    old = _write(client, "本项目原始纪律", headers=headers, **fields).json()["data"]["id"]
    response = _write(client, "本项目新版纪律", headers=headers, supersedes=old, **fields)
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    new = response.json()["data"]["id"]
    rows = {row[0]: row for row in _rows(database)}
    assert rows[old][4] is not None and rows[old][5] == new
    assert rows[new][2:5] == (projects["X"], "本项目新版纪律", None)
    assert set(_visible(client, headers)) == {new}
    assert _visible(client, {"X-Project-Id": projects["Y"]}) == {}


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
def test_project_write_without_context_refused_even_with_scope_id(memory_api, explicit):
    client, database, projects = memory_api
    before = _rows(database)
    fields = {"scope_id": projects["Y"]} if explicit else {}
    response = _write(client, "无上下文不得指定其他项目", **fields)
    assert response.status_code == 422, {"body": response.json(), "rows": _rows(database)}
    assert "X-Project-Dir" in response.json()["detail"]
    assert _rows(database) == before


def test_unregistered_directory_can_only_write_own_bucket(memory_api):
    client, database, projects = memory_api
    directory = "/tmp/memory-write-unregistered"
    bucket = dir_bucket_scope_id(directory)
    headers = {"X-Project-Dir": directory}
    old = _write(client, "目录临时桶纪律", headers=headers).json()["data"]["id"]
    response = _write(client, "目录临时桶新版", headers=headers, scope_id=bucket, supersedes=old)
    assert response.status_code == 200 and response.json()["success"] is True
    new = response.json()["data"]["id"]
    before = _rows(database)
    for foreign in (projects["Y"], dir_bucket_scope_id("/tmp/memory-write-other")):
        denied = _write(client, "临时目录越界纪律", headers=headers, scope_id=foreign)
        assert denied.status_code == 403, denied.text
        assert _rows(database) == before
    assert set(_visible(client, headers)) == {new}
    assert _visible(client, {"X-Project-Id": projects["Y"]}) == {}


def test_same_project_scope_cannot_supersede_other_bucket(memory_api):
    client, database, projects = memory_api
    old = _write(client, "Y 项目原始纪律", headers={"X-Project-Id": projects["Y"]}).json()["data"]["id"]
    before = _rows(database)
    response = _write(client, "X 项目置换失败", headers={"X-Project-Id": projects["X"]},
                      scope_id=projects["X"], supersedes=old, confirm_shared_scope=True)
    assert response.status_code == 200 and response.json()["success"] is False
    assert "禁止跨桶置换" in response.json()["error"]
    assert _rows(database) == before
