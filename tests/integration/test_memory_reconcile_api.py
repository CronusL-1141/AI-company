"""记忆系统 v2 P2 — 按需整理 REST 端到端测试.

覆盖 GET /api/memory/reconcile/candidates（候选组 + 方向层清单 + 操作说明）、
POST /api/memory/reconcile/apply（merge/score/promote/invalidate + 幂等 +
promote 红线）。用 TestClient + 内存 SQLite，X-Project-Id 头注入项目上下文。
"""

from __future__ import annotations

import asyncio


def _seed(repo) -> tuple[str, dict[str, str], list[str]]:
    """建项目 + 两条高相似 memo（同 scope_path）。

    返回 (project_id, headers, memo_ids)——X-Project-Id 用真实项目 id，
    这样 last_reconcile_at 能落到该项目的 config。
    """

    async def _run() -> tuple[str, list[str]]:
        project = await repo.create_project(name="R")
        pid = project.id
        m1 = await repo.add_task_memo(
            "t1",
            content="部署 API 到生产环境使用 docker compose 命令",
            project_id=pid,
            scope_path="/deploy",
        )
        m2 = await repo.add_task_memo(
            "t1",
            content="生产环境部署 API 用 docker compose 命令启动",
            project_id=pid,
            scope_path="/deploy",
        )
        return pid, [m1.id, m2.id]

    pid, ids = asyncio.get_event_loop().run_until_complete(_run())
    return pid, {"X-Project-Id": pid}, ids


def test_candidates_returns_groups_and_guide(repo_and_client) -> None:
    """候选粗筛返回候选组（含全文）+ 操作说明 + 方向层清单."""
    repo, client = repo_and_client
    _pid, headers, _ids = _seed(repo)
    resp = client.get("/api/memory/reconcile/candidates", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["candidate_groups"]) == 1
    assert data["candidate_groups"][0]["member_count"] == 2
    assert "operations" in data["operation_guide"]
    assert "MERGE" in data["operation_guide"]["operations"]
    assert data["stats"]["total_valid_memos"] == 2


def test_candidates_requires_project(integration_client) -> None:
    """无项目上下文 → 400."""
    resp = integration_client.get("/api/memory/reconcile/candidates")
    assert resp.status_code == 400


def test_apply_merge_invalidates_sources(repo_and_client) -> None:
    """merge：建新 memo，被并各条置失效并指向新条."""
    repo, client = repo_and_client
    _pid, headers, ids = _seed(repo)
    resp = client.post(
        "/api/memory/reconcile/apply",
        headers=headers,
        json={
            "operations": [
                {
                    "op": "merge",
                    "content": "生产部署 API：docker compose 命令启动",
                    "memo_ids": ids,
                    "scope_path": "/deploy",
                }
            ]
        },
    )
    assert resp.status_code == 200
    res = resp.json()["data"]["results"][0]
    assert res["status"] == "applied"
    new_id = res["new_memo_id"]

    # 候选源全部失效，新条有效
    remaining = client.get(
        "/api/memory/reconcile/candidates", headers=headers
    ).json()["data"]
    valid_ids = {
        m["id"]
        for g in remaining["candidate_groups"]
        for m in g["members"]
    }
    assert not (set(ids) & valid_ids)
    assert resp.json()["data"]["last_reconcile_at"] is not None
    assert new_id


def test_apply_merge_idempotent_on_invalidated(repo_and_client) -> None:
    """对已失效条目重复 merge → noop 不报错."""
    repo, client = repo_and_client
    _pid, headers, ids = _seed(repo)
    body = {
        "operations": [
            {"op": "invalidate", "memo_ids": ids},
        ]
    }
    first = client.post("/api/memory/reconcile/apply", headers=headers, json=body)
    assert first.json()["data"]["results"][0]["status"] == "applied"
    # 二次：全部已失效 → noop
    second = client.post("/api/memory/reconcile/apply", headers=headers, json=body)
    assert second.json()["data"]["results"][0]["status"] == "noop"


def test_apply_score(repo_and_client) -> None:
    """score：补质量分 1-10，越界报错."""
    repo, client = repo_and_client
    _pid, headers, ids = _seed(repo)
    ok = client.post(
        "/api/memory/reconcile/apply",
        headers=headers,
        json={"operations": [{"op": "score", "memo_id": ids[0], "quality_score": 9, "reason": "关键决策"}]},
    ).json()["data"]["results"][0]
    assert ok["status"] == "applied"
    bad = client.post(
        "/api/memory/reconcile/apply",
        headers=headers,
        json={"operations": [{"op": "score", "memo_id": ids[0], "quality_score": 11}]},
    ).json()["data"]["results"][0]
    assert bad["status"] == "error"


def test_apply_promote_builds_direction_and_enforces_redline(
    repo_and_client,
) -> None:
    """promote：建方向层条目；单条超 400 字触发红线返回 error."""
    repo, client = repo_and_client
    _pid, headers, ids = _seed(repo)
    ok = client.post(
        "/api/memory/reconcile/apply",
        headers=headers,
        json={
            "operations": [
                {
                    "op": "promote",
                    "content": "所有输出使用中文",
                    "kind": "constraint",
                    "scope": "global",
                    "source_refs": [ids[0]],
                }
            ]
        },
    ).json()["data"]["results"][0]
    assert ok["status"] == "applied"
    assert ok["memory_id"]
    # 该方向层条目应出现在候选清单的 direction_inventory
    inv = client.get(
        "/api/memory/reconcile/candidates", headers=headers
    ).json()["data"]["direction_inventory"]
    assert any(d["content"] == "所有输出使用中文" for d in inv)

    # 红线：单条 > 400 字 → error
    over = client.post(
        "/api/memory/reconcile/apply",
        headers=headers,
        json={"operations": [{"op": "promote", "content": "字" * 401, "kind": "design", "scope": "global"}]},
    ).json()["data"]["results"][0]
    assert over["status"] == "error"
    assert "400" in over["error"] or "指针" in over["error"]


def test_task_memo_hint_over_threshold(repo_and_client) -> None:
    """task_memo_add 响应：项目新增有效 memo > 150 → 附整理 hint."""
    repo, client = repo_and_client

    # 直接建项目 + 任务并写 151 条有效 memo（>150 阈值）
    async def _seed_task() -> str:
        project = await repo.create_project(name="bulk")
        t = await repo.create_task(None, "bulk", project_id=project.id)
        for i in range(151):
            await repo.add_task_memo(t.id, content=f"m{i}", project_id=project.id)
        return t.id

    task_id = asyncio.get_event_loop().run_until_complete(_seed_task())
    resp = client.post(
        f"/api/tasks/{task_id}/memo",
        json={"content": "第 152 条", "type": "progress"},
    )
    assert resp.status_code == 200
    assert "hint" in resp.json()
    assert "memory_reconcile" in resp.json()["hint"]
