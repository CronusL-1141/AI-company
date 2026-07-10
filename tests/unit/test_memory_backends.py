"""AI Team OS — 记忆后端单元测试."""

from __future__ import annotations

from aiteam.memory.backends import MemoryBackend
from aiteam.memory.backends.sqlite_backend import SqliteMemoryBackend
from aiteam.storage.repository import StorageRepository
from aiteam.types import Memory, MemoryScope

# ================================================================
# SqliteMemoryBackend
# ================================================================


async def test_sqlite_backend_create(db_repository: StorageRepository) -> None:
    """SQLite后端创建记忆."""
    backend = SqliteMemoryBackend(db_repository)
    memory = await backend.create("agent", "a1", "测试记忆内容", {"tag": "test"})

    assert isinstance(memory, Memory)
    assert memory.scope == MemoryScope.AGENT
    assert memory.scope_id == "a1"
    assert memory.content == "测试记忆内容"
    assert memory.metadata == {"tag": "test"}


async def test_sqlite_backend_search(db_repository: StorageRepository) -> None:
    """SQLite后端搜索记忆."""
    backend = SqliteMemoryBackend(db_repository)
    await backend.create("agent", "a1", "Python编程语言")
    await backend.create("agent", "a1", "Java虚拟机")
    await backend.create("agent", "a1", "Python数据分析")

    results = await backend.search("agent", "a1", "Python", limit=5)
    assert len(results) >= 1
    assert all("Python" in m.content for m in results)


async def test_sqlite_backend_list(db_repository: StorageRepository) -> None:
    """SQLite后端列出所有记忆."""
    backend = SqliteMemoryBackend(db_repository)
    await backend.create("team", "t1", "记忆A")
    await backend.create("team", "t1", "记忆B")
    await backend.create("team", "t2", "其他团队记忆")

    t1_list = await backend.list_all("team", "t1")
    assert len(t1_list) == 2

    t2_list = await backend.list_all("team", "t2")
    assert len(t2_list) == 1


async def test_sqlite_backend_get(db_repository: StorageRepository) -> None:
    """SQLite后端根据ID获取记忆."""
    backend = SqliteMemoryBackend(db_repository)
    created = await backend.create("agent", "a1", "获取测试")

    fetched = await backend.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.content == "获取测试"

    # 不存在的ID返回None
    assert await backend.get("nonexistent-id") is None


async def test_sqlite_backend_delete(db_repository: StorageRepository) -> None:
    """SQLite后端删除记忆."""
    backend = SqliteMemoryBackend(db_repository)
    created = await backend.create("agent", "a1", "待删除")

    result = await backend.delete(created.id)
    assert result is True

    # 删除后获取应返回None
    assert await backend.get(created.id) is None

    # 删除不存在的ID返回False
    assert await backend.delete("nonexistent-id") is False


async def test_sqlite_backend_implements_protocol(
    db_repository: StorageRepository,
) -> None:
    """SQLite后端应满足 MemoryBackend Protocol."""
    backend = SqliteMemoryBackend(db_repository)
    assert isinstance(backend, MemoryBackend)
