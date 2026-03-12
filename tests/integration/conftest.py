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
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def integration_client():
    """创建集成测试客户端，每个测试独立的内存SQLite数据库."""
    # 初始化独立的存储层
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)

    # 注入到deps模块
    deps._repository = repo
    deps._memory_store = memory
    deps._manager = manager

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
    deps._manager = None


@pytest.fixture()
def repo_and_client(integration_client):
    """同时提供repository和client，用于需要直接操作数据库的测试."""
    return deps._repository, integration_client
