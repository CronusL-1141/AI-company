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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle management."""
    # Startup: initialize dependencies
    await init_dependencies()
    yield
    # Shutdown: clean up resources
    await cleanup_dependencies()


def _create_mcp_http_app():
    """Create FastMCP ASGI app for HTTP Streamable transport mounted at /mcp.

    Returns None if FastMCP is not available (should not happen in normal installs).
    The MCP app is created lazily to avoid circular imports during module load.
    """
    try:
        from aiteam.mcp.server import mcp
        return mcp.http_app(transport="streamable-http")
    except Exception:
        return None


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

    # Mount FastMCP HTTP Streamable transport at /mcp
    # CC connects via: {"type": "streamable-http", "url": "http://localhost:8000/mcp"}
    mcp_http_app = _create_mcp_http_app()
    if mcp_http_app is not None:
        app.mount("/mcp", mcp_http_app)

    # Mount Dashboard static files (must be after API routes to avoid intercepting /api/*)
    _project_root = Path(__file__).resolve().parent.parent.parent.parent
    _dist_dir = _project_root / "dashboard" / "dist"

    if _dist_dir.is_dir():
        # /assets static resources served directly by StaticFiles
        _assets_dir = _dist_dir / "assets"
        if _assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="dashboard-assets")

        # SPA catch-all: all non-API, non-assets paths return index.html
        @app.get("/{path:path}")
        async def spa_fallback(path: str) -> FileResponse:
            if path.startswith("api/") or path.startswith("assets/"):
                raise HTTPException(status_code=404)
            index = _dist_dir / "index.html"
            if index.exists():
                return FileResponse(str(index))
            raise HTTPException(status_code=404)

    return app
