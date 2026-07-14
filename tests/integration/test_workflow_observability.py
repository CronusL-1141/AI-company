"""AI Team OS — I3a Workflow 可观测层 端到端测试（TestClient / 内存 SQLite，离线可跑）。

覆盖设计文档 8.2 验收：EventType 往返、parse_workflow_receipt 抽键、
ingest_run_from_file 落 run+agents+team 回写+completed 事件、PostToolUse 回执骨架、
三读端点、POST /reconcile、幂等、项目隔离。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiteam.api import workflow_ingest
from aiteam.api.app import create_app
from aiteam.api.deps import (
    get_event_bus,
    get_hook_translator,
    get_repository,
    get_scoped_repository,
)
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType

WF_ID = "wf_8e92fe01-67c"
PID_A = "proj-wf-a-0001"
PID_B = "proj-wf-b-0002"

RECEIPT = (
    "Workflow launched in background. Task ID: westwrtgj\n"
    "Summary: 多路并行调研国家知识产权局专利电子申请 XML 文件的确切格式\n"
    "Transcript dir: /Users/cronus/.claude/projects/-Users-cronus-Desktop-Test/"
    "SESSION/subagents/workflows/wf_8e92fe01-67c\n"
    "Script file: /Users/cronus/.claude/projects/-Users-cronus-Desktop-Test/"
    "SESSION/workflows/scripts/cnipa-xml-format-research-wf_8e92fe01-67c.js\n"
    '(Edit this file with Write/Edit and re-invoke Workflow with {scriptPath: "..."})'
)

WF_SCRIPT = (
    "export const meta = {\n"
    "  name: 'cnipa-xml-format-research',\n"
    "  phases: [ { title: '调研' }, { title: '汇总' } ],\n"
    "};\n"
    "agent({ label: 'a1' });\n"
    "agent({ label: 'a2' });\n"
)


def _fixture_snapshot() -> dict:
    """真实 18 键快照的裁剪版（wf_8e92fe01-67c），数值字段沿用字符串型（同真快照）。"""
    return {
        "runId": WF_ID,
        "timestamp": "2026-06-12T11:26:02.248Z",
        "taskId": "westwrtgj",
        "script": "export const meta = {...}",
        "scriptPath": "/Users/x/workflows/scripts/cnipa-xml-format-research-wf_8e92fe01-67c.js",
        "result": {"synthesis": "结论...", "rawFindings": ["a", "b"]},
        "agentCount": "2",
        "logs": ["done"],
        "durationMs": "1498565",
        "summary": "多路并行调研国家知识产权局专利电子申请 XML 文件的确切格式",
        "workflowName": "cnipa-xml-format-research",
        "status": "completed",
        "startTime": "1781262063681",
        "phases": [
            {"title": "调研", "detail": "4 路并行研究员"},
            {"title": "汇总", "detail": "交叉比对"},
        ],
        "defaultModel": "claude-fable-5",
        "totalTokens": "551440",
        "totalToolCalls": "297",
        "workflowProgress": [
            {"type": "workflow_phase", "index": 1, "title": "调研"},
            {
                "type": "workflow_agent",
                "index": 1,
                "label": "调研:cpc-samples",
                "phaseIndex": 1,
                "phaseTitle": "调研",
                "agentId": "aa3b60f522593a7f8",
                "model": "claude-fable-5",
                "state": "done",
                "startedAt": 1781262063718,
                "queuedAt": 1781262063705,
                "lastToolName": "StructuredOutput",
                "lastToolSummary": "high",
                "promptPreview": "你是研究员...",
                "tokens": 79848,
                "toolCalls": 58,
                "durationMs": 790705,
                "resultPreview": "{\"confidence\":\"high\"}",
            },
            {
                "type": "workflow_agent",
                "index": 2,
                "label": "汇总:synth",
                "phaseIndex": 2,
                "phaseTitle": "汇总",
                "agentId": "bb9c71e633604b8g9",
                "model": "claude-opus-4-8[1m]",
                "state": "done",
                "startedAt": 1781262854000,
                "queuedAt": 1781262853000,
                "lastToolName": "StructuredOutput",
                "lastToolSummary": "done",
                "promptPreview": "交叉汇总...",
                "tokens": 120000,
                "toolCalls": 40,
                "durationMs": 200000,
                "resultPreview": "最终结论",
            },
        ],
    }


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def event_bus(repo: StorageRepository) -> EventBus:
    return EventBus(repo=repo)


@pytest_asyncio.fixture()
async def client(repo: StorageRepository, event_bus: EventBus) -> AsyncClient:
    translator = HookTranslator(repo=repo, event_bus=event_bus)
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_scoped_repository] = lambda: repo
    app.dependency_overrides[get_event_bus] = lambda: event_bus
    app.dependency_overrides[get_hook_translator] = lambda: translator
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac  # type: ignore[misc]


# ============================================================
# 1. EventType 往返不抛（对照现状 workflow.planned -> ValueError）
# ============================================================


def test_eventtype_workflow_members_roundtrip():
    for val in ("workflow.planned", "workflow.started", "workflow.completed"):
        et = EventType(val)  # 修复前这里会抛 ValueError
        assert et.value == val


# ============================================================
# 2. parse_workflow_receipt 抽键
# ============================================================


def test_parse_workflow_receipt():
    r = workflow_ingest.parse_workflow_receipt(RECEIPT)
    assert r["wf_id"] == WF_ID
    assert r["cc_task_id"] == "westwrtgj"
    assert r["name"] == "cnipa-xml-format-research"
    assert r["summary"].startswith("多路并行调研")
    assert r["script_path"].endswith("cnipa-xml-format-research-wf_8e92fe01-67c.js")
    assert r["transcript_dir"].endswith("subagents/workflows/wf_8e92fe01-67c")


def test_parse_workflow_receipt_empty():
    r = workflow_ingest.parse_workflow_receipt("garbage no keys here")
    assert r["wf_id"] == ""
    assert r["cc_task_id"] == ""


def test_wf_run_id_re_bounded_does_not_swallow_worktree_suffix():
    """Regression for task f8207497: _WF_RUN_ID_RE used an unbounded `(?:-[0-9a-z]+)*`
    group that greedily consumed CC's per-branch worktree instance suffix
    (".claude/worktrees/wf_a69e7d46-a66-1" -> branch "worktree-wf_a69e7d46-a66-1"),
    over-matching the run id to "wf_a69e7d46-a66-1" instead of the true run id
    "wf_a69e7d46-a66". That mismatch made team lookups miss the real wf_<id>.json
    snapshot and spawn an orphan team. Bounded to a single optional dash-suffix, this
    must stop exactly at the run id.
    """
    m = workflow_ingest._WF_RUN_ID_RE.search(
        "cnipa-xml-format-research-wf_a69e7d46-a66-1.js"
    )
    assert m is not None
    assert m.group(0) == "wf_a69e7d46-a66"
    # Legacy single-suffix run ids (the module's own docstring example) must still
    # match in full — the fix must not regress the common case.
    m2 = workflow_ingest._WF_RUN_ID_RE.search(
        "cnipa-xml-format-research-wf_8e92fe01-67c.js"
    )
    assert m2 is not None
    assert m2.group(0) == "wf_8e92fe01-67c"


def test_extract_workflow_run_id_from_worktree_cwd():
    """Same regression, exercised through the real extraction method (not just the
    bare regex) with a cwd payload shaped like an actual worktree in this repo
    (see `git worktree list`: .claude/worktrees/wf_a69e7d46-a66-1, branch
    worktree-wf_a69e7d46-a66-1).
    """
    payload = {
        "cwd": "/repo/.claude/worktrees/wf_a69e7d46-a66-1",
    }
    wf_id = HookTranslator._extract_workflow_run_id(None, payload)
    assert wf_id == "wf_a69e7d46-a66"


# ============================================================
# 3. ingest_run_from_file → run + N agents + team 回写 + completed 事件
# ============================================================


@pytest.mark.asyncio
async def test_ingest_run_from_file(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    # 预置既有 workflow-<wf_id> 团队 + 一个 cc_tool_use_id 匹配的成员（测 os_agent_id 关联）。
    team = await repo.create_team(
        name=f"workflow-{WF_ID}",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": WF_ID},
        project_id=PID_A,
    )
    member = await repo.create_agent(
        team_id=team.id, name="wf-aa3b60f522", role="workflow-subagent",
        source="hook", cc_tool_use_id="aa3b60f522593a7f8",
    )

    wf_file = tmp_path / f"{WF_ID}.json"
    wf_file.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")

    res = await workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)
    assert res["ok"] is True
    assert res["agents"] == 2
    assert res["emitted"] is True

    run = await repo.get_workflow_run(WF_ID)
    assert run is not None
    assert run.status == "completed"
    assert run.total_tokens == 551440
    assert run.total_tool_calls == 297
    assert run.agent_count == 2
    assert run.duration_ms == 1498565
    assert run.team_id == team.id
    assert run.project_id == PID_A
    assert run.completed_at is not None
    # phases 归一为 [{index,title}]
    assert run.phases == [{"index": 1, "title": "调研"}, {"index": 2, "title": "汇总"}]

    agents = await repo.list_workflow_agents(WF_ID)
    assert len(agents) == 2
    # os_agent_id 关联既有成员（agents.cc_tool_use_id == cc_agent_id）
    linked = [a for a in agents if a.cc_agent_id == "aa3b60f522593a7f8"]
    assert linked and linked[0].os_agent_id == member.id
    assert linked[0].tokens == 79848 and linked[0].tool_calls == 58

    # team.completed_at 回写（既有 nullable 字段写入）
    team_after = await repo.get_team(team.id)
    assert team_after.completed_at is not None

    # emit workflow.completed
    events = await repo.list_events(type_prefix="workflow.")
    assert any(e.type == EventType.WORKFLOW_COMPLETED for e in events)


# ============================================================
# 7. 幂等：连跑两次 → 无重复行、totals 不翻倍、不重复 emit completed
# ============================================================


@pytest.mark.asyncio
async def test_ingest_idempotent(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    wf_file = tmp_path / f"{WF_ID}.json"
    wf_file.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")

    await workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)
    await workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)

    runs = await repo.list_workflow_runs()
    assert len([r for r in runs if r.wf_id == WF_ID]) == 1
    run = await repo.get_workflow_run(WF_ID)
    assert run.total_tokens == 551440  # 不翻倍
    agents = await repo.list_workflow_agents(WF_ID)
    assert len(agents) == 2  # 不翻倍

    completed = [
        e for e in await repo.list_events(type_prefix="workflow.")
        if e.type == EventType.WORKFLOW_COMPLETED
    ]
    assert len(completed) == 1  # 事件不重复（transition-guard）


# ============================================================
# 4. POST /api/hooks/event 合成 PostToolUse(Workflow) 回执 → 骨架 + workflow.started
#    （顺带走 PreToolUse 暂存计划，验证 planned_agent_count 补齐 + workflow.planned 落库）
# ============================================================


@pytest.mark.asyncio
async def test_hook_receipt_creates_running_skeleton(
    client: AsyncClient, repo: StorageRepository
):
    session_id = "sess-hook-1"
    # PreToolUse(Workflow)：暂存计划 + emit workflow.planned（枚举修好后才真正落库）。
    pre = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PreToolUse",
            "session_id": session_id,
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
        },
    )
    assert pre.status_code == 200

    planned = [
        e for e in await repo.list_events(type_prefix="workflow.")
        if e.type == EventType.WORKFLOW_PLANNED
    ]
    assert planned, "workflow.planned 应已落库（枚举修复生效）"

    # PostToolUse(Workflow)：回执明文 → run 骨架(running) + workflow.started。
    post = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
        },
    )
    assert post.status_code == 200

    run = await repo.get_workflow_run(WF_ID)
    assert run is not None
    assert run.status == "running"
    assert run.source == "hook"
    assert run.cc_task_id == "westwrtgj"
    assert run.name == "cnipa-xml-format-research"
    # 计划补齐：literal agent() 计数 = 2
    assert run.planned_agent_count == 2
    assert run.phases == [{"index": 1, "title": "调研"}, {"index": 2, "title": "汇总"}]

    started = [
        e for e in await repo.list_events(type_prefix="workflow.")
        if e.type == EventType.WORKFLOW_STARTED
    ]
    assert started


@pytest.mark.asyncio
async def test_hook_receipt_adopts_session_fallback_team(
    client: AsyncClient, repo: StorageRepository
):
    """回执认养 session 兜底队（反「一 run 两队」碎片化回归测试）。

    wf_id 迟到时早到的 agent 全挂在 workflow-session-<sid[:8]> 兜底队；回执是
    第一个拿到 wf_id 的时点，必须就地补上 run.team_id 与 team.config.
    workflow_run_id 双向链接（2026-07-06 监控实录：不补则 live 全程双向皆空，
    终态对账还会另建空的 workflow-<wf_id> 队造成碎片化）。
    """
    session_id = "sess-adopt-1"
    team = await repo.create_team(
        name=f"workflow-session-{session_id[:8]}",
        mode="coordinate",
        config={"kind": "workflow", "auto_created": True, "workflow_run_id": None},
    )

    post = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
        },
    )
    assert post.status_code == 200

    run = await repo.get_workflow_run(WF_ID)
    assert run is not None
    assert run.team_id == team.id, "run 应认养 session 兜底队而非留空"

    linked = await repo.get_team_by_name(team.name)
    assert (linked.config or {}).get("workflow_run_id") == WF_ID


@pytest.mark.asyncio
async def test_session_end_spares_workflow_teams(
    client: AsyncClient, repo: StorageRepository
):
    """SessionEnd 只关本 session 拥有的队（fleet-layer §5，合并 7ae3b7cd）。

    历史两桩误杀都在此覆盖：① 旁路会话 SessionEnd 曾把别会话仍 running 的 workflow
    队全部误杀成 completed+0 成员（2026-07-08 c4fab878 杀 abff40af）；② 更宽的普通队
    误杀——旁路会话 SessionEnd 关全库所有 active 普通队，误杀别会话正在用的队。
    """
    # workflow 队（别会话拥有）——必须豁免
    wf_team = await repo.create_team(
        name="workflow-wf_spare-1", mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_spare-1"},
    )
    member = await repo.create_agent(
        team_id=wf_team.id, name="wf-spare1", role="workflow-subagent",
        session_id="sess-other-9",
    )
    await repo.update_agent(member.id, status="busy")
    # 别会话拥有的普通队——旁路 SessionEnd 不得关（7ae3b7cd 核心）
    owned_by_other = await repo.create_team(
        name="t-owned-other", mode="coordinate",
        config={"owner_session_id": "sess-other-9"},
    )
    # 无归属标记的遗留普通队——保守不关（等 reaper 按自身活性收）
    legacy = await repo.create_team(name="t-legacy-se", mode="coordinate")
    # 结束会话自己拥有的普通队——必须关
    owned_by_ending = await repo.create_team(
        name="t-owned-ending", mode="coordinate",
        config={"owner_session_id": "sess-bystander-1"},
    )

    resp = await client.post(
        "/api/hooks/event",
        json={"hook_event_name": "SessionEnd", "session_id": "sess-bystander-1"},
    )
    assert resp.status_code == 200

    wf_after = await repo.get_team(wf_team.id)
    assert str(wf_after.status).endswith("active"), "workflow 队不得被 SessionEnd 关闭"
    m_after = (await repo.find_agents_by_session("sess-other-9"))[0]
    assert str(m_after.status).endswith("busy"), "workflow 成员不得被 SessionEnd 清扫"
    other_after = await repo.get_team(owned_by_other.id)
    assert str(other_after.status).endswith("active"), "别会话拥有的普通队不得被误杀"
    legacy_after = await repo.get_team(legacy.id)
    assert str(legacy_after.status).endswith("active"), "无归属遗留队保守不关，交 reaper"
    ending_after = await repo.get_team(owned_by_ending.id)
    assert str(ending_after.status).endswith("completed"), "本会话拥有的队必须被关闭"


@pytest.mark.asyncio
async def test_ingest_team_status_follows_run(
    repo: StorageRepository, event_bus: EventBus, tmp_path
):
    """队状态跟随 run：running run 的队被误杀成 completed 后，下个 ingest 周期
    自动复活 active；终态 run 的 active 队收敛 completed。"""
    import json as _json

    from aiteam.api import workflow_ingest as wi

    team = await repo.create_team(
        name="workflow-wf_follow-1", mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_follow-1"},
    )
    await repo.update_team(team.id, status="completed")  # 模拟被误杀

    wf_json = tmp_path / "wf_follow-1.json"
    wf_json.write_text(_json.dumps({
        "runId": "wf_follow-1", "workflowName": "follow-test",
        "status": "running", "startTime": 1783500000000,
        "workflowProgress": [],
    }))
    await wi.ingest_run_from_file(repo, event_bus, wf_json)
    revived = await repo.get_team_by_name("workflow-wf_follow-1")
    assert str(revived.status).endswith("active"), "running run 的队应被复活"

    wf_json.write_text(_json.dumps({
        "runId": "wf_follow-1", "workflowName": "follow-test",
        "status": "completed", "startTime": 1783500000000, "durationMs": 1000,
        "workflowProgress": [],
    }))
    await wi.ingest_run_from_file(repo, event_bus, wf_json)
    closed = await repo.get_team_by_name("workflow-wf_follow-1")
    assert str(closed.status).endswith("completed"), "终态 run 的队应收敛 completed"


@pytest.mark.asyncio
async def test_hook_receipt_creates_per_run_team(
    client: AsyncClient, repo: StorageRepository
):
    """回执直接建 per-run 队（2026-07-07 D1 实录回归）。

    per-run 队原本只在 SubagentStop 的 promote 时刻懒创建——被 kill 在
    turn 中途 / 长 turn 未结束的 run 永远无队，项目页全程隐形。回执是
    每条 run 最早可靠拿到 wf_id 的时点，无认养对象时必须就地建队。
    """
    post = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": "sess-mkteam-1",
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
        },
    )
    assert post.status_code == 200

    team = await repo.get_team_by_name(f"workflow-{WF_ID}")
    assert team is not None, "回执应就地创建 per-run 队"
    assert (team.config or {}).get("workflow_run_id") == WF_ID
    run = await repo.get_workflow_run(WF_ID)
    assert run.team_id == team.id


@pytest.mark.asyncio
async def test_hook_receipt_migrates_busy_agents_from_occupied_fallback(
    client: AsyncClient, repo: StorageRepository
):
    """兜底队被别的 run 占用时：建新 per-run 队并只迁走仍活跃(busy)的成员，
    历史 offline 成员留在兜底队（避免把昨天 run 的尸体拖进新队）。"""
    session_id = "sess-mig-1"
    fallback = await repo.create_team(
        name=f"workflow-session-{session_id[:8]}",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_older-run"},
    )
    live = await repo.create_agent(
        team_id=fallback.id, name="wf-live1", role="workflow-subagent",
        session_id=session_id,
    )
    await repo.update_agent(live.id, status="busy")
    dead = await repo.create_agent(
        team_id=fallback.id, name="wf-dead1", role="workflow-subagent",
        session_id=session_id,
    )
    await repo.update_agent(dead.id, status="offline")

    post = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": session_id,
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
        },
    )
    assert post.status_code == 200

    team = await repo.get_team_by_name(f"workflow-{WF_ID}")
    assert team is not None and team.id != fallback.id
    live2 = (await repo.find_agents_by_session(session_id))
    by_name = {a.name: a for a in live2}
    assert by_name["wf-live1"].team_id == team.id, "busy 成员应迁入 per-run 队"
    assert by_name["wf-dead1"].team_id == fallback.id, "offline 成员应留在兜底队"


# ============================================================
# 5. GET 三端点
# ============================================================


@pytest.mark.asyncio
async def test_read_endpoints(
    client: AsyncClient, repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    wf_file = tmp_path / f"{WF_ID}.json"
    wf_file.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")
    await workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)

    # GET /api/workflows
    lst = await client.get("/api/workflows")
    assert lst.status_code == 200
    body = lst.json()
    assert body["total"] >= 1
    assert any(r["wf_id"] == WF_ID for r in body["data"])

    # ?status= 过滤
    lst_running = await client.get("/api/workflows?status=running")
    assert lst_running.json()["total"] == 0
    lst_done = await client.get("/api/workflows?status=completed")
    assert any(r["wf_id"] == WF_ID for r in lst_done.json()["data"])

    # GET /api/workflows/{wf_id}
    detail = await client.get(f"/api/workflows/{WF_ID}")
    assert detail.status_code == 200
    assert detail.json()["total_tokens"] == 551440

    # GET /api/workflows/{wf_id}/agents
    ag = await client.get(f"/api/workflows/{WF_ID}/agents")
    assert ag.status_code == 200
    assert ag.json()["total"] == 2

    # 404
    missing = await client.get("/api/workflows/wf_does-not-exist")
    assert missing.status_code == 404


# ============================================================
# 6. POST /api/workflows/reconcile（temp 目录，monkeypatch projects 根）
# ============================================================


@pytest.mark.asyncio
async def test_reconcile_endpoint(
    client: AsyncClient, repo: StorageRepository, tmp_path: Path, monkeypatch
):
    root_path = "/tmp/test-workflows-project"
    await repo.create_project(name="wf-recon", root_path=root_path)
    slug = workflow_ingest._project_slug(root_path)

    base = tmp_path / "projects"
    run_dir = base / slug / "SESSION-XYZ" / "workflows"
    run_dir.mkdir(parents=True)
    (run_dir / f"{WF_ID}.json").write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")

    monkeypatch.setattr(workflow_ingest, "_claude_projects_dir", lambda: base)

    resp = await client.post(
        "/api/workflows/reconcile", json={"project_dir": root_path}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ingested"] == 1
    assert data["scanned"] == 1

    run = await repo.get_workflow_run(WF_ID)
    assert run is not None and run.status == "completed"


# ============================================================
# 8. 项目隔离：带 project_id 的 run 只在对应 scope 可见
# ============================================================


@pytest.mark.asyncio
async def test_project_isolation(repo: StorageRepository):
    from aiteam.types import WorkflowRun

    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_aaa-1", project_id=PID_A, status="completed"))
    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_bbb-2", project_id=PID_B, status="completed"))

    # 全局仓看到两条
    assert len(await repo.list_workflow_runs()) == 2

    # scoped 仓（project_scope=A）只看到 A
    scoped_a = StorageRepository(db_url=repo._db_url, project_scope=PID_A)
    runs_a = await scoped_a.list_workflow_runs()
    assert len(runs_a) == 1 and runs_a[0].wf_id == "wf_aaa-1"
    # 跨 scope get 不可见（不重演 teams 全消失，但隔离生效）
    assert await scoped_a.get_workflow_run("wf_bbb-2") is None
    assert await scoped_a.get_workflow_run("wf_aaa-1") is not None

    # 端点 ?project_id= 过滤
    runs_b_query = await repo.list_workflow_runs(project_id=PID_B)
    assert len(runs_b_query) == 1 and runs_b_query[0].wf_id == "wf_bbb-2"


# ============================================================
# Phase2 §10.9 后端验收（live tail / lastCtx / fingerprint / interrupted / .output）
# ============================================================

import os as _os  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

from aiteam.types import WorkflowRun as _WFRun  # noqa: E402

WF_LIVE = "wf_live0001-abc"


def _journal_line(kind: str, ccid: str) -> str:
    return json.dumps({"type": kind, "key": f"v2:{ccid}-key", "agentId": ccid}) + "\n"


def _assistant_line(inp: int, cc: int, cr: int, out: int, ts: str = "2026-07-06T03:00:05.000Z") -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": cr,
                "output_tokens": out,
            },
        },
    }) + "\n"


def _user_line(ts: str = "2026-07-06T03:00:00.000Z") -> str:
    return json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": "go"}}) + "\n"


async def _setup_live_run(
    repo: StorageRepository,
    tmp_path: Path,
    monkeypatch,
    wf_id: str = WF_LIVE,
    session: str = "SESS-LIVE",
    root_path: str = "/tmp/wf-live-project",
):
    """live 环境脚手架：项目 + running run + <slug>/<session>/subagents/workflows/<wf_id>/。"""
    proj = await repo.create_project(name=f"wf-live-{wf_id[-4:]}", root_path=root_path)
    slug = workflow_ingest._project_slug(root_path)
    base = tmp_path / "projects"
    wf_dir = base / slug / session / "subagents" / "workflows" / wf_id
    wf_dir.mkdir(parents=True)
    monkeypatch.setattr(workflow_ingest, "_claude_projects_dir", lambda: base)
    await repo.upsert_workflow_run(_WFRun(
        wf_id=wf_id, project_id=proj.id, session_id=session,
        status="running", source="hook", cc_task_id="tsk123abc",
    ))
    run = await repo.get_workflow_run(wf_id)
    return proj, base, wf_dir, run


# ---------- 1. EventType：新 2 成员往返 + MVP 3 成员原样 ----------


def test_eventtype_phase2_members_roundtrip():
    for val in ("workflow.agent_updated", "workflow.run_ingested"):
        et = EventType(val)
        assert et.value == val
    # MVP 3 成员原样（append-only 不破坏）
    for val in ("workflow.planned", "workflow.started", "workflow.completed"):
        assert EventType(val).value == val


# ---------- 2. 老库升级：文件库 init_db → 5 新列就位，既有行默认 0/''/NULL ----------


@pytest.mark.asyncio
async def test_file_db_migration_adds_phase2_columns(tmp_path: Path):
    import sqlite3

    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)
    # MVP 版 schema（无 Phase2 5 列）
    con.execute(
        "CREATE TABLE workflow_runs ("
        "id TEXT PRIMARY KEY, wf_id TEXT, project_id TEXT, team_id TEXT, session_id TEXT,"
        "cc_task_id TEXT, name TEXT, status TEXT, source TEXT, phases TEXT,"
        "planned_agent_count INTEGER, dynamic_nodes INTEGER, agent_count INTEGER,"
        "total_tokens INTEGER, total_tool_calls INTEGER, duration_ms INTEGER,"
        "summary TEXT, result TEXT, script_path TEXT, started_at DATETIME,"
        "completed_at DATETIME, created_at DATETIME, updated_at DATETIME)"
    )
    con.execute(
        "CREATE TABLE workflow_agents ("
        "id TEXT PRIMARY KEY, run_id TEXT, wf_id TEXT, project_id TEXT, cc_agent_id TEXT,"
        "os_agent_id TEXT, label TEXT, phase_index INTEGER, phase_title TEXT, model TEXT,"
        "state TEXT, tokens INTEGER, tool_calls INTEGER, duration_ms INTEGER,"
        "last_tool_name TEXT, last_tool_summary TEXT, prompt_preview TEXT,"
        "result_preview TEXT, started_at DATETIME, queued_at DATETIME,"
        "created_at DATETIME, updated_at DATETIME)"
    )
    con.execute(
        "INSERT INTO workflow_runs (id, wf_id, status, source, phases, name, project_id,"
        " summary, script_path, created_at, updated_at) VALUES"
        " ('old-1', 'wf_old-1', 'completed', 'file', '[]', 'legacy', '', '', '',"
        "  '2026-07-01 00:00:00.000000', '2026-07-01 00:00:00.000000')"
    )
    con.commit()
    con.close()

    r2 = StorageRepository(db_url=f"sqlite+aiosqlite:///{db_file}")
    await r2.init_db()  # create_all（跳过既有表）+ COLUMNS_TO_ENSURE ALTER 迁移

    run = await r2.get_workflow_run("wf_old-1")  # ORM 读通 = 列真实就位
    assert run is not None
    assert run.journal_offset == 0
    assert run.source_fingerprint == ""
    assert run.live_tokens == 0
    assert run.last_activity_at is None
    await close_db()

    con = sqlite3.connect(db_file)
    run_cols = {row[1] for row in con.execute("PRAGMA table_info(workflow_runs)")}
    agent_cols = {row[1] for row in con.execute("PRAGMA table_info(workflow_agents)")}
    con.close()
    assert {"journal_offset", "source_fingerprint", "live_tokens", "last_activity_at"} <= run_cols
    assert "last_activity_at" in agent_cols


# ---------- 3. journal tail：增量消费 + 半行防护 ----------


@pytest.mark.asyncio
async def test_journal_tail_and_half_line_guard(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    _, _, wf_dir, run = await _setup_live_run(repo, tmp_path, monkeypatch)
    journal = wf_dir / "journal.jsonl"
    journal.write_text(
        _journal_line("started", "aga1") + _journal_line("started", "aga2")
        + _journal_line("result", "aga2"),
        encoding="utf-8",
    )

    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    assert res["ok"] and not res["terminal"]
    run = await repo.get_workflow_run(WF_LIVE)
    assert run.journal_offset == journal.stat().st_size  # 全量消费
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents(WF_LIVE)}
    assert agents["aga1"].state == "running"
    assert agents["aga2"].state == "done"

    # append 1 result + 无换行尾段半行 → offset 只前进到最后 \n、半行不解析
    half = json.dumps({"type": "started", "key": "v2:k3", "agentId": "aga3"})
    with journal.open("a", encoding="utf-8") as f:
        f.write(_journal_line("result", "aga1"))
        f.write(half[: len(half) // 2])  # 并发写半行
    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    run = await repo.get_workflow_run(WF_LIVE)
    size_to_last_nl = journal.read_bytes().rfind(b"\n") + 1
    assert run.journal_offset == size_to_last_nl
    assert run.journal_offset < journal.stat().st_size
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents(WF_LIVE)}
    assert agents["aga1"].state == "done"
    assert "aga3" not in agents  # 半行不解析

    # 补齐 \n 后下 tick 消费
    with journal.open("a", encoding="utf-8") as f:
        f.write(half[len(half) // 2:] + "\n")
    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    run = await repo.get_workflow_run(WF_LIVE)
    assert run.journal_offset == journal.stat().st_size
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents(WF_LIVE)}
    assert agents["aga3"].state == "running"

    # 聚合事件已发（每 run 每 tick 至多一条 agent_updated）
    ev = [e for e in await repo.list_events(type_prefix="workflow.")
          if e.type == EventType.WORKFLOW_AGENT_UPDATED]
    assert ev, "live tail 应聚合 emit workflow.agent_updated"


# ---------- 4. 文件重写：size < offset → 复位 0 重新 tail ----------


@pytest.mark.asyncio
async def test_journal_rewrite_resets_offset(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    _, _, wf_dir, run = await _setup_live_run(repo, tmp_path, monkeypatch, wf_id="wf_live0002-rst")
    journal = wf_dir / "journal.jsonl"
    journal.write_text(
        _journal_line("started", "agb1") + _journal_line("started", "agb2"), encoding="utf-8"
    )
    await workflow_ingest.tail_live_run(repo, event_bus, run)
    run = await repo.get_workflow_run("wf_live0002-rst")
    assert run.journal_offset == journal.stat().st_size

    # truncate 重写为更短内容 → 复位 0 → 重新消费
    journal.write_text(_journal_line("result", "agb1"), encoding="utf-8")
    assert journal.stat().st_size < run.journal_offset
    await workflow_ingest.tail_live_run(repo, event_bus, run)
    run = await repo.get_workflow_run("wf_live0002-rst")
    assert run.journal_offset == journal.stat().st_size  # 复位后重新推进到新文件长
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents("wf_live0002-rst")}
    assert agents["agb1"].state == "done"


# ---------- 5. lastCtx 口径：最后一条 assistant 四字段和 ≠ 跨轮累加；无 jsonl 记 0 ----------


@pytest.mark.asyncio
async def test_lastctx_token_metric(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    _, _, wf_dir, run = await _setup_live_run(repo, tmp_path, monkeypatch, wf_id="wf_live0003-ctx")
    (wf_dir / "journal.jsonl").write_text(
        _journal_line("started", "agc1") + _journal_line("started", "agc2"), encoding="utf-8"
    )
    # agc1 有 jsonl：3 条 assistant usage 递增、含大 cache_read
    rounds = [(10, 5, 100, 20), (2, 3, 200, 30), (2, 15010, 145842, 13479)]
    (wf_dir / "agent-agc1.jsonl").write_text(
        _user_line() + "".join(_assistant_line(*r) for r in rounds), encoding="utf-8"
    )
    last_ctx = sum(rounds[-1])          # 174333
    cross_sum = sum(sum(r) for r in rounds)  # 跨轮累加（cache_read 重复计入，否决口径）

    await workflow_ingest.tail_live_run(repo, event_bus, run)
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents("wf_live0003-ctx")}
    assert agents["agc1"].tokens == last_ctx
    assert agents["agc1"].tokens != cross_sum
    assert agents["agc1"].started_at is not None  # 首行 timestamp → started_at
    assert agents["agc1"].last_activity_at is not None
    assert agents["agc2"].tokens == 0  # journal 已 started 但无 jsonl → cached 记 0

    run = await repo.get_workflow_run("wf_live0003-ctx")
    assert run.live_tokens == last_ctx  # Σ agents（agc2 记 0）
    assert run.last_activity_at is not None
    assert run.status == "running"  # 文件新鲜，绝不误判 interrupted


# ---------- 6. 水位合并：显式 0 复位；ingest 不携带 offset 不抹 0 ----------


@pytest.mark.asyncio
async def test_watermark_merge_semantics(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    t1 = datetime(2026, 7, 6, 10, 0, 0)
    await repo.upsert_workflow_run(_WFRun(
        wf_id="wf_wl-1", status="running", journal_offset=123, live_tokens=42,
        source_fingerprint="111:222", last_activity_at=t1,
    ))
    # None = 不改
    await repo.upsert_workflow_run(_WFRun(wf_id="wf_wl-1", status=""))
    run = await repo.get_workflow_run("wf_wl-1")
    assert run.journal_offset == 123 and run.live_tokens == 42
    assert run.source_fingerprint == "111:222" and run.last_activity_at == t1

    # 显式 0/'' = 复位（证明未套「新非零胜出」）
    await repo.upsert_workflow_run(_WFRun(
        wf_id="wf_wl-1", status="", journal_offset=0, source_fingerprint="",
    ))
    run = await repo.get_workflow_run("wf_wl-1")
    assert run.journal_offset == 0 and run.source_fingerprint == ""
    assert run.live_tokens == 42  # 未携带的列不动

    # last_activity_at 单调取 max（旧值不回退）
    await repo.upsert_workflow_run(_WFRun(
        wf_id="wf_wl-1", status="", last_activity_at=t1 - timedelta(hours=1),
    ))
    run = await repo.get_workflow_run("wf_wl-1")
    assert run.last_activity_at == t1
    t2 = t1 + timedelta(minutes=5)
    await repo.upsert_workflow_run(_WFRun(wf_id="wf_wl-1", status="", last_activity_at=t2))
    run = await repo.get_workflow_run("wf_wl-1")
    assert run.last_activity_at == t2

    # ingest_run_from_file 不携带 offset（None）→ 不被抹 0
    await repo.upsert_workflow_run(_WFRun(wf_id=WF_ID, status="running", journal_offset=77))
    wf_file = tmp_path / f"{WF_ID}.json"
    wf_file.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")
    await workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)
    run = await repo.get_workflow_run(WF_ID)
    assert run.journal_offset == 77
    st = wf_file.stat()
    assert run.source_fingerprint == f"{st.st_mtime_ns}:{st.st_size}"  # fp 随 ingest 写入


# ---------- 7. fingerprint：同 fp 跳过；同秒不同 size 重 ingest；老行走 mtime 规则 ----------


@pytest.mark.asyncio
async def test_fingerprint_skip_and_same_second_reingest(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    wf3 = "wf_fp0001-abc"
    root_path = "/tmp/wf-fp-project"
    await repo.create_project(name="wf-fp", root_path=root_path)
    slug = workflow_ingest._project_slug(root_path)
    base = tmp_path / "projects"
    run_dir = base / slug / "S1" / "workflows"
    run_dir.mkdir(parents=True)
    snap = _fixture_snapshot()
    snap["runId"] = wf3
    jf = run_dir / f"{wf3}.json"
    jf.write_text(json.dumps(snap), encoding="utf-8")
    monkeypatch.setattr(workflow_ingest, "_claude_projects_dir", lambda: base)

    r1 = await workflow_ingest.reconcile(repo, event_bus, project_dir=root_path)
    assert r1["ingested"] == 1
    st0 = jf.stat()
    run = await repo.get_workflow_run(wf3)
    assert run.source_fingerprint == f"{st0.st_mtime_ns}:{st0.st_size}"

    # 计数 wrapper：reconcile 内部经模块全局名调 ingest_run_from_file
    calls: list[str] = []
    orig_ingest = workflow_ingest.ingest_run_from_file

    async def counting(repo_, bus_, path_):
        calls.append(str(path_))
        return await orig_ingest(repo_, bus_, path_)

    monkeypatch.setattr(workflow_ingest, "ingest_run_from_file", counting)

    # 同 fp 二跑 → 不重读
    r2 = await workflow_ingest.reconcile(repo, event_bus, project_dir=root_path)
    assert r2["scanned"] == 1 and len(calls) == 0

    # 同 mtime 秒（ns 级相同）不同 size → fingerprint 抓住，mtime 规则会漏
    snap["summary"] = snap["summary"] + " —— resumed 追加内容让 size 变化"
    jf.write_text(json.dumps(snap), encoding="utf-8")
    _os.utime(jf, ns=(st0.st_atime_ns, st0.st_mtime_ns))  # mtime 回拨到与旧完全相同
    assert jf.stat().st_mtime_ns == st0.st_mtime_ns and jf.stat().st_size != st0.st_size
    await workflow_ingest.reconcile(repo, event_bus, project_dir=root_path)
    assert len(calls) == 1  # 重 ingest（老 mtime 规则下 mtime<=updated_at 会误跳过）

    # 老行 fp='' → 走原 mtime 规则（MVP 回归）：mtime 旧 → skip；mtime 变新 → ingest
    await repo.upsert_workflow_run(_WFRun(wf_id=wf3, status="", source_fingerprint=""))
    await workflow_ingest.reconcile(repo, event_bus, project_dir=root_path)
    assert len(calls) == 1  # mtime(旧) <= updated_at(刚 upsert) → 跳过
    future_ns = st0.st_mtime_ns + 3_600_000_000_000  # +1h
    _os.utime(jf, ns=(future_ns, future_ns))
    await workflow_ingest.reconcile(repo, event_bus, project_dir=root_path)
    assert len(calls) == 2  # mtime 变新 → 重 ingest


# ---------- 8. interrupted：四条件打标 + 取 max 不误判 + 终态 2→3 自愈 ----------


@pytest.mark.asyncio
async def test_interrupted_mark_and_self_heal(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    wf4 = "wf_intr0001-abc"
    _, base, wf_dir, run = await _setup_live_run(
        repo, tmp_path, monkeypatch, wf_id=wf4, root_path="/tmp/wf-intr-project"
    )
    journal = wf_dir / "journal.jsonl"
    journal.write_text(_journal_line("started", "agd1"), encoding="utf-8")
    ajson = wf_dir / "agent-agd1.jsonl"
    ajson.write_text(_user_line() + _assistant_line(10, 0, 0, 5), encoding="utf-8")

    # 子案例A：仅 journal 陈旧而 agent jsonl 新鲜 → 不打标（条件3 取 max）
    stale_ns = int((datetime.now() - timedelta(seconds=2000)).timestamp() * 1e9)
    _os.utime(journal, ns=(stale_ns, stale_ns))
    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    assert res["marked_interrupted"] is False
    assert (await repo.get_workflow_run(wf4)).status == "running"

    # 子案例B：全部文件静止 >900s → 四条件满足 → 打标 + emit run_ingested
    _os.utime(ajson, ns=(stale_ns, stale_ns))
    run = await repo.get_workflow_run(wf4)
    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    assert res["marked_interrupted"] is True
    run = await repo.get_workflow_run(wf4)
    assert run.status == "interrupted"  # rank 1→2，只打标不删行
    ev = [e for e in await repo.list_events(type_prefix="workflow.")
          if e.type == EventType.WORKFLOW_RUN_INGESTED]
    assert ev and any(e.data.get("stall_seconds", 0) > 900 for e in ev)

    # 子案例C：终态 json 落盘 → reaper 对账 rank 2→3 自愈，live 观察集不再含它
    from aiteam.api.state_reaper import StateReaper

    snap = _fixture_snapshot()
    snap["runId"] = wf4
    json_dir = wf_dir.parent.parent.parent / "workflows"
    json_dir.mkdir(parents=True, exist_ok=True)
    final = json_dir / f"{wf4}.json"
    final.write_text(json.dumps(snap), encoding="utf-8")
    fresh_ns = int((datetime.now() + timedelta(seconds=5)).timestamp() * 1e9)
    _os.utime(final, ns=(fresh_ns, fresh_ns))  # 确保 mtime > run.updated_at（确定性）

    reaper = StateReaper(repo, event_bus)
    await reaper._check_workflow_ingest(repo)  # 24h 窗内 interrupted 仍被对账
    run = await repo.get_workflow_run(wf4)
    assert run.status == "completed"  # 2→3 合法翻转自愈

    # 子案例D：wf json 已存在 → tail 走终态优先，绝不再打标
    res = await workflow_ingest.tail_live_run(repo, event_bus, run)
    assert res.get("terminal") is True


# ---------- 9. 稳态零 stat：无 running 且 interrupted 出 24h 窗 → 零文件访问 ----------


@pytest.mark.asyncio
async def test_steady_state_zero_stat(repo: StorageRepository, event_bus: EventBus, monkeypatch):
    import pathlib

    from sqlalchemy import text

    from aiteam.api.state_reaper import StateReaper
    from aiteam.storage.connection import get_session

    # 一条 interrupted 但 updated_at 已出 24h 复查窗（+一条终态，均不该触发 IO）
    await repo.upsert_workflow_run(_WFRun(wf_id="wf_zz-old", status="interrupted"))
    await repo.upsert_workflow_run(_WFRun(wf_id="wf_zz-done", status="completed"))
    old = (datetime.now() - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S.%f")
    async with get_session(repo._db_url) as session:
        await session.execute(
            text("UPDATE workflow_runs SET updated_at = :t WHERE wf_id = 'wf_zz-old'"),
            {"t": old},
        )

    counter = {"n": 0}
    orig_stat = pathlib.Path.stat

    def counting_stat(self, *a, **kw):
        counter["n"] += 1
        return orig_stat(self, *a, **kw)

    reaper = StateReaper(repo, event_bus)
    monkeypatch.setattr(pathlib.Path, "stat", counting_stat)
    try:
        await reaper._check_workflow_ingest(repo)
    finally:
        monkeypatch.setattr(pathlib.Path, "stat", orig_stat)
    assert counter["n"] == 0  # 观察集为空 → 整段短路，零文件 stat


# ---------- 10. .output 兜底：真 JSON 富化；软链/0 字节分流跳过；终态列不动 ----------


@pytest.mark.asyncio
async def test_output_fallback_enrichment(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path, monkeypatch
):
    wf5 = "wf_out0001-abc"
    root_path = "/tmp/wf-out-project"
    proj = await repo.create_project(name="wf-out", root_path=root_path)
    slug = workflow_ingest._project_slug(root_path)
    tmp_claude = tmp_path / "claude-tmp"
    tasks_dir = tmp_claude / slug / "SESS-OUT" / "tasks"
    tasks_dir.mkdir(parents=True)
    monkeypatch.setattr(workflow_ingest, "_claude_tmp_dir", lambda: tmp_claude)

    await repo.upsert_workflow_run(_WFRun(
        wf_id=wf5, project_id=proj.id, status="interrupted", cc_task_id="tskout01",
    ))

    # 真 JSON：7 键子集、缺 runId（绝不能喂 ingest_run_from_file）
    output = {
        "summary": "兜底快照摘要",
        "agentCount": 2,
        "logs": ["l1"],
        "result": None,
        "totalTokens": 12345,
        "totalToolCalls": 7,
        "workflowProgress": [
            {"type": "workflow_agent", "agentId": "age1", "label": "map:x",
             "phaseIndex": 1, "phaseTitle": "调研", "state": "done", "tokens": 8000},
            {"type": "workflow_agent", "agentId": "age2", "label": "reduce:y",
             "phaseIndex": 2, "phaseTitle": "汇总", "state": "running", "tokens": 4345},
        ],
    }
    (tasks_dir / "tskout01.output").write_text(json.dumps(output), encoding="utf-8")

    res = await workflow_ingest.enrich_from_task_output(repo, await repo.get_workflow_run(wf5))
    assert res["ok"] is True and res["agents"] == 2
    agents = {a.cc_agent_id: a for a in await repo.list_workflow_agents(wf5)}
    assert agents["age1"].label == "map:x" and agents["age1"].tokens == 8000
    assert agents["age2"].phase_index == 2
    run = await repo.get_workflow_run(wf5)
    assert run.status == "interrupted"  # status 不动
    assert run.total_tokens == 0  # 终态列不动（只归 wf-json）
    assert run.live_tokens == 12345 and run.summary == "兜底快照摘要"

    # 软链 = 普通 Task transcript → 跳过（悬空软链 open 会抛，必须 lstat 分流）
    wf6 = "wf_out0002-lnk"
    await repo.upsert_workflow_run(_WFRun(
        wf_id=wf6, project_id=proj.id, status="interrupted", cc_task_id="tsklnk02",
    ))
    target = tmp_path / "transcript.jsonl"
    target.write_text(_user_line(), encoding="utf-8")
    _os.symlink(target, tasks_dir / "tsklnk02.output")
    res = await workflow_ingest.enrich_from_task_output(repo, await repo.get_workflow_run(wf6))
    assert res["ok"] is False and res["reason"] == "no_output"

    # 0 字节 = 任务在跑 → 跳过
    wf7 = "wf_out0003-zero"
    await repo.upsert_workflow_run(_WFRun(
        wf_id=wf7, project_id=proj.id, status="interrupted", cc_task_id="tskzero3",
    ))
    (tasks_dir / "tskzero3.output").write_text("", encoding="utf-8")
    res = await workflow_ingest.enrich_from_task_output(repo, await repo.get_workflow_run(wf7))
    assert res["ok"] is False and res["reason"] == "no_output"


# ============================================================
# 跨项目修复 A+B+C 回归（2026-07-06 巡检发现的盲区三连症）
# ============================================================


@pytest.mark.asyncio
async def test_receipt_persists_transcript_dir(
    client: AsyncClient, repo: StorageRepository
):
    """修复A：回执的 Transcript dir 必须持久化到 run 行（live/终态直接寻址的根基）。"""
    post = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": "sess-tdir-1",
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
        },
    )
    assert post.status_code == 200
    run = await repo.get_workflow_run(WF_ID)
    assert run is not None
    assert run.transcript_dir, "transcript_dir 应从回执持久化"
    assert WF_ID in run.transcript_dir


@pytest.mark.asyncio
async def test_receipt_strict_project_attribution(
    client: AsyncClient, repo: StorageRepository, tmp_path
):
    """修复C：project_id 只按发起 cwd → 已注册项目最长前缀解析；解析不到留空绝不猜。

    旧 _find_leader 跨会话回退曾把未注册项目的 run 归到别的项目（隔离违规实录）。
    """
    proj = await repo.create_project(name="attr-proj", root_path=str(tmp_path / "projA"))

    # cwd 落在已注册项目内 → 归属该项目
    r1 = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": "sess-attr-1",
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": RECEIPT,
            "cwd": str(tmp_path / "projA" / "sub"),
        },
    )
    assert r1.status_code == 200
    run1 = await repo.get_workflow_run(WF_ID)
    assert run1 is not None and run1.project_id == proj.id

    # cwd 在任何已注册项目之外 → 留空，绝不猜
    other_receipt = RECEIPT.replace(WF_ID, "wf_attr2-9999")
    r2 = await client.post(
        "/api/hooks/event",
        json={
            "hook_event_name": "PostToolUse",
            "session_id": "sess-attr-2",
            "tool_name": "Workflow",
            "tool_input": {"script": WF_SCRIPT},
            "tool_response": other_receipt,
            "cwd": str(tmp_path / "elsewhere"),
        },
    )
    assert r2.status_code == 200
    run2 = await repo.get_workflow_run("wf_attr2-9999")
    assert run2 is not None and (run2.project_id or "") == ""


@pytest.mark.asyncio
async def test_candidate_slugs_session_fallback(
    repo: StorageRepository, tmp_path, monkeypatch
):
    """修复B：无 transcript_dir 的存量行按 session_id 全局反查 slug（未注册项目可达）。"""
    from aiteam.api import workflow_ingest as wi
    from aiteam.types import WorkflowRun

    base = tmp_path / "projects"
    (base / "slug-unregistered" / "sess-fb-1").mkdir(parents=True)
    monkeypatch.setattr(wi, "_claude_projects_dir", lambda: base)

    run = WorkflowRun(wf_id="wf_fb-1", session_id="sess-fb-1", transcript_dir="")
    slugs = await wi._candidate_slugs(repo, run)
    assert "slug-unregistered" in slugs

    # A 存在 transcript_dir 的新行不进回退路径
    run2 = WorkflowRun(
        wf_id="wf_fb-2", session_id="sess-fb-1", transcript_dir=str(tmp_path / "t")
    )
    slugs2 = await wi._candidate_slugs(repo, run2)
    assert "slug-unregistered" not in slugs2


@pytest.mark.asyncio
async def test_stop_refreshes_leader_model(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    """Stop 每轮尾读 transcript 刷新 Leader 真实模型（2026-07-07 用户四次实测的
    4-7 幽灵战役收官回归——曾因 hook_translator 缺 import json 被 try 静默吞掉）。"""
    from aiteam.api.hook_translator import HookTranslator

    team = await repo.create_team(name="t-lm", mode="coordinate")
    await repo.create_agent(
        team_id=team.id, name="Leader", role="leader", session_id="sess-lm-1"
    )
    tp = tmp_path / "s.jsonl"
    tp.write_text(
        '{"type":"user","message":{}}\n'
        '{"type":"assistant","message":{"model":"claude-test-9"}}\n',
        encoding="utf-8",
    )
    ht = HookTranslator(repo=repo, event_bus=event_bus)
    await ht._on_stop({"session_id": "sess-lm-1", "transcript_path": str(tp)})
    a = (await repo.find_agents_by_session("sess-lm-1"))[0]
    assert a.model == "claude-test-9"


def test_detect_live_session_file_truth(tmp_path, monkeypatch):
    """Leader 身份 = 项目目录下最新 CC 主会话（文件真相源直读，零注册依赖）。
    用户裁定 2026-07-07：模型/活跃状态后端自动检测，不经 hook 注册链。"""
    from aiteam.api import session_probe as sp

    base = tmp_path / "projects"
    slug_dir = base / sp.project_slug("/Users/x/My Proj")
    slug_dir.mkdir(parents=True)
    old = slug_dir / "sess-old.jsonl"
    old.write_text('{"type":"assistant","message":{"model":"claude-a"}}\n')
    new = slug_dir / "sess-new.jsonl"
    new.write_text(
        '{"type":"assistant","message":{"model":"claude-b"}}\n'
        '{"type":"assistant","message":{"model":"<synthetic>"}}\n'
    )
    import os

    os.utime(old, (1, 1))  # 旧会话按 mtime 落后
    # 子 agent 目录不应干扰主会话 glob
    (slug_dir / "sess-new").mkdir()
    monkeypatch.setattr(sp, "_claude_projects_dir", lambda: base)

    probe = sp.detect_live_session("/Users/x/My Proj")
    assert probe is not None
    assert probe["session_id"] == "sess-new"
    assert probe["model"] == "claude-b"  # synthetic 合成行被跳过
    assert probe["live"] is True  # 刚写入，mtime 在 15 分钟窗口内

    assert sp.detect_live_session("/no/such/project") is None


def test_detect_live_sessions_multi_ceo(tmp_path, monkeypatch):
    """多会话并行 = 每个活跃 session 一条 CEO-<英文名>（用户裁定 2026-07-10）：
    活跃窗内全部返回、名字确定性且不重复、全静默时退回最新一条。"""
    from aiteam.api import session_probe as sp

    base = tmp_path / "projects"
    slug_dir = base / sp.project_slug("/Users/x/Multi Proj")
    slug_dir.mkdir(parents=True)
    a = slug_dir / "sess-aaaa.jsonl"
    a.write_text('{"type":"assistant","message":{"model":"claude-fable-5"}}\n')
    b = slug_dir / "sess-bbbb.jsonl"
    b.write_text('{"type":"assistant","message":{"model":"claude-opus-4-8"}}\n')
    stale = slug_dir / "sess-stale.jsonl"
    stale.write_text('{"type":"assistant","message":{"model":"claude-old"}}\n')
    import os

    os.utime(stale, (1, 1))  # 活跃窗外，不应出现在并列清单
    monkeypatch.setattr(sp, "_claude_projects_dir", lambda: base)

    sessions = sp.detect_live_sessions("/Users/x/Multi Proj")
    assert len(sessions) == 2  # 只出活跃窗内两条，stale 被排除
    ids = {s["session_id"] for s in sessions}
    assert ids == {"sess-aaaa", "sess-bbbb"}
    assert all(s["live"] is True for s in sessions)
    names = [s["name"] for s in sessions]
    assert len(set(names)) == 2  # 不重复
    assert all(n in sp.CEO_NAMES for n in names)
    # 确定性：重复探测同一批会话，分配结果不变（刷新不换名）
    again = sp.detect_live_sessions("/Users/x/Multi Proj")
    assert {s["session_id"]: s["name"] for s in again} == {
        s["session_id"]: s["name"] for s in sessions
    }

    # 全静默：只退回最新一条，live=False（mtime 全部拨到远古过去）
    os.utime(a, (100_000, 100_000))
    os.utime(b, (50_000, 50_000))
    idle = sp.detect_live_sessions("/Users/x/Multi Proj")
    assert len(idle) == 1
    assert idle[0]["session_id"] == "sess-aaaa"
    assert idle[0]["live"] is False


@pytest.mark.asyncio
async def test_stop_ignores_synthetic_model(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    """compact 会写入 model="<synthetic>" 的合成 assistant 行——若恰为尾部
    最后一条，不得污染 Leader 模型（2026-07-07 巡检实测：DB 出现 <synthetic>）。"""
    from aiteam.api.hook_translator import HookTranslator

    team = await repo.create_team(name="t-syn", mode="coordinate")
    await repo.create_agent(
        team_id=team.id, name="Leader", role="leader", session_id="sess-syn-1"
    )
    tp = tmp_path / "s.jsonl"
    tp.write_text(
        '{"type":"assistant","message":{"model":"claude-test-9"}}\n'
        '{"type":"assistant","message":{"model":"<synthetic>"}}\n',
        encoding="utf-8",
    )
    ht = HookTranslator(repo=repo, event_bus=event_bus)
    await ht._on_stop({"session_id": "sess-syn-1", "transcript_path": str(tp)})
    a = (await repo.find_agents_by_session("sess-syn-1"))[0]
    assert a.model == "claude-test-9"


@pytest.mark.asyncio
async def test_tool_event_revives_leader(
    repo: StorageRepository, event_bus: EventBus
):
    """工具事件在流 = 对话进行中：offline Leader 应被复活为 busy（曾实测
    "正在对话却显示关闭"——5 分钟心跳在长回合中误杀且无人复活）。"""
    from aiteam.api.hook_translator import HookTranslator

    team = await repo.create_team(name="t-rv", mode="coordinate")
    ld = await repo.create_agent(
        team_id=team.id, name="Leader", role="leader", session_id="sess-rv-1"
    )
    await repo.update_agent(ld.id, status="offline")
    ht = HookTranslator(repo=repo, event_bus=event_bus)
    await ht._touch_session_leader("sess-rv-1")
    a = (await repo.find_agents_by_session("sess-rv-1"))[0]
    assert str(a.status).endswith("busy") or a.status == "busy"


# ============================================================
# 服务端写面三条 med 修复的回归测试（任务 6f313f77）
# ============================================================


@pytest.mark.asyncio
async def test_upsert_workflow_run_reports_terminal_transition_atomically(
    repo: StorageRepository,
):
    """WP10 根治：把「本次是否首次转 completed / 首次入终态」的判定收进 upsert 事务内
    原子返回，替代事务外先 get→was_completed 的 check-then-act（三条无串行 ingest 驱动
    交错时会重复 emit workflow.completed）。这里锁定跃迁判定的契约。"""
    from aiteam.types import WorkflowRun

    # 首见 running：非完成、非终态。
    r1 = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-1", status="running"))
    assert (r1.became_completed, r1.became_terminal) == (False, False)
    # running → completed：首次转移，两者皆 True。
    r2 = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-1", status="completed"))
    assert (r2.became_completed, r2.became_terminal) == (True, True)
    # 再 upsert 一次 completed：已完成，不再是转移——这正是防重复 emit 的关键位。
    r3 = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-1", status="completed"))
    assert (r3.became_completed, r3.became_terminal) == (False, False)
    # 直接首见即 completed（reaper 首扫已终态的老 run）：算首次转移。
    r4 = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-2", status="completed"))
    assert (r4.became_completed, r4.became_terminal) == (True, True)
    # running → killed：入终态但非 completed（驱动 run_ingested，不驱动 completed）。
    await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-3", status="running"))
    rk = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-3", status="killed"))
    assert (rk.became_completed, rk.became_terminal) == (False, True)
    # killed → completed（等秩终态互转，resumeFromRunId 场景）：became_completed 仍 True
    # （与旧 was_completed 语义一致：prev!=completed 即 emit），但已是终态故 became_terminal
    # False——不重复 emit run_ingested。
    rkc = await repo.upsert_workflow_run(WorkflowRun(wf_id="wf_trans-3", status="completed"))
    assert (rkc.became_completed, rkc.became_terminal) == (True, False)


@pytest.mark.asyncio
async def test_ingest_concurrent_emits_completed_once(
    repo: StorageRepository, event_bus: EventBus, tmp_path: Path
):
    """WP10 并发场景：同一「新完成」的 wf 文件被多个无串行驱动（reaper 对账 / SessionStart
    对账 / hook 回执 ingest）同时处理，workflow.completed 只能落一条。"""
    import asyncio

    from aiteam.types import WorkflowRun

    wf_file = tmp_path / f"{WF_ID}.json"
    wf_file.write_text(json.dumps(_fixture_snapshot()), encoding="utf-8")
    # 预置 running 骨架，逼近「running→completed 首次跃迁」的真实并发起点。
    await repo.upsert_workflow_run(WorkflowRun(wf_id=WF_ID, status="running"))

    results = await asyncio.gather(
        *[
            workflow_ingest.ingest_run_from_file(repo, event_bus, wf_file)
            for _ in range(3)
        ]
    )
    # 直接度量「emit 决策」次数——WP10 治的就是这个：三条驱动交错，只有一条能拿到
    # running→completed 跃迁并发射 workflow.completed，其余 became_completed=False。
    #（不查 list_events：内存库单连接拓扑下并发 gather 会丢部分 event 持久化，是测试
    #  环境伪影；生产独立连接 + WAL 无此问题。返回值里的 emitted 才是 emit 决策的真相。）
    assert sum(1 for r in results if r.get("emitted")) == 1
    assert sum(1 for r in results if r.get("new_completion")) == 1


@pytest.mark.asyncio
async def test_agents_cc_tool_use_id_partial_unique(repo: StorageRepository):
    """B1：agents.cc_tool_use_id 加 partial unique index（WHERE NOT NULL）。两条
    create-if-absent 路径并发时第二条 create 触 IntegrityError（由调用方吞）。NULL 豁免，
    leader / 普通 api agent 可继续共享 NULL。"""
    from sqlalchemy.exc import IntegrityError

    team = await repo.create_team(name="t-ccdup", mode="coordinate")
    await repo.create_agent(
        team_id=team.id, name="wf-1", role="workflow-subagent", cc_tool_use_id="cc-DUP"
    )
    with pytest.raises(IntegrityError):
        await repo.create_agent(
            team_id=team.id, name="wf-2", role="workflow-subagent",
            cc_tool_use_id="cc-DUP",
        )
    # NULL cc 的多行可共存（leader 不带 cc_tool_use_id，绝不被约束波及）。
    await repo.create_agent(team_id=team.id, name="ld-1", role="leader")
    await repo.create_agent(team_id=team.id, name="ld-2", role="leader")
    # 空串 cc = 「无 cc id」（agent_id 被超长 payload 裁掉），同样豁免——两个各自独立，
    # 绝不能被唯一约束误并成一行。
    await repo.create_agent(
        team_id=team.id, name="e-1", role="workflow-subagent", cc_tool_use_id=""
    )
    await repo.create_agent(
        team_id=team.id, name="e-2", role="workflow-subagent", cc_tool_use_id=""
    )
    rows = await repo.list_agents(team.id)
    assert sum(1 for a in rows if a.cc_tool_use_id == "cc-DUP") == 1
    assert sum(1 for a in rows if a.cc_tool_use_id is None) == 2
    assert sum(1 for a in rows if a.cc_tool_use_id == "") == 2


@pytest.mark.asyncio
async def test_register_workflow_subagent_swallows_dup_create(
    repo: StorageRepository, event_bus: EventBus, monkeypatch
):
    """B1：并发 create 竞态——本协程顶部 dedup find 落空（走 create），但 create 时行已
    被先到方占位 → IntegrityError → 吞掉 → re-fetch 命中既有行 → update 收敛，不产生双行、
    不向 hook 端点抛 500。"""
    from aiteam.api.hook_translator import WORKFLOW_AGENT_TYPE, HookTranslator

    ht = HookTranslator(repo=repo, event_bus=event_bus)
    cc = "cc-race-1"
    team = await repo.create_team(
        name="workflow-wf_race",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_race"},
    )
    winner = await repo.create_agent(
        team_id=team.id, name="wf-winner", role=WORKFLOW_AGENT_TYPE,
        source="hook", cc_tool_use_id=cc,
    )

    real_find = repo.find_agent_by_cc_id
    calls = {"n": 0}

    async def fake_find(cid: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # 顶部 dedup 落空 → 逼入 create 路径
        return await real_find(cid)  # except 内 re-fetch → 命中 winner

    monkeypatch.setattr(repo, "find_agent_by_cc_id", fake_find)

    res = await ht._register_workflow_subagent(
        {"cwd": "", "transcript_path": "x/subagents/workflows/wf_race/agent-x.jsonl"},
        cc_agent_id=cc,
        session_id="sess-race",
    )
    assert res["status"] == "updated"
    assert res["agent_id"] == winner.id
    all_rows = [
        a for t in await repo.list_teams() for a in await repo.list_agents(t.id)
    ]
    assert sum(1 for a in all_rows if a.cc_tool_use_id == cc) == 1


@pytest.mark.asyncio
async def test_session_end_exempts_running_workflow_subagent(
    repo: StorageRepository, event_bus: EventBus
):
    """WP6：SessionEnd 不再无条件 offline 全会话 agent——仍在跑的 workflow 子 agent
    （role=workflow-subagent，run 可远长于发起会话）豁免，与 kind=workflow 队豁免对称；
    普通 agent 照常 offline。"""
    from aiteam.api.hook_translator import WORKFLOW_AGENT_TYPE, HookTranslator

    sess = "sess-end-wf"
    wteam = await repo.create_team(
        name="workflow-wf_end",
        mode="coordinate",
        config={"kind": "workflow", "workflow_run_id": "wf_end"},
    )
    wf_member = await repo.create_agent(
        team_id=wteam.id, name="wf-m", role=WORKFLOW_AGENT_TYPE,
        source="hook", session_id=sess,
    )
    await repo.update_agent(wf_member.id, status="busy")
    normal_team = await repo.create_team(name="t-normal", mode="coordinate")
    normal = await repo.create_agent(
        team_id=normal_team.id, name="worker", role="developer",
        source="hook", session_id=sess,
    )
    await repo.update_agent(normal.id, status="busy")

    ht = HookTranslator(repo=repo, event_bus=event_bus)
    await ht._on_session_end({"session_id": sess})

    wf_after = await repo.get_agent(wf_member.id)
    assert str(wf_after.status).endswith("busy"), (
        "在跑的 workflow 子 agent 不应被 SessionEnd offline"
    )
    assert wf_after.session_id == sess, "workflow 子 agent 的 session_id 应保留"
    normal_after = await repo.get_agent(normal.id)
    assert str(normal_after.status).endswith("offline"), "普通 agent 应照常 offline"
