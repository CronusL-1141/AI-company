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


async def test_initial_ultrafast_pair_survives_cursor_restart(stores, tmp_path):
    from unit.services.test_codex_initial_settings import encoded, rows

    first, _, _, url = stores
    records = rows()
    path = source(tmp_path, encoded(records[:2]))
    namespace = source_namespace(tmp_path)
    before = scan_journal_file(path, namespace, None)
    assert before.cursor.context.initial_settings_turn_at is not None
    assert await first.commit_batch(before)
    await engine_pool.get_engine(url).dispose()
    restarted = CodexUsageJournalRepository(url)
    await restarted.init_db()
    cursor = await restarted.get_cursor(before.cursor.source_id)
    with path.open("ab") as stream:
        stream.write(encoded(records[2:]))
    after = scan_journal_file(path, namespace, cursor)
    assert await restarted.commit_batch(after)
    assert await restarted.commit_batch(scan_journal_file(path, namespace, after.cursor))
    facts = await restarted.list_observations()
    assert len(facts) == 1
    assert (facts[0].service_tier, facts[0].service_tier_source) == ("ultrafast", "thread_settings")
    assert facts[0].usage.input_tokens == 100
    assert after.cursor.context.initial_settings_turn_at is None


async def test_facts_are_numbered_in_recording_order_with_their_lookup_keys(stores, tmp_path):
    first, second, path_db, _ = stores
    path = source(tmp_path, encode(ledger("one"), ledger("two")))
    namespace = source_namespace(tmp_path)
    batch = scan_journal_file(path, namespace, None)
    assert await first.commit_batch(batch)
    copy = tmp_path / "sessions" / "copy.jsonl"
    copy.write_bytes(path.read_bytes() + encode(ledger("three")))
    assert await second.commit_batch(scan_journal_file(copy, namespace, None))  # same facts keep their numbers
    assert await second.recorded_through() == 3
    with sqlite3.connect(f"file:{path_db}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT seq, kind, response_id, session_id, occurred_at FROM codex_usage_observations ORDER BY seq",
        ).fetchall()
    assert rows == [
        (1, "ledger", "one", None, "2026-09-15 03:00:00.000000"),
        (2, "ledger", "two", None, "2026-09-15 03:00:00.000000"),
        (3, "ledger", "three", None, "2026-09-15 03:00:00.000000"),
    ]


async def test_upgrade_fills_the_lookup_columns_of_existing_facts_once(tmp_path):
    import json

    from aiteam.storage.connection import init_db

    path = tmp_path / "upgraded.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE codex_usage_observations (observation_id VARCHAR(64) NOT NULL, "
            "source_namespace VARCHAR(64) NOT NULL, source_id VARCHAR(64) NOT NULL, saved_at DATETIME NOT NULL, "
            "payload JSON NOT NULL, PRIMARY KEY (observation_id));"
        )
        facts = [
            ("b", "2026-11-01 00:00:02.000000", {"kind": "ledger", "occurred_at": "2026-11-01T00:00:01Z",
                                                 "response_id": "r1", "session_id": "s"}),
            ("a", "2026-11-01 00:00:01.000000", {"kind": "legacy", "occurred_at": "2026-11-01T00:00:00.500000Z",
                                                 "session_id": "s"}),
            ("c", "2026-11-01 00:00:02.000000", {"kind": "ledger", "response_id": "r2",
                                                 "occurred_at": "2026-11-01T08:00:00.250000+08:00"}),
            ("d", "2026-11-01 00:00:03.000000", {"kind": "diagnostic"}),
        ]
        connection.executemany("INSERT INTO codex_usage_observations VALUES (?, 'n', 's', ?, ?)", [
            (identifier * 64, saved, json.dumps(payload)) for identifier, saved, payload in facts
        ])
    url = f"sqlite+aiosqlite:///{path}"
    try:
        await init_db(url)
        repository = CodexUsageJournalRepository(url)
        assert await repository.recorded_through() == 4
        await init_db(url)  # a second start changes nothing
    finally:
        await engine_pool.get_engine(url).dispose()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT substr(observation_id, 1, 1), seq, kind, response_id, session_id, occurred_at "
            "FROM codex_usage_observations ORDER BY seq",
        ).fetchall()
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='codex_usage_observations'",
        )}
    assert rows == [
        ("a", 1, "legacy", None, "s", "2026-11-01 00:00:00.500000"),
        ("b", 2, "ledger", "r1", "s", "2026-11-01 00:00:01.000000"),
        ("c", 3, "ledger", "r2", None, "2026-11-01 00:00:00.250000"),  # the offset converted exactly
        ("d", 4, "diagnostic", None, None, None),
    ]
    assert {"ix_codex_usage_observations_seq", "ix_codex_usage_observations_occurred_at",
            "ix_codex_usage_observations_response_id", "ix_codex_usage_observations_session_kind_seq",
            "ix_codex_usage_observations_source_kind_seq"} <= indexes
