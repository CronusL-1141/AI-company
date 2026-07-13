"""集成测试 — pytest fixtures.

每个测试使用独立的临时SQLite数据库，通过FastAPI TestClient执行端到端测试。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.loop.engine import LoopEngine
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def integration_client(tmp_path, monkeypatch):
    """创建集成测试客户端，每个测试独立的内存SQLite数据库."""
    # 配置文件隔离：team_config 的 CONFIG_FILE 是指向仓内真实文件的模块常量，
    # 测试打真实 API 曾把 'integ'/fixer 测试数据写进 checked-in 的
    # plugin/config/team-defaults.json（2026-07-13 实录）——重定向到 tmp
    from aiteam.api.routes import team_config

    monkeypatch.setattr(team_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(team_config, "CONFIG_FILE", tmp_path / "team-defaults.json")

    # 初始化独立的存储层
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)

    # 注入到deps模块（含EventBus、LoopEngine）
    event_bus = EventBus(repo=repo)
    loop_engine = LoopEngine(repo=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager
    deps._loop_engine = loop_engine

    app = create_app()

    # 覆盖lifespan：测试中不需要自动init/cleanup
    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan

    client = TestClient(app)
    yield client

    # 清理
    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._loop_engine = None


@pytest.fixture()
def repo_and_client(integration_client):
    """同时提供repository和client，用于需要直接操作数据库的测试."""
    return deps._repository, integration_client
