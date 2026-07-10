"""Tests for _sqlite_migrate() idempotency and correctness."""

import sqlite3
from pathlib import Path

from aiteam.storage.connection import COLUMNS_TO_ENSURE, _sqlite_migrate


def _create_legacy_db(path: str) -> None:
    """Create a SQLite DB with old schema (missing new columns)."""
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE meetings (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            status TEXT NOT NULL,
            participants JSON NOT NULL,
            created_at DATETIME NOT NULL
        )"""
    )
    con.execute(
        """CREATE TABLE meeting_messages (
            id TEXT PRIMARY KEY,
            meeting_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            content TEXT NOT NULL,
            round_number INTEGER NOT NULL,
            timestamp DATETIME NOT NULL
        )"""
    )
    con.commit()
    con.close()


def _column_names(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


class TestSqliteMigration:
    def test_adds_missing_columns(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.db")
        _create_legacy_db(db)
        _sqlite_migrate(db)
        con = sqlite3.connect(db)
        assert "meta_json" in _column_names(con, "meetings")
        assert "metadata_json" in _column_names(con, "meeting_messages")
        con.close()

    def test_idempotent_on_repeated_calls(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.db")
        _create_legacy_db(db)
        # Running twice must not raise
        _sqlite_migrate(db)
        _sqlite_migrate(db)
        con = sqlite3.connect(db)
        assert "meta_json" in _column_names(con, "meetings")
        assert "metadata_json" in _column_names(con, "meeting_messages")
        con.close()

    def test_no_error_when_columns_already_exist(self, tmp_path: Path) -> None:
        """Migration must succeed on a fully up-to-date schema (e.g. fresh install)."""
        db = str(tmp_path / "test.db")
        con = sqlite3.connect(db)
        con.execute(
            """CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                meta_json JSON DEFAULT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE meeting_messages (
                id TEXT PRIMARY KEY,
                metadata_json JSON DEFAULT NULL
            )"""
        )
        con.commit()
        con.close()
        _sqlite_migrate(db)  # must not raise

    def test_columns_to_ensure_covers_known_targets(self) -> None:
        pairs = {(t, c) for t, c, _ in COLUMNS_TO_ENSURE}
        assert ("meetings", "meta_json") in pairs
        assert ("meeting_messages", "metadata_json") in pairs

    def test_new_columns_default_null(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.db")
        _create_legacy_db(db)
        con_pre = sqlite3.connect(db)
        con_pre.execute(
            "INSERT INTO meetings VALUES (?,?,?,?,?,?)",
            ("m1", "t1", "topic", "active", "[]", "2026-01-01"),
        )
        con_pre.execute(
            "INSERT INTO meeting_messages VALUES (?,?,?,?,?,?,?)",
            ("mm1", "m1", "a1", "agent", "hi", 1, "2026-01-01"),
        )
        con_pre.commit()
        con_pre.close()

        _sqlite_migrate(db)

        con = sqlite3.connect(db)
        row_m = con.execute("SELECT meta_json FROM meetings WHERE id='m1'").fetchone()
        row_mm = con.execute(
            "SELECT metadata_json FROM meeting_messages WHERE id='mm1'"
        ).fetchone()
        assert row_m[0] is None
        assert row_mm[0] is None
        con.close()
