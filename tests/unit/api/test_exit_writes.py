"""Exit writes past the budget are left running, and a late failure is still retrieved."""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest

from aiteam.api import exit_writes


@pytest.mark.asyncio
async def test_slow_write_is_left_behind_and_its_late_failure_is_logged(caplog):
    release = asyncio.Event()

    async def stuck_then_fails():
        await release.wait()
        raise RuntimeError("database is locked")

    async def quick():
        return None

    loop = asyncio.get_running_loop()
    unretrieved: list[dict] = []
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
    try:
        started = loop.time()
        left = await exit_writes.write_or_abandon(
            {"slow": stuck_then_fails(), "quick": quick()}, timeout=0.1,
        )
        assert left == ["slow"]
        assert loop.time() - started < 0.5
        with caplog.at_level(logging.DEBUG, logger="aiteam.api.exit_writes"):
            release.set()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not exit_writes._abandoned
            gc.collect()
        assert any("failed later" in r.getMessage() for r in caplog.records)
        assert unretrieved == []  # no "Task exception was never retrieved"
    finally:
        loop.set_exception_handler(None)
