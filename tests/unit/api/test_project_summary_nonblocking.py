"""Project probes must not stall unrelated requests on the API event loop."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from aiteam.api import app as app_module
from aiteam.api import deps, session_probe, session_registry, worktree_probe
from aiteam.api import event_bus as bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.ws.manager import ConnectionManager
from aiteam.loop.task_wall_engine import TaskWallEngine
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        [
            "git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
            "-c", "user.name=Probe Fixture", "-c", "user.email=probe@example.invalid",
            *args,
        ],
        cwd=cwd, check=True, capture_output=True, timeout=5,
    )


@pytest.fixture()
def summary_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated_home))
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    monkeypatch.setattr(bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))

    project_root = tmp_path / "project"
    project_root.mkdir()
    _git(["init", "-b", "codex/fixture"], project_root)
    _git(["commit", "--allow-empty", "-m", "Create isolated fixture"], project_root)
    _git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/heads/codex/fixture"], project_root)
    worktree = project_root / ".worktrees" / "probe"
    _git(["worktree", "add", "-b", "codex/probe", str(worktree)], project_root)

    transcript_dir = isolated_home / ".claude" / "projects" / session_probe.project_slug(
        str(project_root)
    )
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    transcript.write_text(
        json.dumps({"type": "assistant", "message": {"model": "fixture-model"}}) + "\n",
        encoding="utf-8",
    )

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'summary.db'}"
    repo = StorageRepository(db_url=db_url)
    memory = MemoryStore(repository=repo)
    bus = EventBus(repo=repo)
    for name, value in {
        "_repository": repo,
        "_memory_store": memory,
        "_event_bus": bus,
        "_manager": TeamManager(repository=repo, memory=memory, event_bus=bus),
        "_hook_translator": HookTranslator(repo=repo, event_bus=bus),
        "_task_wall_engine": TaskWallEngine(repo=repo),
    }.items():
        monkeypatch.setattr(deps, name, value)

    @asynccontextmanager
    async def isolated_lifespan(app: FastAPI):
        # Keep production dependencies without starting unrelated harvesters.
        await repo.init_db()
        try:
            yield
        finally:
            await get_engine(db_url).dispose()

    loggers = [logging.getLogger(name) for name in (
        "aiteam", "uvicorn", "uvicorn.access", "uvicorn.error",
    )]
    previous_handlers = {logger: set(logger.handlers) for logger in loggers}
    previous_levels = {logger: logger.level for logger in loggers}
    app = app_module.create_app()
    app.router.lifespan_context = isolated_lifespan
    assert not app.dependency_overrides

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, loop="asyncio", lifespan="on",
        access_log=False, log_config=None,
    ))
    service_errors: list[BaseException] = []

    def serve() -> None:
        try:
            asyncio.run(server.serve(sockets=[listener]))
        except BaseException as exc:
            service_errors.append(exc)

    thread = threading.Thread(target=serve, name="summary-probe-api")
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive(), service_errors
            assert time.monotonic() < deadline, "isolated API startup timed out"
            time.sleep(0.01)
        base_url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base_url, trust_env=False, timeout=5) as client:
            assert client.get("/api/health").status_code == 200
            created = client.post("/api/projects", json={
                "name": "isolated-summary-probe", "root_path": str(project_root),
            })
            assert created.status_code == 201, created.text
            project_id = created.json()["data"]["id"]
            yield SimpleNamespace(
                client=client, base_url=base_url,
                path=f"/api/projects/{project_id}/summary", project_id=project_id,
                worktree=str(worktree), thread=thread,
            )
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        added_handlers = set()
        for logger in loggers:
            for handler in set(logger.handlers) - previous_handlers[logger]:
                logger.removeHandler(handler)
                added_handlers.add(handler)
            logger.setLevel(previous_levels[logger])
        for handler in added_handlers:
            handler.close()
        assert not thread.is_alive(), "isolated API thread leaked"
        assert not service_errors, service_errors
        with socket.socket() as check:
            assert check.connect_ex(("127.0.0.1", port)) != 0, "isolated API port leaked"


def _summary(server: SimpleNamespace) -> dict[str, Any]:
    response = server.client.get(server.path)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("boundary", ["sessions", "git"])
def test_slow_probe_keeps_health_responsive(
    summary_server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    baseline = _summary(summary_server)
    entered = threading.Event()
    release = threading.Event()
    boundary_finished = threading.Event()
    result: dict[str, Any] = {}
    module, name = (
        (session_registry, "read_sessions") if boundary == "sessions"
        else (worktree_probe, "_run_git")
    )
    original = getattr(module, name)

    def slow_probe(*args: Any, **kwargs: Any) -> Any:
        if not entered.is_set():
            entered.set()
            try:
                assert release.wait(3), "probe was never released"
                return original(*args, **kwargs)
            finally:
                boundary_finished.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, slow_probe)
    with httpx.Client(base_url=summary_server.base_url, trust_env=False, timeout=5) as client:
        def fetch_summary() -> None:
            try:
                result["response"] = client.get(summary_server.path)
            except BaseException as exc:
                result["error"] = exc

        request = threading.Thread(target=fetch_summary, name="summary-probe-client")
        # The release timer runs outside the server's event loop.
        timer = threading.Timer(1.2, release.set)
        request.start()
        try:
            assert entered.wait(5), "summary did not reach the I/O boundary"
            assert not boundary_finished.is_set()
            timer.start()
            started = time.perf_counter()
            health = summary_server.client.get("/api/health")
            elapsed = time.perf_counter() - started
            health_while_probe_blocked = not release.is_set()
        finally:
            if timer.ident is not None:
                timer.join(2)
            else:
                timer.cancel()
            release.set()
            request.join(5)
        assert not request.is_alive(), "summary client thread leaked"
        assert not timer.is_alive(), "probe release timer leaked"
    assert "error" not in result, result
    assert health.status_code == 200
    assert result["response"].status_code == 200
    assert result["response"].json() == baseline
    print(f"{boundary} probe: health={elapsed:.4f}s, blocked={health_while_probe_blocked}")
    assert elapsed < 0.2, f"health waited {elapsed:.3f}s for the {boundary} probe"
    assert health_while_probe_blocked, "health ran only after the probe was released"


def test_summary_preserves_real_probe_data_across_requests(summary_server: SimpleNamespace) -> None:
    baseline = _summary(summary_server)
    assert baseline["leaders"][0]["model"] == "fixture-model"
    assert baseline["leader"] == baseline["leaders"][0]
    assert baseline["status"] == "active"
    assert baseline["worktrees"][0]["path"] == summary_server.worktree
    assert baseline["worktrees"][0]["merged"] is True
    assert baseline["worktrees"][0]["dirty"] is False
    assert _summary(summary_server) == baseline
    persisted = summary_server.client.get(f"/api/projects/{summary_server.project_id}")
    assert persisted.status_code == 200
    assert persisted.json()["data"]["name"] == "isolated-summary-probe"


def test_concurrent_project_summaries_match_their_serial_results(
    summary_server: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_root = tmp_path / "second-project"
    second_root.mkdir()
    created = summary_server.client.post("/api/projects", json={
        "name": "second-isolated-project", "root_path": str(second_root),
    })
    assert created.status_code == 201
    second_path = f"/api/projects/{created.json()['data']['id']}/summary"
    first_baseline = _summary(summary_server)
    second_response = summary_server.client.get(second_path)
    assert second_response.status_code == 200
    second_baseline = second_response.json()
    assert first_baseline != second_baseline

    original = session_probe.detect_live_sessions
    overlapping = threading.Barrier(2)

    def concurrent_probe(root_path: str) -> list[dict]:
        overlapping.wait(timeout=3)
        return original(root_path)

    monkeypatch.setattr(session_probe, "detect_live_sessions", concurrent_probe)

    def request(path: str) -> dict[str, Any]:
        with httpx.Client(base_url=summary_server.base_url, trust_env=False, timeout=5) as client:
            response = client.get(path)
            assert response.status_code == 200
            return response.json()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(request, summary_server.path)
        second = workers.submit(request, second_path)
        assert first.result(timeout=5) == first_baseline
        assert second.result(timeout=5) == second_baseline


@pytest.mark.parametrize("failure", ["session-files", "git-process", "worktree-probe"])
def test_summary_preserves_probe_failure_fallback(
    summary_server: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    expected = _summary(summary_server)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise OSError("isolated probe failure")

    if failure == "session-files":
        monkeypatch.setattr(session_probe, "_claude_projects_dir", unavailable)
        expected.update(leaders=[], leader=None, last_activity_at=None, status="inactive")
    elif failure == "git-process":
        monkeypatch.setattr(worktree_probe, "subprocess", SimpleNamespace(run=unavailable))
        expected["worktrees"] = []
    else:
        monkeypatch.setattr(worktree_probe, "detect_worktrees", unavailable)
        expected["worktrees"] = []
    assert _summary(summary_server) == expected
    assert _summary(summary_server) == expected
