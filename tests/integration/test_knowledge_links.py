"""知识层 P1a — 跨域引用图谱集成测试（docs/knowledge-layer-design.md）。

覆盖：抽取器纯函数 → memo/report 写入自动建边 → 正反向查询 → 递归扇出回溯。
"""

from __future__ import annotations

from aiteam.api.link_extract import extract_refs

# ── 抽取器单测（纯函数，中英混排）──


def test_extract_wf_and_commit_with_fix_cue():
    text = "修复了 wf_cbad7348-934 的建队断链，commit 3a31d8b 已提交并验证"
    refs = {(r.to_kind, r.to_id): r for r in extract_refs(text)}
    assert ("run", "wf_cbad7348-934") in refs
    assert refs[("run", "wf_cbad7348-934")].link_type == "fixes"
    assert ("commit", "3a31d8b") in refs
    assert refs[("commit", "3a31d8b")].link_type == "fixes"


def test_extract_bare_hex_without_cue_is_ignored():
    # 无 commit 语境词的裸 hex（如日志片段）不得误抽
    refs = extract_refs("会话 abff40af 的数据里出现了 deadbeef99 字样")
    assert all(r.to_kind != "commit" for r in refs)


def test_extract_uuid_and_memory_link():
    text = (
        "关联任务 8d1feb9e-9485-416f-9dae-c884598a1fb6，"
        "背景见 [[ai-team-os-attribution-principle]]"
    )
    kinds = {(r.to_kind, r.to_id) for r in extract_refs(text)}
    assert ("task", "8d1feb9e-9485-416f-9dae-c884598a1fb6") in kinds
    assert ("memory", "ai-team-os-attribution-principle") in kinds


def test_extract_dedup_and_context_snapshot():
    text = "wf_12345678 出现两次：wf_12345678 也在这里"
    refs = extract_refs(text)
    assert len([r for r in refs if r.to_kind == "run"]) == 1
    assert "wf_12345678" in refs[0].context


def test_extract_empty_and_plain_chinese():
    assert extract_refs("") == []
    assert extract_refs("纯中文内容没有任何引用标识") == []


# ── 集成：写入自动建边 → 查询 → 扇出 ──


def test_memo_write_creates_links_and_fanout(repo_and_client):
    repo, client = repo_and_client
    import asyncio

    loop = asyncio.get_event_loop()
    project = loop.run_until_complete(
        repo.create_project(name="kl-test", root_path="/tmp/kl-test")
    )
    project_id = project.id
    team = loop.run_until_complete(
        repo.create_team(name="kl-team", mode="coordinate", project_id=project_id)
    )
    task = loop.run_until_complete(
        repo.create_task(
            team_id=team.id, title="知识层测试任务", project_id=project_id
        )
    )
    task_id = task.id

    # 写 memo：引用一个 run 和一个 commit（带修复语境）
    r = client.post(
        f"/api/tasks/{task_id}/memo",
        json={
            "content": "修复 wf_deadbeef 的断链，commit abc1234 提交",
            "author": "leader",
            "type": "progress",
        },
    )
    assert r.status_code == 200

    # 正向查：该 run 被谁引用（in 方向）
    links = client.get(
        "/api/links", params={"kind": "run", "id": "wf_deadbeef", "direction": "in"}
    ).json()["data"]
    assert len(links) == 1
    assert links[0]["from_kind"] == "task_memo"
    assert links[0]["link_type"] == "fixes"
    assert links[0]["project_id"] == project_id

    # 扇出：从 commit 出发 2 跳应可达 run（经同一条 memo）
    fanout = client.get(
        "/api/links/fanout", params={"kind": "commit", "id": "abc1234", "depth": 2}
    ).json()["data"]
    reachable = {(n["kind"], n["id"]) for n in fanout}
    assert ("task_memo", links[0]["from_id"]) in reachable
    assert ("run", "wf_deadbeef") in reachable


