"""AI Team OS — pytest 全局 fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """为同步测试兜底一个可用 event loop（Python 3.12 + pytest-asyncio）.

    pytest-asyncio 在 async 测试结束后会关闭并清除 MainThread 的 loop，
    之后排队的同步测试再调旧式 asyncio.get_event_loop().run_until_complete
    会 RuntimeError（单独跑通过、全量跑报错的测试间污染）。
    """
    try:
        closed = asyncio.get_event_loop().is_closed()
    except RuntimeError:
        closed = True
    if closed:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield


@pytest.fixture()
def tmp_project_dir(tmp_path: Path) -> Path:
    """创建临时目录作为项目目录."""
    project_dir = tmp_path / "test-project"
    project_dir.mkdir()
    aiteam_dir = project_dir / ".aiteam"
    aiteam_dir.mkdir()
    return project_dir


@pytest_asyncio.fixture()
async def db_repository() -> StorageRepository:
    """创建内存 SQLite 的 StorageRepository 实例.

    使用 sqlite+aiosqlite:// 内存数据库，测试结束后自动清理。
    """
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    yield repo  # type: ignore[misc]
    await close_db()
