"""Every write entry named in the write-side decision, field by field, over the real app.

Single-line fields (titles, names, senders, authors, tags, mentions, participants)
are stored cleaned: control and format characters become spaces, whitespace is
collapsed. Long text (descriptions, results, bodies) carrying an invisible
character is refused the memo way (200, success false, a safety block) and nothing
is stored. Each value carries a unique token so the check reads the database file
itself, not the route's response.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from testlib import make_team

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

RLO, ZWSP, ESC = chr(0x202E), chr(0x200B), chr(0x1B)
_counter = [0]


def _token() -> str:
    _counter[0] += 1
    return f"TOK{_counter[0]:04d}"


def _dirty(token: str) -> str:
    return f"{token}a{RLO}b{ZWSP}c\n  d{ESC}e"


def _clean(token: str) -> str:
    return f"{token}a b c d e"


@pytest.fixture()
def env(tmp_path):
    loop = asyncio.get_event_loop()
    database = tmp_path / "fields.db"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{database}")
    loop.run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    bus = EventBus(repo=repo)
    deps._repository, deps._memory_store, deps._event_bus = repo, memory, bus
    deps._manager = TeamManager(repository=repo, memory=memory)
    deps._hook_translator = HookTranslator(repo=repo, event_bus=bus)
    app = create_app()

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    app.router.lifespan_context = no_lifespan
    client = TestClient(app)
    project = client.post("/api/projects", json={"name": "fields", "root_path": str(tmp_path)}).json()["data"]
    team = make_team({"name": "fields-team", "project_id": project["id"]})
    task = client.post(f"/api/projects/{project['id']}/tasks", json={"title": "t"}).json()["data"]
    meeting = client.post(f"/api/teams/{team['id']}/meetings", json={"topic": "m"}).json()["data"]
    agent = client.post(f"/api/teams/{team['id']}/agents", json={"name": "ag", "role": "worker"}).json()["data"]
    briefing = client.post("/api/leader-briefings", json={"title": "b", "project_id": project["id"]}).json()
    issue = client.post(f"/api/teams/{team['id']}/issues", json={"title": "iss"}).json()
    job = client.post("/api/scheduler", json={"name": "job", "interval_seconds": 300,
                                               "action_type": "wake_agent"}).json()
    ids = {"pid": project["id"], "tid": team["id"], "task": task["id"], "mid": meeting["id"],
           "aid": agent["id"], "bid": briefing["id"], "iid": (issue.get("data") or issue)["id"],
           "sid": (job.get("data") or job)["id"]}
    yield client, database, ids
    loop.run_until_complete(close_db())
    deps._repository = deps._memory_store = deps._event_bus = deps._manager = deps._hook_translator = None


def _stored(database: Path, token: str) -> list[str]:
    """Every stored text (JSON decoded where it parses) that contains the token."""
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    found = []
    try:
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            for column in [row[1] for row in con.execute(f"PRAGMA table_info('{table}')")]:
                for (value,) in con.execute(
                    f"SELECT \"{column}\" FROM \"{table}\" WHERE CAST(\"{column}\" AS TEXT) LIKE ?",
                    (f"%{token}%",),
                ):
                    try:
                        leaves = _strings(json.loads(value))
                    except (TypeError, ValueError):
                        leaves = [str(value)]
                    if table != "events":  # the audit log keeps request copies
                        found.extend(leaf for leaf in leaves if token in leaf)
    finally:
        con.close()
    return found


def _strings(value) -> list[str]:
    """Every string inside a decoded JSON value (a JSON column holds text in its leaves)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in (*value.keys(), *value.values()) for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


