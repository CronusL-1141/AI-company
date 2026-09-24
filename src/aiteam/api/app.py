"""AI Team OS — FastAPI application factory.

Provides create_app() function for creating and configuring FastAPI instances.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aiteam import __version__
from aiteam.api.account_monitor_lifecycle import account_monitor_lifespan
from aiteam.api.deps import cleanup_dependencies, init_dependencies
from aiteam.api.errors import register_error_handlers
from aiteam.api.lifecycle_diagnostics import (
    capture_process_snapshot,
    observe_signals,
    record_lifecycle_event,
)
from aiteam.api.routes import api_router
from aiteam.storage.connection import DEFAULT_DB_URL

logger = logging.getLogger(__name__)

_mcp_http_app = None


def _complete_build(candidate: Path) -> bool:
    """index.html plus at least one JS bundle: a half-written build serves a blank page."""
    assets = candidate / "assets"
    return (candidate / "index.html").is_file() and assets.is_dir() and any(assets.glob("*.js"))


def pick_dashboard_dist(project_root: Path, plugin_root: str = "", cache_base: Path | None = None) -> Path | None:
    """The Dashboard build to serve, or None.

    1. ``$CLAUDE_PLUGIN_ROOT/dashboard-dist``: a marketplace install serves its own build.
    2. A source checkout has two builds: ``dashboard/dist`` (a local build, never
       tracked) and ``plugin/dashboard-dist`` (tracked, updated by every pull).
       Of the complete ones the newer ``index.html`` wins. Fixed order let a local
       build from days ago shadow the tracked one after an update, so the API
       kept serving the old interface (task e873e445).
    3. The marketplace cache.
    """
    if plugin_root:
        candidate = Path(plugin_root) / "dashboard-dist"
        if candidate.is_dir() and (candidate / "index.html").exists():
            return candidate
    builds = [
        candidate for candidate in (project_root / "dashboard" / "dist", project_root / "plugin" / "dashboard-dist")
        if _complete_build(candidate)
    ]
    if builds:
        # max() keeps the first of equal keys: the local build on a tie.
        chosen = max(builds, key=lambda candidate: (candidate / "index.html").stat().st_mtime)
        others = [candidate for candidate in builds if candidate != chosen]
        logger.info("Dashboard: serving %s (newest index.html%s)", chosen,
                    f"; also found {others[0]}" if others else "")
        return chosen
    cache_base = cache_base if cache_base is not None else Path.home() / ".claude" / "plugins" / "cache" / "ai-team-os"
    if cache_base.is_dir():
        for match in cache_base.glob("**/dashboard-dist/index.html"):
            return match.parent
    return None


def _get_mcp_http_app():
    """Get or create the FastMCP ASGI app (lazy, module-level cached).

    path='/' is required: FastAPI mount('/mcp') strips the '/mcp' prefix before
    forwarding to the sub-app, so the sub-app route must be at '/' to match.
    Using the default path='/mcp' would require CC to call /mcp/mcp instead.
    """
    global _mcp_http_app
    if _mcp_http_app is None:
        try:
            from aiteam.mcp.server import mcp
            _mcp_http_app = mcp.http_app(transport="streamable-http", path="/")
        except Exception:
            pass
    return _mcp_http_app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Observe lifecycle boundaries without changing dependency ownership."""
    startup_process = capture_process_snapshot()
    with observe_signals(startup_process):
        phase = "startup"
        # Which Dashboard build this process serves (pick_dashboard_dist): the
        # startup record is where a stale interface can be traced back to.
        record_lifecycle_event("api.startup.begin", startup_process=startup_process,
                               dashboard_dist=getattr(app.state, "dashboard_dist", ""))
        try:
            async with _application_lifespan(app):
                record_lifecycle_event("api.startup.complete", startup_process=startup_process)
                phase = "running"
                yield
                phase = "shutdown"
                record_lifecycle_event("api.shutdown.begin", startup_process=startup_process)
            record_lifecycle_event("api.shutdown.complete", startup_process=startup_process)
        except BaseException as exc:
            record_lifecycle_event(
                "api.lifecycle.failed", phase=phase, exception_type=type(exc).__name__,
                startup_process=startup_process,
            )
            raise


