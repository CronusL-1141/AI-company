"""Serve production Dashboard/API code against an isolated SQLite snapshot.

No host lifecycle workers, auto-migrations, MCP autostart, configuration writes,
or external notifications are started. This is a browser verification fixture,
not deployment. The source SQLite connection is explicitly read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path


def snapshot(source: Path, destination: Path) -> None:
    if destination.exists() or source.resolve() == destination.resolve():
        raise ValueError("Snapshot target must be new and distinct from source")
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=8192)
            if src.execute("PRAGMA user_version").fetchone() != dst.execute(
                "PRAGMA user_version"
            ).fetchone():
                raise ValueError("Snapshot clock/schema version mismatch")
    destination.chmod(0o600)


async def serve(directory: Path, requested_port: int = 0) -> None:
    import uvicorn
    from fastapi.responses import JSONResponse

    import aiteam.api.app as app_module
    import aiteam.api.debug_log as debug_log
    import aiteam.api.deps as deps
    from aiteam.api.event_bus import EventBus
    from aiteam.api.hook_translator import HookTranslator
    from aiteam.config import settings
    from aiteam.loop.task_wall_engine import TaskWallEngine
    from aiteam.memory.store import MemoryStore
    from aiteam.orchestrator.team_manager import TeamManager
    from aiteam.storage.connection import close_db
    from aiteam.storage.repository import StorageRepository

    logging.basicConfig(level=logging.WARNING)
    debug_log.setup_debug_log = lambda **_: directory / "preview.log"
    app_module._get_mcp_http_app = lambda: None
    settings.SLACK_WEBHOOK_URL = ""

    @asynccontextmanager
    async def isolated_lifespan(_app):
        repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{directory / 'snapshot.db'}")
        deps._repository = repo
        deps._memory_store = MemoryStore(repository=repo, archive_dir=directory / "archive")
        deps._event_bus = EventBus(repo=repo)
        deps._manager = TeamManager(
            repository=repo, memory=deps._memory_store, event_bus=deps._event_bus,
        )
        deps._hook_translator = HookTranslator(repo=repo, event_bus=deps._event_bus)
        deps._task_wall_engine = TaskWallEngine(repo=repo)
        await deps.refresh_project_dir_cache(repo)
        try:
            yield
        finally:
            await close_db()

    app = app_module.create_app()
    app.router.lifespan_context = isolated_lifespan

    @app.middleware("http")
    async def preview_boundary(request, call_next):
        # The sole write is replaying an explicitly supplied observation into
        # this private snapshot; no UI configuration/dispatch mutation is allowed.
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.method != "POST" or request.url.path != "/api/hooks/event"
        ):
            return JSONResponse({"error": "snapshot_preview_read_only"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Aiteam-Preview"] = "isolated-snapshot"
        return response

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", requested_port))
        listener.listen(128)
        port = listener.getsockname()[1]
        print(json.dumps({
            "preview": True, "url": f"http://127.0.0.1:{port}",
            "database": str(directory / "snapshot.db"), "pid": os.getpid(),
        }), flush=True)
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", loop="asyncio"))
        await server.serve(sockets=[listener])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--reuse-snapshot", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    directory = args.directory.resolve(strict=True)
    destination = directory / "snapshot.db"
    if args.reuse_snapshot:
        if not destination.is_file() or destination.resolve() == args.source_db.resolve():
            raise ValueError("Expected a separate existing snapshot")
    else:
        snapshot(args.source_db, destination)
    os.environ["AITEAM_DB_PATH"] = str(destination)
    os.environ["AITEAM_DIAGNOSTICS_DIR"] = str(directory / "diagnostics")
    os.environ["AITEAM_DIAGNOSTICS_ENABLED"] = "0"
    if args.port != 0 and not 1024 <= args.port <= 65535:
        raise ValueError("Expected an unprivileged localhost port")
    asyncio.run(serve(directory, args.port))


if __name__ == "__main__":
    main()
