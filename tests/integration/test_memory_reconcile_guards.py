"""记忆整理的越界与并发防护 — REST 端到端测试（2026-09-28 加固）.

三道闸，每道都有拒绝与放行两类用例，断言一律再发一次请求（或直查库）读回：
- 作用域：reconcile apply 只动当前项目的 memo，别的项目的 memo id 按条报错、
  整条不执行，同批其他操作照常；
- 共享条目：memory_invalidate 失效 global/user 条目须带 confirm_shared_scope，
  别的项目的 project 桶条目按 id 也够不着；
- 整理权：同一项目同一时刻只有一个会话能整理（candidates 占位、apply 验证、
  全部成功即释放、有报错保留、TTL 过期可接管）。

用 TestClient + 内存 SQLite（conftest.integration_client）。真实 uvicorn + MCP 工具 +
高并发的用例在 tests/unit/api/test_memory_reconcile_guards_e2e.py。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from aiteam.api.routes import memory_reconcile

ROOT = Path(__file__).resolve().parents[2]

_CANDIDATES = "/api/memory/reconcile/candidates"
_APPLY = "/api/memory/reconcile/apply"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _two_projects(repo) -> dict:
    """项目 X、Y 各一个任务、各两条同簇 memo。"""

    async def seed() -> dict:
        out: dict = {}
        for name in ("X", "Y"):
            project = await repo.create_project(name=f"proj-{name}", root_path=f"/tmp/guard-{name}")
            task = await repo.create_task(None, f"{name}-task", project_id=project.id)
            memos = [
                await repo.add_task_memo(
                    task.id, content=text, project_id=project.id, scope_path="/deploy"
                )
                for text in (
                    f"{name} 部署 API 到生产环境使用 docker compose 命令",
                    f"{name} 生产环境部署 API 用 docker compose 命令启动",
                )
            ]
            out[name] = {"pid": project.id, "task": task.id, "memos": [m.id for m in memos]}
        return out

    return _run(seed())


def _headers(pid: str, session: str = "sess-guard") -> dict[str, str]:
    return {"X-Project-Id": pid, "X-CC-Session-Id": session}


def _lease(client, headers: dict[str, str]) -> str:
    body = client.get(_CANDIDATES, headers=headers).json()
    assert body["success"] is True, body
    return body["data"]["reconcile_lease"]["lease_id"]


def _memo_rows(repo, ids: list[str]) -> dict:
    async def read() -> dict:
        return {mid: await repo.get_task_memo(mid) for mid in ids}

    return _run(read())


def _valid_direction(client, headers: dict[str, str] | None = None) -> set[str]:
    return {m["id"] for m in client.get("/api/memories", headers=headers or {}).json()["data"]}


# ================================================================
# 缺口 A：apply 只动当前项目的 memo
# ================================================================


def test_apply_refuses_other_projects_memos_per_operation(repo_and_client) -> None:
    """X 会话提交的 Y 的 memo：invalidate/merge/score 各自整条报错；同批本项目操作照常."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx = _headers(w["X"]["pid"])
    y0, y1 = w["Y"]["memos"]
    x0, x1 = w["X"]["memos"]

    resp = client.post(
        _APPLY,
        headers=hx,
        json={
            "lease_id": _lease(client, hx),
            "operations": [
                {"op": "invalidate", "memo_ids": [y0]},
                # 混装本项目与外项目 memo：整条不执行，x0 也不能被并掉
                {"op": "merge", "content": "X 会话写的摘要", "memo_ids": [x0, y1]},
                {"op": "score", "memo_id": y1, "quality_score": 1, "reason": "from X"},
                {"op": "invalidate", "memo_ids": [x1]},
            ],
        },
    ).json()
    results = resp["data"]["results"]
    assert [r["status"] for r in results] == ["error", "error", "error", "applied"]
    assert results[0]["foreign_memo_ids"] == [y0]
    assert results[1]["foreign_memo_ids"] == [y1]
    assert results[2]["foreign_memo_ids"] == [y1]
    assert results[3]["invalidated"] == [x1]

    # 读回：Y 的 memo 原样有效、没被打分、没有 reconcile 写进 Y；x0 未被并掉
    rows = _memo_rows(repo, [y0, y1, x0, x1])
    assert rows[y0].invalid_at is None and rows[y1].invalid_at is None
    assert rows[y1].quality_score is None
    assert rows[x0].invalid_at is None
    assert rows[x1].invalid_at is not None
    y_view = client.get(f"/api/tasks/{w['Y']['task']}/memo", headers=_headers(w["Y"]["pid"]))
    assert sorted(m["id"] for m in y_view.json()["data"]) == sorted([y0, y1])
    assert not any(m["author"] == "reconcile" for m in y_view.json()["data"])


