"""AI Team OS — FastAPI application factory.

Provides create_app() function for creating and configuring FastAPI instances.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aiteam.api.deps import cleanup_dependencies, init_dependencies
from aiteam.api.errors import register_error_handlers
from aiteam.api.routes import api_router

_mcp_http_app = None


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
    """Application lifecycle management."""
    mcp_app = _get_mcp_http_app()
    if mcp_app is not None:
        # FastAPI mount() does NOT trigger sub-app lifespan automatically.
        # We must enter it here so StreamableHTTPSessionManager._task_group
        # is initialized before the first request arrives.
        async with mcp_app.lifespan(mcp_app):
            await init_dependencies()
            yield
            await cleanup_dependencies()
    else:
        await init_dependencies()
        yield
        await cleanup_dependencies()


def create_app() -> FastAPI:
    """Create a FastAPI application instance."""
    app = FastAPI(
        title="AI Team OS",
        description="通用可复用的AI Agent团队操作系统 API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Debug file logging
    from aiteam.api.debug_log import setup_debug_log
    setup_debug_log()

    # L1 input guardrails (added first so it runs outermost — before DB throttle)
    from aiteam.api.middleware import InputGuardrailMiddleware, SQLiteConcurrencyMiddleware

    app.add_middleware(InputGuardrailMiddleware)

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

        @app.api_route("/mcp", methods=["GET", "POST", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"],
                       include_in_schema=False)
        async def _mcp_redirect():
            return RedirectResponse("/mcp/", status_code=308)

        app.mount("/mcp/", mcp_app)

    # Mount Dashboard static files (must be after API routes to avoid intercepting /api/*)
    # Search multiple locations: dev repo, plugin directory, pip-installed package
    _project_root = Path(__file__).resolve().parent.parent.parent.parent
    _dist_dir = None
    import os as _os

    # Check CLAUDE_PLUGIN_ROOT first (most reliable for marketplace installs)
    _plugin_root = _os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if _plugin_root:
        _candidate = Path(_plugin_root) / "dashboard-dist"
        if _candidate.is_dir() and (_candidate / "index.html").exists():
            _dist_dir = _candidate

    # Then check known static locations
    if _dist_dir is None:
        for _candidate in [
            _project_root / "dashboard" / "dist",           # dev: repo root
            _project_root / "plugin" / "dashboard-dist",    # dev: plugin subdir
        ]:
            if _candidate.is_dir() and (_candidate / "index.html").exists():
                _dist_dir = _candidate
                break

    # Finally, search marketplace cache (nested: cache/name/name/version/)
    if _dist_dir is None:
        _cache_base = Path.home() / ".claude" / "plugins" / "cache" / "ai-team-os"
        if _cache_base.is_dir():
            for _match in _cache_base.glob("**/dashboard-dist/index.html"):
                _dist_dir = _match.parent
                break

    if _dist_dir.is_dir():
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
