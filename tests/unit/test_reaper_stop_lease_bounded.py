"""StateReaper.stop (the SIGTERM / lifespan exit path) must not wait out a locked database.

stop() hands the governance lease back so a new instance can govern at once. That
is a write; with another process holding the SQLite write lock it used to wait for
the lock (up to the 30s busy timeout). A cancel-based timeout cannot help: the
cancel's cleanup waits for the lock too, and on this path asyncio.run cancels any
task left behind at loop teardown, which waits the same way. So the release is
bounded at the lock wait itself, and the lease's TTL covers a release that gives up.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
import pytest_asyncio

from aiteam.api.event_bus import EventBus
from aiteam.api.exit_writes import EXIT_WRITE_BUDGET_SECONDS
from aiteam.api.state_reaper import StateReaper
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

LOCK_HOLD = 6.0


@pytest_asyncio.fixture
async def reaper_on_file(tmp_path):
    database = tmp_path / "lease.sqlite"
    url = f"sqlite+aiosqlite:///{database}"
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    try:
        yield StateReaper(repo, EventBus(repo=repo)), repo, database
    finally:
        await get_engine(url).dispose()


def _holder(database) -> str:
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return con.execute("SELECT holder FROM governance_lease WHERE id = 'governance'").fetchone()[0]
    finally:
        con.close()


@pytest.mark.asyncio
async def test_stop_gives_up_the_lease_release_on_a_locked_database_in_time(reaper_on_file):
    reaper, repo, database = reaper_on_file
    assert await repo.try_acquire_governance_lease(reaper._lease_holder, ttl_seconds=180)
    locked = threading.Event()

    def hold_lock():
        con = sqlite3.connect(database, isolation_level=None)
        con.execute("BEGIN IMMEDIATE")
        locked.set()
        time.sleep(LOCK_HOLD)
        con.execute("ROLLBACK")
        con.close()

    locker = threading.Thread(target=hold_lock)
    locker.start()
    await asyncio.to_thread(locked.wait)
    started = time.monotonic()
    await reaper.stop()
    elapsed = time.monotonic() - started
    await asyncio.to_thread(locker.join)
    print(f"reaper stop under a held lock: {elapsed:.2f}s")
    assert elapsed < EXIT_WRITE_BUDGET_SECONDS + 1.0, elapsed
    assert _holder(database) == reaper._lease_holder  # not released: the TTL covers it


@pytest.mark.asyncio
async def test_stop_still_releases_the_lease_when_the_database_is_free(reaper_on_file):
    reaper, repo, database = reaper_on_file
    assert await repo.try_acquire_governance_lease(reaper._lease_holder, ttl_seconds=180)
    await reaper.stop()
    assert _holder(database) == ""


@pytest.mark.asyncio
async def test_bounded_release_on_an_in_memory_database_uses_the_normal_path():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    assert await repo.try_acquire_governance_lease("api-x", ttl_seconds=180)
    assert await repo.release_governance_lease("api-x", lock_wait=EXIT_WRITE_BUDGET_SECONDS)


@pytest.mark.parametrize(("url", "expected"), [
    ("sqlite+aiosqlite:////abs/dir with space/a.db", "/abs/dir with space/a.db"),
    ("sqlite+aiosqlite:///relative.db", "relative.db"),
    ("sqlite+aiosqlite:////abs/a.db?check_same_thread=false", "/abs/a.db"),
    ("sqlite+aiosqlite://", None),
    ("sqlite+aiosqlite:///:memory:", None),
    # The engine opens a file: URI as a URI; a plain sqlite3.connect would create a
    # file literally named "file:..." and the release would hit nothing.
    ("sqlite+aiosqlite:///file:/abs/a.db?mode=rw&uri=true", None),
    ("postgresql+asyncpg://user@host/db", None),
], ids=["absolute", "relative", "query", "memory-bare", "memory", "file-uri", "not-sqlite"])
def test_direct_release_only_takes_urls_a_plain_connect_opens_the_same_way(url, expected):
    assert StorageRepository(db_url=url)._sqlite_file_path() == expected
