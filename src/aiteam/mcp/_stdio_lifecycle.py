"""Cancel this stdio server when its POSIX input pipe loses every writer."""

from __future__ import annotations

import asyncio
import logging
import os
import select
import stat
import sys
from contextlib import asynccontextmanager

import anyio

logger = logging.getLogger(__name__)


def _pipe_eof_watcher(fd: int):
    """Observe pipe hangup without reading or competing with the MCP transport."""
    if os.name != "posix" or not stat.S_ISFIFO(os.fstat(fd).st_mode):
        return None
    if hasattr(select, "kqueue"):
        watcher = select.kqueue()
        try:
            watcher.control([
                select.kevent(fd, filter=select.KQ_FILTER_READ,
                              flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR),
            ], 0, 0)
        except BaseException:
            watcher.close()
            raise

        def has_eof():
            return any(event.flags & select.KQ_EV_EOF for event in watcher.control(None, 1, 0))

    elif hasattr(select, "epoll"):
        watcher = select.epoll()
        try:
            watcher.register(fd, select.EPOLLHUP)
        except BaseException:
            watcher.close()
            raise

        def has_eof():
            return any(events & select.EPOLLHUP for _, events in watcher.poll(0, 1))

    else:
        return None
    return watcher, has_eof


@asynccontextmanager
async def _cancel_on_pipe_eof(fd: int | None):
    """Keep normal idle connections open; unsupported inputs retain SDK behavior."""
    try:
        monitor = _pipe_eof_watcher(fd) if fd is not None else None
    except (OSError, ValueError):
        logger.debug("Stdio pipe EOF observation unavailable", exc_info=True)
        monitor = None
    if monitor is None:
        yield
        return

    watcher, has_eof = monitor
    loop = asyncio.get_running_loop()
    watcher_fd = watcher.fileno()
    registered = False
    try:
        with anyio.CancelScope() as scope:
            def check_eof():
                if has_eof():
                    loop.remove_reader(watcher_fd)
                    # FastMCP 3.4.5 waits for handlers after EOF in its overridden
                    # LowLevelServer.run. Cancel our scope to release those waits.
                    scope.cancel()

            try:
                loop.add_reader(watcher_fd, check_eof)
                registered = True
            except (OSError, NotImplementedError):
                logger.debug("Event loop cannot observe stdio pipe EOF", exc_info=True)
            yield
    finally:
        if registered:
            loop.remove_reader(watcher_fd)
        watcher.close()


async def run_stdio_server(server) -> None:
    """Run through FastMCP's public API with connection-scoped EOF cleanup."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    async with _cancel_on_pipe_eof(fd):
        await server.run_async(transport="stdio")
