"""Minimal producer evidence across persistence and native identity boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from aiteam.api.deps import get_repository
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes.external_exec import router
from aiteam.services import codex_exec_launch as service
from aiteam.services.codex_exec_launch_parser import classify_bash_launches, wrapper_launch_metadata
from aiteam.services.codex_local_usage import read_local_usage_and_pricing
from aiteam.services.codex_usage_journal import source_namespace
from aiteam.storage.codex_exec_launch import CodexExecLaunchRepository
from aiteam.storage.connection import get_session
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.external_exec import ExternalExecRepository
from aiteam.storage.models import CodexExecLaunchModel, ExternalExecInvocationModel
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    CodexExecLaunchObservation,
    ExternalExecFinish,
    ExternalExecInvocation,
    ExternalExecRegistration,
)

START = datetime(2026, 10, 13, tzinfo=UTC)


@pytest.fixture
async def context(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'os.sqlite'}"
    repo = StorageRepository(db_url=db_url)
    await repo.init_db()
    yield db_url, home, repo
    await engine_pool.get_engine(db_url).dispose()


def native(home: Path, thread: str, at=START + timedelta(seconds=1), *, parent=None):
    (home / "sessions").mkdir(exist_ok=True)
    rollout = home / "sessions" / f"{thread}.jsonl"
    payload = {"id": thread, "source": "exec", "model_provider": "openai"}
    if parent:
        payload["forked_from_id"] = parent
    rollout.write_text(json.dumps({"type": "session_meta", "timestamp": at.isoformat(), "payload": payload}) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT)")
        db.execute("INSERT INTO threads(id,rollout_path) VALUES (?,?)", (thread, str(rollout)))


def event(command, phase="PreToolUse", *, tool="call-one", stdout="", background=False):
    return {
        "hook_event_name": phase,
        "tool_name": "Bash",
        "session_id": "cc-session",
        "tool_use_id": tool,
        "tool_input": {"command": command, "run_in_background": background},
        "tool_response": {"stdout": stdout},
    }


def header(thread):
    return (
        "OpenAI Codex v0.162.1\n--------\nmodel: gpt-6-astra\nprovider: openai\n"
        f"session id: {thread}\n--------\nPRIVATE PROMPT CONTENT\n"
    )


def test_argv_delimiter_repeat_overrides_and_old_payload_defaults():
    result = wrapper_launch_metadata(
        [
            "-c",
            'service_tier="fast"',
            "--config=service_tier=standard",
            "-m",
            "gpt-6-astra",
            "-cmodel_reasoning_effort=high",
            "--",
            "resume",
        ]
    )
    assert result == {
        "requested_service_tier": "standard",
        "requested_model": "gpt-6-astra",
        "requested_reasoning_effort": "high",
        "exec_mode": "new",
        "requested_metadata_source": "wrapper_argv",
    }
    for mode in ("resume", "fork", "review"):
        assert wrapper_launch_metadata(["-c", "service_tier=fast", mode])["exec_mode"] == mode
    value = ExternalExecRegistration(invocation_id=str(uuid4()), source_namespace="a" * 64)
    assert value.requested_service_tier is None and value.exec_mode == "unknown"


@pytest.mark.parametrize(
    "command",
    [
        "# codex exec -c service_tier=fast",
        'echo "codex exec -c service_tier=fast"',
        'bash -c "codex exec -c service_tier=fast"',
        "cat <<EOF\ncodex exec -c service_tier=fast\nEOF",
        "$PROGRAM exec -c service_tier=fast",
        "python /tmp/codex_script.py",
        "echo codex && echo exec",
        "codex exec " + "x" * 131072,
        "codex exec -c service_tier=fast ...(truncated)",
    ],
    ids=[
        "comment",
        "echo",
        "nested-script",
        "heredoc",
        "variable-program",
        "script",
        "echo-chain",
        "over-budget",
        "truncated",
    ],
)
def test_opaque_or_nonlaunch_shell_is_not_detected(command):
    assert classify_bash_launches(command) == []


@pytest.mark.parametrize(
    "command,reason",
    [
        ('codex exec --json -c "service_tier=$TIER" p', "dynamic_shell"),
        ("codex exec --json -c service_tier=fast p && echo codex", "complex_shell"),
        ("codex exec --json -c service_tier=fast p | cat", "complex_shell"),
        ("codex exec --json -c service_tier=fast p >> /tmp/run.log", "background_or_redirect"),
        ("codex exec --json -c service_tier=fast p; codex exec p", "multiple_launches"),
    ],
    ids=["dynamic-option", "chain", "pipe", "append", "multiple"],
)
def test_unsupported_shell_does_not_claim_binding(command, reason):
    assert classify_bash_launches(command)[0].binding_reason == reason


async def test_pre_is_only_intent_post_native_identity_and_reopen(context):
    db_url, home, _ = context
    thread = str(uuid4())
    command = "codex exec --json -c service_tier=fast PRIVATE-PROMPT"
    await service.observe_cc_bash_launch(db_url, event(command), at=START, project_id="project-one")
    store = CodexExecLaunchRepository(db_url)
    items, total = await store.list(project_id="project-one")
    assert total == 1 and items[0].state == "planned" and items[0].thread_id is None
    native(home, thread)
    stdout = json.dumps({"type": "thread.started", "thread_id": thread}) + "\n"
    await service.observe_cc_bash_launch(
        db_url, event(command, "PostToolUse", stdout=stdout), at=START + timedelta(seconds=2), project_id="project-one"
    )
    reopened = CodexExecLaunchRepository(db_url)
    assert (await reopened.get(items[0].launch_id)).state == "bound"
    evidence = await service.load_launch_tier_evidence(
        db_url, codex_home=home, since=START, until=START + timedelta(seconds=3)
    )
    assert len(evidence) == 1 and evidence[0].thread_id == thread
    assert evidence[0].binding_source == "cc_stdout_thread_started"
    with sqlite3.connect(Path(db_url.split("///", 1)[1])) as db:
        assert "PRIVATE" not in db.execute("SELECT payload FROM codex_exec_launches").fetchone()[0]
    assert not (home / "data/ai-team-os/external-exec.db").exists()


async def test_real_background_shape_reads_only_fresh_closed_header(context, tmp_path):
    db_url, home, _ = context
    thread = str(uuid4())
    output = tmp_path / "run.log"
    command = (
        f"codex exec -s read-only -c service_tier=fast -C /repo -o /tmp/report.md "
        f'"$(cat /tmp/prompt.md)" < /dev/null > {output} 2>&1; echo "exit $?"'
    )
    assert classify_bash_launches(command)[0].binding_reason == "output_pending"
    await service.observe_cc_bash_launch(db_url, event(command, background=True), at=START)
    await service.observe_cc_bash_launch(
        db_url, event(command, "PostToolUse", background=True), at=START + timedelta(milliseconds=100)
    )
    assert (await CodexExecLaunchRepository(db_url).list())[0][0].state == "unbound"
    native(home, thread)
    output.write_text(header(thread))
    evidence = await service.load_launch_tier_evidence(
        db_url, codex_home=home, since=START, until=START + timedelta(seconds=10)
    )
    assert len(evidence) == 1 and evidence[0].binding_source == "cc_output_header"
    assert evidence[0].finished_at is None  # Background Bash return is not native completion.
    stored = (await CodexExecLaunchRepository(db_url).list())[0][0]
    assert stored.background and stored.output_file_key


async def test_background_header_producer_to_native_request_pricing(context, tmp_path):
    db_url, home, _ = context
    thread = str(uuid4())
    output = tmp_path / "native.log"
    command = f'codex exec -c service_tier=fast "$(cat /tmp/prompt.md)" < /dev/null > {output} 2>&1; echo "exit $?"'
    await service.observe_cc_bash_launch(db_url, event(command, background=True), at=START)
    await service.observe_cc_bash_launch(
        db_url, event(command, "PostToolUse", background=True), at=START + timedelta(milliseconds=100)
    )
    native(home, thread)
    path = home / "sessions" / f"{thread}.jsonl"
    rows = [
        {
            "type": "turn_context",
            "timestamp": (START + timedelta(seconds=2)).isoformat(),
            "payload": {"turn_id": "turn-one", "model": "gpt-6-astra"},
        },
        {
            "type": "token_usage_record",
            "timestamp": (START + timedelta(seconds=5)).isoformat(),
            "payload": {
                "response_id": "response-one",
                "thread_id": thread,
                "turn_id": "turn-one",
                "model": "gpt-6-astra",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 20,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 0,
                    "total_tokens": 110,
                },
            },
        },
    ]
    with path.open("a") as stream:
        stream.write("".join(json.dumps(row) + "\n" for row in rows))
    output.write_text(header(thread))
    end = START + timedelta(seconds=10)
    evidence = await service.load_launch_tier_evidence(db_url, codex_home=home, since=START, until=end)
    total, entries, counts = await read_local_usage_and_pricing(home, START, end, launch_tier_evidence=evidence)
    assert total == 110 and len(entries) == 1 and entries[0].request.service_tier == "fast"
    assert entries[0].service_tier_source == "launch_requested" and entries[0].launch_id == evidence[0].launch_id
    assert counts["pricing_service_tier_requested"] == counts["pricing_incomplete"] == 1
    assert counts.get("pricing_service_tier_observed", 0) == 0


async def test_offline_spool_is_read_without_creating_or_losing_requested_evidence(context):
    db_url, home, _ = context
    path = home / "data/ai-team-os/external-exec.db"
    path.parent.mkdir(parents=True)
    spool_url = f"sqlite+aiosqlite:///{path}"
    spool = ExternalExecRepository(spool_url)
    await spool.init_db()
    value = ExternalExecRegistration(
        invocation_id=str(uuid4()),
        source_namespace=source_namespace(home),
        started_at=START,
        **wrapper_launch_metadata(["-c", "service_tier=fast", "p"]),
    )
    await spool.register(value)
    await spool.bind_thread(value.invocation_id, str(uuid4()))
    await spool.finish(
        value.invocation_id,
        ExternalExecFinish(status="completed", exit_code=0, finished_at=START + timedelta(seconds=5)),
    )
    await engine_pool.get_engine(spool_url).dispose()
    evidence = await service.load_launch_tier_evidence(
        db_url, codex_home=home, since=START, until=START + timedelta(seconds=4)
    )
    assert len(evidence) == 1 and evidence[0].launch_id == value.invocation_id
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM external_exec_invocations").fetchone()[0] == 1


async def test_many_old_background_bindings_do_not_hide_current_native_thread(context):
    db_url, home, _ = context
    old_start = START - timedelta(hours=1)
    values = []
    for index in range(1001):
        values.append(
            CodexExecLaunchObservation(
                launch_id=hashlib.sha256(f"old-{index}".encode()).hexdigest(),
                caller_session_id="cc-session",
                tool_use_id=f"old-{index}",
                exec_mode="new",
                requested_service_tier="fast",
                state="bound",
                binding_reason="bound",
                pre_seen=True,
                background=True,
                source_namespace=source_namespace(home),
                thread_id=str(uuid4()),
                started_at=old_start,
            )
        )
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,updated_at INTEGER)")
        db.execute("CREATE INDEX idx_threads_updated_at ON threads(updated_at,id)")
        db.executemany(
            "INSERT INTO threads VALUES (?,NULL,?)", [(value.thread_id, int(old_start.timestamp())) for value in values]
        )
    current_thread = str(uuid4())
    native(home, current_thread)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET updated_at=? WHERE id=?", (int(START.timestamp()), current_thread))
    current = CodexExecLaunchObservation(
        launch_id=hashlib.sha256(b"current").hexdigest(),
        caller_session_id="cc-session",
        tool_use_id="current",
        exec_mode="new",
        requested_service_tier="fast",
        state="bound",
        binding_reason="bound",
        pre_seen=True,
        background=True,
        source_namespace=source_namespace(home),
        thread_id=current_thread,
        started_at=START,
    )
    values.append(current)
    async with get_session(db_url) as session:
        session.add_all(
            [
                CodexExecLaunchModel(
                    launch_id=value.launch_id,
                    started_at=value.started_at,
                    thread_id=value.thread_id,
                    state=value.state,
                    payload=value.model_dump(mode="json"),
                )
                for value in values
            ]
        )
    evidence = await service.load_launch_tier_evidence(
        db_url, codex_home=home, since=START, until=START + timedelta(seconds=5)
    )
    assert len(evidence) == 1 and evidence[0].thread_id == current_thread
    # Native active filtering must retain all competing modes/tier-unknown records.
    competitor = current.model_copy(
        update={
            "launch_id": hashlib.sha256(b"unknown").hexdigest(),
            "tool_use_id": "unknown",
            "exec_mode": "unknown",
            "requested_service_tier": None,
        }
    )
    await CodexExecLaunchRepository(db_url).record(competitor, "reconcile")
    assert (
        await service.load_launch_tier_evidence(
            db_url, codex_home=home, since=START, until=START + timedelta(seconds=5)
        )
        == []
    )


@pytest.mark.parametrize("kind", ["stale", "symlink", "fifo", "fake-header", "fork"])
async def test_unsafe_or_stale_output_never_binds(context, tmp_path, kind):
    db_url, home, _ = context
    thread = str(uuid4())
    output = tmp_path / "run.log"
    command = f"codex exec -c service_tier=fast p > {output} 2>&1"
    await service.observe_cc_bash_launch(db_url, event(command, background=True), at=START)
    native(
        home,
        thread,
        START - timedelta(seconds=1) if kind == "stale" else START + timedelta(seconds=1),
        parent="original" if kind == "fork" else None,
    )
    if kind == "symlink":
        target = tmp_path / "target.log"
        target.write_text(header(thread))
        output.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(output)
    elif kind == "fake-header":
        output.write_text("user content\n" + header(thread))
    else:
        output.write_text(header(thread))
    assert (
        await service.load_launch_tier_evidence(
            db_url, codex_home=home, since=START, until=START + timedelta(seconds=5)
        )
        == []
    )


async def test_output_alias_and_unknown_tier_participate_in_conflicts(context, tmp_path):
    db_url, home, _ = context
    output = tmp_path / "run.log"
    child = tmp_path / "sub"
    child.mkdir()
    first = f"codex exec -c service_tier=fast p > {output}"
    second = f"codex exec p > {child / '../run.log'}"
    await service.observe_cc_bash_launch(db_url, event(first, background=True), at=START)
    await service.observe_cc_bash_launch(
        db_url, event(second, tool="call-two", background=True), at=START + timedelta(seconds=1)
    )
    thread = str(uuid4())
    native(home, thread, START + timedelta(seconds=2))
    output.write_text(header(thread))
    assert (
        await service.load_launch_tier_evidence(
            db_url, codex_home=home, since=START, until=START + timedelta(seconds=5)
        )
        == []
    )
    items, _ = await CodexExecLaunchRepository(db_url).list()
    assert all(item.binding_reason == "output_path_conflict" for item in items)


async def test_unknown_mode_hardlink_conflict_cannot_reprice_other_launch(context, tmp_path):
    db_url, home, _ = context
    first, second = tmp_path / "a.log", tmp_path / "b.log"
    first.touch()
    second.hardlink_to(first)
    for number, command in enumerate(
        (f"codex exec -c service_tier=fast p > {first}", f"codex exec --image /tmp/img.png p > {second}")
    ):
        await service.observe_cc_bash_launch(db_url, event(command, tool=f"call-{number}", background=True), at=START)
    thread = str(uuid4())
    native(home, thread)
    first.write_text(header(thread))
    assert (
        await service.load_launch_tier_evidence(
            db_url, codex_home=home, since=START, until=START + timedelta(seconds=5)
        )
        == []
    )


async def test_failure_with_native_thread_is_evidence_but_no_thread_is_failed(context):
    db_url, home, _ = context
    command = "codex exec --json -c service_tier=fast p"
    await service.observe_cc_bash_launch(db_url, event(command), at=START)
    thread = str(uuid4())
    native(home, thread)
    stdout = json.dumps({"type": "thread.started", "thread_id": thread}) + "\n"
    await service.observe_cc_bash_launch(
        db_url, event(command, "PostToolUseFailure", stdout=stdout), at=START + timedelta(seconds=2)
    )
    await service.observe_cc_bash_launch(db_url, event(command, tool="failed"), at=START)
    await service.observe_cc_bash_launch(
        db_url, event(command, "PostToolUseFailure", tool="failed"), at=START + timedelta(seconds=2)
    )
    items, _ = await CodexExecLaunchRepository(db_url).list()
    assert {item.state for item in items} == {"bound", "failed"}
    assert all(item.execution_failed for item in items)


@pytest.mark.parametrize("pre_seen", [False, True], ids=["missing-pre", "valid-pre"])
async def test_failure_binding_requires_pre_across_database_reopen_and_replay(context, pre_seen):
    db_url, home, repo = context
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_repository] = lambda: repo
    command = "codex exec --json -c service_tier=fast p"
    if pre_seen:
        await service.observe_cc_bash_launch(db_url, event(command), at=START)
    thread = str(uuid4())
    completed_at = START + timedelta(seconds=2)
    # Equality is accepted by the native-start fence, including a missing-Pre completion.
    native(home, thread, completed_at)
    stdout = json.dumps({"type": "thread.started", "thread_id": thread}) + "\n"
    failure = event(command, "PostToolUseFailure", stdout=stdout)

    for _ in range(2):  # Independent completion/replay transactions, each read after reopening.
        await service.observe_cc_bash_launch(db_url, failure, at=completed_at)
        await engine_pool.get_engine(db_url).dispose()
        reopened = CodexExecLaunchRepository(db_url)
        items, total = await reopened.list()
        assert total == 1
        stored = await reopened.get(items[0].launch_id)
        assert stored.pre_seen is pre_seen
        assert stored.execution_failed is True
        assert stored.state == ("bound" if pre_seen else "unbound")
        assert stored.binding_reason == ("bound" if pre_seen else "missing_pre_event")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/external-execs/launch-observations")
        assert response.status_code == 200
        assert response.json()["data"]["items"] == [stored.model_dump(mode="json")]
        evidence = await service.load_launch_tier_evidence(
            db_url, codex_home=home, since=START, until=START + timedelta(seconds=3)
        )
        assert [item.thread_id for item in evidence] == ([thread] if pre_seen else [])


async def test_replay_post_before_pre_and_conflicting_channel(context):
    db_url, home, _ = context
    command = "codex exec --json -c service_tier=fast p"
    thread = str(uuid4())
    native(home, thread, START + timedelta(seconds=3))
    stdout = json.dumps({"type": "thread.started", "thread_id": thread}) + "\n"
    await service.observe_cc_bash_launch(
        db_url, event(command, "PostToolUse", stdout=stdout), at=START + timedelta(seconds=2)
    )
    assert (await CodexExecLaunchRepository(db_url).list())[0][0].binding_reason == "missing_pre_event"
    for _ in range(2):
        await service.observe_cc_bash_launch(db_url, event(command), at=START)
    items, total = await CodexExecLaunchRepository(db_url).list()
    assert total == 1 and items[0].state == "bound"
    await service.observe_cc_bash_launch(db_url, event(command.replace(" p", " p > /tmp/other.log")), at=START)
    assert (await CodexExecLaunchRepository(db_url).get(items[0].launch_id)).binding_reason == "conflicting_thread"


async def test_wrapper_namespace_unknown_competitor_and_spool_missing(context):
    db_url, home, _ = context
    store = ExternalExecRepository(db_url)
    thread = str(uuid4())
    value = ExternalExecRegistration(
        invocation_id=str(uuid4()),
        source_namespace=source_namespace(home),
        started_at=START,
        **wrapper_launch_metadata(["-c", "service_tier=fast", "p"]),
    )
    await store.register(value)
    await store.bind_thread(value.invocation_id, thread)
    await store.finish(
        value.invocation_id, ExternalExecFinish(status="failed", exit_code=1, finished_at=START + timedelta(seconds=5))
    )
    assert (
        len(
            await service.load_launch_tier_evidence(
                db_url, codex_home=home, since=START + timedelta(seconds=2), until=START + timedelta(seconds=4)
            )
        )
        == 1
    )
    assert (
        await service.load_launch_tier_evidence(
            db_url, codex_home=home / "other", since=START, until=START + timedelta(seconds=4)
        )
        == []
    )
    competitor = value.model_copy(
        update={
            "invocation_id": str(uuid4()),
            "requested_service_tier": None,
            "requested_metadata_source": None,
            "exec_mode": "resume",
        }
    )
    await store.register(competitor)
    await store.bind_thread(competitor.invocation_id, thread)
    assert (
        await service.load_launch_tier_evidence(
            db_url, codex_home=home, since=START, until=START + timedelta(seconds=4)
        )
        == []
    )


async def test_default_repository_route_scope_and_translator_entry(context, monkeypatch):
    db_url, _, repo = context
    monkeypatch.setattr(service, "default_db_url", lambda: db_url)
    command = "codex exec --json -c service_tier=fast p"
    await service.observe_cc_bash_launch(None, event(command), at=START, project_id="one")
    await service.observe_cc_bash_launch(db_url, event(command, tool="two"), at=START, project_id="two")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_repository] = lambda: repo
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/external-execs/launch-observations", params={"project_id": "one", "limit": 1})
    assert response.status_code == 200 and response.json()["data"]["total"] == 1
    assert response.json()["data"]["items"][0]["project_id"] == "one"
    translator = HookTranslator(repo, EventBus(repo))
    called = []

    async def capture(*args, **kwargs):
        called.append((args, kwargs))

    monkeypatch.setattr(service, "observe_cc_bash_launch", capture)
    await translator._on_pre_tool_use(event(command, tool="entry"))
    assert called and called[0][0][1]["tool_input"]["command"] == command


def test_native_recent_index_missing_or_wrong_does_not_claim_filter(tmp_path):
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,updated_at INTEGER)")
        db.execute("INSERT INTO threads VALUES (?,?)", (str(uuid4()), int(START.timestamp())))
    assert service._recent_thread_ids(tmp_path, START) is None
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("CREATE INDEX idx_threads_updated_at ON threads(id)")
    assert service._recent_thread_ids(tmp_path, START) is None


def test_native_recent_index_keeps_second_boundary_and_over_budget_unknown(tmp_path):
    thread = str(uuid4())
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,updated_at INTEGER)")
        db.execute("CREATE INDEX idx_threads_updated_at ON threads(updated_at,id)")
    assert service._recent_thread_ids(tmp_path, START) == []
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.execute("INSERT INTO threads VALUES (?,?)", (thread, int(START.timestamp())))
    assert service._recent_thread_ids(tmp_path, START + timedelta(milliseconds=999)) == [thread]
    with sqlite3.connect(tmp_path / "state_5.sqlite") as db:
        db.executemany(
            "INSERT INTO threads VALUES (?,?)",
            [(str(uuid4()), int(START.timestamp())) for _ in range(service._MAX_RECORDS)],
        )
    assert service._recent_thread_ids(tmp_path, START) is None


@pytest.mark.parametrize(
    "prefix,allowed",
    [
        ("Reading additional input from stdin...\n", True),
        (
            "Reading additional input from stdin...\n"
            "2026-10-13T09:21:50.713085Z ERROR codex_models_manager::manager: "
            "failed to refresh available models: request timed out\n"
            "2026-10-13T09:21:50.719854Z ERROR codex_models_manager::manager: "
            "failed to refresh available models: request timed out\n",
            True,
        ),
        ("PRIVATE USER TEXT\n", False),
        ("Reading additional input from stdin...\nPRIVATE USER TEXT\n", False),
        ("2026-10-13T09:21:50.713085Z ERROR other::module: request timed out\n", False),
    ],
    ids=["native-stdin", "native-timeouts", "arbitrary-prefix", "mixed-prefix", "unknown-module"],
)
def test_output_header_skips_only_closed_native_preamble(context, tmp_path, prefix, allowed):
    thread = str(uuid4())
    path = tmp_path / "preamble.log"
    path.write_text(prefix + header(thread))
    found, reason, _ = service._output_thread(str(path))
    assert found == (thread if allowed else None)
    assert reason == ("bound" if allowed else "invalid_output_header")


async def test_spool_over_budget_does_not_hide_competitors_behind_main_fast(context):
    db_url, home, _ = context
    main = ExternalExecRepository(db_url)
    thread = str(uuid4())
    value = ExternalExecRegistration(
        invocation_id=str(uuid4()),
        source_namespace=source_namespace(home),
        started_at=START,
        **wrapper_launch_metadata(["-c", "service_tier=fast", "p"]),
    )
    await main.register(value)
    await main.bind_thread(value.invocation_id, thread)
    spool_path = home / "data/ai-team-os/external-exec.db"
    spool_path.parent.mkdir(parents=True)
    spool_url = f"sqlite+aiosqlite:///{spool_path}"
    await ExternalExecRepository(spool_url).init_db()
    async with get_session(spool_url) as session:
        for _ in range(service._MAX_RECORDS + 1):
            competitor = ExternalExecInvocation(
                invocation_id=str(uuid4()),
                source_namespace=source_namespace(home),
                started_at=START,
                thread_id=thread,
                exec_mode="resume",
                requested_metadata_source="wrapper_argv",
            )
            session.add(
                ExternalExecInvocationModel(
                    invocation_id=competitor.invocation_id,
                    thread_id=thread,
                    started_at=START,
                    revision=1,
                    uploaded_revision=0,
                    payload=competitor.model_dump(mode="json"),
                )
            )
    await engine_pool.get_engine(spool_url).dispose()
    assert service._spool_rows(home, START, START + timedelta(seconds=5)) is None
    assert (
        await service.load_launch_tier_evidence(
            db_url, codex_home=home, since=START, until=START + timedelta(seconds=5)
        )
        == []
    )


async def test_replayed_post_cannot_bind_a_row_whose_pre_was_never_seen(context):
    db_url, home, _ = context
    command = "codex exec --json -c service_tier=fast p"
    thread = str(uuid4())
    native(home, thread, START + timedelta(seconds=3))
    stdout = json.dumps({"type": "thread.started", "thread_id": thread}) + "\n"
    for _ in range(2):
        await service.observe_cc_bash_launch(
            db_url, event(command, "PostToolUse", stdout=stdout), at=START + timedelta(seconds=2)
        )
    items, total = await CodexExecLaunchRepository(db_url).list()
    assert total == 1 and not items[0].pre_seen
    assert items[0].state == "unbound" and items[0].binding_reason == "missing_pre_event"
    assert (
        await service.load_launch_tier_evidence(
            db_url, codex_home=home, since=START, until=START + timedelta(seconds=5)
        )
        == []
    )


async def test_hardlinked_output_log_never_binds_without_a_competing_launch(context, tmp_path):
    db_url, home, _ = context
    thread = str(uuid4())
    output = tmp_path / "run.log"
    command = f"codex exec -c service_tier=fast p > {output} 2>&1"
    await service.observe_cc_bash_launch(db_url, event(command, background=True), at=START)
    native(home, thread)
    output.write_text(header(thread))
    (tmp_path / "unrelated.log").hardlink_to(output)
    assert (
        await service.load_launch_tier_evidence(
            db_url, codex_home=home, since=START, until=START + timedelta(seconds=5)
        )
        == []
    )
    stored = (await CodexExecLaunchRepository(db_url).list())[0][0]
    assert stored.binding_reason == "unsafe_output_file" and stored.thread_id is None


async def test_reconcile_does_not_rewrite_rows_whose_binding_is_unchanged(context, tmp_path, monkeypatch):
    db_url, home, _ = context
    output = tmp_path / "pending.log"
    command = f"codex exec -c service_tier=fast p > {output} 2>&1"
    await service.observe_cc_bash_launch(db_url, event(command, background=True), at=START)
    writes = []
    real_record = CodexExecLaunchRepository.record

    async def counted(self, incoming, phase):
        writes.append((incoming.state, incoming.binding_reason, phase))
        return await real_record(self, incoming, phase)

    monkeypatch.setattr(CodexExecLaunchRepository, "record", counted)
    await service.reconcile_output_launches(db_url, codex_home=home)
    assert writes == [("unbound", "output_pending", "reconcile")]
    before = (await CodexExecLaunchRepository(db_url).list())[0][0]
    for _ in range(3):
        await service.reconcile_output_launches(db_url, codex_home=home)
    assert len(writes) == 1
    assert (await CodexExecLaunchRepository(db_url).list())[0][0] == before
    thread = str(uuid4())
    native(home, thread)
    output.write_text(header(thread))
    await service.reconcile_output_launches(db_url, codex_home=home)
    assert writes[1:] == [("bound", "bound", "reconcile")]
    stored = await CodexExecLaunchRepository(db_url).get(before.launch_id)
    assert stored.state == "bound" and stored.thread_id == thread


async def test_symlinked_codex_home_binds_under_the_resolved_namespace(context, tmp_path, monkeypatch):
    db_url, _, _ = context
    real = tmp_path / "real-codex"
    real.mkdir()
    alias = tmp_path / "alias-codex"
    alias.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(alias))
    thread = str(uuid4())
    # Native rows name rollouts under the configured, unresolved root.
    native(alias, thread)
    command = "codex exec --json -c service_tier=fast p"
    await service.observe_cc_bash_launch(db_url, event(command), at=START)
    stdout = json.dumps({"type": "thread.started", "thread_id": thread}) + "\n"
    await service.observe_cc_bash_launch(
        db_url, event(command, "PostToolUse", stdout=stdout), at=START + timedelta(seconds=2)
    )
    stored = (await CodexExecLaunchRepository(db_url).list())[0][0]
    assert stored.state == "bound" and stored.source_namespace == source_namespace(real.resolve())
    # Local plan capture reads with the resolved root.
    evidence = await service.load_launch_tier_evidence(
        db_url, codex_home=real.resolve(), since=START, until=START + timedelta(seconds=3)
    )
    assert [item.thread_id for item in evidence] == [thread]


async def test_post_reconcile_runs_after_the_hook_response_not_inside_it(context, monkeypatch):
    db_url, _, repo = context
    order = []

    async def reconcile(url, **kwargs):
        order.append(("reconcile", url))

    monkeypatch.setattr(service, "reconcile_output_launches", reconcile)
    translator = HookTranslator(repo, EventBus(repo))
    command = "codex exec -c service_tier=fast p > /tmp/scheduled.log 2>&1"
    await translator.handle_event(event(command, tool="scheduled", background=True))
    assert await translator.drain(5)
    assert order == []  # Pre records intent only.
    await translator.handle_event(event(command, "PostToolUse", tool="scheduled", background=True))
    order.append("response")
    assert await translator.drain(5)
    assert order == ["response", ("reconcile", db_url)]
    items, total = await CodexExecLaunchRepository(db_url).list()
    assert total == 1 and items[0].pre_seen and items[0].state == "unbound"


def test_ultrafast_launch_remains_requested_metadata():
    metadata = wrapper_launch_metadata(["-m", "gpt-6.1-sol", "-c", 'service_tier="ultrafast"'])
    record = ExternalExecRegistration(invocation_id=str(uuid4()), source_namespace="a" * 64, **metadata)
    assert record.requested_service_tier == "ultrafast"
    assert record.requested_metadata_source == "wrapper_argv"
    parsed = classify_bash_launches('codex exec --json -c service_tier=ultrafast -m gpt-6.1-sol prompt')
    assert parsed[0].metadata["requested_service_tier"] == "ultrafast"
