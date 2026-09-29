"""Shared fixtures for the API tests.

hook_server: the full app under a real uvicorn (httptools, as in production) in a
background thread, on a temporary SQLite file. Yields (port, database path, the DB
slots the concurrency middleware allows).
"""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager

import pytest
import uvicorn

from aiteam.api import app as app_module
from aiteam.api import debug_log, deps
from aiteam.api import event_bus as event_bus_module
from aiteam.api import middleware as middleware_module
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import hooks
from aiteam.api.ws.manager import ConnectionManager
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository


class _Server:
    def __init__(self, app) -> None:
        self.server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=0, http="httptools", log_level="warning", lifespan="on",
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> int:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        return self.server.servers[0].sockets[0].getsockname()[1]

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture()
def hook_server(tmp_path, monkeypatch):
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "0")
    monkeypatch.setattr(debug_log, "setup_debug_log", lambda: None)
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    monkeypatch.setattr(event_bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(event_bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.delenv(hooks.HOOK_RAW_DUMP_ENV, raising=False)
    database = tmp_path / "ingest.sqlite"
    database_url = f"sqlite+aiosqlite:///{database}"
    app = app_module.create_app()

    @asynccontextmanager
    async def lifespan(_app):
        # Built on the server's own loop: aiosqlite connections are loop-bound.
        repo = StorageRepository(db_url=database_url)
        await repo.init_db()
        bus = EventBus(repo=repo)
        translator = HookTranslator(repo=repo, event_bus=bus)
        app.dependency_overrides.update({
            deps.get_repository: lambda: repo,
            deps.get_event_bus: lambda: bus,
            deps.get_hook_translator: lambda: translator,
        })
        monkeypatch.setattr(deps, "_repository", repo)
        monkeypatch.setattr(deps, "_event_bus", bus)
        monkeypatch.setattr(deps, "_hook_translator", translator)
        try:
            yield
        finally:
            await translator.drain(5)
            await get_engine(database_url).dispose()

    app.router.lifespan_context = lifespan
    # DB slots a hook can take: the concurrency middleware's mounted max_concurrent.
    slots = next(
        m for m in app.user_middleware if m.cls is middleware_module.SQLiteConcurrencyMiddleware
    ).kwargs["max_concurrent"]
    with _Server(app) as port:
        yield port, database, slots
