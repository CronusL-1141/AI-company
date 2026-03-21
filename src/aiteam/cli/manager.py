"""AI Team OS CLI — TeamManager factory function.

Provides a lazily-initialized TeamManager instance for all CLI commands.
"""

from __future__ import annotations

import asyncio
from typing import Any

_manager_instance: Any = None


def run_async(coro: Any) -> Any:
    """Run an async coroutine in a synchronous CLI environment."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Event loop already running (e.g. Jupyter); use nest_asyncio or new thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def get_manager() -> Any:
    """Get TeamManager instance (lazy initialization).

    Internally creates StorageRepository + MemoryStore + TeamManager.
    Wrapped in try/except since these modules may not be implemented yet.

    Returns:
        TeamManager instance

    Raises:
        RuntimeError: If dependency modules are not yet implemented
    """
    global _manager_instance  # noqa: PLW0603

    if _manager_instance is not None:
        return _manager_instance

    try:
        from aiteam.memory.store import MemoryStore
        from aiteam.orchestrator.team_manager import TeamManager
        from aiteam.storage.repository import StorageRepository

        repo = StorageRepository()
        run_async(repo.init_db())
        memory = MemoryStore(repository=repo)
        _manager_instance = TeamManager(repository=repo, memory=memory)
        return _manager_instance

    except ImportError as e:
        raise RuntimeError(
            f"依赖模块尚未实现: {e}。"
            "请确保 storage、memory、orchestrator 模块已正确安装。"
        ) from e