def test_apply_own_project_memos_still_applies(repo_and_client) -> None:
    """放行：本项目的 merge 照常建新 memo 并失效被并各条（跨请求读回）."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx = _headers(w["X"]["pid"])
    resp = client.post(
        _APPLY,
        headers=hx,
        json={
            "lease_id": _lease(client, hx),
            "operations": [{"op": "merge", "content": "X 部署摘要", "memo_ids": w["X"]["memos"]}],
        },
    ).json()["data"]
    assert resp["results"][0]["status"] == "applied"
    rows = _memo_rows(repo, [*w["X"]["memos"], resp["results"][0]["new_memo_id"]])
    new = rows.pop(resp["results"][0]["new_memo_id"])
    assert new.project_id == w["X"]["pid"] and new.invalid_at is None
    assert all(m.invalid_at is not None for m in rows.values())


def test_apply_requires_project_context(integration_client) -> None:
    """无项目上下文 → 400，与 candidates 同规（此前可按裸 id 动任意项目的 memo）."""
    resp = integration_client.post(
        _APPLY, json={"operations": [{"op": "invalidate", "memo_ids": ["whatever"]}]}
    )
    assert resp.status_code == 400


# ================================================================
# 缺口 B：共享条目失效须显式确认；别的项目的条目够不着
# ================================================================


def _direction(client, headers: dict[str, str], scope: str, content: str) -> str:
    body = client.post(
        "/api/memories",
        headers=headers,
        json={"content": content, "kind": "constraint", "scope": scope},
    ).json()
    assert body["success"] is True, body
    return body["data"]["id"]


def test_invalidate_shared_scope_by_match_requires_confirmation(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx = _headers(w["X"]["pid"])
    gid = _direction(client, hx, "global", "守卫用例全局纪律：所有输出使用中文")

    refused = client.post(
        "/api/memories/invalidate", headers=hx, json={"content_match": "守卫用例全局纪律"}
    ).json()
    assert refused["success"] is False
    assert refused["requires_confirmation"] is True
    assert refused["target"]["id"] == gid
    assert refused["target"]["content"] == "守卫用例全局纪律：所有输出使用中文"
    # 读回：别的项目的会话照样继承它
    assert gid in _valid_direction(client, _headers(w["Y"]["pid"]))

    ok = client.post(
        "/api/memories/invalidate",
        headers=hx,
        json={"content_match": "守卫用例全局纪律", "confirm_shared_scope": True},
    ).json()
    assert ok["success"] is True
    assert gid not in _valid_direction(client, _headers(w["Y"]["pid"]))


def test_invalidate_shared_scope_by_id_requires_confirmation(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx = _headers(w["X"]["pid"])
    uid = _direction(client, hx, "user", "守卫用例用户偏好：先给结论")

    refused = client.post(f"/api/memories/{uid}/invalidate", headers=hx, json={}).json()
    assert refused["success"] is False and refused["requires_confirmation"] is True
    # 无请求体（旧调用形态）同样拒绝
    bare = client.post(f"/api/memories/{uid}/invalidate", headers=hx).json()
    assert bare["success"] is False and bare["requires_confirmation"] is True
    assert uid in _valid_direction(client, hx)

    ok = client.post(
        f"/api/memories/{uid}/invalidate", headers=hx, json={"confirm_shared_scope": True}
    ).json()
    assert ok["success"] is True and ok["data"]["invalid_at"] is not None
    assert uid not in _valid_direction(client, hx)


def test_invalidate_other_projects_entry_by_id_is_out_of_reach(repo_and_client) -> None:
    """X 会话按 id 够不着 Y 的 project 桶条目（确认标志也不行）；Y 自己失效不需确认."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx, hy = _headers(w["X"]["pid"]), _headers(w["Y"]["pid"])
    yid = _direction(client, hy, "project", "守卫用例 Y 项目方向：只读生产库")

    for body in ({}, {"confirm_shared_scope": True}):
        resp = client.post(f"/api/memories/{yid}/invalidate", headers=hx, json=body)
        assert resp.status_code == 404
    # 没有任何上下文的裸调用也够不着 project 桶条目
    assert client.post(f"/api/memories/{yid}/invalidate", json={}).status_code == 404
    assert yid in _valid_direction(client, hy)

    own = client.post(f"/api/memories/{yid}/invalidate", headers=hy, json={}).json()
    assert own["success"] is True
    assert yid not in _valid_direction(client, hy)


