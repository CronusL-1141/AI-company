"""AI Team OS — pytest 全局 fixtures."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
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


_USER_NOTICE_PATH = Path(__file__).resolve().parents[1] / "plugin" / "hooks" / "user_notice.py"


@pytest.fixture(autouse=True)
def _isolate_user_notice(tmp_path_factory, monkeypatch):
    """Hooks run in-process write the user-notice ledger; keep it off the real HOME.

    Every hook loads the shared module as sys.modules["user_notice"]. Pin that
    module to a per-test state directory, reset its per-run latches (one output
    document per hook run, the last fetch failure), and never let it reach the
    real service on the default port.
    """
    module = sys.modules.get("user_notice")
    if module is None:
        spec = importlib.util.spec_from_file_location("user_notice", _USER_NOTICE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["user_notice"] = module
        spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE_DIR_OVERRIDE", str(tmp_path_factory.mktemp("notice-state")))
    monkeypatch.setattr(module, "_WROTE_DOCUMENT", False)
    monkeypatch.setattr(module, "_LAST_FAILURE", "")
    # Without an explicit AITEAM_API_URL the module would fall back to port 8000,
    # which on a developer machine is the real OS service. Refuse instead.
    monkeypatch.setattr(module, "api_url", lambda: os.environ.get("AITEAM_API_URL") or "http://127.0.0.1:9")
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
