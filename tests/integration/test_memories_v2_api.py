"""记忆系统 v2 P1 — 方向层 REST 端到端测试.

覆盖 POST /api/memories（含体量红线拒绝）、GET /api/memories（valid-only 默认）、
POST /api/memories/{id}/invalidate。用 TestClient + 内存 SQLite。
"""

from __future__ import annotations


def test_create_and_list_direction_memory(integration_client) -> None:
    """写入方向层条目 → GET 能查到，valid-only 默认."""
    resp = integration_client.post(
        "/api/memories",
        json={"content": "所有输出使用中文", "kind": "constraint", "scope": "global"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    mem_id = body["data"]["id"]
    assert body["data"]["kind"] == "constraint"

    listed = integration_client.get("/api/memories").json()
    ids = [m["id"] for m in listed["data"]]
    assert mem_id in ids


def test_kind_filter(integration_client) -> None:
    """GET ?kind= 过滤."""
    integration_client.post(
        "/api/memories",
        json={"content": "约束条", "kind": "constraint", "scope": "global"},
    )
    integration_client.post(
        "/api/memories",
        json={"content": "偏好条", "kind": "preference", "scope": "global"},
    )
    only = integration_client.get("/api/memories?kind=constraint").json()
    assert all(m["kind"] == "constraint" for m in only["data"])
    assert any(m["content"] == "约束条" for m in only["data"])


def test_content_length_redline(integration_client) -> None:
    """单条 > 400 字 → 拒绝并提示指针条目."""
    resp = integration_client.post(
        "/api/memories",
        json={"content": "字" * 401, "kind": "design", "scope": "global"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "指针" in body["error"] or "400" in body["error"]


def test_count_redline_rejects_when_full(integration_client) -> None:
    """同桶有效条目达 40 → 第 41 条被拒并提示 memory_reconcile."""
    for i in range(40):
        r = integration_client.post(
            "/api/memories",
            json={"content": f"条目{i}", "kind": "preference", "scope": "global"},
        )
        assert r.json()["success"] is True

    over = integration_client.post(
        "/api/memories",
        json={"content": "第41条", "kind": "preference", "scope": "global"},
    ).json()
    assert over["success"] is False
    assert "memory_reconcile" in over["error"]


def test_invalidate_flow(integration_client) -> None:
    """失效后不再出现在 valid-only 列表，include_invalidated 可见."""
    mem_id = integration_client.post(
        "/api/memories",
        json={"content": "会过时的偏好", "kind": "directive", "scope": "global"},
    ).json()["data"]["id"]

    inv = integration_client.post(f"/api/memories/{mem_id}/invalidate", json={})
    assert inv.status_code == 200
    assert inv.json()["data"]["invalid_at"] is not None

    valid = integration_client.get("/api/memories").json()
    assert mem_id not in [m["id"] for m in valid["data"]]

    with_inv = integration_client.get(
        "/api/memories?include_invalidated=true"
    ).json()
    assert mem_id in [m["id"] for m in with_inv["data"]]


def test_invalid_scope_and_kind_rejected(integration_client) -> None:
    """非法 scope/kind → 422."""
    assert integration_client.post(
        "/api/memories",
        json={"content": "x", "kind": "constraint", "scope": "task"},
    ).status_code == 422
    assert integration_client.post(
        "/api/memories",
        json={"content": "x", "kind": "bogus", "scope": "global"},
    ).status_code == 422


def test_supersede_via_api(integration_client) -> None:
    """supersedes 置换：旧条失效，新条有效，且不触发数量红线（supersede 不计新增）."""
    old_id = integration_client.post(
        "/api/memories",
        json={"content": "旧偏好", "kind": "preference", "scope": "global"},
    ).json()["data"]["id"]
    new_id = integration_client.post(
        "/api/memories",
        json={
            "content": "新偏好",
            "kind": "preference",
            "scope": "global",
            "supersedes": old_id,
        },
    ).json()["data"]["id"]

    valid_ids = [m["id"] for m in integration_client.get("/api/memories").json()["data"]]
    assert new_id in valid_ids
    assert old_id not in valid_ids
