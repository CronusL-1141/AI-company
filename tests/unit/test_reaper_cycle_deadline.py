"""Reap cycle budget: one timeout layer at a time, never a timeout around a timeout.

The cycle used to run under ``wait_for(30s)`` while each step ran under its own
``wait_for(10s)``. When a step overran on a locked database, the step timeout
cancelled it, the cancelled query kept the aiosqlite thread busy, and the cycle
timeout cancelled the same task again while SQLAlchemy was invalidating the
connection: "Exception terminating connection", a never-finished terminate task,
and a pooled connection the garbage collector later found checked out. The cycle
budget is now the deadline of each part instead of an outer layer.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import sqlite3
import threading
import time

import pytest
import pytest_asyncio
from sqlalchemy import text

import aiteam.api.state_reaper as reaper_mod
from aiteam.api.event_bus import EventBus
from aiteam.api.state_reaper import StateReaper
from aiteam.storage.connection import get_engine, get_session
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture
async def reaper_on_file(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'reaper.sqlite'}"
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    try:
        yield StateReaper(repo, EventBus(repo=repo)), url, tmp_path / "reaper.sqlite"
    finally:
        await get_engine(url).dispose()


@pytest.mark.asyncio
async def test_steps_are_capped_by_the_cycle_deadline(reaper_on_file, monkeypatch):
    reaper, _, _ = reaper_on_file
    monkeypatch.setattr(reaper_mod, "REAPER_STEP_TIMEOUT", 5.0)
    ran: list[str] = []

    async def hangs():
        ran.append("hang")
        await asyncio.sleep(10)

    async def later():
        ran.append("later")

    loop = asyncio.get_running_loop()
    started = time.monotonic()
    await reaper._run_cycle_steps((("hangs", hangs), ("later", later)), deadline=loop.time() + 0.2)
    assert time.monotonic() - started < 1.0  # the deadline, not the 5s step budget
    assert ran == ["hang"]  # past the deadline the remaining steps are skipped


@pytest.mark.asyncio
async def test_cycle_reports_timeout_without_an_outer_wait_for(reaper_on_file, monkeypatch, caplog):
    reaper, _, _ = reaper_on_file
    monkeypatch.setattr(reaper_mod, "REAPER_CYCLE_TIMEOUT", 0.2)

    async def slow_list_teams():
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(reaper._repo, "list_teams", slow_list_teams)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="aiteam.api.state_reaper"):
        await reaper._reap_cycle()
    assert time.monotonic() - started < 1.0
    assert any("Reap cycle timed out" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_step_overrunning_on_a_locked_db_leaves_no_connection_behind(
    reaper_on_file, monkeypatch, caplog,
):
    reaper, url, database = reaper_on_file
    await asyncio.sleep(0)
    async with get_session(url) as session:
        await session.execute(text("CREATE TABLE probe (v TEXT)"))
    monkeypatch.setattr(reaper_mod, "REAPER_STEP_TIMEOUT", 0.5)

    def hold_lock():
        con = sqlite3.connect(database, isolation_level=None)
        con.execute("BEGIN IMMEDIATE")
        time.sleep(2.0)
        con.execute("COMMIT")
        con.close()

    async def write():
        async with get_session(url) as session:
            await session.execute(text("INSERT INTO probe VALUES ('x')"))

    locker = threading.Thread(target=hold_lock)
    locker.start()
    await asyncio.sleep(0.1)
    loop = asyncio.get_running_loop()
    with caplog.at_level(logging.WARNING, logger="sqlalchemy.pool"):
        await reaper._run_cycle_steps(
            (("w1", write), ("w2", write)), deadline=loop.time() + 1.0,
        )
        await asyncio.to_thread(locker.join)
        # The cancelled statement finishes on its worker thread once the lock is
        # free, then the connection is invalidated; give that a bounded while.
        deadline = time.monotonic() + 3.0
        while get_engine(url).pool.checkedout() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        gc.collect()
    pool_errors = [r.getMessage() for r in caplog.records if r.name.startswith("sqlalchemy.pool")]
    assert not pool_errors, pool_errors
    assert get_engine(url).pool.checkedout() == 0
