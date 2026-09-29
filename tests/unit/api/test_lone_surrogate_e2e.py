"""Lone surrogates end to end: MCP tool -> real uvicorn -> DB -> the SessionStart hook.

A lone surrogate (U+D800-U+DFFF on its own) cannot be encoded as UTF-8. Before the
fix, one written into a JSON column (tags, mentions, source_refs, participants)
landed anyway, because the JSON column stores it as an ASCII escape; the response
then failed to serialize, so the caller saw a 400 for a write that had happened,
and every read that listed the row answered 400 from then on. The SessionStart
hook swallows that error, so one memory_add silently removed the direction layer
from every later session.

The harness is production code end to end: the MCP tools called through FastMCP
(they reach the API over HTTP, as a host's tool call does), a real uvicorn on a
temporary SQLite file, and plugin/hooks/session_bootstrap.py / send_event.py run as
subprocesses. Nothing touches the real data directory or port 8000.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from aiteam.mcp.tools import briefing as briefing_tools
from aiteam.mcp.tools import channels as channel_tools
from aiteam.mcp.tools import meeting as meeting_tools
from aiteam.mcp.tools import memory as memory_tools
from aiteam.mcp.tools import task as task_tools

ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = ROOT / "plugin" / "hooks" / "session_bootstrap.py"
SEND_EVENT = ROOT / "plugin" / "hooks" / "send_event.py"
LONE = "x" + chr(0xD800) + "y"


def _http(api: str, method: str, path: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None  # ASCII escapes, as _api_call sends
    request = urllib.request.Request(api + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _tools() -> FastMCP:
    mcp = FastMCP("lone-surrogate")
    for module in (task_tools, memory_tools, briefing_tools, channel_tools, meeting_tools):
        module.register(mcp)
    return mcp


def _call(mcp: FastMCP, name: str, args: dict) -> dict:
    result = asyncio.run(mcp.call_tool(name, args))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _bootstrap(api: str, home: Path, work: Path) -> str:
    env = {**os.environ, "AITEAM_API_URL": api, "HOME": str(home)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    proc = subprocess.run(
        [sys.executable, str(BOOTSTRAP)], input=json.dumps({"session_id": "e2e", "source": "startup"}),
        capture_output=True, text=True, timeout=60, env=env, cwd=str(work),
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    try:
        return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", out)
    except ValueError:
        return out


def _lone_surrogate_rows(database: Path) -> list[str]:
    """table.column cells whose stored JSON decodes to a lone surrogate."""
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    found = []
    try:
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            for column in [row[1] for row in con.execute(f"PRAGMA table_info('{table}')")]:
                rows = con.execute(
                    f"SELECT \"{column}\" FROM \"{table}\" WHERE instr(lower(CAST(\"{column}\" AS TEXT)), '\\ud') > 0"
                ).fetchall()
                for (value,) in rows:
                    try:
                        text = json.dumps(json.loads(value), ensure_ascii=False)
                    except (TypeError, ValueError):
                        continue
                    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
                        found.append(f"{table}.{column}")
    finally:
        con.close()
    return found


@pytest.fixture()
def world(hook_server, tmp_path, monkeypatch):
    """A project whose session start shows all three sections, plus a team with a meeting."""
    port, database, _ = hook_server
    api = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("AITEAM_API_URL", api)
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    _, project = _http(api, "POST", "/api/projects", {"name": "e2e", "root_path": str(work)})
    pid = project["data"]["id"]
    _http(api, "POST", "/api/memories", {"content": "e2e 方向条目：输出用中文", "kind": "directive", "scope": "global"})
    _http(api, "POST", f"/api/projects/{pid}/tasks", {"title": "e2e 进行中任务", "status": "running"})
    _http(api, "POST", "/api/leader-briefings", {"title": "e2e 待决事项", "project_id": pid})
    _http(api, "POST", "/api/hooks/event", {"hook_event_name": "SubagentStart", "session_id": "e2e-team",
                                            "agent_id": "a-1", "agent_type": "member", "cc_team_name": "e2e-team"})
    _, teams = _http(api, "GET", "/api/teams")
    team_id = next(t["id"] for t in teams["data"] if t["name"] == "e2e-team")
    _, meeting = _http(api, "POST", f"/api/teams/{team_id}/meetings", {"topic": "e2e 会议"})
    return {"api": api, "database": database, "home": home, "work": work, "pid": pid,
            "team_id": team_id, "meeting_id": meeting["data"]["id"]}


CASES = {
    "memory_add.source_refs": (
        "memory_add", lambda w: {"content": "看起来正常的一条", "kind": "preference", "scope": "global",
                                 "source_refs": [LONE]},
        lambda w: "/api/memories", "方向记忆"),
    "task_create.tags": (
        "task_create", lambda w: {"title": "看起来正常的任务", "project_id": w["pid"], "tags": [LONE]},
        lambda w: f"/api/projects/{w['pid']}/task-wall?limit=20&include_completed=false", "=== 任务墙"),
    "briefing_add.tags": (
        "briefing_add", lambda w: {"title": "看起来正常的简报", "tags": [LONE]},
        lambda w: "/api/leader-briefings?status=pending&real_only=true", "Leader简报"),
    "channel_send.mentions": (
        "channel_send", lambda w: {"channel": "team:e2e", "message": "hi", "mentions": [LONE], "project_id": w["pid"]},
        lambda w: f"/api/channels/team:e2e/messages?project_id={w['pid']}", None),
    "meeting_send_message.agent_name": (
        "meeting_send_message", lambda w: {"meeting_id": w["meeting_id"], "agent_id": "a-1", "agent_name": LONE,
                                           "content": "hi"},
        lambda w: f"/api/teams/{w['team_id']}/meetings", None),
}


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_a_lone_surrogate_is_refused_and_leaves_every_read_intact(case, world):
    tool, args, read_path, section = CASES[case]
    mcp = _tools()
    if section:
        assert section in _bootstrap(world["api"], world["home"], world["work"]), "baseline lacks the section"

    result = _call(mcp, tool, args(world))

    # Damage first, so an unfixed build shows what it costs rather than a wording miss.
    assert _lone_surrogate_rows(world["database"]) == [], "a write reported as failed still landed"
    status, body = _http(world["api"], "GET", read_path(world))
    assert status == 200, f"GET {read_path(world)} -> {status}: {str(body)[:120]}"
    if section:
        assert section in _bootstrap(world["api"], world["home"], world["work"]), (
            f"the session start lost its {section} section")
    assert result.get("success") is False, f"{tool} accepted a lone surrogate: {result}"
    assert "孤立代理项" in json.dumps(result, ensure_ascii=False), result


def test_hook_ingest_keeps_the_event_and_replaces_the_surrogate(world):
    """The auto-registration path must not drop the event: U+FFFD stands in, the agent is enrolled."""
    env = {**os.environ, "AITEAM_API_URL": world["api"], "HOME": str(world["home"])}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    stdin = json.dumps({"session_id": "e2e-hook", "agent_id": "a-lone", "cwd": str(world["work"]),
                        "agent_type": "worker-" + chr(0xD800) + "-x", "cc_team_name": "e2e-team"})
    assert "\\ud800" in stdin  # json.dumps writes the lone surrogate as the escape a host sends
    proc = subprocess.run([sys.executable, str(SEND_EVENT), "SubagentStart"], input=stdin,
                          capture_output=True, text=True, timeout=30, env=env, cwd=str(world["work"]))
    assert proc.returncode == 0, proc.stderr
    con = sqlite3.connect(f"file:{world['database']}?mode=ro", uri=True)
    try:
        names = [row[0] for row in con.execute("SELECT name FROM agents WHERE name LIKE 'worker-%'")]
    finally:
        con.close()
    assert names == ["worker-\ufffd-x"], f"registration lost or stored raw: {names!r}; stderr={proc.stderr}"
    assert _lone_surrogate_rows(world["database"]) == []
    status, _ = _http(world["api"], "GET", "/api/teams")
    assert status == 200