# (label, method, path template, body builder taking the dirty value)
SINGLE_LINE = [
    ("task_create.title", "POST", "/api/projects/{pid}/tasks", lambda v: {"title": v}),
    ("task_create.assigned_to", "POST", "/api/projects/{pid}/tasks", lambda v: {"title": "t", "assigned_to": v}),
    ("task_create.tags", "POST", "/api/projects/{pid}/tasks", lambda v: {"title": "t", "tags": [v]}),
    ("task_run.title", "POST", "/api/teams/{tid}/tasks/run", lambda v: {"title": v, "description": "d"}),
    ("task_update.title", "PUT", "/api/tasks/{task}", lambda v: {"title": v}),
    ("task_update.assigned_to", "PUT", "/api/tasks/{task}", lambda v: {"assigned_to": v}),
    ("task_update.tags", "PUT", "/api/tasks/{task}", lambda v: {"tags": [v]}),
    ("decompose.title", "POST", "/api/teams/{tid}/tasks/decompose",
     lambda v: {"title": v, "subtasks": [{"title": "s"}]}),
    ("decompose.subtask_title", "POST", "/api/teams/{tid}/tasks/decompose",
     lambda v: {"title": "p", "subtasks": [{"title": v}]}),
    ("issue.title", "POST", "/api/teams/{tid}/issues", lambda v: {"title": v}),
    ("agent.name", "POST", "/api/teams/{tid}/agents", lambda v: {"name": v, "role": "worker"}),
    ("agent_status.current_task", "PUT", "/api/agents/{aid}/status", lambda v: {"status": "busy", "current_task": v}),
    ("project.name", "PUT", "/api/projects/{pid}", lambda v: {"name": v}),
    ("phase.name", "POST", "/api/projects/{pid}/phases", lambda v: {"name": v}),
    ("briefing.title", "POST", "/api/leader-briefings", lambda v: {"title": v}),
    ("briefing.tags", "POST", "/api/leader-briefings", lambda v: {"title": "b", "tags": [v]}),
    ("meeting.topic", "POST", "/api/teams/{tid}/meetings", lambda v: {"topic": v}),
    ("meeting.participants", "POST", "/api/teams/{tid}/meetings", lambda v: {"topic": "m", "participants": [v]}),
    ("meeting_update.topic", "PUT", "/api/meetings/{mid}", lambda v: {"topic": v}),
    ("meeting_message.agent_name", "POST", "/api/meetings/{mid}/messages",
     lambda v: {"agent_id": "a1", "agent_name": v, "content": "c"}),
    ("channel.sender", "POST", "/api/channels/team:fields/messages",
     lambda v: {"sender": v, "content": "c", "project_id": "{pid}"}),
    ("channel.mentions", "POST", "/api/channels/team:fields/messages",
     lambda v: {"sender": "s", "content": "c", "mentions": [v], "project_id": "{pid}"}),
    ("memo.author", "POST", "/api/tasks/{task}/memo", lambda v: {"content": "c", "author": v}),
    ("report.topic", "POST", "/api/reports", lambda v: {"author": "a", "topic": v, "content": "c"}),
    ("report.author", "POST", "/api/reports", lambda v: {"author": v, "topic": "t", "content": "c"}),
    # Beyond the decision's list, same classes (Leader 09-27): names and one-liners.
    ("agent.role", "POST", "/api/teams/{tid}/agents", lambda v: {"name": "role-" + v[3:7], "role": v}),
    ("scheduler.name", "POST", "/api/scheduler",
     lambda v: {"name": v, "interval_seconds": 300, "action_type": "wake_agent"}),
    ("scheduler_update.name", "PUT", "/api/scheduler/{sid}", lambda v: {"name": v}),
]

