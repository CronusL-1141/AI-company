"""未注册目录方向层记忆隔离 — REST 端到端测试（2026-07-21 串线事故根治）.

覆盖：
- 未注册目录 scope=project 写入落「目录指纹临时桶」，system 全局桶零污染；
- 读路径对称：本目录读得回自己的临时桶，其他目录读不到；
- 未注册目录读仍纳入 global/user 真全局条目；
- 无 cwd（无 X-Project-Dir 头）的 scope=project 裸写被 422 拒绝，绝不落 system；
- 已注册项目路径（X-Project-Id）不受影响，仍落项目桶（回归）。

用 TestClient + 内存 SQLite（conftest.integration_client）。
"""

from __future__ import annotations

from aiteam.memory.scoping import dir_bucket_scope_id

_DIR_A = "/tmp/aiteam-unreg-a"
_DIR_B = "/tmp/aiteam-unreg-b"


def _project_ids(listed: dict) -> list[str]:
    return [m["id"] for m in listed["data"]]


def test_unregistered_write_lands_in_dir_bucket_not_system(integration_client) -> None:
    """未注册目录 scope=project 写入 → 落 dir:<sha1> 临时桶，system 桶零增长."""
    bucket = dir_bucket_scope_id(_DIR_A)
    assert bucket.startswith("dir:")

    resp = integration_client.post(
        "/api/memories",
        json={"content": "本目录专属约束", "kind": "constraint", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    mem_id = body["data"]["id"]
    assert body["data"]["scope"] == "project"
    assert body["data"]["scope_id"] == bucket

    # 落在目录指纹桶
    in_bucket = integration_client.get(
        f"/api/memory?scope=project&scope_id={bucket}"
    ).json()
    assert mem_id in _project_ids(in_bucket)

    # system 全局桶零污染（旧 bug 会静默落这里广播给所有会话）
    in_system = integration_client.get(
        "/api/memory?scope=global&scope_id=system"
    ).json()
    assert mem_id not in _project_ids(in_system)


def test_read_symmetry_same_dir_reads_own_bucket(integration_client) -> None:
    """写目录 A 的 project 记忆 → 以 X-Project-Dir=A 读 /api/memories 能读回."""
    mem_id = integration_client.post(
        "/api/memories",
        json={"content": "A 目录的设计意图", "kind": "design", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    ).json()["data"]["id"]

    listed_a = integration_client.get(
        "/api/memories", headers={"X-Project-Dir": _DIR_A}
    ).json()
    assert mem_id in _project_ids(listed_a)


def test_read_isolation_other_dir_cannot_see(integration_client) -> None:
    """目录 A 的 project 记忆对目录 B 不可见（隔离是目录的默认权利）."""
    mem_id = integration_client.post(
        "/api/memories",
        json={"content": "A 私有约束", "kind": "constraint", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    ).json()["data"]["id"]

    listed_b = integration_client.get(
        "/api/memories", headers={"X-Project-Dir": _DIR_B}
    ).json()
    assert mem_id not in _project_ids(listed_b)


def test_unregistered_read_still_includes_global(integration_client) -> None:
    """未注册目录读路径仍纳入 global 真全局条目（合法全局纪律照常继承）."""
    global_id = integration_client.post(
        "/api/memories",
        json={"content": "所有输出使用中文", "kind": "constraint", "scope": "global"},
    ).json()["data"]["id"]
    dir_id = integration_client.post(
        "/api/memories",
        json={"content": "A 目录偏好", "kind": "preference", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    ).json()["data"]["id"]

    listed_a = integration_client.get(
        "/api/memories", headers={"X-Project-Dir": _DIR_A}
    ).json()
    ids_a = _project_ids(listed_a)
    assert global_id in ids_a  # 全局继承
    assert dir_id in ids_a  # 本目录临时桶

    # 另一目录 B：见全局、不见 A 的临时桶
    listed_b = integration_client.get(
        "/api/memories", headers={"X-Project-Dir": _DIR_B}
    ).json()
    ids_b = _project_ids(listed_b)
    assert global_id in ids_b
    assert dir_id not in ids_b


def test_scope_project_without_cwd_rejected(integration_client) -> None:
    """无 X-Project-Dir 的 scope=project 裸写 → 422 拒绝，绝不静默落 system."""
    resp = integration_client.post(
        "/api/memories",
        json={"content": "无目录上下文", "kind": "constraint", "scope": "project"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "X-Project-Dir" in detail or "scope=global" in detail

    # 确认没有任何条目静默落进 system 桶
    in_system = integration_client.get(
        "/api/memory?scope=global&scope_id=system"
    ).json()
    assert all(m["content"] != "无目录上下文" for m in in_system["data"])


def test_search_unregistered_dir_finds_own_bucket(integration_client) -> None:
    """GET /api/memory(BM25 search)未注册目录 scope=project 缺省 scope_id → 命中本目录临时桶.

    对应 leader follow-up：search 端点原走非 scoped repo，未注册目录查不到自己的桶。
    """
    integration_client.post(
        "/api/memories",
        json={"content": "probe-token 专属条目", "kind": "preference", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    )

    # 带 query（BM25 路径）
    hit = integration_client.get(
        "/api/memory?scope=project&query=probe-token",
        headers={"X-Project-Dir": _DIR_A},
    ).json()
    assert any("probe-token" in m["content"] for m in hit["data"])

    # 不带 query（list 路径）也应命中
    listed = integration_client.get(
        "/api/memory?scope=project", headers={"X-Project-Dir": _DIR_A}
    ).json()
    assert any("probe-token" in m["content"] for m in listed["data"])


def test_search_cross_dir_isolation(integration_client) -> None:
    """search 端点：目录 A 的临时桶条目对目录 B 的 project search 不可见."""
    integration_client.post(
        "/api/memories",
        json={"content": "onlyA-marker", "kind": "preference", "scope": "project"},
        headers={"X-Project-Dir": _DIR_A},
    )
    from_b = integration_client.get(
        "/api/memory?scope=project&query=onlyA-marker",
        headers={"X-Project-Dir": _DIR_B},
    ).json()
    assert all("onlyA-marker" not in m["content"] for m in from_b["data"])


def test_search_project_scope_without_cwd_rejected(integration_client) -> None:
    """search scope=project 缺省 scope_id 且无 cwd → 422（不静默搜 system）."""
    resp = integration_client.get("/api/memory?scope=project&query=x")
    assert resp.status_code == 422


def test_search_explicit_scope_id_respected(integration_client) -> None:
    """显式传 scope_id 一律尊重（M4 巡检等显式路径回归）."""
    # 显式 global/system
    integration_client.post(
        "/api/memories",
        json={"content": "global-explicit", "kind": "constraint", "scope": "global"},
    )
    got = integration_client.get(
        "/api/memory?scope=global&scope_id=system&query=global-explicit"
    ).json()
    assert any("global-explicit" in m["content"] for m in got["data"])

    # 显式 project scope_id（真项目桶，非 system）——直接尊重，不被上下文覆盖
    proj_id = "proj-explicit-search"
    integration_client.post(
        "/api/memories",
        json={"content": "proj-explicit", "kind": "constraint", "scope": "project"},
        headers={"X-Project-Id": proj_id},
    )
    got2 = integration_client.get(
        f"/api/memory?scope=project&scope_id={proj_id}&query=proj-explicit"
    ).json()
    assert any("proj-explicit" in m["content"] for m in got2["data"])


def test_registered_project_path_unaffected(integration_client) -> None:
    """已注册项目（X-Project-Id）写读仍落项目桶，不走 dir 指纹分支（回归）."""
    proj_id = "proj-registered-xyz"
    mem_id = integration_client.post(
        "/api/memories",
        json={"content": "注册项目约束", "kind": "constraint", "scope": "project"},
        headers={"X-Project-Id": proj_id},
    ).json()["data"]["id"]

    # 落在真项目桶（非 dir: 前缀）
    in_proj = integration_client.get(
        f"/api/memory?scope=project&scope_id={proj_id}"
    ).json()
    assert mem_id in _project_ids(in_proj)

    # 以同项目 id 读 /api/memories 能读回
    listed = integration_client.get(
        "/api/memories", headers={"X-Project-Id": proj_id}
    ).json()
    assert mem_id in _project_ids(listed)
