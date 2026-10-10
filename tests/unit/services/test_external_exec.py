"""Fake executable fault injection; this suite never sends a model request."""

import io
import json
import sqlite3
import sys
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from aiteam.api.routes import external_exec as routes
from aiteam.services.external_exec import ExecEventCollector, run_external_exec, upload_invocation
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.external_exec import ExternalExecRepository
from aiteam.storage.repository import StorageRepository
from aiteam.types import ExternalExecFinish, ExternalExecRegistration


@pytest.fixture
async def store(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'spool.sqlite'}"
    repository = ExternalExecRepository(url)
    await repository.init_db()
    yield repository
    await engine_pool.get_engine(url).dispose()


def registration():
    return ExternalExecRegistration(
        invocation_id=str(uuid4()),
        source_namespace="a" * 64,
        ephemeral=True,
        caller_harness="claude-code",
        caller_session_id="parent-claude",
    )


def fake_cli(tmp_path, events, tail=""):
    path = tmp_path / "fake-codex"
    body = "\n".join(json.dumps(row) for row in events) + "\n"
    path.write_text(
        f"#!{sys.executable}\nimport sys, os, signal\n"
        "assert sys.argv[1:3] == ['exec','--json']\nassert '--ephemeral' in sys.argv\n"
        f"sys.stdout.write({body!r})\nsys.stdout.flush()\n{tail}\n"
    )
    path.chmod(0o700)
    return str(path), body.encode()


async def test_non_posix_is_rejected_before_pipes_signals_or_process_launch(store, monkeypatch):
    from types import SimpleNamespace

    from aiteam.services import external_exec as service

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported platform must not launch or allocate pipes")

    monkeypatch.setattr(service, "os", SimpleNamespace(name="nt", pipe=forbidden))
    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", forbidden)
    value = registration()
    assert await run_external_exec(store, value, [], stdout=io.BytesIO()) == 127
    saved = await store.get(value.invocation_id)
    assert saved.status == "failed" and saved.exit_code == 127
    assert saved.diagnostics == ["spawn_failed", "missing_usage"]


async def test_guard_import_isolation_ignores_shadow_stdlib_in_script_directory(store, tmp_path, monkeypatch):
    from pathlib import Path

    from aiteam.services import external_exec as service

    scripts = tmp_path / "shadow-services"
    scripts.mkdir()
    source = Path(service.__file__).with_name("external_exec_guard.py")
    (scripts / source.name).write_bytes(source.read_bytes())
    marker = tmp_path / "shadow-loaded"
    (scripts / "select.py").write_text(
        f"open({str(marker)!r},'w').write('loaded')\nraise RuntimeError('synthetic shadow module')\n"
    )
    monkeypatch.setattr(service, "__file__", str(scripts / "external_exec.py"))
    value, output = registration(), io.BytesIO()
    assert await run_external_exec(store, value, ["SAFE"], executable="/bin/echo", stdout=output) == 0
    assert output.getvalue() == b"exec --json --ephemeral SAFE\n" and not marker.exists()


