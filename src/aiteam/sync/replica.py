"""Rows of the shared face as they travel: read on the center, written into an edge's replica.

A row travels as its stored columns (``SELECT *``: text, numbers, NULL; JSON
columns as their text), keyed like its change-log entries (aiteam.sync.tables
``sync_key``). Writing one into the replica makes the local row equal to the
center's: an upsert under the row's sync key, after removing any local row that
holds one of its other unique values (a row staged here that the center created
under another id). Columns that only number rows on this node (the integer id of
knowledge_links) and columns that record this node's own use (LOCAL_COLUMNS) keep
their local values. Deletions remove the row.

``Replica`` works on a synchronous SQLAlchemy connection inside the caller's
transaction; the edge's applier uses it with change capture suppressed.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.engine import Connection

from aiteam.sync.tables import LOCAL_COLUMNS, SHARED_TABLES, sync_key


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class TableShape:
    """What applying rows to one table needs to know about it."""

    def __init__(self, conn: Connection, table: str) -> None:
        info = conn.exec_driver_sql(f"PRAGMA table_info({_quote(table)})").all()
        self.table = table
        self.columns = [row[1] for row in info]
        primary = tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5])
        self.key = sync_key(table, primary)
        # A primary key that is not the sync key numbers rows per node: never copied.
        self.node_local = frozenset(primary) - frozenset(self.key) if self.key != primary else frozenset()
        self.unique = [cols for cols in _unique_indexes(conn, table) if tuple(cols) != self.key]

    def key_values(self, pk: str) -> list[Any]:
        if len(self.key) == 1:
            return [pk]
        values = json.loads(pk)
        if not isinstance(values, list) or len(values) != len(self.key):
            raise ValueError(f"{self.table}: key {pk!r} does not match {self.key}")
        return values

    def where_key(self) -> str:
        return " AND ".join(f"{_quote(c)} = ?" for c in self.key)


def _unique_indexes(conn: Connection, table: str) -> list[list[str]]:
    unique = []
    for row in conn.exec_driver_sql(f"PRAGMA index_list({_quote(table)})").all():
        if row[2] and row[3] != "pk":
            columns = [info[2] for info in conn.exec_driver_sql(f"PRAGMA index_info({_quote(row[1])})").all()]
            if columns and all(columns):
                unique.append(columns)
    return unique


class Replica:
    """Row access for one transaction; table shapes are read once per table."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._shapes: dict[str, TableShape] = {}

    def shape(self, table: str) -> TableShape:
        if table not in SHARED_TABLES:
            raise ValueError(f"{table} is not a shared table")
        if table not in self._shapes:
            self._shapes[table] = TableShape(self.conn, table)
        return self._shapes[table]

    def key_of(self, table: str, row: dict[str, Any]) -> str:
        """The sync key of a row, in the change log's text form."""
        columns = self.shape(table).key
        if len(columns) == 1:
            value = row[columns[0]]
            return "" if value is None else str(value)
        return json.dumps([row[c] for c in columns], separators=(",", ":"), ensure_ascii=False)

    def read(self, table: str, pk: str) -> dict[str, Any] | None:
        """The stored row under this sync key, or None."""
        table_shape = self.shape(table)
        result = self.conn.exec_driver_sql(
            f"SELECT * FROM {_quote(table)} WHERE {table_shape.where_key()}", tuple(table_shape.key_values(pk)))
        row = result.mappings().first()
        return dict(row) if row is not None else None

    def write(self, table: str, pk: str, row: dict[str, Any] | None) -> None:
        """Make the local row under ``pk`` equal to ``row``; None deletes it."""
        table_shape = self.shape(table)
        key = table_shape.key_values(pk)
        if row is None:
            self.conn.exec_driver_sql(f"DELETE FROM {_quote(table)} WHERE {table_shape.where_key()}", tuple(key))
            return
        columns = [c for c in table_shape.columns if c in row and c not in table_shape.node_local]
        for unique in table_shape.unique:
            if all(c in row for c in unique):
                self.conn.exec_driver_sql(
                    f"DELETE FROM {_quote(table)} WHERE "
                    + " AND ".join(f"{_quote(c)} IS ?" for c in unique)
                    + f" AND NOT ({table_shape.where_key()})",
                    (*[row[c] for c in unique], *key),
                )
        keep_local = LOCAL_COLUMNS.get(table, frozenset())
        updates = [c for c in columns if c not in table_shape.key and c not in keep_local]
        conflict = ", ".join(_quote(c) for c in table_shape.key)
        action = ("DO UPDATE SET " + ", ".join(f"{_quote(c)} = excluded.{_quote(c)}" for c in updates)
                  if updates else "DO NOTHING")
        self.conn.exec_driver_sql(
            f"INSERT INTO {_quote(table)} ({', '.join(_quote(c) for c in columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) ON CONFLICT ({conflict}) {action}",
            tuple(row[c] for c in columns),
        )
