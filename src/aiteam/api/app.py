"""AI Team OS — FastAPI应用工厂.

提供 create_app() 函数，用于创建和配置 FastAPI 实例。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from aiteam.api.deps import cleanup_dependencies, init_dependencies
from aiteam.api.errors import register_error_handlers
from aiteam.api.routes import api_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理."""
    # 启动：初始化依赖
    await init_dependencies()
    yield
    # 关闭：清理资源
    await cleanup_dependencies()


def create_app() -> FastAPI:
    """创建FastAPI应用实例."""
    app = FastAPI(
        title="AI Team OS",
        description="通用可复用的AI Agent团队操作系统 API",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS中间件
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

    # 注册路由
    app.include_router(api_router)

    # 注册统一错误处理
    register_error_handlers(app)

    # 挂载Dashboard静态文件（必须在API路由之后，确保/api/*不被拦截）
    _project_root = Path(__file__).resolve().parent.parent.parent.parent
    _dist_dir = _project_root / "dashboard" / "dist"

    if _dist_dir.is_dir():
        _assets_dir = _dist_dir / "assets"
        if _assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="dashboard-assets")
        app.mount("/", StaticFiles(directory=str(_dist_dir), html=True), name="dashboard")

    return app