async def test_json_passthrough_usage_subsets_and_no_content_persisted(store, tmp_path):
    usage = {
        "input_tokens": 123,
        "cached_input_tokens": 100,
        "output_tokens": 22,
        "reasoning_output_tokens": 9,
        "secret": "PRIVATE-USAGE",
    }
    rows = [
        {"type": "thread.started", "thread_id": "thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "PRIVATE-OUTPUT"}},
        {"type": "turn.completed", "usage": usage},
        {"type": "turn.completed", "usage": usage},
    ]
    executable, raw = fake_cli(tmp_path, rows)
    value, output = registration(), io.BytesIO()
    code = await run_external_exec(store, value, ["PRIVATE-PROMPT"], executable=executable, stdout=output)
    assert code == 0 and output.getvalue() == raw
    saved = await ExternalExecRepository(store._db_url).get(value.invocation_id)
    assert saved.status == "completed" and len(saved.receipts) == 1
    receipt = saved.receipts["turn:1"]
    assert receipt.usage.input_tokens == 123 and receipt.usage.total_tokens is None
    assert receipt.model is None and receipt.account_key is None and receipt.service_tier is None
    assert saved.caller_harness == "claude-code" and saved.callee_harness == "codex"
    with sqlite3.connect(store._db_url.split("///", 1)[1]) as db:
        persisted = db.execute("SELECT payload FROM external_exec_invocations").fetchone()[0]
    assert "PRIVATE" not in persisted


@pytest.mark.parametrize(
    "tail,status,code",
    [
        ("sys.exit(3)", "failed", 3),
        ("os.kill(os.getpid(), signal.SIGTERM)", "interrupted", 143),
    ],
    ids=["failure", "signal-interruption"],
)
async def test_terminated_execution_keeps_unknown_usage(store, tmp_path, tail, status, code):
    executable, raw = fake_cli(tmp_path, [{"type": "thread.started", "thread_id": "thread"}], tail)
    value, output = registration(), io.BytesIO()
    assert await run_external_exec(store, value, [], executable=executable, stdout=output) == code
    saved = await store.get(value.invocation_id)
    assert saved.status == status and saved.thread_id == "thread"
    assert saved.receipts == {} and "missing_usage" in saved.diagnostics
    assert output.getvalue() == raw


async def test_spawn_failure_is_durable(store, tmp_path):
    value = registration()
    assert await run_external_exec(store, value, [], executable=str(tmp_path / "missing"), stdout=io.BytesIO()) == 127
    saved = await store.get(value.invocation_id)
    assert saved.status == "failed" and saved.receipts == {} and saved.exit_code == 127


@pytest.mark.parametrize(
    "sequence,exit_code,status",
    [
        (["error", "error", "error", "error", "turn.completed"], 0, "completed"),
        (["turn.completed", "error"], 0, "completed"),
        (["turn.failed"], 0, "failed"),
        (["turn.failed", "turn.started", "turn.completed"], 0, "completed"),
        (["turn.completed", "turn.started", "turn.failed"], 0, "failed"),
        (["error", "turn.completed"], 3, "failed"),
    ],
    ids=[
        "native-retry-then-complete",
        "notification-after-complete",
        "terminal-failure-with-zero-exit",
        "later-turn-completes",
        "later-turn-fails",
        "nonzero-exit-still-fails",
    ],
)
async def test_error_notification_does_not_override_terminal_turn_outcome(store, tmp_path, sequence, exit_code, status):
    events = [{"type": "thread.started", "thread_id": "thread"}, {"type": "turn.started"}]
    for kind in sequence:
        event = {"type": kind}
        if kind == "error":
            event["message"] = "PRIVATE-RETRY-MESSAGE"
        if kind == "turn.completed":
            event["usage"] = {
                "input_tokens": 22139,
                "cached_input_tokens": 7168,
                "output_tokens": 5,
                "reasoning_output_tokens": 0,
            }
        events.append(event)
    executable, raw = fake_cli(tmp_path, events, f"sys.exit({exit_code})")
    value, output = registration(), io.BytesIO()
    assert await run_external_exec(store, value, [], executable=executable, stdout=output) == exit_code
    saved = await store.get(value.invocation_id)
    assert saved.status == status and saved.exit_code == exit_code and output.getvalue() == raw
    if "error" in sequence:
        assert "error_event" in saved.diagnostics
    if "turn.completed" in sequence:
        assert any(receipt.usage.input_tokens == 22139 for receipt in saved.receipts.values())
    assert "PRIVATE" not in saved.model_dump_json()


async def test_bounded_parser_ignores_oversized_private_event_and_bad_usage(store):
    value = registration()
    await store.register(value)
    collector = ExecEventCollector(store, value.invocation_id)
    await collector.feed(b'{"type":"item.completed","text":"' + b"PRIVATE" * 50000)
    await collector.feed(b'"}\n{"type":"thread.started","thread_id":"thread"}\n')
    await collector.feed(b'{"type":"turn.started"}\n{"type":"turn.completed","usage":{"input_tokens":true}}\n')
    await collector.end()
    saved = await store.get(value.invocation_id)
    assert saved.thread_id == "thread" and saved.receipts["turn:1"].usage is None
    assert "oversized_event" in collector.diagnostics and "invalid_usage" in collector.diagnostics
    assert not collector._pending


async def test_receipt_replay_failure_preserves_pending_then_retries_without_content(store, tmp_path):
    value = registration()
    await store.register(value)
    collector = ExecEventCollector(store, value.invocation_id)
    await collector.feed(
        b'{"type":"thread.started","thread_id":"thread"}\n{"type":"turn.started"}\n'
        b'{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":4}}\n'
    )
    await store.finish(value.invocation_id, ExternalExecFinish(status="completed", exit_code=0))
    calls = []
    offline = True

    remote_storage = StorageRepository(f"sqlite+aiosqlite:///{tmp_path / 'remote.sqlite'}")
    await remote_storage.init_db()
    remote = ExternalExecRepository(remote_storage._db_url)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_repository] = lambda: remote_storage
    app.dependency_overrides[routes.get_external_exec_repository] = lambda: remote

    @app.middleware("http")
    async def connectivity(request, call_next):
        calls.append((request.url.path, json.loads(await request.body())))
        if offline:
            return JSONResponse({"success": False}, status_code=503)
        return await call_next(request)

    try:
        async with httpx.AsyncClient(base_url="http://test", transport=httpx.ASGITransport(app=app)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await upload_invocation(store, value.invocation_id, client)
            assert (await store.list(pending_only=True))[1] == 1
            offline = False
            assert await upload_invocation(store, value.invocation_id, client)
            assert (await store.list(pending_only=True))[1] == 0
        actual = await remote.get(value.invocation_id)
        assert actual.status == "completed" and actual.receipts["turn:1"].usage.input_tokens == 12
    finally:
        await engine_pool.get_engine(remote_storage._db_url).dispose()
    assert (await store.get(value.invocation_id)).receipts["turn:1"].usage.input_tokens == 12
    assert calls[-1][0].endswith("/finish")
    assert "prompt" not in str(calls) and "stdout" not in str(calls)


async def test_http_200_without_success_ack_is_not_uploaded(store):
    value = registration()
    await store.register(value)

    def handler(request):
        return httpx.Response(200, json={"success": False})

    async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError, match="not_confirmed"):
            await upload_invocation(store, value.invocation_id, client)
    assert (await store.list(pending_only=True))[1] == 1


@pytest.mark.parametrize("number,evidence", [(0, "zero_unverified"), (1, "reported")])
async def test_native_aggregate_labels_zero_without_claiming_proven_zero_consumption(store, number, evidence):
    value = registration()
    await store.register(value)
    collector = ExecEventCollector(store, value.invocation_id)
    await collector.feed(
        (json.dumps({"type": "turn.completed", "usage": {"input_tokens": number, "output_tokens": 0}}) + "\n").encode()
    )
    receipt = (await store.get(value.invocation_id)).receipts["turn:1"]
    assert receipt.counting_level == "thread_aggregate" and receipt.usage_evidence == evidence
    assert receipt.usage.input_tokens == number and receipt.usage.cached_input_tokens is None
    assert ("usage_zero_unverified" in collector.diagnostics) == (number == 0)