def test_invalidate_own_project_entry_by_match_needs_no_confirmation(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx = _headers(w["X"]["pid"])
    xid = _direction(client, hx, "project", "守卫用例 X 项目方向：发版前跑机检")
    ok = client.post(
        "/api/memories/invalidate", headers=hx, json={"content_match": "发版前跑机检"}
    ).json()
    assert ok["success"] is True and ok["data"]["id"] == xid
    assert xid not in _valid_direction(client, hx)


def test_global_over_quota_message_mentions_confirmation(integration_client) -> None:
    """超限协议叫调用方当轮腾空间；global 桶腾空间要先过目确认，提示里得说清."""
    filler = "占位" * 50
    for _ in range(12):
        assert integration_client.post(
            "/api/memories", json={"content": filler, "kind": "preference", "scope": "global"}
        ).json()["success"]
    over = integration_client.post(
        "/api/memories", json={"content": "再加一条", "kind": "design", "scope": "global"}
    ).json()
    assert over["success"] is False
    assert "confirm_shared_scope" in over["error"]
    # apply 的操作只作用于情景层或往方向层加条，腾不出方向层空间，别把人往那儿推
    assert "memory_reconcile_apply" not in over["error"]


# ================================================================
# 缺口 C：整理权（同一项目同一时刻只有一个会话能整理）
# ================================================================


def test_second_session_is_refused_until_first_finishes(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    s1, s2 = _headers(pid, "sess-one-aaaaaaaa"), _headers(pid, "sess-two-bbbbbbbb")

    lease1 = _lease(client, s1)
    refused = client.get(_CANDIDATES, headers=s2).json()
    assert refused["success"] is False
    assert "data" not in refused  # 不交出候选
    held = refused["reconcile_lease"]
    assert held["holder_session"] == "sess-one"
    assert "lease_id" not in held  # 凭据不外泄

    # S2 直接 apply 也被拒，且整批不执行
    blocked = client.post(
        _APPLY, headers=s2, json={"operations": [{"op": "invalidate", "memo_ids": w["X"]["memos"]}]}
    ).json()
    assert blocked["success"] is False
    assert all(m.invalid_at is None for m in _memo_rows(repo, w["X"]["memos"]).values())

    done = client.post(
        _APPLY,
        headers=s1,
        json={
            "lease_id": lease1,
            "operations": [{"op": "merge", "content": "S1 摘要", "memo_ids": w["X"]["memos"]}],
        },
    ).json()["data"]
    assert done["results"][0]["status"] == "applied"
    assert done["reconcile_lease"] == {"status": "released"}

    # S1 用完即释放：S2 现在拿得到，候选里已是 S1 整理后的状态
    after = client.get(_CANDIDATES, headers=s2).json()
    assert after["success"] is True
    assert after["data"]["stats"]["total_valid_memos"] == 1


def test_lease_is_per_project(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    _lease(client, _headers(w["X"]["pid"], "sess-one"))
    assert client.get(_CANDIDATES, headers=_headers(w["Y"]["pid"], "sess-two")).json()["success"]


def test_same_session_renews_without_lease_id(repo_and_client) -> None:
    """CC 会话内按会话 id 认人：同会话再调 candidates 是续约，apply 可不传 lease_id."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    h = _headers(w["X"]["pid"])
    first = client.get(_CANDIDATES, headers=h).json()["data"]["reconcile_lease"]
    again = client.get(_CANDIDATES, headers=h).json()["data"]["reconcile_lease"]
    assert first["status"] == "acquired" and again["status"] == "renewed"
    assert first["lease_id"] and "lease_id" not in again  # 明文只给新发的那一次
    applied = client.post(
        _APPLY, headers=h, json={"operations": [{"op": "invalidate", "memo_ids": w["X"]["memos"][:1]}]}
    ).json()
    assert applied["data"]["results"][0]["status"] == "applied"


def test_non_cc_caller_must_present_lease_id(repo_and_client) -> None:
    """无会话头（如 Codex）只能凭 lease_id 认人."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    h = {"X-Project-Id": w["X"]["pid"]}
    lease_id = _lease(client, h)

    # 再调 candidates 不带 lease_id：认不出是自己 → 拒绝；带上 → 续约
    assert client.get(_CANDIDATES, headers=h).json()["success"] is False
    renewed = client.get(_CANDIDATES, headers={**h, "X-Aiteam-Reconcile-Lease": lease_id}).json()
    assert renewed["data"]["reconcile_lease"]["status"] == "renewed"
    # URL 里的 lease_id 会连 query 一起进访问日志：明确拒收并告诉调用方改走请求头
    in_url = client.get(_CANDIDATES, headers=h, params={"lease_id": lease_id})
    assert in_url.status_code == 400
    assert "X-Aiteam-Reconcile-Lease" in in_url.json()["detail"]

    ops = [{"op": "invalidate", "memo_ids": w["X"]["memos"][:1]}]
    assert client.post(_APPLY, headers=h, json={"operations": ops}).json()["success"] is False
    assert _memo_rows(repo, w["X"]["memos"][:1])[w["X"]["memos"][0]].invalid_at is None
    ok = client.post(_APPLY, headers=h, json={"operations": ops, "lease_id": lease_id}).json()
    assert ok["data"]["results"][0]["status"] == "applied"


def test_lease_retained_on_error_then_released_on_clean_retry(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    s1, s2 = _headers(pid, "sess-one"), _headers(pid, "sess-two")
    lease1 = _lease(client, s1)

    partial = client.post(
        _APPLY,
        headers=s1,
        json={
            "lease_id": lease1,
            "operations": [
                {"op": "score", "memo_id": w["X"]["memos"][0], "quality_score": 99},
                {"op": "invalidate", "memo_ids": w["X"]["memos"][1:]},
            ],
        },
    ).json()["data"]
    assert [r["status"] for r in partial["results"]] == ["error", "applied"]
    assert partial["reconcile_lease"]["status"] == "retained"
    assert "lease_id" not in partial["reconcile_lease"]
    assert client.get(_CANDIDATES, headers=s2).json()["success"] is False

    retry = client.post(
        _APPLY,
        headers=s1,
        json={
            "lease_id": lease1,
            "operations": [{"op": "score", "memo_id": w["X"]["memos"][0], "quality_score": 8}],
        },
    ).json()["data"]
    assert retry["reconcile_lease"] == {"status": "released"}
    assert client.get(_CANDIDATES, headers=s2).json()["success"] is True


def test_keep_lease_and_empty_batch_release(repo_and_client) -> None:
    """分批：keep_lease=true 保留；判完无改动提交空批即释放."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    s1, s2 = _headers(pid, "sess-one"), _headers(pid, "sess-two")
    lease1 = _lease(client, s1)
    kept = client.post(
        _APPLY,
        headers=s1,
        json={
            "lease_id": lease1,
            "keep_lease": True,
            "operations": [{"op": "invalidate", "memo_ids": w["X"]["memos"][:1]}],
        },
    ).json()["data"]
    assert kept["reconcile_lease"]["status"] == "retained"
    assert client.get(_CANDIDATES, headers=s2).json()["success"] is False

    empty = client.post(_APPLY, headers=s1, json={"lease_id": lease1, "operations": []}).json()
    assert empty["data"]["reconcile_lease"] == {"status": "released"}
    assert client.get(_CANDIDATES, headers=s2).json()["success"] is True


def test_expired_lease_can_be_taken_over_and_stale_holder_is_refused(
    repo_and_client, monkeypatch
) -> None:
    """TTL 按需判定：过期即可被接管；被接管的一方 apply 整批不执行."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    s1, s2 = _headers(pid, "sess-one"), _headers(pid, "sess-two")

    monkeypatch.setattr(memory_reconcile, "_LEASE_TTL_SECONDS", 0)
    lease1 = _lease(client, s1)  # 即刻过期
    lease2 = _lease(client, s2)  # 接管
    assert lease2 != lease1
    monkeypatch.setattr(memory_reconcile, "_LEASE_TTL_SECONDS", 1800)
    assert client.get(_CANDIDATES, headers=s2).json()["success"] is True  # S2 续约

    stale = client.post(
        _APPLY,
        headers=s1,
        json={"lease_id": lease1, "operations": [{"op": "invalidate", "memo_ids": w["X"]["memos"]}]},
    ).json()
    assert stale["success"] is False
    assert all(m.invalid_at is None for m in _memo_rows(repo, w["X"]["memos"]).values())


def test_expired_but_not_taken_over_lease_still_works(repo_and_client, monkeypatch) -> None:
    """过期但没人接管：持有者照常 apply（期间没有别的整理发生，候选仍可信）."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    h = _headers(w["X"]["pid"])
    monkeypatch.setattr(memory_reconcile, "_LEASE_TTL_SECONDS", 0)
    lease_id = _lease(client, h)
    monkeypatch.setattr(memory_reconcile, "_LEASE_TTL_SECONDS", 1800)
    ok = client.post(
        _APPLY,
        headers=h,
        json={"lease_id": lease_id, "operations": [{"op": "invalidate", "memo_ids": w["X"]["memos"][:1]}]},
    ).json()
    assert ok["data"]["results"][0]["status"] == "applied"


def test_lease_survives_last_reconcile_at_write(repo_and_client) -> None:
    """整理时间戳与整理权同住 project.config['memory']，写一个不能冲掉另一个."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    h = _headers(pid)
    lease_id = _lease(client, h)
    kept = client.post(
        _APPLY,
        headers=h,
        json={
            "lease_id": lease_id,
            "keep_lease": True,
            "operations": [{"op": "invalidate", "memo_ids": w["X"]["memos"][:1]}],
        },
    ).json()["data"]
    assert kept["last_reconcile_at"] is not None

    project = _run(repo.get_project(pid))
    mem = project.config["memory"]
    assert mem["last_reconcile_at"] == kept["last_reconcile_at"]
    assert mem["reconcile_lease"]["lease_id_hash"] == _sha(lease_id)


# ================================================================
# 审查 2252d272 跟进：peek、被挡方的出路、HTTP MCP 认人、遗留分区、无归属 memo、
# 配置编辑不冲掉整理权
# ================================================================


def _stored_lease(repo, pid: str) -> dict | None:
    project = _run(repo.get_project(pid))
    return ((project.config or {}).get("memory") or {}).get("reconcile_lease")


def test_peek_looks_without_taking_the_lease(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    s1, s2 = _headers(pid, "sess-one-aaaaaaaa"), _headers(pid, "sess-two-bbbbbbbb")

    # 没人持有时 peek：照常给候选，不写任何整理权
    looked = client.get(_CANDIDATES, headers=s2, params={"peek": "true"}).json()
    assert looked["success"] is True
    assert looked["data"]["stats"]["total_valid_memos"] == 2
    assert looked["data"]["reconcile_lease"]["status"] == "peek"
    assert "lease_id" not in looked["data"]["reconcile_lease"]
    assert looked["data"]["reconcile_lease"]["held_by"] is None
    assert _stored_lease(repo, pid) is None

    # peek 拿到的候选不能拿去 apply
    ops = [{"op": "invalidate", "memo_ids": w["X"]["memos"][:1]}]
    assert client.post(_APPLY, headers=s2, json={"operations": ops}).json()["success"] is False
    assert _memo_rows(repo, w["X"]["memos"][:1])[w["X"]["memos"][0]].invalid_at is None

    # 有人持有时 peek：照样看得到候选，持有者的租约原封不动（不顺延、不换人）
    lease1 = _lease(client, s1)
    before = _stored_lease(repo, pid)
    other = client.get(_CANDIDATES, headers=s2, params={"peek": "true"}).json()
    assert other["success"] is True
    held = other["data"]["reconcile_lease"]
    assert held["held_by"]["holder_kind"] == "cc_session"
    assert held["held_by"]["holder_session"] == "sess-one"
    assert held["held_by_you"] is False
    mine = client.get(_CANDIDATES, headers=s1, params={"peek": "true"}).json()
    assert mine["data"]["reconcile_lease"]["held_by_you"] is True
    assert _stored_lease(repo, pid) == before
    assert before["lease_id_hash"] == _sha(lease1)


def test_refused_session_is_told_how_long_and_what_to_do(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    _lease(client, _headers(pid, "sess-one-aaaaaaaa"))
    refused = client.get(_CANDIDATES, headers=_headers(pid, "sess-two")).json()
    assert refused["success"] is False
    message = refused["error"]
    assert "CC 会话 sess-one" in message
    assert "约 30 分钟后过期" in message
    assert "peek=true" in message
    assert "memory_reconcile_apply(operations=[])" in message


def test_http_mcp_connection_is_recognised_without_lease_id(repo_and_client) -> None:
    """Codex 类宿主没有 CC 会话头：凭 MCP 连接的会话 id 认人，丢了 lease_id 也能续约与释放."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    conn_a = {"X-Project-Id": pid, "X-Aiteam-Mcp-Session-Id": "a" * 32}
    conn_b = {"X-Project-Id": pid, "X-Aiteam-Mcp-Session-Id": "b" * 32}

    first = client.get(_CANDIDATES, headers=conn_a).json()["data"]["reconcile_lease"]
    assert first["status"] == "acquired"
    assert client.get(_CANDIDATES, headers=conn_a).json()["data"]["reconcile_lease"]["status"] == "renewed"

    refused = client.get(_CANDIDATES, headers=conn_b).json()
    assert refused["success"] is False
    assert refused["reconcile_lease"]["holder_kind"] == "mcp_connection"
    assert refused["reconcile_lease"]["holder_session"] == "a" * 8
    assert "HTTP MCP 连接" in refused["error"]

    # 连接 A 忘了 lease_id：空批照样释放
    released = client.post(_APPLY, headers=conn_a, json={"operations": []}).json()
    assert released["data"]["reconcile_lease"] == {"status": "released"}
    assert client.get(_CANDIDATES, headers=conn_b).json()["success"] is True


@pytest.mark.parametrize("scope", ["team", "agent"])
def test_legacy_partition_entry_by_id_requires_confirmation(repo_and_client, scope) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx = _headers(w["X"]["pid"])
    entry = _run(repo.create_memory(scope=scope, scope_id=f"{scope}-legacy", content=f"{scope} 遗留经验"))

    refused = client.post(f"/api/memories/{entry.id}/invalidate", headers=hx, json={}).json()
    assert refused["success"] is False and refused["requires_confirmation"] is True
    assert "遗留分区" in refused["error"]
    assert _run(repo.get_memory(entry.id)).invalid_at is None

    ok = client.post(
        f"/api/memories/{entry.id}/invalidate", headers=hx, json={"confirm_shared_scope": True}
    ).json()
    assert ok["success"] is True
    assert _run(repo.get_memory(entry.id)).invalid_at is not None


def test_memo_without_project_is_reported_apart_from_foreign(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx = _headers(w["X"]["pid"])
    orphan = _run(repo.add_task_memo(w["X"]["task"], content="历史遗留：没有项目归属", project_id=None))
    y0 = w["Y"]["memos"][0]

    results = client.post(
        _APPLY,
        headers=hx,
        json={
            "lease_id": _lease(client, hx),
            "operations": [
                {"op": "invalidate", "memo_ids": [orphan.id]},
                {"op": "invalidate", "memo_ids": [orphan.id, y0]},
                {"op": "score", "memo_id": orphan.id, "quality_score": 5},
            ],
        },
    ).json()["data"]["results"]
    assert results[0]["status"] == "error"
    assert results[0]["unowned_memo_ids"] == [orphan.id]
    assert "foreign_memo_ids" not in results[0]
    assert "须先补归属" in results[0]["error"]
    assert results[1]["unowned_memo_ids"] == [orphan.id]
    assert results[1]["foreign_memo_ids"] == [y0]
    assert results[2]["unowned_memo_ids"] == [orphan.id]
    rows = _memo_rows(repo, [orphan.id, y0])
    assert rows[orphan.id].invalid_at is None and rows[orphan.id].quality_score is None
    assert rows[y0].invalid_at is None


def test_project_config_edit_keeps_the_lease_and_reconcile_stamp(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    h = _headers(pid)
    lease_id = _lease(client, h)
    _run(repo.set_last_reconcile_at(pid))
    stamped = _run(repo.get_project(pid)).config["memory"]["last_reconcile_at"]

    resp = client.put(
        f"/api/projects/{pid}",
        json={"config": {"dashboard": {"theme": "dark"}, "memory": {"note": "编辑时写的"}}},
    )
    assert resp.status_code == 200

    config = _run(repo.get_project(pid)).config
    assert config["dashboard"] == {"theme": "dark"}
    assert config["memory"]["note"] == "编辑时写的"
    assert config["memory"]["last_reconcile_at"] == stamped
    assert config["memory"]["reconcile_lease"]["lease_id_hash"] == _sha(lease_id)
    # 整理权仍然有效：持有者照常 apply
    applied = client.post(_APPLY, headers=h, json={"operations": []}).json()
    assert applied["data"]["reconcile_lease"] == {"status": "released"}


def test_leader_loop_looks_with_peek() -> None:
    """Leader 循环的例行查看不能占整理权（审查 M1 场景二：每轮续约、永久挡人）."""
    text = (ROOT / "plugin" / "loop.md").read_text(encoding="utf-8")
    assert "memory_reconcile_candidates(peek=true)" in text
    assert "memory_reconcile_apply(operations=[])" in text


# ================================================================
# 用户裁定（审查 2252d272 M2）：memory_add 的 supersedes 置换 global/user 条目
# 与失效同一道闸
# ================================================================


def _direction_rows(repo, scope: str, scope_id: str) -> list:
    return _run(repo.list_memories(scope, scope_id, include_invalidated=True))


@pytest.mark.parametrize(("scope", "scope_id"), [("global", "system"), ("user", "user")])
def test_superseding_a_shared_entry_requires_confirmation(repo_and_client, scope, scope_id) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx, hy = _headers(w["X"]["pid"]), _headers(w["Y"]["pid"])
    old_id = _direction(client, hy, scope, f"守卫用例 {scope} 原文：模型分层按规矩派工")
    rows_before = len(_direction_rows(repo, scope, scope_id))

    refused = client.post(
        "/api/memories",
        headers=hx,
        json={"content": "守卫用例放宽版：随便派", "kind": "constraint", "scope": scope, "supersedes": old_id},
    ).json()
    assert refused["success"] is False and refused["requires_confirmation"] is True
    assert refused["target"]["id"] == old_id
    assert refused["target"]["content"] == f"守卫用例 {scope} 原文：模型分层按规矩派工"
    assert refused["replacement"] == "守卫用例放宽版：随便派"
    assert "没有写入新条目" in refused["error"]
    # 读回：没有新行，旧条对别的项目照样有效
    assert len(_direction_rows(repo, scope, scope_id)) == rows_before
    assert old_id in _valid_direction(client, hy)
    assert "守卫用例放宽版：随便派" not in {
        m["content"] for m in client.get("/api/memories", headers=hy).json()["data"]
    }

    ok = client.post(
        "/api/memories",
        headers=hx,
        json={
            "content": "守卫用例放宽版：随便派",
            "kind": "constraint",
            "scope": scope,
            "supersedes": old_id,
            "confirm_shared_scope": True,
        },
    ).json()
    assert ok["success"] is True
    new_id = ok["data"]["id"]
    valid = _valid_direction(client, hy)
    assert new_id in valid and old_id not in valid
    assert _run(repo.get_memory(old_id)).invalidated_by == new_id


def test_superseding_an_own_project_entry_needs_no_confirmation(repo_and_client) -> None:
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx = _headers(w["X"]["pid"])
    old_id = _direction(client, hx, "project", "守卫用例 X 项目方向：旧版")
    ok = client.post(
        "/api/memories",
        headers=hx,
        json={"content": "守卫用例 X 项目方向：新版", "kind": "constraint", "scope": "project", "supersedes": old_id},
    ).json()
    assert ok["success"] is True
    valid = _valid_direction(client, hx)
    assert ok["data"]["id"] in valid and old_id not in valid


def test_promote_to_global_still_needs_no_confirmation(repo_and_client) -> None:
    """promote 往 global 写是新建不是置换，不在裁定范围内."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    hx = _headers(w["X"]["pid"])
    result = client.post(
        _APPLY,
        headers=hx,
        json={
            "lease_id": _lease(client, hx),
            "operations": [{"op": "promote", "content": "守卫用例提升条", "kind": "directive", "scope": "global"}],
        },
    ).json()["data"]["results"][0]
    assert result["status"] == "applied"
    assert result["memory_id"] in _valid_direction(client, hx)


def test_global_over_quota_hint_covers_supersedes(integration_client) -> None:
    filler = "占位" * 50
    for _ in range(12):
        assert integration_client.post(
            "/api/memories", json={"content": filler, "kind": "preference", "scope": "global"}
        ).json()["success"]
    over = integration_client.post(
        "/api/memories", json={"content": "再加一条", "kind": "design", "scope": "global"}
    ).json()
    assert "置换（supersedes）" in over["error"]
    assert "confirm_shared_scope" in over["next_action"]


# ================================================================
# 复核 1f62451a M1'：整理权凭据不能从任何可读面拿到
# ================================================================

_HOLDER_CC = "sess-holder-11111111-2222-3333-4444-555555555555"
_HOLDER_MCP = "c0ffee" * 5 + "ab"


def _strings(value) -> list[str]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


@pytest.mark.parametrize(
    ("holder_headers", "holder_secret"),
    [
        ({"X-CC-Session-Id": _HOLDER_CC}, _HOLDER_CC),
        ({"X-Aiteam-Mcp-Session-Id": _HOLDER_MCP}, _HOLDER_MCP),
        ({}, None),
    ],
    ids=["cc-session", "mcp-connection", "lease-id-only"],
)
def test_lease_credentials_are_not_readable_from_project_surfaces(
    repo_and_client, holder_headers, holder_secret
) -> None:
    """审查员的实验路径：被挡方读项目信息 → 拿读到的东西冒充持有者。现在必须全部失败."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    holder = {"X-Project-Id": pid, **holder_headers}
    lease_id = _lease(client, holder)

    intruder = _headers(pid, "sess-intruder")
    assert client.get(_CANDIDATES, headers=intruder).json()["success"] is False
    readable = {
        "list": client.get("/api/projects", headers=intruder).json(),
        "get": client.get(f"/api/projects/{pid}", headers=intruder).json(),
        "summary": client.get(f"/api/projects/{pid}/summary", headers=intruder).json(),
    }
    dumped = json.dumps(readable, ensure_ascii=False)
    assert lease_id not in dumped
    if holder_secret:
        assert holder_secret not in dumped
    stored = next(p for p in readable["list"]["data"] if p["id"] == pid)["config"]["memory"]["reconcile_lease"]
    assert set(stored) == {"lease_id_hash", "holder_hash", "holder_kind", "holder_prefix", "acquired_at", "expires_at"}

    # 拿读到的每一个字符串去冒充：当 lease_id 回传、当会话头发送，都必须被拒
    ops = [{"op": "invalidate", "memo_ids": w["X"]["memos"]}]
    for candidate in {s for s in _strings(readable) if s}:
        forged = client.post(_APPLY, headers=intruder, json={"operations": ops, "lease_id": candidate}).json()
        assert forged["success"] is False, candidate
        for header in ("X-CC-Session-Id", "X-Aiteam-Mcp-Session-Id"):
            posed = client.get(_CANDIDATES, headers={"X-Project-Id": pid, header: candidate}).json()
            assert posed["success"] is False, (header, candidate)
    assert all(m.invalid_at is None for m in _memo_rows(repo, w["X"]["memos"]).values())
    assert _stored_lease(repo, pid)["lease_id_hash"] == _sha(lease_id)

    # 持有者本人不受影响：凭自己的身份或 lease_id 照常 apply 并释放
    done = client.post(_APPLY, headers=holder, json={"operations": ops, "lease_id": lease_id}).json()["data"]
    assert done["results"][0]["status"] == "applied"
    assert done["reconcile_lease"] == {"status": "released"}


def test_plaintext_lease_from_before_hashing_counts_as_no_lease(repo_and_client) -> None:
    """旧库兼容：明文形态的记录一律按无租约处理，不迁移（泄漏过的明文换不来任何东西）."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    legacy = {
        "lease_id": "0" * 32,
        "holder_session": "sess-legacy",
        "acquired_at": "2026-09-28T00:00:00+00:00",
        "expires_at": "2999-01-01T00:00:00+00:00",
    }
    _run(repo.update_project(pid, config={"memory": {"reconcile_lease": legacy}}))
    ops = [{"op": "invalidate", "memo_ids": w["X"]["memos"][:1]}]
    posed = client.post(
        _APPLY, headers=_headers(pid, "sess-legacy"), json={"operations": ops, "lease_id": "0" * 32}
    ).json()
    assert posed["success"] is False
    taken = client.get(_CANDIDATES, headers=_headers(pid, "sess-new")).json()
    assert taken["success"] is True and taken["data"]["reconcile_lease"]["status"] == "acquired"
    assert "lease_id" not in _stored_lease(repo, pid)


def test_cc_session_header_cannot_pose_as_an_mcp_connection(repo_and_client) -> None:
    """两种身份不共用哈希空间：X-CC-Session-Id 填 "mcp:<id>" 认不成 HTTP MCP 连接的持有者."""
    repo, client = repo_and_client
    w = _two_projects(repo)
    pid = w["X"]["pid"]
    _lease(client, {"X-Project-Id": pid, "X-Aiteam-Mcp-Session-Id": _HOLDER_MCP})
    posed = client.get(
        _CANDIDATES, headers={"X-Project-Id": pid, "X-CC-Session-Id": f"mcp:{_HOLDER_MCP}"}
    ).json()
    assert posed["success"] is False
    ops = [{"op": "invalidate", "memo_ids": w["X"]["memos"][:1]}]
    forged = client.post(
        _APPLY, headers={"X-Project-Id": pid, "X-CC-Session-Id": f"mcp:{_HOLDER_MCP}"}, json={"operations": ops}
    ).json()
    assert forged["success"] is False
    assert _memo_rows(repo, w["X"]["memos"][:1])[w["X"]["memos"][0]].invalid_at is None
