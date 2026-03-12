"""AI Team OS — 记忆后端单元测试."""

from __future__ import annotations

import pytest

from aiteam.memory.backends import MemoryBackend
from aiteam.memory.backends.resilient import ResilientMemoryBackend
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


# ================================================================
# Mem0MemoryBackend
# ================================================================


async def test_mem0_backend_import_error() -> None:
    """未安装 mem0 时应抛出友好的 ImportError."""
    # mem0 在测试环境中通常未安装，直接测试导入失败
    try:
        from aiteam.memory.backends.mem0_backend import Mem0MemoryBackend

        Mem0MemoryBackend()
        # 如果 mem0 恰好已安装，跳过此测试
        pytest.skip("mem0ai 已安装，无法测试导入失败场景")
    except ImportError as exc:
        assert "mem0ai" in str(exc) or "mem0" in str(exc)


# ================================================================
# ResilientMemoryBackend
# ================================================================


class _FailingBackend:
    """总是抛出异常的假后端，用于测试降级."""

    async def create(
        self, scope: str, scope_id: str, content: str, metadata: dict | None = None
    ) -> Memory:
        raise ConnectionError("primary 不可用")

    async def search(
        self, scope: str, scope_id: str, query: str, limit: int = 5
    ) -> list[Memory]:
        raise ConnectionError("primary 不可用")

    async def list_all(self, scope: str, scope_id: str) -> list[Memory]:
        raise ConnectionError("primary 不可用")

    async def get(self, memory_id: str) -> Memory | None:
        raise ConnectionError("primary 不可用")

    async def delete(self, memory_id: str) -> bool:
        raise ConnectionError("primary 不可用")


class _CountingBackend:
    """计数后端，可选择性失败，用于测试 circuit breaker."""

    def __init__(self, fail_until: int = 0) -> None:
        self.call_count = 0
        self._fail_until = fail_until

    async def create(
        self, scope: str, scope_id: str, content: str, metadata: dict | None = None
    ) -> Memory:
        self.call_count += 1
        if self.call_count <= self._fail_until:
            raise ConnectionError(f"失败 #{self.call_count}")
        return Memory(
            scope=MemoryScope(scope), scope_id=scope_id, content=content, metadata=metadata or {}
        )

    async def search(
        self, scope: str, scope_id: str, query: str, limit: int = 5
    ) -> list[Memory]:
        self.call_count += 1
        if self.call_count <= self._fail_until:
            raise ConnectionError(f"失败 #{self.call_count}")
        return []

    async def list_all(self, scope: str, scope_id: str) -> list[Memory]:
        self.call_count += 1
        if self.call_count <= self._fail_until:
            raise ConnectionError(f"失败 #{self.call_count}")
        return []

    async def get(self, memory_id: str) -> Memory | None:
        self.call_count += 1
        if self.call_count <= self._fail_until:
            raise ConnectionError(f"失败 #{self.call_count}")
        return None

    async def delete(self, memory_id: str) -> bool:
        self.call_count += 1
        if self.call_count <= self._fail_until:
            raise ConnectionError(f"失败 #{self.call_count}")
        return True


async def test_resilient_backend_fallback(
    db_repository: StorageRepository,
) -> None:
    """primary 失败时应降级到 fallback."""
    failing_primary = _FailingBackend()
    fallback = SqliteMemoryBackend(db_repository)

    resilient = ResilientMemoryBackend(
        primary=failing_primary, fallback=fallback, threshold=1
    )

    # primary 会失败，应该降级到 fallback（SQLite）
    memory = await resilient.create("agent", "a1", "降级测试")
    assert memory.content == "降级测试"

    # search 也应该降级
    results = await resilient.search("agent", "a1", "降级", limit=5)
    assert isinstance(results, list)


async def test_resilient_backend_circuit_breaker(
    db_repository: StorageRepository,
) -> None:
    """连续失败达到阈值后应触发熔断."""
    failing_primary = _FailingBackend()
    fallback = SqliteMemoryBackend(db_repository)

    resilient = ResilientMemoryBackend(
        primary=failing_primary, fallback=fallback, threshold=3
    )

    # 前3次调用会尝试 primary（均失败），触发熔断
    for i in range(3):
        await resilient.create("agent", "a1", f"测试{i}")

    # 熔断后 _circuit_open 应为 True
    assert resilient._circuit_open is True

    # 后续调用直接走 fallback，不再尝试 primary（除了 probe）
    memory = await resilient.create("agent", "a1", "熔断后创建")
    assert memory.content == "熔断后创建"


async def test_resilient_backend_recovery() -> None:
    """primary 恢复后应自动切回."""
    # primary 前3次失败，之后恢复
    primary = _CountingBackend(fail_until=3)
    fallback = _CountingBackend()

    resilient = ResilientMemoryBackend(
        primary=primary, fallback=fallback, threshold=3
    )
    # 设置较小的 probe 间隔便于测试
    resilient._probe_interval = 2

    # 前3次调用尝试 primary 均失败，触发熔断
    for i in range(3):
        await resilient.create("agent", "a1", f"测试{i}")

    assert resilient._circuit_open is True
    assert fallback.call_count == 3  # fallback 被调用了3次

    # 第4次调用：熔断中，不是 probe 时机，走 fallback
    await resilient.create("agent", "a1", "第4次")
    assert fallback.call_count == 4

    # 第5次调用：probe 间隔到了（call_count_since_open=2），尝试 primary
    # primary 此时 call_count=3 已超过 fail_until=3，应成功
    await resilient.create("agent", "a1", "第5次恢复")

    # primary 恢复成功，熔断应关闭
    assert resilient._circuit_open is False

    # 后续调用应直接走 primary
    await resilient.create("agent", "a1", "第6次正常")
    # primary: 3次失败 + 第5次probe成功 + 第6次正常 = 5次
    assert primary.call_count == 5