@asynccontextmanager
async def _application_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle management."""
    mcp_app = _get_mcp_http_app()
    if mcp_app is not None:
        # FastAPI mount() does NOT trigger sub-app lifespan automatically.
        # We must enter it here so StreamableHTTPSessionManager._task_group
        # is initialized before the first request arrives.
        async with mcp_app.lifespan(mcp_app):
            await init_dependencies()
            try:
                async with account_monitor_lifespan(app, DEFAULT_DB_URL):
                    yield
            finally:
                await cleanup_dependencies()
    else:
        await init_dependencies()
        try:
            async with account_monitor_lifespan(app, DEFAULT_DB_URL):
                yield
        finally:
            await cleanup_dependencies()


def create_app() -> FastAPI:
    """Create a FastAPI application instance."""
    app = FastAPI(
        title="AI Team OS",
        description="通用可复用的AI Agent团队操作系统 API",
        version=__version__,
        lifespan=lifespan,
    )

    # Debug file logging
    from aiteam.api.debug_log import setup_debug_log
    setup_debug_log()

    # L1 input guardrails. 后加的中间件包在外层；这里最先加，所以在最内层：实际顺序是
    # Diagnostics -> CORS -> SQLite -> Ledger -> Guardrail，扫描发生在 DB 限流放行之后、路由之前。
    from aiteam.api.middleware import InputGuardrailMiddleware, SQLiteConcurrencyMiddleware

    app.add_middleware(InputGuardrailMiddleware)

    # 请求级账本：给"零调用"判断补第二个采集口径（MCP 工具面只看得见自己那一半）。
    # 内存计数 + 按小时惰性翻滚成一条 rollup 事件，不新建表、不逐请求写库。
    from aiteam.api.request_ledger import RequestLedgerMiddleware

    app.add_middleware(RequestLedgerMiddleware)

    # SQLite concurrency throttling (must be added BEFORE CORS)
    app.add_middleware(SQLiteConcurrencyMiddleware, max_concurrent=5, queue_timeout=30.0)

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from aiteam.api.request_diagnostics import RequestDiagnosticsMiddleware

    app.add_middleware(RequestDiagnosticsMiddleware)

    # Register routes
    app.include_router(api_router)

    # Register unified error handlers
    register_error_handlers(app)

    # Mount FastMCP HTTP Streamable transport at /mcp/
    # CC connects via: {"type": "streamable-http", "url": "http://localhost:8000/mcp/"}
    # mount('/mcp/') is required: mount('/mcp') causes Starlette to not match bare
    # /mcp POST requests (they fall through to the SPA fallback GET handler -> 405).
    # A 308 redirect from /mcp -> /mcp/ is added as a convenience fallback for
    # clients that omit the trailing slash (308 preserves the HTTP method).
    mcp_app = _get_mcp_http_app()
    if mcp_app is not None:
        from fastapi.responses import RedirectResponse

        @app.get("/api/mcp/http-readiness", tags=["mcp"])
        async def _http_mcp_readiness():
            return {"status": "ok", "transport": "streamable-http",
                    "project_context": "connection-cwd-v1", "session_context": "unavailable"}

        @app.api_route("/mcp", methods=["GET", "POST", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"],
                       include_in_schema=False)
        async def _mcp_redirect():
            return RedirectResponse("/mcp/", status_code=308)

        app.mount("/mcp/", mcp_app)

    # Mount Dashboard static files (must be after API routes to avoid intercepting /api/*)
    _project_root = Path(__file__).resolve().parent.parent.parent.parent
    _dist_dir = pick_dashboard_dist(_project_root, os.environ.get("CLAUDE_PLUGIN_ROOT", ""))
    app.state.dashboard_dist = str(_dist_dir or "")

    if _dist_dir is not None and _dist_dir.is_dir():
        # /assets static resources served directly by StaticFiles
        _assets_dir = _dist_dir / "assets"
        if _assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="dashboard-assets")

        # SPA catch-all: all non-API, non-assets, non-mcp paths return index.html
        @app.get("/{path:path}")
        async def spa_fallback(path: str) -> FileResponse:
            if path.startswith("api/") or path.startswith("assets/") or path.startswith("mcp"):
                raise HTTPException(status_code=404)
            index = _dist_dir / "index.html"
            if index.exists():
                return FileResponse(str(index))
            raise HTTPException(status_code=404)

    return app
