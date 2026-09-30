"""resume 之后的记账边：MCP 工具 -> 真 uvicorn -> SQLite 文件，端到端。

``claude --resume`` 重启 CC 时，CC 先以一次性会话 id 启动（SessionStart 据此建一个
空壳 Leader 与容器队），再续上旧对话；此后 hook 与 CC 的进程登记都用被续上的对话
id，MCP 子进程的环境变量却停在启动 id 上。修复前，resume 会话里 Leader 的记账边全绑
到空壳 Leader 行上（空壳 7 天后随容器清理删掉，边就悬空），子 agent 的记账一条边也
建不出来（它的行挂在被续上的对话下，按启动 id 找不到）。

装配都是生产代码：会话与子 agent 由真实 hook 事件经 /api/hooks/event 建出，记账走
FastMCP 注册的 MCP 工具，请求经真 uvicorn 与全套中间件落到临时 SQLite 文件，断言用
另开的 sqlite3 连接读回。唯一的布景是 CC 的进程登记：测试进程的父进程号下写一份，
内容与 CC 2.1.285 写的同形。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import urllib.request
from pathlib import Path

import psutil
import pytest
from mcp.server.fastmcp import FastMCP

from aiteam.mcp import _base as mcp_base
from aiteam.mcp.tools import infra as infra_tools
from aiteam.mcp.tools import task as task_tools

STARTUP_ID = "91ca1de1-162f-47c5-a9e3-1510f4f7a667"
RESUMED_ID = "39c7d5a3-52c6-4735-b866-1fa68229709a"
OTHER_ID = "cd193951-0000-4000-8000-000000000001"  # another CC pane in the same project
SUBAGENT_NAME = "attr-sub"


def _post(api: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(api + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode())


def _call(mcp: FastMCP, name: str, args: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, args))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _rows(database: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _register_cc_process(config_dir: Path, session_id: str) -> None:
    """CC's per-process registry for the process that spawned this one."""
    ppid = os.getppid()
    started_ms = int(psutil.Process(ppid).create_time() * 1000) + 2500
    path = config_dir / "sessions" / f"{ppid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": ppid, "sessionId": session_id, "cwd": "/work/repo", "startedAt": started_ms,
        "version": "2.1.285", "kind": "interactive", "entrypoint": "cli", "status": "busy",
    }), encoding="utf-8")


@pytest.fixture()
def resumed_session(hook_server, monkeypatch, tmp_path):
    port, database, _ = hook_server
    api = f"http://127.0.0.1:{port}"
    root = tmp_path / "repo"
    root.mkdir()
    project = _post(api, "/api/projects", {"name": "resume-e2e", "root_path": str(root)})["data"]["id"]

    def hook(event: str, session_id: str, **extra) -> None:
        _post(api, "/api/hooks/event",
              {"hook_event_name": event, "session_id": session_id, "cwd": str(root), **extra})

    # Weeks ago: the conversation started, and its first turn brought the leader row in.
    hook("SessionStart", RESUMED_ID, source="startup")
    hook("Stop", RESUMED_ID, stop_hook_active=False)
    # Today: `claude` + /resume restarts CC. It starts under a one-off id, which
    # leaves no rows (a session's leader waits for its first activity), then resumes.
    hook("SessionStart", STARTUP_ID, source="startup")
    hook("SessionEnd", STARTUP_ID, reason="other")
    hook("SessionStart", RESUMED_ID, source="resume")
    # A subagent dispatched after the resume: hooks carry the resumed id.
    hook("SubagentStart", RESUMED_ID, agent_id="toolu_resume_e2e", agent_type=SUBAGENT_NAME)
    # Another pane in the same project, busy since: its container team is the newest.
    hook("SessionStart", OTHER_ID, source="startup")
    hook("Stop", OTHER_ID, stop_hook_active=False)

    leaders = dict(_rows(database, "SELECT session_id, id FROM agents WHERE role = 'leader'"))
    assert set(leaders) == {RESUMED_ID, OTHER_ID}, leaders
    (subagent,) = _rows(database, "SELECT id FROM agents WHERE name = ? AND session_id = ?",
                        (SUBAGENT_NAME, RESUMED_ID))

    # The MCP server CC spawned at startup: its env still holds the one-off id,
    # while CC's registry (rewritten on resume) holds the resumed conversation.
    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", STARTUP_ID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(mcp_base, "_cc_registry_last_good", {}, raising=False)
    monkeypatch.setattr(mcp_base, "_cc_parent_started_at", {}, raising=False)
    _register_cc_process(config_dir, RESUMED_ID)
    monkeypatch.setenv("AITEAM_API_URL", api)
    monkeypatch.setattr(mcp_base, "PROJECT_DIR", str(root))
    monkeypatch.setattr(mcp_base, "_session_project_id", project)
    monkeypatch.chdir(root)
    mcp = FastMCP("resume-e2e")
    task_tools.register(mcp)
    infra_tools.register(mcp)
    return {
        "api": api, "database": database, "mcp": mcp, "project": project,
        "real_leader": leaders[RESUMED_ID], "subagent": subagent[0],
    }


def _task(world: dict, title: str) -> str:
    return _post(world["api"], f"/api/projects/{world['project']}/tasks", {"title": title})["data"]["id"]


def test_memos_after_resume_link_to_the_resumed_conversations_rows(resumed_session):
    world = resumed_session
    leader_task = _task(world, "leader task")
    subagent_task = _task(world, "subagent task")

    leader_memo = _call(world["mcp"], "task_memo_add",
                        {"task_id": leader_task, "content": "Leader 记一笔", "author": "leader"})
    subagent_memo = _call(world["mcp"], "task_memo_add",
                          {"task_id": subagent_task, "content": "子 agent 记一笔", "author": SUBAGENT_NAME})
    assert leader_memo["success"] is True and subagent_memo["success"] is True

    edges = _rows(world["database"],
                  "SELECT from_id, to_id FROM knowledge_links WHERE link_type = 'worked_on' ORDER BY to_id")
    assert sorted(edges) == sorted([
        (world["real_leader"], leader_task),
        (world["subagent"], subagent_task),
    ]), {"edges": edges}


def test_current_team_is_the_resumed_conversations_container(resumed_session):
    world = resumed_session
    # Both container teams carry the project, as their production rows do (the hook
    # path here leaves project_id to a later back-fill this test does not drive).
    con = sqlite3.connect(world["database"], timeout=5)
    try:
        con.execute("UPDATE teams SET project_id = ? WHERE name IN (?, ?)",
                    (world["project"], f"session-{RESUMED_ID[:8]}", f"session-{OTHER_ID[:8]}"))
        con.commit()
    finally:
        con.close()

    context = _call(world["mcp"], "context_resolve", {})

    assert context["project"]["id"] == world["project"]
    assert context["team"]["name"] == f"session-{RESUMED_ID[:8]}", context