LONG_TEXT = [
    ("task_create.description", "POST", "/api/projects/{pid}/tasks", lambda v: {"title": "t", "description": v}),
    ("task_run.description", "POST", "/api/teams/{tid}/tasks/run", lambda v: {"title": "t", "description": v}),
    ("task_update.description", "PUT", "/api/tasks/{task}", lambda v: {"description": v}),
    ("task_update.result", "PUT", "/api/tasks/{task}", lambda v: {"result": v}),
    ("decompose.description", "POST", "/api/teams/{tid}/tasks/decompose",
     lambda v: {"title": "p", "description": v, "subtasks": [{"title": "s"}]}),
    ("decompose.subtask_description", "POST", "/api/teams/{tid}/tasks/decompose",
     lambda v: {"title": "p", "subtasks": [{"title": "s", "description": v}]}),
    ("issue.description", "POST", "/api/teams/{tid}/issues", lambda v: {"title": "i", "description": v}),
    ("memo.content", "POST", "/api/tasks/{task}/memo", lambda v: {"content": v}),
    ("report.content", "POST", "/api/reports", lambda v: {"author": "a", "topic": "t", "content": v}),
    ("channel.content", "POST", "/api/channels/team:fields/messages",
     lambda v: {"sender": "s", "content": v, "project_id": "{pid}"}),
    ("meeting_message.content", "POST", "/api/meetings/{mid}/messages",
     lambda v: {"agent_id": "a1", "agent_name": "n", "content": v}),
    ("briefing.description", "POST", "/api/leader-briefings", lambda v: {"title": "b", "description": v}),
    ("briefing.options", "POST", "/api/leader-briefings", lambda v: {"title": "b", "options": v}),
    ("briefing.recommendation", "POST", "/api/leader-briefings", lambda v: {"title": "b", "recommendation": v}),
    ("briefing.resolution", "PUT", "/api/leader-briefings/{bid}/resolve", lambda v: {"resolution": v}),
    ("memory.content", "POST", "/api/memories", lambda v: {"content": v, "kind": "preference", "scope": "global"}),
    ("project_create.description", "POST", "/api/projects",
     lambda v: {"name": "p2", "root_path": "/tmp/p2-fields", "description": v}),
    ("project_update.description", "PUT", "/api/projects/{pid}", lambda v: {"description": v}),
    # Beyond the decision's list, same class (Leader 09-27): long text an author writes.
    ("phase.description", "POST", "/api/projects/{pid}/phases", lambda v: {"name": "ph", "description": v}),
    ("agent.system_prompt", "POST", "/api/teams/{tid}/agents",
     lambda v: {"name": "sp-" + v[3:7], "role": "worker", "system_prompt": v}),
    ("scheduler.description", "POST", "/api/scheduler",
     lambda v: {"name": "j", "interval_seconds": 300, "action_type": "wake_agent", "description": v}),
    ("scheduler_update.description", "PUT", "/api/scheduler/{sid}", lambda v: {"description": v}),
    ("issue_status.resolution", "PUT", "/api/issues/{iid}/status",
     lambda v: {"status": "investigating", "resolution": v}),
]


def _send(client, ids, method, path, body):
    path = path.format(**ids)
    body = json.loads(json.dumps(body).replace('"{pid}"', json.dumps(ids["pid"])))
    return client.request(method, path, json=body)


@pytest.mark.parametrize("case", SINGLE_LINE, ids=[c[0] for c in SINGLE_LINE])
def test_single_line_fields_are_stored_cleaned(case, env):
    client, database, ids = env
    label, method, path, body = case
    token = _token()
    resp = _send(client, ids, method, path, body(_dirty(token)))
    assert resp.status_code < 300 and resp.json().get("success") is not False, f"{label}: {resp.text[:200]}"
    stored = _stored(database, token)
    assert stored, f"{label}: nothing stored"
    for text in stored:
        assert _clean(token) in text, f"{label}: stored {text[:120]!r}"
        assert RLO not in text and ZWSP not in text and ESC not in text


@pytest.mark.parametrize("case", LONG_TEXT, ids=[c[0] for c in LONG_TEXT])
def test_long_text_with_an_invisible_character_is_refused_the_memo_way(case, env):
    client, database, ids = env
    label, method, path, body = case
    token = _token()
    resp = _send(client, ids, method, path, body(f"{token} body{RLO}hidden"))
    assert resp.status_code == 200, f"{label}: {resp.status_code} {resp.text[:200]}"
    answer = resp.json()
    assert answer["success"] is False and answer["safety"]["category"] == "invisible_unicode", answer
    assert "U+202E" in answer["error"], answer
    assert _stored(database, token) == [], f"{label}: a refused body was stored"


