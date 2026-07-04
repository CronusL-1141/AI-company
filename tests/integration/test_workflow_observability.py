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
