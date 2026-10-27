"""Replay native hook shapes through a real sender, HTTP API, and temporary DB."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "plugin/harness/codex/hooks/send_event_codex.py"
FIXTURES = ROOT / "tests/fixtures/codex"
CAPTURE = FIXTURES / "hooks/probe-0152/capture-run4-subagent.jsonl"


def create_isolated_app():
    """Expose one test-only reaper action inside a fully isolated server process."""
    from unittest.mock import patch

    profile = Path(os.environ["AITEAM_LIVENESS_TEST_PROFILE"])
    profile_patch = patch.object(Path, "home", return_value=profile)
    profile_patch.start()
    from aiteam.api.app import create_app
    from aiteam.api.deps import get_reaper, get_repository
    from aiteam.api.routes.hooks import HookEventPayload

    app = create_app()
    app.state.profile_patch = profile_patch
    captured = []

    @app.post("/__test__/capture/api/hooks/event")
    async def capture(payload: dict):
        captured.append(HookEventPayload.model_validate(payload).model_dump())
        return {"captured": True}

    @app.post("/__test__/captured")
    async def captured_packets():
        return captured

    @app.post("/__test__/reap-config")
    async def reap_config():
        await get_reaper()._check_agent_liveness(get_repository())
        return {"success": True}

    @app.post("/__test__/reap-heartbeat/{native_id}")
    async def reap_heartbeat(native_id: str):
        from aiteam.clock import utc_now

        repo = get_repository()
        agent = await repo.find_agent_by_cc_id(native_id)
        await repo.update_agent(agent.id, last_active_at=utc_now() - timedelta(minutes=10))
        snapshot = await repo.get_agent(agent.id)
        reaped = await get_reaper()._check_hook_agent(snapshot, utc_now(), repo)
        return {"reaped": reaped}

    @app.post("/__test__/reap-teams")
    async def reap_teams():
        from aiteam.clock import utc_now
        await get_reaper()._check_stale_teams(utc_now(), get_repository())
        return {"success": True}

    @app.post("/__test__/close-container/{native_id}")
    async def close_container(native_id: str):
        from aiteam.clock import utc_now

        repo = get_repository()
        agent = await repo.find_agent_by_cc_id(native_id)
        old = utc_now() - timedelta(hours=1)
        await repo.update_team(agent.team_id, created_at=old)
        for member in await repo.list_agents(agent.team_id):
            await repo.update_agent(member.id, status="busy", last_active_at=old)
            assert await repo.auto_offline_agent(
                await repo.get_agent(member.id), reason="heartbeat_timeout", occurred_at=utc_now(),
            )
        await get_reaper()._check_stale_teams(utc_now(), repo)
        return {"team_id": agent.team_id, "project_id": agent.project_id,
                "status": str((await repo.get_team(agent.team_id)).status)}

    return app


def _api_log_tail(path: Path, limit: int = 8192) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - limit))
        tail = stream.read(limit).decode("utf-8", errors="replace")
    return f"Isolated API log: {path}\n{tail}"


@pytest.fixture
def live_liveness(tmp_path):
    profile = tmp_path / "profile"
    (profile / ".claude/teams").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    (profile / ".aiteam").mkdir(parents=True, exist_ok=True)  # the API never starts without a data directory
    environment = {
        **os.environ, "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))),
        "PYTHONDONTWRITEBYTECODE": "1", "AITEAM_DB_PATH": str(tmp_path / "core.db"),
        "AITEAM_API_URL": base, "AITEAM_CODEX_STATE_DIR": str(tmp_path / "sender-state"),
        "AITEAM_LIVENESS_TEST_PROFILE": str(profile), "HOME": str(profile),
        "CODEX_HOME": str(profile / ".codex"),
        "XDG_CONFIG_HOME": str(profile / "config"),
        "XDG_STATE_HOME": str(profile / "state"),
        "TMPDIR": str(tmp_path), "NO_PROXY": "127.0.0.1,localhost",
    }
    environment.pop("AITEAM_HOOK_RAW_DUMP", None)
    api_log = tmp_path / "isolated-api.log"
    with api_log.open("wb") as api_output:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn",
             "tests.integration.test_codex_agent_liveness:create_isolated_app", "--factory",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=environment, stdout=api_output, stderr=subprocess.STDOUT,
        )
    try:
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert process.poll() is None, f"Isolated API exited before health\n{_api_log_tail(api_log)}"
                try:
                    if client.get("/api/health").json().get("status") == "ok":
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail(f"Isolated API health timeout\n{_api_log_tail(api_log)}")
            response = client.post("/api/projects", json={
                "name": "native-liveness-test", "root_path": str(project),
            })
            assert response.status_code == 201, response.text
            yield client, environment, project
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _native(tmp_path, project, event, *, child=False):
    rows = (json.loads(line)["payload"] for line in CAPTURE.read_text().splitlines())
    payload = next(row for row in rows if row.get("hook_event_name") == event
                   and bool(row.get("agent_id")) == child)
    source = FIXTURES / "rollouts/probe-0152/sessions" / (
        payload["transcript_path"].split("/sessions/", 1)[1]
    )
    transcript = tmp_path / source.name
    shutil.copyfile(source, transcript)
    payload.update(cwd=str(project), transcript_path=str(transcript))
    if event in {"PreToolUse", "PostToolUse"}:
        payload["tool_name"] = "Bash"
        payload["tool_input"] = {"command": "true"}
        payload.pop("tool_response", None)
    return payload


def _send(environment, payload):
    result = subprocess.run(
        [sys.executable, str(HOOK), payload["hook_event_name"]],
        input=json.dumps(payload), text=True, capture_output=True, env=environment,
        cwd=ROOT, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "[aiteam-codex-hook] posted" in result.stderr, result.stderr


def _row(environment, native_id):
    with sqlite3.connect(environment["AITEAM_DB_PATH"]) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, status, harness, session_id, cc_tool_use_id, last_active_at "
            "FROM agents WHERE cc_tool_use_id = ?", (native_id,),
        ).fetchone()
    return dict(row) if row else None


def _start(live_liveness, tmp_path, *, codex=True):
    client, environment, project = live_liveness
    parent = _native(tmp_path, project, "SessionStart")
    child = _native(tmp_path, project, "SubagentStart", child=True)
    for payload in (parent, child):
        if codex:
            _send(environment, payload)
        else:
            response = client.post("/api/hooks/event", json=payload)
            assert response.status_code == 200, response.text
    identity = client.get("/api/agents/whoami", params={"cc_agent_id": child["agent_id"]}).json()
    assert identity["found"] is True
    assert identity["matched_by"] == "cc_agent_id"
    assert _row(environment, child["agent_id"])["status"] == "busy"
    return child


def test_native_activity_survives_cc_config_probe(live_liveness, tmp_path):
    client, environment, project = live_liveness
    child = _start(live_liveness, tmp_path)
    _send(environment, _native(tmp_path, project, "PreToolUse", child=True))
    response = client.post("/__test__/reap-config")
    assert response.status_code == 200, response.text
    after_probe = _row(environment, child["agent_id"])
    _send(environment, _native(tmp_path, project, "PostToolUse", child=True))
    after_activity = _row(environment, child["agent_id"])
    assert after_activity["status"] == "busy", {"after_probe": after_probe,
                                               "after_activity": after_activity}
    assert after_probe["status"] == "busy"
    assert after_activity["harness"] == "codex"


def test_unmarked_cc_event_keeps_cc_config_liveness(live_liveness, tmp_path):
    client, environment, _ = live_liveness
    child = _start(live_liveness, tmp_path, codex=False)
    response = client.post("/__test__/reap-config")
    assert response.status_code == 200, response.text
    row = _row(environment, child["agent_id"])
    assert row["status"] == "offline"
    # CC 事件建的行 09-30 起标 claude-code（按用户裁定 P-4），不再是 NULL
    assert row["harness"] == "claude-code"


@pytest.mark.parametrize("ending", ["manual", "session-end"])
def test_unknown_offline_reason_never_revives_on_tool_activity(live_liveness, tmp_path, ending):
    client, environment, project = live_liveness
    child = _start(live_liveness, tmp_path)
    row = _row(environment, child["agent_id"])
    if ending == "manual":
        response = client.put(f"/api/agents/{row['id']}/status", json={"status": "offline"})
        assert response.status_code == 200, response.text
    else:
        parent = _native(tmp_path, project, "SessionStart")
        parent["hook_event_name"] = "SessionEnd"
        _send(environment, parent)
    assert _row(environment, child["agent_id"])["status"] == "offline"
    _send(environment, _native(tmp_path, project, "PreToolUse", child=True))
    _send(environment, _native(tmp_path, project, "PostToolUse", child=True))
    assert _row(environment, child["agent_id"])["status"] == "offline"
    if ending == "session-end":
        assert _row(environment, parent["session_id"])["status"] == "offline"


def test_unknown_native_id_does_not_gain_registration_from_activity(live_liveness, tmp_path):
    client, environment, project = live_liveness
    payload = _native(tmp_path, project, "PreToolUse", child=True)
    _send(environment, payload)
    assert client.get("/api/agents/whoami", params={"cc_agent_id": payload["agent_id"]}).json() == {
        "success": True, "found": False,
    }
    assert _row(environment, payload["agent_id"]) is None


def test_fresh_native_activity_recovers_current_automatic_offline(live_liveness, tmp_path):
    client, environment, project = live_liveness
    child = _start(live_liveness, tmp_path)
    response = client.post(f"/__test__/reap-heartbeat/{child['agent_id']}")
    assert response.json() == {"reaped": True}
    assert _row(environment, child["agent_id"])["status"] == "offline"
    payload = _native(tmp_path, project, "PreToolUse", child=True)
    assert "timestamp" not in payload
    assert "source_observed_at" not in payload
    _send(environment, payload)
    assert _row(environment, child["agent_id"])["status"] == "busy"


@pytest.mark.parametrize("source_time", ["missing", "old", "future", "naive", "invalid"])
def test_unproven_source_time_does_not_recover_automatic_offline(
    live_liveness, tmp_path, source_time,
):
    client, environment, project = live_liveness
    child = _start(live_liveness, tmp_path)
    assert client.post(f"/__test__/reap-heartbeat/{child['agent_id']}").json()["reaped"]
    payload = _native(tmp_path, project, "PreToolUse", child=True)
    _send({**environment, "AITEAM_API_URL": environment["AITEAM_API_URL"] + "/__test__/capture"},
          payload)
    packet = client.post("/__test__/captured").json()[-1]
    values = {
        "old": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        "future": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "naive": datetime.now().isoformat(), "invalid": "PRIVATE_INVALID_TIME",
    }
    if source_time != "missing":
        packet["source_observed_at"] = values[source_time]
    else:
        packet.pop("source_observed_at")
    response = client.post("/api/hooks/event", json=packet)
    assert response.status_code == 200, response.text
    assert response.json()["liveness_reason"].startswith("source_observed_at_")
    assert _row(environment, child["agent_id"])["status"] == "offline"


@pytest.mark.parametrize("ending", ["manual-same-value", "session-end", "team-completed"])
def test_explicit_end_revokes_current_automatic_recovery(live_liveness, tmp_path, ending):
    client, environment, project = live_liveness
    child = _start(live_liveness, tmp_path)
    assert client.post(f"/__test__/reap-heartbeat/{child['agent_id']}").json()["reaped"]
    row = _row(environment, child["agent_id"])
    if ending == "manual-same-value":
        response = client.put(f"/api/agents/{row['id']}/status", json={"status": "offline"})
        assert response.status_code == 200, response.text
    elif ending == "session-end":
        parent = _native(tmp_path, project, "SessionStart")
        parent["hook_event_name"] = "SessionEnd"
        _send(environment, parent)
    else:
        team_id = client.get("/api/agents/whoami", params={"cc_agent_id": child["agent_id"]}).json()[
            "team_id"
        ]
        response = client.put(f"/api/teams/{team_id}", json={"status": "completed"})
        assert response.status_code == 200, response.text
    payload = _native(tmp_path, project, "PostToolUse", child=True)
    _send(environment, payload)
    assert _row(environment, child["agent_id"])["status"] == "offline"


def test_delayed_collector_packet_cannot_revive_but_new_observation_can(live_liveness, tmp_path):
    client, environment, project = live_liveness
    child = _start(live_liveness, tmp_path)
    payload = _native(tmp_path, project, "PreToolUse", child=True)
    assert "timestamp" not in payload and "source_observed_at" not in payload
    capture_env = {**environment, "AITEAM_API_URL": environment["AITEAM_API_URL"] + "/__test__/capture"}
    _send(capture_env, payload)
    packet = client.post("/__test__/captured").json()[-1]
    original_observation = packet["source_observed_at"]
    assert client.post(f"/__test__/reap-heartbeat/{child['agent_id']}").json()["reaped"]
    response = client.post("/api/hooks/event", json=packet)
    assert response.json()["liveness_reason"] == "source_observation_precedes_offline"
    assert _row(environment, child["agent_id"])["status"] == "offline"
    assert packet["source_observed_at"] == original_observation
    _send(environment, payload)
    assert _row(environment, child["agent_id"])["status"] == "busy"
    assert client.post(f"/__test__/reap-heartbeat/{child['agent_id']}").json()["reaped"]
    response = client.post("/api/hooks/event", json=packet)
    assert response.json()["liveness_reason"] == "source_observation_precedes_offline"
    assert _row(environment, child["agent_id"])["status"] == "offline"


def test_original_leader_and_project_summary_recover_after_native_container_close(live_liveness, tmp_path):
    client, environment, project = live_liveness
    _start(live_liveness, tmp_path)
    native = _native(tmp_path, project, "SessionStart")["session_id"]
    before = _row(environment, native)
    closed = client.post(f"/__test__/close-container/{native}")
    assert closed.status_code == 200, closed.text
    scope = closed.json()
    assert scope["status"] == "completed"
    assert _row(environment, native)["status"] == "offline"
    summary = client.get(f"/api/projects/{scope['project_id']}/summary")
    assert summary.status_code == 200, summary.text
    assert not any(row["session_id"] == native for row in summary.json()["leaders"])
    payload = _native(tmp_path, project, "PreToolUse")
    _send(environment, payload)
    restored = _row(environment, native)
    assert restored["id"] == before["id"] and restored["status"] == "busy"
    assert client.get(f"/api/teams/{scope['team_id']}").json()["data"]["status"] == "active"
    summary = client.get(f"/api/projects/{scope['project_id']}/summary").json()
    assert next(row for row in summary["leaders"] if row["session_id"] == native)["status"] == "busy"
    _send(environment, _native(tmp_path, project, "PostToolUse"))
    assert _row(environment, native)["id"] == before["id"]
    with sqlite3.connect(environment["AITEAM_DB_PATH"]) as connection:
        events = connection.execute(
            "SELECT type, data FROM events WHERE type IN ('team.auto_revived', 'agent.status_changed')",
        ).fetchall()
    assert any(kind == "team.auto_revived" and json.loads(data).get("team_id") == scope["team_id"]
               for kind, data in events)
    assert any(kind == "agent.status_changed" and json.loads(data).get("agent_id") == before["id"]
               for kind, data in events)


@pytest.mark.parametrize("marker", [{"origin_at": "invalid"}, None])
def test_native_sender_packet_marked_replay_cannot_restore_closed_root(live_liveness, tmp_path, marker):
    client, environment, project = live_liveness
    _start(live_liveness, tmp_path)
    native = _native(tmp_path, project, "SessionStart")["session_id"]
    scope = client.post(f"/__test__/close-container/{native}").json()
    assert scope["status"] == "completed"
    packet = _native(tmp_path, project, "PreToolUse")
    capture_env = {**environment, "AITEAM_API_URL": environment["AITEAM_API_URL"] + "/__test__/capture"}
    _send(capture_env, packet)
    captured = client.post("/__test__/captured").json()[-1]
    captured["_hook_replay"] = marker
    before = _row(environment, native)
    response = client.post("/api/hooks/event", json=captured)
    assert response.status_code == 200, response.text
    assert _row(environment, native)["status"] == "offline"
    assert _row(environment, native)["last_active_at"] == before["last_active_at"]
    assert client.get(f"/api/teams/{scope['team_id']}").json()["data"]["status"] == "completed"



def test_manual_status_http_and_registered_mcp_correct_only_exact_owner(live_liveness, tmp_path, monkeypatch):
    import asyncio

    from fastmcp import FastMCP

    from aiteam.mcp.tools import agent as agent_tools

    client, environment, project = live_liveness
    _start(live_liveness, tmp_path)
    native = _native(tmp_path, project, "SessionStart")["session_id"]
    scope = client.post(f"/__test__/close-container/{native}").json()
    root = _row(environment, native)
    response = client.put(f"/api/agents/{root['id']}/status", json={"status": "busy"})
    assert response.status_code == 200, response.text
    client.post("/__test__/reap-teams").raise_for_status()
    assert client.get(f"/api/teams/{scope['team_id']}").json()["data"]["status"] == "active"
    response = client.put(f"/api/teams/{scope['team_id']}", json={"status": "completed"})
    assert response.status_code == 200, response.text
    assert _row(environment, native)["status"] == "offline"

    # The real registered MCP tool still traverses the production HTTP route.
    # Only its transport destination is confined to this test-owned API.
    def isolated_call(method, path, data=None):
        response = client.request(method, path, json=data)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(agent_tools, "_api_call", isolated_call)
    mcp = FastMCP("manual-owner-scope-test")
    agent_tools.register(mcp)

    async def call_tool():
        tool = await mcp.get_tool("agent_update_status")
        await tool.run({"agent_id": root["id"], "status": "busy"})

    asyncio.run(call_tool())
    client.post("/__test__/reap-teams").raise_for_status()
    assert client.get(f"/api/teams/{scope['team_id']}").json()["data"]["status"] == "active"
    assert _row(environment, native)["status"] == "busy"