@pytest.mark.parametrize("case", LONG_TEXT, ids=[c[0] for c in LONG_TEXT])
def test_long_text_keeps_its_layout_and_emoji_sequences(case, env):
    client, database, ids = env
    label, method, path, body = case
    token = _token()
    family = chr(0x1F468) + chr(0x200D) + chr(0x1F469)
    text = f"{token} line\tone\r\nline two {family} {chr(0x2028)} end"
    resp = _send(client, ids, method, path, body(text))
    assert resp.status_code < 300 and resp.json().get("success") is not False, f"{label}: {resp.text[:200]}"
    assert any(text in stored for stored in _stored(database, token)), f"{label}: not stored as sent"


def test_a_body_with_another_error_keeps_the_422(env):
    client, _, ids = env
    resp = client.post(f"/api/projects/{ids['pid']}/tasks", json={"description": f"x{RLO}"})  # no title
    assert resp.status_code == 422


def test_reconcile_merge_refuses_per_operation(env):
    client, database, ids = env
    memo_ids = [client.post(f"/api/tasks/{ids['task']}/memo", json={"content": f"m{i}"}).json()["data"]["id"]
                for i in range(2)]
    token = _token()
    resp = client.post("/api/memory/reconcile/apply", json={"operations": [
        {"op": "merge", "content": f"{token} merged{ZWSP}", "memo_ids": memo_ids},
        {"op": "score", "memo_id": memo_ids[0], "quality_score": 7, "reason": "ok"},
    ]})
    assert resp.status_code == 200
    results = resp.json()["data"]["results"]
    assert results[0]["status"] == "error" and results[0]["safety"]["category"] == "invisible_unicode", results
    assert results[1]["status"] != "error", results  # the rest of the batch still applies
    assert _stored(database, token) == []


def test_a_team_named_in_the_path_is_looked_up_cleaned(env):
    """Team names are stored cleaned; a raw name in the meeting route still finds the team."""
    client, _, _ = env
    make_team({"name": "named team"})
    resp = client.post(f"/api/teams/named{ZWSP}team/meetings", json={"topic": "t"})
    assert resp.status_code == 201, resp.text[:200]


def _hook(client, payload: dict) -> dict:
    resp = client.post("/api/hooks/event", json=payload)
    assert resp.status_code == 200, resp.text[:200]
    return resp.json()


def test_teammate_idle_finds_the_agent_by_the_raw_name_the_host_sends(env):
    client, database, _ = env
    started = _hook(client, {"hook_event_name": "SubagentStart", "session_id": "s-idle", "agent_id": "cc-idle",
                             "agent_type": f"mate{ZWSP}one", "cc_team_name": "idle-team"})
    idle = _hook(client, {"hook_event_name": "TeammateIdle", "session_id": "s-idle",
                          "teammate_name": f"mate{ZWSP}one", "team_name": f"idle{RLO}team"})
    assert idle["agent_id"] == started["agent_id"], (started, idle)
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        data = [json.loads(row[0]) for row in con.execute("SELECT data FROM events WHERE type = 'cc.teammate_idle'")]
    finally:
        con.close()
    assert [(d["teammate_name"], d["cc_team_name"]) for d in data] == [("mate one", "idle team")], data


def test_the_activity_summary_a_hook_writes_is_one_clean_line(env):
    client, database, _ = env
    token = _token()
    _hook(client, {"hook_event_name": "SubagentStart", "session_id": "s-act", "agent_id": "cc-act",
                   "agent_type": "actor", "cc_team_name": "act-team"})
    _hook(client, {"hook_event_name": "PreToolUse", "session_id": "s-act", "agent_id": "cc-act",
                   "agent_type": "actor", "tool_name": "Bash", "tool_use_id": "tu-act",
                   "tool_input": {"command": "ls", "description": _dirty(token)}})
    stored = _stored(database, token)
    assert stored and all(_clean(token) in text for text in stored), stored

