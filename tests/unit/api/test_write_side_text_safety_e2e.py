"""Write-side text safety end to end: MCP tools and real hooks against a real uvicorn.

Single-line fields (titles, names, senders, authors, tags) are cleaned on the way
in, with the same rule as the injection side. Long text (descriptions, results,
report and message bodies) that carries an invisible character is refused with the
memo-style answer: 200, success false, a safety block, nothing stored. A name the
host hands the hook is cleaned once at the hook entry, and every later lookup by
that name (the same hook payload, whoami) finds the same row.

Everything below is production code: the MCP tools called through FastMCP (they
reach the API over HTTP), a real uvicorn on a temporary SQLite file, and
plugin/hooks scripts run as subprocesses.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from aiteam.mcp.tools import reports as report_tools
from aiteam.mcp.tools import task as task_tools
from tests.unit.api.test_hook_ingest_preread import hook_server  # noqa: F401 - pytest fixture

ROOT = Path(__file__).resolve().parents[3]
SEND_EVENT = ROOT / "plugin" / "hooks" / "send_event.py"
RLO, ZWSP, ESC = chr(0x202E), chr(0x200B), chr(0x1B)


def _http(api: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(api + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def _call(name: str, args: dict) -> dict:
    mcp = FastMCP("write-side")
    task_tools.register(mcp)
    report_tools.register(mcp)
    result = asyncio.run(mcp.call_tool(name, args))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads(blocks[0].text)


def _rows(database: Path, sql: str, *args) -> list[tuple]:
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


@pytest.fixture()
def api(hook_server, tmp_path, monkeypatch):  # noqa: F811
    port, database, _ = hook_server
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("AITEAM_API_URL", url)
    # The fixture wires repository, bus and translator; the agent routes also need the
    # team manager, built on that same repository (used only inside the server loop).
    from aiteam.api import deps
    from aiteam.memory.store import MemoryStore
    from aiteam.orchestrator.team_manager import TeamManager

    monkeypatch.setattr(deps, "_manager", TeamManager(
        repository=deps._repository, memory=MemoryStore(repository=deps._repository)))
    work = tmp_path / "work"
    work.mkdir()
    _, project = _http(url, "POST", "/api/projects", {"name": "e2e", "root_path": str(work)})
    return {"url": url, "database": database, "pid": project["data"]["id"], "work": work,
            "home": tmp_path}


def test_an_mcp_single_line_field_is_cleaned_on_the_way_in(api):
    """task_create: the title and a tag arrive with RLO, ZWSP and ESC; one clean line is stored."""
    result = _call("task_create", {"title": f"alpha{RLO}beta{ZWSP}gamma{ESC}[31m  delta\nepsilon",
                                   "project_id": api["pid"], "tags": [f"tag{ZWSP}one"]})
    assert result.get("success") is not False, result
    task_id = result["data"]["id"]
    title, tags = _rows(api["database"], "SELECT title, tags FROM tasks WHERE id = ?", task_id)[0]
    assert title == "alpha beta gamma [31m delta epsilon", repr(title)
    assert json.loads(tags) == ["tag one"], tags


def test_an_mcp_long_text_with_an_invisible_character_is_refused(api):
    """report_save and task_update: memo-style refusal, nothing stored."""
    result = _call("report_save", {"author": "a", "topic": "t", "content": f"body{RLO}hidden"})
    assert result.get("success") is False, result
    assert result.get("safety", {}).get("category") == "invisible_unicode", result
    assert "U+202E" in result.get("error", ""), result
    assert _rows(api["database"], "SELECT COUNT(*) FROM reports")[0][0] == 0

    task_id = _call("task_create", {"title": "t", "project_id": api["pid"]})["data"]["id"]
    result = _call("task_update", {"task_id": task_id, "result": f"out{ESC}[0m put"})
    assert result.get("success") is False and result["safety"]["category"] == "invisible_unicode", result
    assert _rows(api["database"], "SELECT result FROM tasks WHERE id = ?", task_id)[0][0] in (None, "")
    # Tab, newline, carriage return and an emoji ZWJ sequence are ordinary text.
    family = chr(0x1F468) + chr(0x200D) + chr(0x1F469)
    result = _call("task_update", {"task_id": task_id, "result": f"a\tb\r\nc {family}"})
    assert result.get("success") is True, result


def _subagent_start(api: dict, cc_id: str, agent_type: str, team: str) -> None:
    env = {**os.environ, "AITEAM_API_URL": api["url"], "HOME": str(api["home"])}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    payload = {"session_id": "s-hook", "agent_id": cc_id, "agent_type": agent_type,
               "cc_team_name": team, "cwd": str(api["work"])}
    proc = subprocess.run([sys.executable, str(SEND_EVENT), "SubagentStart"], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30, env=env, cwd=str(api["work"]))
    assert proc.returncode == 0, proc.stderr


def test_a_hook_registered_name_is_cleaned_once_and_found_by_its_raw_form(api):
    """send_event.py SubagentStart: the host's agent and team names carry ZWSP / RLO."""
    raw_team = f"team{RLO}x"
    _subagent_start(api, "cc-1", f"worker{ZWSP}one", raw_team)
    agents = _rows(api["database"], "SELECT id, name FROM agents WHERE name LIKE 'worker%'")
    assert [name for _, name in agents] == ["worker one"], agents
    teams = _rows(api["database"], "SELECT id, name FROM teams WHERE name LIKE 'team%'")
    assert [name for _, name in teams] == ["team x"], teams

    # A row created over the API (name cleaned there) is the one a later spawn under
    # the same raw name binds to: both sides compare the cleaned form.
    status, created = _http(api["url"], "POST", f"/api/teams/{teams[0][0]}/agents",
                            {"name": f"helper{ZWSP}two", "role": "worker"})
    assert status == 201, created
    _subagent_start(api, "cc-2", f"helper{ZWSP}two", raw_team)
    helpers = _rows(api["database"], "SELECT id, name, cc_tool_use_id FROM agents WHERE name LIKE 'helper%'")
    assert helpers == [(created["data"]["id"], "helper two", "cc-2")], helpers

    query = urllib.parse.urlencode({"name": f"worker{ZWSP}one"})
    status, found = _http(api["url"], "GET", f"/api/agents/whoami?{query}")
    assert status == 200 and found.get("found") is True and found["agent_id"] == agents[0][0], found


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_hook_side_posts_carry_a_lone_surrogate_as_an_escape(api, monkeypatch):
    """user_notice.fetch_pending and uninstall_main_chain._post, as the hooks encode their bodies."""
    lone = "x" + chr(0xD800) + "y"
    monkeypatch.setenv("HOME", str(api["home"]))
    notice = _load(ROOT / "plugin" / "hooks" / "user_notice.py", "_e2e_user_notice")
    pending = notice.fetch_pending("cc", "UserPromptSubmit", "", {"session_id": "s1", "cwd": "/w/" + lone},
                                   reader="leader-cc", timeout=10)
    assert pending is not None, f"fetch_pending failed: {notice.last_failure()}"
    uninstall = _load(ROOT / "plugin" / "hooks" / "uninstall_main_chain.py", "_e2e_uninstall")
    record = {"uuid": "abcdef0123456789", "kind": "consent", "at": "2026-09-27T01:00:00Z",
              "change": "sync_installed_copies", "user_quote": lone, "host": "cc",
              "targets": [{"path": "/x", "action": "write"}]}
    assert uninstall._post(f"{api['url']}/api/notices/consent", record) is True


