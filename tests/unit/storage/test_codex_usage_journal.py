"""Cross-connection commits and deletion prove durable usage storage."""

import asyncio
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from aiteam.services.codex_usage_journal import scan_journal_file, source_namespace
from aiteam.storage.codex_usage_journal import CodexUsageJournalRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.models import CodexUsageSourceCursorModel
from unit.services.test_codex_usage_journal import encode, ledger, source


@pytest.fixture
async def stores(tmp_path):
    db_path = tmp_path / "journal.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    first, second = CodexUsageJournalRepository(url), CodexUsageJournalRepository(url)
    await asyncio.gather(first.init_db(), second.init_db())
    yield first, second, db_path, url
    await engine_pool.get_engine(url).dispose()


@pytest.mark.parametrize("url", [None, "", " ", "postgresql+asyncpg://localhost/test"])
def test_requires_explicit_sqlite(url):
    with pytest.raises(ValueError):
        CodexUsageJournalRepository(url)


async def test_init_creates_only_two_tables(stores):
    _, _, path, _ = stores
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"codex_usage_observations", "codex_usage_source_cursors"}


async def test_stale_cursor_cannot_save_any_facts_and_rescan_does_not_lose_append(stores, tmp_path):
    first, second, _, _ = stores
    path = source(tmp_path, encode(ledger("first")))
    namespace = source_namespace(tmp_path)
    old = scan_journal_file(path, namespace, None)
    path.write_bytes(path.read_bytes() + encode(ledger("second")))
    newer = scan_journal_file(path, namespace, None)
    assert await first.commit_batch(old)
    assert not await second.commit_batch(newer)
    assert await second.count_observations() == 1
    cursor = await second.get_cursor(old.cursor.source_id)
    assert cursor.offset == old.cursor.offset
    assert await second.commit_batch(scan_journal_file(path, namespace, cursor))
    assert {row.response_id for row in await first.list_observations()} == {"first", "second"}


async def test_cursor_failure_rolls_back_fact_and_cursor_together(stores, tmp_path, monkeypatch):
    first, second, _, _ = stores
    path = source(tmp_path, encode(ledger()))
    batch = scan_journal_file(path, source_namespace(tmp_path), None)
    original = AsyncSession.flush

    async def fail_cursor_flush(session, *args, **kwargs):
        if any(isinstance(row, CodexUsageSourceCursorModel) for row in session.new):
            raise RuntimeError("synthetic precommit failure")
        return await original(session, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(AsyncSession, "flush", fail_cursor_flush)
        with pytest.raises(RuntimeError, match="synthetic"):
            await first.commit_batch(batch)
    assert await second.count_observations() == 0
    assert await second.get_cursor(batch.cursor.source_id) is None
    assert await second.commit_batch(batch)
    assert await first.count_observations() == 1


async def test_archive_copy_truncation_and_source_deletion_survive_new_connection(stores, tmp_path):
    first, second, _, url = stores
    namespace = source_namespace(tmp_path)
    path = source(tmp_path, encode(ledger("same-native-id")))
    original = scan_journal_file(path, namespace, None)
    assert await first.commit_batch(original)
    archive = tmp_path / "archived_sessions"
    archive.mkdir()
    moved = archive / path.name
    path.rename(moved)
    assert await second.commit_batch(scan_journal_file(moved, namespace, original.cursor))
    copied = path.parent / "copy.jsonl"
    copied.write_bytes(moved.read_bytes())
    assert await second.commit_batch(scan_journal_file(copied, namespace, None))
    assert await first.count_observations() == 1
    current = await first.get_cursor(original.cursor.source_id)
    moved.write_bytes(encode(ledger("same-native-id", usage={"input_tokens": 999})))
    assert await first.commit_batch(scan_journal_file(moved, namespace, current))
    moved.unlink()
    copied.unlink()
    await engine_pool.get_engine(url).dispose()
    restarted = CodexUsageJournalRepository(url)
    await restarted.init_db()
    records = await restarted.list_observations()
    assert len(records) == 2
    assert {record.usage.input_tokens for record in records} == {123, 999}
    assert {record.generation for record in records} == {0, 1}
    assert len(await restarted.list_cursors()) == 2


async def test_storage_revalidates_extra_fields_before_any_write(stores, tmp_path):
    first, second, _, _ = stores
    path = source(tmp_path, encode(ledger()))
    batch = scan_journal_file(path, source_namespace(tmp_path), None)
    batch.observations[0].__dict__["message"] = "PRIVATE"
    # Validate an explicit unknown field at the public schema boundary as well.
    payload = batch.model_dump()
    payload["observations"][0]["message"] = "PRIVATE"
    with pytest.raises(ValueError):
        type(batch).model_validate(payload)
    batch.observations[0].usage.input_tokens = -1
    with pytest.raises(ValueError):
        await first.commit_batch(batch)
    assert await second.count_observations() == 0