def test_duplicate_memo_links_are_idempotent(repo_and_client):
    repo, client = repo_and_client
    # 幂等性验证针对同一 from_id 的 UNIQUE 冲突，用 repo 直插模拟回扫重跑
    import asyncio

    from aiteam.types import KnowledgeLink

    lk = KnowledgeLink(
        from_kind="report",
        from_id="rep-1",
        to_kind="run",
        to_id="wf_11112222",
        link_source="regex-report",
    )
    loop = asyncio.get_event_loop()
    n1 = loop.run_until_complete(repo.insert_knowledge_links([lk]))
    n2 = loop.run_until_complete(repo.insert_knowledge_links([lk]))
    assert n1 == 1 and n2 == 0  # 重跑零新增


def test_report_write_creates_links(repo_and_client):
    repo, client = repo_and_client
    r = client.post(
        "/api/reports",
        json={
            "author": "leader",
            "topic": "kl-report",
            "content": "分析 wf_aabbccdd 的运行数据，参考 [[some-memory-note]]",
        },
    )
    assert r.status_code == 201
    links = client.get(
        "/api/links", params={"kind": "run", "id": "wf_aabbccdd", "direction": "in"}
    ).json()["data"]
    assert len(links) == 1
    assert links[0]["from_kind"] == "report"
    mem = client.get(
        "/api/links",
        params={"kind": "memory", "id": "some-memory-note", "direction": "in"},
    ).json()["data"]
    assert len(mem) == 1


# ── P1b：统一检索三臂 RRF ──


def test_unified_search_bm25_chinese(repo_and_client):
    repo, client = repo_and_client
    import asyncio

    loop = asyncio.get_event_loop()
    project = loop.run_until_complete(
        repo.create_project(name="搜索测试", root_path="/tmp/us-test")
    )
    team = loop.run_until_complete(
        repo.create_team(name="us-team", mode="coordinate", project_id=project.id)
    )
    task = loop.run_until_complete(
        repo.create_task(
            team_id=team.id, title="火星归属修复批次", project_id=project.id
        )
    )
    client.post(
        f"/api/tasks/{task.id}/memo",
        json={
            "content": "火星 workflow 误入 OS 项目，归属改文件真相源后迁回火星项目",
            "author": "leader",
            "type": "progress",
        },
    )

    # 中文查询（bigram 命中 memo 与任务标题）
    r = client.get("/api/search", params={"q": "火星归属"}).json()["data"]
    assert r, "中文 bigram 查询应有结果"
    kinds = {x["kind"] for x in r}
    assert "task" in kinds or "task_memo" in kinds
    assert any("火星" in x["snippet"] or "火星" in x["title"] for x in r)


def test_unified_search_graph_arm_by_id(repo_and_client):
    repo, client = repo_and_client
    import asyncio

    loop = asyncio.get_event_loop()
    project = loop.run_until_complete(
        repo.create_project(name="图谱臂测试", root_path="/tmp/us-graph")
    )
    team = loop.run_until_complete(
        repo.create_team(name="ga-team", mode="coordinate", project_id=project.id)
    )
    task = loop.run_until_complete(
        repo.create_task(team_id=team.id, title="观测修复", project_id=project.id)
    )
    client.post(
        f"/api/tasks/{task.id}/memo",
        json={
            "content": "修复 wf_99887766 的断链问题，commit fedcba9 提交",
            "author": "leader",
            "type": "progress",
        },
    )

    # 用 run ID 查询 → 图谱臂应召回引用它的 memo
    r = client.get("/api/search", params={"q": "wf_99887766"}).json()["data"]
    assert any(x["kind"] == "task_memo" for x in r), "图谱臂应召回引用该 run 的 memo"

    # 用关联 commit 查询 → 2 跳扇出仍可达同一条 memo
    r2 = client.get("/api/search", params={"q": "fedcba9"}).json()["data"]
    assert any(x["kind"] == "task_memo" for x in r2)


def test_unified_search_empty_and_no_hit(repo_and_client):
    repo, client = repo_and_client
    r = client.get("/api/search", params={"q": "zzz不存在的查询词xyz"}).json()["data"]
    assert isinstance(r, list)