def _send_hook(api: dict, event: str, payload: dict) -> None:
    env = {**os.environ, "AITEAM_API_URL": api["url"], "HOME": str(api["home"])}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    proc = subprocess.run([sys.executable, str(SEND_EVENT), event], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=30, env=env, cwd=str(api["work"]))
    assert proc.returncode == 0, proc.stderr


def test_a_tool_output_summary_is_one_clean_line(api):
    """send_event.py PostToolUse: the host's tool_response carries ZWSP, RLO and ESC.

    Like the input summary, it is the host's payload with no author to refuse, so the
    activity row keeps one clean line; agent_activity_query reads that line back.
    """
    from aiteam.mcp.tools import agent as agent_tools

    _subagent_start(api, "cc-out", "outworker", "out-team")
    call = {"session_id": "s-hook", "agent_id": "cc-out", "agent_type": "outworker", "cwd": str(api["work"]),
            "tool_name": "Bash", "tool_use_id": "toolu_out_1", "tool_input": {"command": "ls"}}
    _send_hook(api, "PreToolUse", call)
    _send_hook(api, "PostToolUse", {**call, "tool_response": {
        "stdout": f"line one{ZWSP}\nline{RLO} two{ESC}[0m", "stderr": ""}})

    stored = _rows(api["database"], "SELECT output_summary FROM agent_activities WHERE tool_name = 'Bash'")
    assert stored == [("line one line two [0m",)], [ascii(row) for row in stored]
    (team_id,) = _rows(api["database"], "SELECT id FROM teams WHERE name = 'out-team'")[0]
    mcp = FastMCP("write-side-activity")
    agent_tools.register(mcp)
    result = asyncio.run(mcp.call_tool("agent_activity_query", {"team_id": team_id, "fields": "all"}))
    blocks = result[0] if isinstance(result, tuple) else result
    answer = json.loads(blocks[0].text)
    rows = answer.get("activities") or answer.get("data") or []
    outputs = [row.get("output_summary") for row in rows if row.get("tool_name") == "Bash"]
    assert outputs == ["line one line two [0m"], ascii(answer)[:600]
