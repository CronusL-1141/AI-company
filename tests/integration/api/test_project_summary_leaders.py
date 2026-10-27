"""Project summaries merge file sessions and persisted leaders without writes."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

import aiteam.mcp.tools.project as project_tools
from aiteam.api import session_probe
from aiteam.api.deps import get_repository
from aiteam.api.routes import projects
from aiteam.services import project_sessions as project_sessions_module
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository
from aiteam.types import Agent, AgentStatus, HarnessId, TaskStatus

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
CC_SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CODEX_SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
async def summary_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, StorageRepository, Path]]:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated_home))
    # The Claude Code config folder resolves from the environment (aiteam.paths).
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated_home / ".claude"))
    monkeypatch.setattr(projects, "utc_now", lambda: NOW)
    monkeypatch.setattr(session_probe, "utc_now", lambda: NOW)
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'leaders.db'}"
    repo = StorageRepository(db_url=db_url)
    await repo.init_db()
    app = FastAPI()
    app.include_router(projects.router)
    app.dependency_overrides[get_repository] = lambda: repo
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False,
        ) as client:
            yield client, repo, tmp_path
    finally:
        await get_engine(db_url).dispose()


async def _project(client: httpx.AsyncClient, root: Path) -> str:
    root.mkdir()
    response = await client.post("/api/projects", json={"name": root.name, "root_path": str(root)})
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _transcript(root: Path, session_id: str, *, active_at: datetime, model: str,
                registry_status: str | None = None) -> None:
    directory = Path.home() / ".claude" / "projects" / session_probe.project_slug(str(root))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(json.dumps({
        "type": "assistant",
        "message": {"model": model, "usage": {"input_tokens": 100, "output_tokens": 10}},
    }) + "\n", encoding="utf-8")
    os.utime(path, (active_at.timestamp(), active_at.timestamp()))
    if registry_status:
        registry = Path.home() / ".claude" / "sessions"
        registry.mkdir(parents=True, exist_ok=True)
        (registry / f"{session_id}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": session_id, "cwd": str(root), "status": registry_status,
        }), encoding="utf-8")


async def _leader(
    repo: StorageRepository, project_id: str, session_id: str | None,
    *, status: AgentStatus = AgentStatus.BUSY,
    active_at: datetime | None = NOW,
    created_at: datetime = NOW - timedelta(hours=1),
    harness: HarnessId | None = HarnessId.CODEX,
    model: str = "", current_task: str = "", role: str = "leader",
) -> Agent:
    team = await repo.create_team(name=f"fixture-{uuid4().hex}", project_id=project_id, mode="coordinate")
    leader = await repo.create_agent(
        team_id=team.id, name="Leader", role=role, source="hook", session_id=session_id, model=model,
    )
    return await repo.update_agent(
        leader.id, project_id=project_id, status=status, last_active_at=active_at,
        created_at=created_at, harness=harness, current_task=current_task,
    )


async def _summary(client: httpx.AsyncClient, project_id: str) -> dict:
    response = await client.get(f"/api/projects/{project_id}/summary")
    assert response.status_code == 200, response.text
    return response.json()


async def test_old_cc_file_does_not_hide_current_registered_codex_leader(summary_client) -> None:
    client, repo, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    _transcript(root, CC_SESSION, active_at=NOW - timedelta(days=1), model="observed-cc-model")
    assert (await _summary(client, project_id))["leaders"] == []
    codex = await _leader(repo, project_id, CODEX_SESSION, current_task="Current task")
    before = codex.model_dump(mode="json")

    summary = await _summary(client, project_id)
    leaders = {leader["session_id"]: leader for leader in summary["leaders"]}
    assert set(leaders) == {CODEX_SESSION}
    assert leaders[CODEX_SESSION]["status"] == "busy"
    assert leaders[CODEX_SESSION]["live"] is True
    assert leaders[CODEX_SESSION]["model"] == ""
    assert leaders[CODEX_SESSION]["current_task"] == "Current task"
    assert leaders[CODEX_SESSION]["harness"] == "codex"
    assert summary["leader"] == leaders[CODEX_SESSION]
    assert summary["last_activity_at"] == NOW.isoformat()
    assert await _summary(client, project_id) == summary
    persisted = await repo.get_agent(codex.id)
    assert persisted is not None
    assert persisted.model_dump(mode="json") == before


async def test_registered_active_sessions_are_complete_and_project_scoped(summary_client) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    other_project = await _project(client, tmp_path / "other-project")
    expected = {}
    for session_id, status in (
        (CODEX_SESSION, AgentStatus.BUSY),
        (CC_SESSION, AgentStatus.WAITING),
        ("legacy-observed-session", AgentStatus.OFFLINE),
    ):
        await _leader(repo, project_id, session_id, status=status)
        if status == AgentStatus.BUSY:
            expected[session_id] = status.value
    unknown = await _leader(repo, project_id, "unknown-harness-session", harness=None)
    expected[unknown.session_id] = "busy"
    for missing in (None, "", " "):
        await _leader(repo, project_id, missing)
    await _leader(repo, other_project, "foreign-session")
    await _leader(repo, project_id, "child-session", role="analyst")

    summary = await _summary(client, project_id)
    assert {row["session_id"]: row["status"] for row in summary["leaders"]} == expected
    assert all(row["model"] == "" for row in summary["leaders"])
    assert next(row for row in summary["leaders"] if row["session_id"] == unknown.session_id)["harness"] is None
    assert await _summary(client, project_id) == summary
    persisted = await repo.get_agent(unknown.id)
    assert persisted is not None
    assert persisted.harness is None


async def test_duplicate_session_uses_latest_observation_not_latest_registration(summary_client) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    await _leader(
        repo, project_id, CODEX_SESSION, active_at=NOW - timedelta(minutes=2),
        created_at=NOW - timedelta(minutes=3), model="older-observation", current_task="Old task",
    )
    latest = await _leader(
        repo, project_id, CODEX_SESSION, active_at=NOW, created_at=NOW - timedelta(hours=1),
        status=AgentStatus.OFFLINE, model="latest-observation", current_task="",
    )
    summary = await _summary(client, project_id)
    assert summary["leaders"] == []
    assert summary["leader"] is None
    assert await _summary(client, project_id) == summary
    persisted = await repo.get_agent(latest.id)
    assert persisted is not None
    assert persisted.status == AgentStatus.OFFLINE


async def test_each_matching_cc_session_keeps_file_data_and_receives_its_current_task(summary_client) -> None:
    client, repo, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    second = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    for index, session_id in enumerate((CC_SESSION, second)):
        _transcript(
            root, session_id, active_at=NOW - timedelta(minutes=index), model=f"observed-cc-{index}",
            registry_status="busy",
        )
    before = {row["session_id"]: row for row in (await _summary(client, project_id))["leaders"]}
    assert all(row["harness"] == "claude-code" for row in before.values())
    for index, session_id in enumerate((CC_SESSION, second)):
        await _leader(
            repo, project_id, session_id, harness=HarnessId.CLAUDE_CODE,
            status=AgentStatus.WAITING, active_at=NOW - timedelta(seconds=index),
            model="stale-db-model", current_task=f"Task {index}",
        )

    summary = await _summary(client, project_id)
    assert len(summary["leaders"]) == 2
    for index, session_id in enumerate((CC_SESSION, second)):
        actual = next(row for row in summary["leaders"] if row["session_id"] == session_id)
        # The DB observation is newer than the transcript mtime; time never moves backwards.
        assert actual == before[session_id] | {
            "current_task": f"Task {index}", "started_at": (NOW - timedelta(hours=1)).isoformat(),
            "last_active_at": (NOW - timedelta(seconds=index)).isoformat(),
        }
    assert await _summary(client, project_id) == summary


@pytest.mark.parametrize(("status", "active_at"), [
    (AgentStatus.OFFLINE, NOW),
    (AgentStatus.BUSY, NOW - timedelta(minutes=16)),
    (AgentStatus.WAITING, None),
    (AgentStatus.BUSY, NOW + timedelta(minutes=1)),
])
async def test_db_status_is_preserved_without_inventing_liveness(summary_client, status, active_at) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    leader = await _leader(repo, project_id, CODEX_SESSION, status=status, active_at=active_at)
    summary = await _summary(client, project_id)
    assert summary["leaders"] == []
    assert summary["leader"] is None
    persisted = await repo.get_agent(leader.id)
    assert persisted is not None
    assert persisted.model_dump(mode="json") == leader.model_dump(mode="json")


async def test_historical_offline_sessions_do_not_fill_current_card(summary_client, monkeypatch) -> None:
    client, repo, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    _transcript(root, CC_SESSION, active_at=NOW - timedelta(days=1), model="observed-cc-model")
    current = await _leader(repo, project_id, CODEX_SESSION)
    history = [
        await _leader(
            repo, project_id, f"old-session-{index}", status=AgentStatus.OFFLINE,
            active_at=NOW - timedelta(days=30 + index),
        )
        for index in range(4)
    ]

    rows = (await _summary(client, project_id))["leaders"]
    assert {row["session_id"] for row in rows} == {CODEX_SESSION}
    await repo.update_agent(current.id, status=AgentStatus.OFFLINE)
    rows = (await _summary(client, project_id))["leaders"]
    assert rows == []

    monkeypatch.setattr(session_probe, "_claude_projects_dir", lambda: tmp_path / "no-session-files")
    summary = await _summary(client, project_id)
    assert summary["leaders"] == []
    assert summary["leader"] is None
    for leader in [current, *history]:
        persisted = await repo.get_agent(leader.id)
        assert persisted is not None
        assert persisted.status == AgentStatus.OFFLINE


async def test_history_unites_files_leaders_and_orphaned_containers_without_mutation(summary_client) -> None:
    client, repo, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    _transcript(root, CC_SESSION, active_at=NOW - timedelta(days=1), model="cc")
    leader = await _leader(repo, project_id, CODEX_SESSION, status=AgentStatus.OFFLINE)
    await _leader(repo, project_id, CODEX_SESSION, status=AgentStatus.OFFLINE,
                  active_at=NOW - timedelta(days=1), created_at=NOW - timedelta(days=2))
    container = await repo.create_team(name="retained-container", project_id=project_id, mode="coordinate",
                                      config={"kind": "session", "owner_session_id": CODEX_SESSION})
    orphan = await repo.create_team(name="orphan-container", project_id=project_id, mode="coordinate",
                                   config={"kind": "session", "owner_session_id": "orphan-session"})
    await repo.update_team(orphan.id, status="completed")
    summary = await _summary(client, project_id)
    by_id = {row["session_id"]: row for row in summary["sessions"]}
    assert set(by_id) == {CC_SESSION, CODEX_SESSION, "orphan-session"}
    assert summary["session_count"] == 3
    assert summary["sessions_truncated"] is False
    assert summary["sessions_complete"] is True
    assert by_id[CODEX_SESSION]["activity_state"] == "offline"
    assert by_id[CODEX_SESSION]["started_at"] == (NOW - timedelta(days=2)).isoformat()
    assert by_id["orphan-session"]["activity_state"] == "unknown"
    assert by_id[CC_SESSION]["status_source"] == "activity_only"
    assert summary["leaders"] == []
    assert await _summary(client, project_id) == summary
    assert (await repo.get_agent(leader.id)).status == AgentStatus.OFFLINE
    assert (await repo.get_team(container.id)).status == "active"
    assert (await repo.get_team(orphan.id)).status == "completed"


async def test_cc_idle_and_recent_activity_only_do_not_become_busy(summary_client) -> None:
    client, repo, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    _transcript(root, CC_SESSION, active_at=NOW, model="cc", registry_status="idle")
    await _leader(repo, project_id, CC_SESSION, harness=HarnessId.CLAUDE_CODE)
    _transcript(root, "file-only", active_at=NOW, model="cc")
    summary = await _summary(client, project_id)
    by_id = {row["session_id"]: row for row in summary["sessions"]}
    assert by_id[CC_SESSION]["activity_state"] == "idle"
    assert by_id[CC_SESSION]["status_source"] == "cc_registry"
    assert by_id["file-only"]["activity_state"] == "unknown"
    assert summary["leaders"] == []
    assert summary["status"] == "active"  # recent activity remains distinct from currently busy


async def test_old_registry_busy_is_unknown_and_not_current(summary_client) -> None:
    client, repo, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    _transcript(root, CC_SESSION, active_at=NOW - timedelta(days=1), model="cc", registry_status="busy")
    summary = await _summary(client, project_id)
    row = summary["sessions"][0]
    assert row["status"] == "busy"
    assert row["activity_state"] == "unknown"
    assert row["status_source"] == "cc_registry"
    assert row["live"] is False
    assert summary["leaders"] == []


async def test_running_tasks_use_exact_assignment_then_owner_and_never_shared_team_members(summary_client) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    other_project = await _project(client, tmp_path / "other")
    first = await _leader(repo, project_id, CODEX_SESSION)
    second = await _leader(repo, project_id, CC_SESSION)
    shared = await repo.create_team(name="shared", project_id=project_id, mode="coordinate",
                                   config={"owner_session_id": CODEX_SESSION})
    await repo.update_agent(first.id, team_id=shared.id)
    await repo.update_agent(second.id, team_id=shared.id)
    foreign = await _leader(repo, other_project, "foreign")

    async def running(title, *, team_id=None, assigned_to=None, scope=project_id, parent_id=None):
        task = await repo.create_task(title=title, team_id=team_id, project_id=scope, parent_id=parent_id)
        return await repo.update_task(task.id, status=TaskStatus.RUNNING, assigned_to=assigned_to)

    exact_first = await running("first", assigned_to=first.id)
    exact_second = await running("second", team_id=shared.id, assigned_to=second.id)
    owned = await running("owned", team_id=shared.id)
    child = await running("child", assigned_to=first.id, parent_id=exact_first.id)
    await running("display label", team_id=shared.id, assigned_to="leader-codex")
    await running("foreign assignment", team_id=shared.id, assigned_to=foreign.id)
    await running("unowned")
    await running("other scope", assigned_to=first.id, scope=other_project)
    summary = await _summary(client, project_id)
    by_id = {row["session_id"]: row for row in summary["sessions"]}
    assert by_id[CODEX_SESSION]["in_flight_task_ids"] == sorted([exact_first.id, owned.id, child.id])
    assert by_id[CC_SESSION]["in_flight_task_ids"] == [exact_second.id]
    assert by_id[CODEX_SESSION]["in_flight_tasks"] == 3
    assert summary["unattributed_running_tasks"] == 3
    assert await _summary(client, project_id) == summary


async def test_history_and_task_read_failures_are_distinguished_from_zero(summary_client, monkeypatch) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    await _leader(repo, project_id, CODEX_SESSION)

    async def unavailable(*args, **kwargs):
        raise OSError("isolated failure")

    monkeypatch.setattr(repo, "list_tasks_by_project", unavailable)
    summary = await _summary(client, project_id)
    assert summary["sessions"][0]["in_flight_tasks"] is None
    assert summary["unattributed_running_tasks"] is None
    monkeypatch.setattr(repo, "find_agents_by_role", unavailable)
    summary = await _summary(client, project_id)
    assert summary["sessions"] == []
    assert summary["sessions_complete"] is False


def _native_rollout(path: Path, session_id: str, events: list[tuple[str, datetime, str]]) -> None:
    lines = [{"type": "session_meta", "payload": {"id": session_id}}]
    lines.extend({"type": "event_msg", "timestamp": at.isoformat(),
                  "payload": {"type": event, "turn_id": turn_id}}
                 for event, at, turn_id in events)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


@pytest.mark.parametrize("termination", ["task_complete", "turn_aborted"])
async def test_native_completion_corrects_db_busy_and_new_turn_resumes_without_writes(
    summary_client, termination,
) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    leader = await _leader(repo, project_id, CODEX_SESSION, active_at=NOW - timedelta(minutes=1))
    path = tmp_path / "native.jsonl"
    await repo.update_agent(leader.id, transcript_path=str(path))
    _native_rollout(path, CODEX_SESSION, [
        ("task_started", NOW - timedelta(minutes=2), "first"),
        (termination, NOW - timedelta(seconds=50), "first"),
    ])
    summary = await _summary(client, project_id)
    row = summary["sessions"][0]
    assert row["status"] == "waiting"
    assert row["activity_state"] == "idle"
    assert row["status_source"] == "codex_rollout"
    assert row["observation_at"] == (NOW - timedelta(seconds=50)).isoformat()
    assert summary["leaders"] == []
    assert (await repo.get_agent(leader.id)).status == AgentStatus.BUSY
    assert await _summary(client, project_id) == summary

    _native_rollout(path, CODEX_SESSION, [
        ("task_complete", NOW - timedelta(seconds=50), "first"),
        ("task_started", NOW - timedelta(seconds=20), "second"),
        ("task_complete", NOW - timedelta(seconds=10), "first"),
    ])
    summary = await _summary(client, project_id)
    assert summary["sessions"][0]["activity_state"] == "busy"
    assert summary["leaders"][0]["session_id"] == CODEX_SESSION
    assert (await repo.get_agent(leader.id)).status == AgentStatus.BUSY


@pytest.mark.parametrize("invalid", ["foreign-id", "old-terminal", "partial-tail", "corrupt", "large-tail"],
                         ids=["foreign-id", "old-terminal", "partial-tail", "corrupt", "large-tail"])
async def test_native_unknown_or_old_completion_preserves_later_db_activity(summary_client, invalid) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    leader = await _leader(repo, project_id, CODEX_SESSION)
    path = tmp_path / "native.jsonl"
    await repo.update_agent(leader.id, transcript_path=str(path))
    _native_rollout(path, "foreign" if invalid == "foreign-id" else CODEX_SESSION, [
        ("task_complete", NOW - timedelta(minutes=1) if invalid == "old-terminal" else NOW, "first"),
    ])
    if invalid == "partial-tail":
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    if invalid == "corrupt":
        with path.open("a") as stream:
            stream.write("invalid json\n")
    if invalid == "large-tail":
        with path.open("a") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": "x" * 210_000}))
    summary = await _summary(client, project_id)
    assert summary["sessions"][0]["activity_state"] == "busy"
    assert summary["sessions"][0]["status_source"] == "db_observation"
    assert (await repo.get_agent(leader.id)).status == AgentStatus.BUSY


async def test_summary_caps_history_but_keeps_every_live_session(summary_client) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    live = {f"live-{index}" for index in range(3)}
    for session_id in live:
        await _leader(repo, project_id, session_id)
    history = [f"old-{index:02d}" for index in range(30)]
    for index, session_id in enumerate(history):
        await _leader(repo, project_id, session_id, status=AgentStatus.OFFLINE,
                      active_at=NOW - timedelta(days=1, minutes=index))

    summary = await _summary(client, project_id)
    limit = project_sessions_module.MAX_SUMMARY_SESSIONS
    assert limit == 20
    assert summary["session_count"] == 33
    assert summary["sessions_truncated"] is True
    returned = [row["session_id"] for row in summary["sessions"]]
    assert len(returned) == limit
    assert live <= set(returned)
    assert returned[len(live):] == history[:limit - len(live)]  # newest history fills the rest
    assert {row["session_id"] for row in summary["leaders"]} == live
    assert summary["last_activity_at"] == NOW.isoformat()
    assert await _summary(client, project_id) == summary


async def test_live_sessions_beyond_the_cap_are_never_dropped(summary_client) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    limit = project_sessions_module.MAX_SUMMARY_SESSIONS
    live = {f"live-{index:02d}" for index in range(limit + 2)}
    for session_id in live:
        await _leader(repo, project_id, session_id)
    for index in range(3):
        await _leader(repo, project_id, f"old-{index}", status=AgentStatus.OFFLINE,
                      active_at=NOW - timedelta(days=2))

    summary = await _summary(client, project_id)
    assert {row["session_id"] for row in summary["sessions"]} == live
    assert summary["session_count"] == limit + 5
    assert summary["sessions_truncated"] is True
    assert {row["session_id"] for row in summary["leaders"]} == live


async def test_mcp_project_summary_omits_session_history_but_keeps_counts_and_live_cards(
    summary_client, monkeypatch,
) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    await _leader(repo, project_id, CODEX_SESSION, current_task="Current task")
    for index in range(25):
        await _leader(repo, project_id, f"old-{index}", status=AgentStatus.OFFLINE,
                      active_at=NOW - timedelta(days=3, minutes=index))
    rest = await _summary(client, project_id)
    assert len(rest["sessions"]) == project_sessions_module.MAX_SUMMARY_SESSIONS
    calls = []

    def api_call(method, path, *args, **kwargs):
        calls.append((method, path))
        return copy.deepcopy(rest)

    monkeypatch.setattr(project_tools, "_api_call", api_call)
    tools = {}

    class Capture:
        def tool(self, *args, **kwargs):
            def decorator(fn):
                tools[fn.__name__] = fn
                return fn
            return decorator

    project_tools.register(Capture())
    result = tools["project_summary"](project_id)
    assert calls == [("GET", f"/api/projects/{project_id}/summary")]
    assert "sessions" not in result
    assert result["session_count"] == 26
    assert result["sessions_truncated"] is True
    assert result["sessions_complete"] is True
    assert [row["session_id"] for row in result["leaders"]] == [CODEX_SESSION]
    assert result["leader"] == rest["leader"]
    assert {key: value for key, value in rest.items() if key != "sessions"} == result


async def test_live_pid_without_registry_activity_is_not_inferred_busy(summary_client) -> None:
    client, _, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    _transcript(root, CC_SESSION, active_at=NOW, model="cc")
    registry = Path.home() / ".claude" / "sessions"
    registry.mkdir(parents=True, exist_ok=True)
    # A living pid whose registry row reports neither busy nor idle.
    (registry / f"{CC_SESSION}.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": CC_SESSION, "cwd": str(root),
    }), encoding="utf-8")

    summary = await _summary(client, project_id)
    row = summary["sessions"][0]
    assert row["cc_process_alive"] is True
    assert row["activity_state"] == "unknown"
    assert row["status_source"] == "activity_only"
    assert row["live"] is False
    assert summary["leaders"] == []


async def test_native_tail_is_read_only_for_recently_active_db_sessions(summary_client, monkeypatch) -> None:
    client, repo, tmp_path = summary_client
    project_id = await _project(client, tmp_path / "project")
    sessions = {"recent": NOW - timedelta(minutes=1)}
    sessions.update({f"stale-{index}": NOW - timedelta(days=2, minutes=index) for index in range(5)})
    for session_id, active_at in sessions.items():
        leader = await _leader(repo, project_id, session_id, status=AgentStatus.BUSY, active_at=active_at)
        path = tmp_path / f"{session_id}.jsonl"
        _native_rollout(path, session_id, [
            ("task_started", active_at + timedelta(seconds=1), "turn"),
            ("task_complete", active_at + timedelta(seconds=30), "turn"),
        ])
        await repo.update_agent(leader.id, transcript_path=str(path))
    real = project_sessions_module._codex_activity
    read = []

    def counted(leader, now):
        read.append(leader.session_id)
        return real(leader, now)

    monkeypatch.setattr(project_sessions_module, "_codex_activity", counted)
    summary = await _summary(client, project_id)
    assert read == ["recent"]
    by_id = {row["session_id"]: row for row in summary["sessions"]}
    assert by_id["recent"]["status_source"] == "codex_rollout"
    assert by_id["recent"]["activity_state"] == "idle"
    assert {by_id[f"stale-{index}"]["status_source"] for index in range(5)} == {"db_observation"}


async def test_session_rows_share_one_shape_and_never_move_activity_backwards(summary_client) -> None:
    client, repo, tmp_path = summary_client
    root = tmp_path / "project"
    project_id = await _project(client, root)
    _transcript(root, CC_SESSION, active_at=NOW - timedelta(minutes=5), model="cc", registry_status="idle")
    await _leader(repo, project_id, CC_SESSION, harness=HarnessId.CLAUDE_CODE,
                  status=AgentStatus.WAITING, active_at=NOW - timedelta(minutes=1))
    leader = await _leader(repo, project_id, CODEX_SESSION, active_at=NOW - timedelta(minutes=1))
    path = tmp_path / "native.jsonl"
    _native_rollout(path, CODEX_SESSION, [("task_started", NOW - timedelta(seconds=30), "turn")])
    await repo.update_agent(leader.id, transcript_path=str(path))
    await _leader(repo, project_id, "history-only", status=AgentStatus.OFFLINE, active_at=NOW - timedelta(days=1))
    _transcript(root, "file-only", active_at=NOW - timedelta(days=2), model="cc")

    summary = await _summary(client, project_id)
    rows = {row["session_id"]: row for row in summary["sessions"]}
    assert set(rows) == {CC_SESSION, CODEX_SESSION, "history-only", "file-only"}
    shapes = {frozenset(row) for row in rows.values()}
    assert len(shapes) == 1
    assert {"cc_pid", "cc_status", "cc_process_alive", "observation_at"} <= next(iter(shapes))
    assert rows[CC_SESSION]["last_active_at"] == (NOW - timedelta(minutes=1)).isoformat()
    assert rows[CODEX_SESSION]["observation_at"] == (NOW - timedelta(seconds=30)).isoformat()
    assert rows["history-only"]["observation_at"] is None
    assert rows["file-only"]["cc_pid"] is None
