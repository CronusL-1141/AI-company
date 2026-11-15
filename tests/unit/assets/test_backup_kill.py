"""A backup killed half way leaves no version without its content, and finishes without duplicates.

A child process runs the first backup of a large folder in batches. It stops (and
is SIGKILLed) at the worst moment the order of work allows: a batch's contents are
in the store and its rows are not yet written. Afterwards every version row must
name a content that is there and matches its hash; the next run in a fresh process
must record each file exactly once.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from aiteam.assets import ingest
from aiteam.assets.blob_store import BlobStore
from aiteam.assets.os_manifest import source_root
from aiteam.assets.service import AssetBackup, project_roots
from aiteam.storage.assets import AssetRepository
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository
from aiteam.types import AssetHost, AssetRootStatus

from .conftest import MACHINE

FILES = 240
BATCH = 40
KILL_BEFORE_BATCH = 3  # the third batch's contents are stored, its rows never written

CHILD = textwrap.dedent("""
    import asyncio, os, sys
    from pathlib import Path
    from aiteam.assets import ingest
    from aiteam.assets.machine import MachineKey
    from aiteam.assets.service import AssetBackup, project_roots
    from aiteam.storage.assets import AssetRepository
    from aiteam.storage.repository import StorageRepository
    from aiteam.types import AssetHost, AssetRootStatus

    folder, data_dir, batch, stop_before = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    machine = MachineKey(key=sys.argv[5])
    ingest.BATCH_FILES = ingest.RECORD_CHUNK = batch  # one transaction per batch
    real_record = AssetRepository.record
    calls = 0

    async def record(self, **kwargs):
        global calls
        calls += 1
        if calls == stop_before:
            print("held", flush=True)
            await asyncio.Event().wait()  # killed here
        result = await real_record(self, **kwargs)
        print("committed", flush=True)
        return result

    AssetRepository.record = record

    async def main():
        url = f"sqlite+aiosqlite:///{data_dir / 'aiteam.db'}"
        repo = StorageRepository(db_url=url)
        await repo.init_db()
        backup = AssetBackup(repo=AssetRepository(url), projects=lambda: project_roots(repo), data_dir=data_dir,
                             machine=lambda: machine)
        await backup.start()
        await backup.enroll(host=AssetHost.CLAUDE, path=str(folder), profile="rec",
                            status=AssetRootStatus.ENROLLED, categories=None)
        await backup.drain()

    asyncio.run(main())
""")


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "config"
    memory = folder / "projects" / "-work-big" / "memory"
    memory.mkdir(parents=True)
    for n in range(FILES):
        (memory / f"note-{n:03d}.md").write_text(f"note {n}\n" + "line\n" * (n % 50), encoding="utf-8")
    return folder


def _versions(database: Path) -> list[tuple[str, str | None]]:
    connection = sqlite3.connect(database)
    try:
        return connection.execute("SELECT item_id, content_hash FROM asset_versions").fetchall()
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_killed_first_backup_resumes_without_holes_or_duplicates(tmp_path):
    folder, data_dir = _folder(tmp_path), tmp_path / "data"
    data_dir.mkdir()
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(source_root() / "src")}
    child = subprocess.Popen([sys.executable, str(script), str(folder), str(data_dir), str(BATCH),
                              str(KILL_BEFORE_BATCH), MACHINE.key], stdout=subprocess.PIPE, text=True, env=env)
    try:
        lines = [await asyncio.to_thread(child.stdout.readline) for _ in range(KILL_BEFORE_BATCH)]
        assert lines == ["committed\n"] * (KILL_BEFORE_BATCH - 1) + ["held\n"], lines
        child.send_signal(signal.SIGKILL)
    finally:
        child.wait(30)
        child.stdout.close()
    database = data_dir / "aiteam.db"
    store = BlobStore(data_dir)
    partial = _versions(database)
    assert len(partial) == BATCH * (KILL_BEFORE_BATCH - 1)
    for _, content_hash in partial:
        store.get(content_hash)  # present and intact
    orphans = sum(1 for path in store.root.rglob("*") if path.is_file()) - len(partial)
    assert orphans == BATCH  # the held batch's contents: harmless, reused below

    url = f"sqlite+aiosqlite:///{database}"
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    backup = AssetBackup(repo=AssetRepository(url), projects=lambda: project_roots(repo), data_dir=data_dir,
                         machine=lambda: MACHINE)
    try:
        await backup.start()
        [root] = backup.enrolled
        summary = await backup.run_now(root.id, timeout=60)
        assert summary is not None and not summary.error
        done = _versions(database)
        assert len(done) == FILES and len({item for item, _ in done}) == FILES
        for _, content_hash in done:
            store.get(content_hash)
        assert sum(1 for path in store.root.rglob("*") if path.is_file()) == FILES
    finally:
        await backup.drain(10)
        await get_engine(url).dispose()


@pytest.mark.asyncio
async def test_a_large_batch_runs_in_a_child_process_with_the_same_outcome(tmp_path, monkeypatch):
    folder, data_dir = _folder(tmp_path), tmp_path / "data"
    data_dir.mkdir()
    calls = []
    real_child = ingest.run_child

    async def counted(job):
        calls.append(len(job.files))
        return await real_child(job)

    monkeypatch.setattr(ingest, "run_child", counted)
    url = f"sqlite+aiosqlite:///{data_dir / 'aiteam.db'}"
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    backup = AssetBackup(repo=AssetRepository(url), projects=lambda: project_roots(repo), data_dir=data_dir,
                         machine=lambda: MACHINE)
    try:
        await backup.start()
        root = await backup.enroll(host=AssetHost.CLAUDE, path=str(folder), profile="rec",
                                   status=AssetRootStatus.ENROLLED, categories=None)
        await backup.drain(60)
        assert calls and sum(calls) == FILES
        assert len(_versions(data_dir / "aiteam.db")) == FILES
        assert (await backup.run_now(root.id, timeout=60)).versions == 0
    finally:
        await backup.drain(10)
        await get_engine(url).dispose()
