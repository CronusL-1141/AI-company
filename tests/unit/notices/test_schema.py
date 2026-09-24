"""Notice tables: every non-key ORM column is migratable and the claim index is re-asserted."""

from __future__ import annotations

import sqlite3

import pytest

from aiteam.storage.connection import COLUMNS_TO_ENSURE, _sqlite_migrate
from aiteam.storage.models import NoticeDeliveryModel, NoticeModel


@pytest.mark.parametrize("model", [NoticeModel, NoticeDeliveryModel], ids=["notices", "notice_deliveries"])
def test_every_non_key_column_is_registered(model):
    table = model.__table__
    registered = {column for name, column, _ in COLUMNS_TO_ENSURE if name == table.name}
    expected = {column.name for column in table.columns if not column.primary_key}
    assert expected == registered


def test_migration_repairs_an_early_shape_of_the_tables(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE notices (key VARCHAR(512) PRIMARY KEY, catalog_id VARCHAR(64) NOT NULL)")
    con.execute("CREATE TABLE notice_deliveries (id VARCHAR(36) PRIMARY KEY, key VARCHAR(512) NOT NULL, "
                "host VARCHAR(16) NOT NULL, session_id VARCHAR(256) NOT NULL)")
    con.execute("INSERT INTO notices (key, catalog_id) VALUES ('api_down', 'api_down')")
    con.commit()
    con.close()

    _sqlite_migrate(path)

    con = sqlite3.connect(path)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(notices)")}
        assert {"status", "first_seen_at", "last_seen_at", "params"} <= columns
        assert con.execute("SELECT status FROM notices").fetchone() == ("active",)
        indexes = {row[1]: row[2] for row in con.execute("PRAGMA index_list(notice_deliveries)")}
        assert indexes.get("uq_notice_deliveries_claim") == 1
        con.execute("INSERT INTO notice_deliveries (id, key, host, session_id) VALUES ('a', 'k', 'cc', 's')")
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO notice_deliveries (id, key, host, session_id) VALUES ('b', 'k', 'cc', 's')")
    finally:
        con.close()


def test_duplicate_rows_are_kept_and_the_index_skipped(tmp_path):
    path = str(tmp_path / "dup.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE notice_deliveries (id VARCHAR(36) PRIMARY KEY, key VARCHAR(512) NOT NULL, "
                "host VARCHAR(16) NOT NULL, session_id VARCHAR(256) NOT NULL)")
    con.executemany("INSERT INTO notice_deliveries (id, key, host, session_id) VALUES (?, 'k', 'cc', 's')",
                    [("a",), ("b",)])
    con.commit()
    con.close()
    _sqlite_migrate(path)  # must not raise, must not delete observation rows
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM notice_deliveries").fetchone() == (2,)
    finally:
        con.close()
