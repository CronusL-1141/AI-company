"""Bounded state metadata reaches the real hook API and persisted roster."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from aiteam.api import deps
from aiteam.api import event_bus as bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import agents, hooks, projects, teams
from aiteam.api.ws.manager import ConnectionManager
from aiteam.clock import utc_now
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.services.agent_liveness import AUTO_OFFLINE_KEY
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository
from plugin.harness.codex.hooks.codex_observation import enrich_payload, preserve_identity
from plugin.harness.codex.hooks.hook_core import ESSENTIAL_FIELDS, MAX_PAYLOAD_BYTES

ROOT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHILD = "11111111-2222-3333-4444-555555555555"
OTHER = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


@pytest.fixture
async def capture_api(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("AITEAM_HOOK_RAW_DUMP", raising=False)
    monkeypatch.setattr(bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'os.db'}"
    repo = StorageRepository(db_url=db_url)
    await repo.init_db()
    bus = EventBus(repo)
    manager = TeamManager(repository=repo, memory=MemoryStore(repository=repo), event_bus=bus)
    translator = HookTranslator(repo, bus)
    app = FastAPI()
    for router in (hooks.router, projects.router, teams.router, agents.router):
        app.include_router(router)
    app.dependency_overrides[deps.get_repository] = lambda: repo
    app.dependency_overrides[deps.get_scoped_repository] = lambda: repo
    app.dependency_overrides[deps.get_manager] = lambda: manager
    app.dependency_overrides[deps.get_event_bus] = lambda: bus
    app.dependency_overrides[deps.get_hook_translator] = lambda: translator
    state = tmp_path / "state_5.sqlite"
    with sqlite3.connect(state) as db:
        db.execute(
            "CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT, "
            "agent_nickname TEXT, agent_role TEXT, agent_path TEXT, thread_source TEXT, "
            "model TEXT, cli_version TEXT, secret TEXT)"
        )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False,
        ) as client:
            project = await client.post("/api/projects", json={"name": "capture-test", "root_path": str(tmp_path)})
            assert project.status_code == 201, project.text
            yield SimpleNamespace(
                client=client, repo=repo, app=app, bus=bus, state=state, cwd=str(tmp_path),
                project_id=project.json()["data"]["id"],
            )
    finally:
        await get_engine(db_url).dispose()


def _thread(env, actor: str, *, parent: str | None = None, nickname: str = "", model: str = "") -> None:
    source = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}} if parent else "vscode"
    with sqlite3.connect(env.state) as db:
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?)", (
            actor, str(Path(env.cwd) / f"{actor}.jsonl"), json.dumps(source),
            nickname, "default" if parent else None, f"/root/{nickname}" if parent else "/root",
            "unknown-client", model, "0.152.0", "must-not-be-selected",
        ))


def _payload(env, actor: str, event: str = "SessionStart", **extra) -> dict:
    return {"hook_event_name": event, "session_id": actor, "cwd": env.cwd, **extra}


async def _post(env, payload: dict) -> dict:
    before = env.state.read_bytes()
    captured = enrich_payload(payload, env.state)
    assert env.state.read_bytes() == before
    assert "must-not-be-selected" not in json.dumps(captured)
    response = await env.client.post("/api/hooks/event", json=captured)
    assert response.status_code == 200, response.text
    return response.json()


async def _roster(env) -> list[dict]:
    response = await env.client.get("/api/teams")
    assert response.status_code == 200
    rows = []
    for team in response.json()["data"]:
        response = await env.client.get(f"/api/teams/{team['id']}/agents")
        assert response.status_code == 200, response.text
        rows.extend(response.json()["data"])
    return rows


async def test_root_metadata_round_trip_and_repeated_start_keep_one_leader(capture_api) -> None:
    env = capture_api
    _thread(env, ROOT, model="observed-root-model")
    await _post(env, _payload(env, ROOT))
    rows = await _roster(env)
    assert len(rows) == 1
    root = rows[0]
    assert root["name"] == "Codex Leader"
    assert root["role"] == "leader"
    assert root["harness"] == "codex"
    assert root["model"] == "observed-root-model"
    assert root["transcript_path"] == str(Path(env.cwd) / f"{ROOT}.jsonl")
    assert root["session_id"] == ROOT
    assert root["input_tokens"] is None
    env.app.dependency_overrides[deps.get_hook_translator] = lambda: HookTranslator(env.repo, env.bus)
    await _post(env, _payload(env, ROOT))
    assert [row["id"] for row in await _roster(env)] == [root["id"]]
    same_prefix = "aaaaaaaa-bbbb-cccc-dddd-000000000001"
    _thread(env, same_prefix, model="another-root-model")
    await _post(env, _payload(env, same_prefix))
    rows = await _roster(env)
    assert len(rows) == 2
    assert {row["session_id"] for row in rows} == {ROOT, same_prefix}
    assert len({row["team_id"] for row in rows}) == 2
    assert all(row["role"] == "leader" for row in rows)


async def test_legacy_prefix_team_collision_recovers_exact_root_without_reopening_foreign_team(capture_api) -> None:
    env = capture_api
    other_owner = "aaaaaaaa-1111-2222-3333-444444444444"
    legacy, _ = await env.repo.get_or_create_team(
        name=f"session-{ROOT[:8]}", mode="coordinate",
        config={"kind": "session", "owner_session_id": other_owner},
    )
    old_root = await env.repo.create_agent(
        team_id=legacy.id, name="Leader", role="leader", source="hook", session_id=ROOT,
    )
    await env.repo.update_agent(old_root.id, project_id=env.project_id, status="offline")
    await env.repo.update_team(legacy.id, status="completed")
    _thread(env, ROOT, model="observed-root-model")

    before = await env.client.get(f"/api/projects/{env.project_id}/summary")
    assert before.status_code == 200
    assert before.json()["leaders"] == []
    # An unknown historical offline state requires explicit operator intent.
    resumed = await env.client.put(f"/api/agents/{old_root.id}/status", json={"status": "busy"})
    assert resumed.status_code == 200
    assert (await env.repo.get_agent(old_root.id)).team_id == legacy.id
    activity = _payload(
        env, ROOT, "PreToolUse", tool_name="exec_command", tool_use_id="legacy-root-live",
        tool_input={"cmd": "pwd"}, source_observed_at=utc_now().isoformat(),
    )
    await _post(env, activity)
    await _post(env, activity)
    await _post(env, {**activity, "hook_event_name": "PostToolUse", "tool_response": "ok"})

    rows = await _roster(env)
    assert len(rows) == 1
    current = rows[0]
    assert current["id"] == old_root.id
    assert current["team_id"] != legacy.id
    assert current["status"] == "busy"
    assert current["session_id"] == ROOT
    owned = await env.repo.get_team(current["team_id"])
    assert owned is not None
    assert owned.name == f"session-{ROOT}"
    assert owned.config["owner_session_id"] == ROOT
    foreign = await env.repo.get_team(legacy.id)
    assert foreign is not None
    assert foreign.status == "completed"
    assert foreign.config["owner_session_id"] == other_owner
    summary = await env.client.get(f"/api/projects/{env.project_id}/summary")
    assert summary.status_code == 200
    assert [leader["session_id"] for leader in summary.json()["leaders"]] == [ROOT]

    _thread(env, CHILD, parent=ROOT, nickname="native-member")
    await _post(env, _payload(env, CHILD))
    members = await _roster(env)
    child = next(agent for agent in members if agent["cc_tool_use_id"] == CHILD)
    assert child["team_id"] == current["team_id"]
    assert child["name"] == "native-member"
    migrated_root = await env.repo.get_agent(old_root.id)
    assert migrated_root.cc_tool_use_id == ROOT
    assert await env.repo.auto_offline_agent(
        migrated_root, reason="heartbeat_timeout", occurred_at=utc_now(),
    )
    # Recovery must survive rebuilding the API translator, not an in-memory bind.
    env.app.dependency_overrides[deps.get_hook_translator] = lambda: HookTranslator(env.repo, env.bus)
    await _post(env, _payload(
        env, ROOT, "PostToolUse", tool_name="exec_command", tool_use_id="migrated-root-recovery",
        tool_input={}, tool_response="ok", source_observed_at=utc_now().isoformat(),
    ))
    assert (await env.repo.get_agent(old_root.id)).status == "busy"


async def test_legacy_rehome_does_not_resume_a_manually_stopped_root(capture_api) -> None:
    env = capture_api
    legacy, _ = await env.repo.get_or_create_team(
        name=f"session-{ROOT[:8]}", mode="coordinate",
        config={"kind": "session", "owner_session_id": "aaaaaaaa-1111-2222-3333-444444444444"},
    )
    root = await env.repo.create_agent(
        team_id=legacy.id, name="Leader", role="leader", source="hook", session_id=ROOT,
    )
    await env.repo.update_agent(root.id, project_id=env.project_id)
    await env.repo.update_team(legacy.id, status="completed")
    _thread(env, ROOT)
    before_stop = utc_now() - timedelta(seconds=10)
    response = await env.client.put(f"/api/agents/{root.id}/status", json={"status": "offline"})
    assert response.status_code == 200
    await _post(env, _payload(
        env, ROOT, "PostToolUse", tool_name="exec_command", tool_use_id="delayed-root",
        tool_input={}, tool_response="ok", source_observed_at=before_stop.isoformat(),
    ))
    current = await env.repo.get_agent(root.id)
    assert current.status == "offline"
    assert current.team_id == legacy.id
    assert (await env.repo.get_team(legacy.id)).status == "completed"


async def test_legacy_rehome_keeps_existing_native_members_with_the_root(capture_api) -> None:
    env = capture_api
    owner = "aaaaaaaa-1111-2222-3333-444444444444"
    legacy, _ = await env.repo.get_or_create_team(
        name=f"session-{ROOT[:8]}", mode="coordinate",
        config={"kind": "session", "owner_session_id": owner},
    )
    root = await env.repo.create_agent(
        team_id=legacy.id, name="Leader", role="leader", source="hook", session_id=ROOT,
    )
    await env.repo.update_agent(root.id, project_id=env.project_id, status="busy")
    child = await env.repo.create_agent(
        team_id=legacy.id, name="old-member", role="member", source="hook",
        session_id=ROOT, cc_tool_use_id=CHILD,
    )
    await env.repo.update_agent(child.id, project_id=env.project_id, harness="codex", status="offline")
    foreign = await env.repo.create_agent(
        team_id=legacy.id, name="foreign-member", role="member", source="hook",
        session_id=owner, cc_tool_use_id=OTHER,
    )
    await env.repo.update_agent(foreign.id, project_id=env.project_id, harness="codex")
    other_project = await env.repo.create_project(name="other-project", root_path=f"{env.cwd}/other")
    other_scope = await env.repo.create_agent(
        team_id=legacy.id, name="other-project-member", role="member", source="hook",
        session_id=ROOT, cc_tool_use_id="99999999-2222-3333-4444-555555555555",
    )
    await env.repo.update_agent(other_scope.id, project_id=other_project.id, harness="codex")
    await env.repo.update_team(legacy.id, status="completed")
    _thread(env, ROOT)
    _thread(env, CHILD, parent=ROOT, nickname="existing-native-member")
    await _post(env, _payload(
        env, ROOT, "PreToolUse", tool_name="exec_command", tool_use_id="rehome-members",
        tool_input={}, source_observed_at=utc_now().isoformat(),
    ))
    repaired = await env.repo.get_agent(root.id)
    member = await env.repo.get_agent(child.id)
    assert repaired.team_id != legacy.id
    assert member.team_id == repaired.team_id
    assert member.status == "offline"
    assert (await env.repo.get_agent(foreign.id)).team_id == legacy.id
    assert (await env.repo.get_agent(other_scope.id)).team_id == legacy.id
    received = await _post(env, _payload(env, CHILD, "SubagentStart"))
    assert received["agent_id"] == child.id
    assert (await env.repo.get_agent(child.id)).status == "offline"
    assert (await env.repo.get_team(legacy.id)).status == "completed"


@pytest.mark.parametrize("automatic", [False, True])
async def test_codex_root_activity_distinguishes_manual_stop_from_auto_offline(capture_api, automatic) -> None:
    env = capture_api
    _thread(env, ROOT)
    await _post(env, _payload(env, ROOT))
    root = await env.repo.find_agent_by_cc_id(ROOT)
    if automatic:
        assert await env.repo.auto_offline_agent(root, reason="heartbeat_timeout", occurred_at=utc_now())
    else:
        response = await env.client.put(f"/api/agents/{root.id}/status", json={"status": "offline"})
        assert response.status_code == 200
    activity = _payload(
        env, ROOT, "PreToolUse", tool_name="exec_command", tool_use_id="root-state-proof",
        tool_input={}, source_observed_at=utc_now().isoformat(),
    )
    await _post(env, activity)
    assert (await env.repo.get_agent(root.id)).status == ("busy" if automatic else "offline")
    await _post(env, {**activity, "hook_event_name": "PostToolUse", "tool_response": "ok"})
    assert (await env.repo.get_agent(root.id)).status == ("busy" if automatic else "offline")


async def test_codex_root_touch_cannot_overwrite_a_concurrent_manual_stop(capture_api, monkeypatch) -> None:
    env = capture_api
    _thread(env, ROOT)
    await _post(env, _payload(env, ROOT))
    root = await env.repo.find_agent_by_cc_id(ROOT)
    original_touch = env.repo.touch_codex_active_agent

    async def stop_before_compare(agent, *, now):
        response = await env.client.put(f"/api/agents/{root.id}/status", json={"status": "offline"})
        assert response.status_code == 200
        return await original_touch(agent, now=now)

    monkeypatch.setattr(env.repo, "touch_codex_active_agent", stop_before_compare)
    await _post(env, _payload(
        env, ROOT, "PostToolUse", tool_name="exec_command", tool_use_id="concurrent-root-stop",
        tool_input={}, tool_response="ok", source_observed_at=utc_now().isoformat(),
    ))
    assert (await env.repo.get_agent(root.id)).status == "offline"


async def test_child_own_session_start_uses_nickname_and_never_ends_parent(capture_api) -> None:
    env = capture_api
    _thread(env, ROOT, model="root-model")
    _thread(env, CHILD, parent=ROOT, nickname="Ohm", model="child-model")
    await _post(env, _payload(env, ROOT))
    await _post(env, _payload(env, CHILD, agent_type="default"))
    rows = await _roster(env)
    root = next(row for row in rows if row["role"] == "leader")
    child = next(row for row in rows if row["cc_tool_use_id"] == CHILD)
    assert len(rows) == 2
    assert child["name"] == "Ohm"
    assert child["role"] != "leader"
    assert child["team_id"] == root["team_id"]
    assert child["session_id"] == ROOT
    assert child["model"] == "child-model"
    assert root["model"] == "root-model"
    await _post(env, _payload(env, CHILD, "PreToolUse", tool_name="exec_command"))
    await _post(env, _payload(env, CHILD, "Stop"))
    rows = await _roster(env)
    assert next(row for row in rows if row["id"] == root["id"])["status"] == "busy"
    assert next(row for row in rows if row["id"] == child["id"])["status"] == "waiting"
    team = await env.client.get(f"/api/teams/{root['team_id']}")
    assert team.status_code == 200
    assert team.json()["data"]["status"] == "active"
    await _post(env, _payload(env, CHILD, "SessionEnd"))
    rows = await _roster(env)
    assert len(rows) == 2
    assert next(row for row in rows if row["id"] == root["id"])["status"] == "busy"
    assert next(row for row in rows if row["id"] == child["id"])["status"] == "offline"


async def test_same_role_children_remain_distinct_and_existing_default_name_is_healed(capture_api) -> None:
    env = capture_api
    _thread(env, ROOT)
    _thread(env, CHILD, parent=ROOT, nickname="Ohm")
    _thread(env, OTHER, parent=ROOT, nickname="Poincare")
    await _post(env, _payload(env, ROOT))
    root = (await _roster(env))[0]
    old = await env.repo.create_agent(
        team_id=root["team_id"], name="default", role="default", source="hook",
        session_id=ROOT, cc_tool_use_id=CHILD,
    )
    for actor in (CHILD, OTHER, CHILD):
        await _post(env, _payload(env, ROOT, "SubagentStart", agent_id=actor, agent_type="default"))
    rows = await _roster(env)
    assert len(rows) == 3
    assert {row["name"] for row in rows} == {"Codex Leader", "Ohm", "Poincare"}
    assert next(row for row in rows if row["id"] == old.id)["name"] == "Ohm"
    assert sum(row["role"] == "leader" for row in rows) == 1
    assert all(row["harness"] == "codex" and row["model"] == "" for row in rows)


async def test_nested_child_and_large_payload_preserve_exact_root_membership(capture_api) -> None:
    env = capture_api
    _thread(env, ROOT)
    _thread(env, CHILD, parent=ROOT, nickname="Ohm")
    _thread(env, OTHER, parent=CHILD, nickname="Russell")
    await _post(env, _payload(env, ROOT))
    await _post(env, _payload(env, CHILD))
    payload = enrich_payload(_payload(
        env, OTHER, agent_type="default", tool_input={"large": "x" * 80_000},
    ), env.state)
    payload = preserve_identity(payload, ESSENTIAL_FIELDS, MAX_PAYLOAD_BYTES)
    assert len(json.dumps(payload).encode()) <= MAX_PAYLOAD_BYTES
    assert payload["parent_thread_id"] == CHILD
    response = await env.client.post("/api/hooks/event", json=payload)
    assert response.status_code == 200, response.text
    rows = await _roster(env)
    assert len(rows) == 3
    assert len({row["team_id"] for row in rows}) == 1
    assert {row["session_id"] for row in rows} == {ROOT}
    assert sum(row["role"] == "leader" for row in rows) == 1
    assert next(row for row in rows if row["cc_tool_use_id"] == OTHER)["name"] == "Russell"


async def test_missing_or_conflicting_metadata_cannot_register_a_child_as_leader(capture_api) -> None:
    env = capture_api
    _thread(env, ROOT)
    _thread(env, CHILD, parent=ROOT, nickname="Ohm")
    await _post(env, _payload(env, ROOT))
    before = await _roster(env)
    missing = await _post(env, _payload(env, OTHER))
    header = Path(env.cwd) / "conflicting-header.jsonl"
    header.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": CHILD, "agent_nickname": "Conflicting",
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": OTHER, "depth": 1}}},
    }}) + "\n", encoding="utf-8")
    conflicting = await _post(env, _payload(
        env, OTHER, "SubagentStart", agent_id=CHILD, transcript_path=str(header),
    ))
    assert missing["status"] == conflicting["status"] == "skipped"
    wrong_actor = await _post(env, _payload(env, OTHER, transcript_path=str(header)))
    assert wrong_actor["status"] == "skipped"
    header.write_text(" " * 65_537 + header.read_text(), encoding="utf-8")
    oversized = await _post(env, _payload(env, OTHER, transcript_path=str(header)))
    assert oversized["status"] == "skipped"
    assert await _roster(env) == before


async def test_unmarked_claude_session_start_keeps_legacy_contract(capture_api) -> None:
    env = capture_api
    response = await env.client.post("/api/hooks/event", json=_payload(env, ROOT))
    assert response.status_code == 200
    rows = await _roster(env)
    assert len(rows) == 1
    assert rows[0]["name"] == "Leader"
    assert rows[0]["role"] == "leader"
    assert rows[0]["harness"] is None
    assert rows[0]["model"] == ""


async def test_child_end_revokes_automatic_recovery_even_when_already_offline(capture_api) -> None:
    env = capture_api
    _thread(env, ROOT)
    _thread(env, CHILD, parent=ROOT, nickname="Ohm")
    await _post(env, _payload(env, ROOT))
    await _post(env, _payload(env, CHILD))
    child = await env.repo.find_agent_by_cc_id(CHILD)
    for ending in ("Stop", "SessionEnd"):
        response = await env.client.put(f"/api/agents/{child.id}/status", json={"status": "busy"})
        assert response.status_code == 200
        current = await env.repo.get_agent(child.id)
        assert await env.repo.auto_offline_agent(current, reason="heartbeat_timeout", occurred_at=utc_now())
        assert AUTO_OFFLINE_KEY in (await env.repo.get_agent(child.id)).config
        await _post(env, _payload(env, CHILD, ending))
        ended = await env.repo.get_agent(child.id)
        assert ended.status == "offline"
        assert AUTO_OFFLINE_KEY not in ended.config
        await _post(env, _payload(
            env, CHILD, "PreToolUse", tool_name="exec_command", source_observed_at=utc_now().isoformat(),
        ))
        assert (await env.repo.get_agent(child.id)).status == "offline"
        rows = await _roster(env)
        assert len(rows) == 2
        assert next(row for row in rows if row["role"] == "leader")["status"] == "busy"


async def test_fresh_start_reactivates_waiting_but_replay_and_closed_parent_do_not(capture_api) -> None:
    env = capture_api
    _thread(env, ROOT)
    _thread(env, CHILD, parent=ROOT, nickname="Ohm")
    await _post(env, _payload(env, ROOT))
    original = _payload(env, CHILD, source_observed_at=utc_now().isoformat())
    await _post(env, original)
    child = await env.repo.find_agent_by_cc_id(CHILD)
    await _post(env, _payload(env, CHILD, "Stop"))
    await _post(env, original)
    assert (await env.repo.get_agent(child.id)).status == "waiting"
    fresh = _payload(env, CHILD, "SubagentStart", source_observed_at=utc_now().isoformat())
    await _post(env, fresh)
    assert (await env.repo.get_agent(child.id)).status == "busy"
    await _post(env, _payload(env, CHILD, "Stop"))
    await _post(env, fresh)
    assert (await env.repo.get_agent(child.id)).status == "waiting"
    response = await env.client.put(f"/api/teams/{child.team_id}", json={"status": "completed"})
    assert response.status_code == 200
    closed_status = (await env.repo.get_agent(child.id)).status
    await _post(env, _payload(env, CHILD, "SubagentStart", source_observed_at=utc_now().isoformat()))
    assert (await env.repo.get_agent(child.id)).status == closed_status
    await _post(env, _payload(
        env, CHILD, "PostToolUse", tool_name="exec_command", source_observed_at=utc_now().isoformat(),
    ))
    assert (await env.repo.get_agent(child.id)).status == closed_status
    assert len(await _roster(env)) == 2
