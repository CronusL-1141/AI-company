"""Native EOF observation never consumes protocol data or times out idle pipes."""

from __future__ import annotations

import os
import select
from contextlib import ExitStack
from unittest.mock import AsyncMock

import anyio
import pytest

from aiteam.mcp import _stdio_lifecycle as lifecycle


@pytest.fixture
def pipe():
    reader, writer = os.pipe()
    with ExitStack() as cleanup:
        incoming = cleanup.enter_context(os.fdopen(reader, "rb", buffering=0))
        outgoing = cleanup.enter_context(os.fdopen(writer, "wb", buffering=0))
        yield incoming, outgoing


@pytest.fixture
def watchers(monkeypatch):
    if os.name != "posix" or not (hasattr(select, "kqueue") or hasattr(select, "epoll")):
        pytest.skip("Native POSIX pipe EOF observation")
    opened = []
    original = lifecycle._pipe_eof_watcher

    def observe(fd):
        result = original(fd)
        assert result is not None
        opened.append(result[0])
        return result

    monkeypatch.setattr(lifecycle, "_pipe_eof_watcher", observe)
    yield opened
    assert opened
    for watcher in opened:
        with pytest.raises((OSError, ValueError)):
            watcher.fileno()


@pytest.mark.asyncio
@pytest.mark.parametrize("buffered", [False, True])
async def test_real_pipe_eof_cancels_scope_and_closes_watcher(pipe, watchers, buffered):
    incoming, outgoing = pipe
    if buffered:
        outgoing.write(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
    with anyio.fail_after(2):
        async with lifecycle._cancel_on_pipe_eof(incoming.fileno()):
            outgoing.close()
            await anyio.sleep_forever()
            pytest.fail("EOF must cancel the server scope")
    assert os.fstat(incoming.fileno())


@pytest.mark.asyncio
async def test_open_idle_pipe_and_buffered_input_are_not_cancelled(pipe, watchers):
    incoming, outgoing = pipe
    async with lifecycle._cancel_on_pipe_eof(incoming.fileno()):
        await anyio.sleep(0.05)
        outgoing.write(b"pending protocol bytes\n")
        await anyio.sleep(0.05)
        assert incoming.read(23) == b"pending protocol bytes\n"


@pytest.mark.asyncio
async def test_server_failure_still_closes_watcher(pipe, watchers):
    incoming, _ = pipe
    with pytest.raises(RuntimeError, match="injected server failure"):
        async with lifecycle._cancel_on_pipe_eof(incoming.fileno()):
            raise RuntimeError("injected server failure")


@pytest.mark.asyncio
async def test_non_pipe_stdin_retains_public_server_call(monkeypatch):
    server = AsyncMock()
    with open(os.devnull) as stdin:
        monkeypatch.setattr(lifecycle.sys, "stdin", stdin)
        await lifecycle.run_stdio_server(server)
    server.run_async.assert_awaited_once_with(transport="stdio")


def test_windows_retains_sdk_behavior(pipe, monkeypatch):
    incoming, _ = pipe
    monkeypatch.setattr(lifecycle.os, "name", "nt")
    assert lifecycle._pipe_eof_watcher(incoming.fileno()) is None
