"""Rows of synced tables as they travel between nodes: their shape, key and stored form.

A row travels as its stored columns (``SELECT *``: text, numbers, NULL; JSON columns
as their text), keyed like its change-log entries (aiteam.sync.tables ``sync_key``).
``Rows`` reads them on one synchronous SQLAlchemy connection inside the caller's
transaction; what a node does with a row it receives depends on the face: an edge's
replica takes the center's shared rows (aiteam.sync.replica), the center takes an
edge's session rows by their owner (aiteam.sync.intake).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

from sqlalchemy import Table
from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite
from sqlalchemy.engine import Connection

from aiteam.sync.tables import SYNCED_TABLES, sync_key

_DIALECT = SQLiteDialect_pysqlite()


def quote(name: str) -> str:
    """An SQL identifier, quoted."""
    return '"' + name.replace('"', '""') + '"'


class TableShape:
    """What reading and applying rows of one table needs to know about it."""

    def __init__(self, conn: Connection, table: str) -> None:
        info = conn.exec_driver_sql(f"PRAGMA table_info({quote(table)})").all()
        self.table = table
        self.columns = [row[1] for row in info]
        primary = tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5])
        self.key = sync_key(table, primary)
        # A primary key that is not the sync key numbers rows per node: never copied.
        self.node_local = frozenset(primary) - frozenset(self.key) if self.key != primary else frozenset()
        self.unique = [index for index in _unique_indexes(conn, table) if tuple(index.columns) != self.key]

    def unique_values_of(self, row: Mapping[str, Any]) -> list[list[str]]:
        """The unique indexes ``row`` holds a value of: every column present and set. A
        partial index (unique where the value is not empty) does not count an empty value."""
        return [index.columns for index in self.unique
                if all(c in row and row[c] is not None and not (index.partial and row[c] == "")
                       for c in index.columns)]

    def key_values(self, pk: str) -> list[Any]:
        if len(self.key) == 1:
            return [pk]
        values = json.loads(pk)
        if not isinstance(values, list) or len(values) != len(self.key):
            raise ValueError(f"{self.table}: key {pk!r} does not match {self.key}")
        return values

    def where_key(self) -> str:
        return " AND ".join(f"{quote(c)} = ?" for c in self.key)


class UniqueIndex(NamedTuple):
    columns: list[str]
    partial: bool


def _unique_indexes(conn: Connection, table: str) -> list[UniqueIndex]:
    """Unique indexes other than the primary key; ``partial`` for one with a WHERE clause."""
    unique = []
    for row in conn.exec_driver_sql(f"PRAGMA index_list({quote(table)})").all():
        if row[2] and row[3] != "pk":
            columns = [info[2] for info in conn.exec_driver_sql(f"PRAGMA index_info({quote(row[1])})").all()]
            if columns and all(columns):
                unique.append(UniqueIndex(columns, bool(row[4])))
    return unique


class Rows:
    """Row access for one transaction over ``tables``; shapes are read once per table."""

    def __init__(self, conn: Connection, tables: Iterable[str] = SYNCED_TABLES) -> None:
        self.conn = conn
        self._tables = frozenset(tables)
        self._shapes: dict[str, TableShape] = {}

    def shape(self, table: str) -> TableShape:
        if table not in self._tables:
            raise ValueError(f"{table} is not one of the tables this access covers")
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
            f"SELECT * FROM {quote(table)} WHERE {table_shape.where_key()}", tuple(table_shape.key_values(pk)))
        row = result.mappings().first()
        return dict(row) if row is not None else None

    def upsert(self, table: str, row: Mapping[str, Any], columns: list[str],
               keep: frozenset[str] = frozenset()) -> None:
        """Insert ``row`` (its ``columns``), or update the row under its sync key.

        ``keep``: columns an existing row keeps (this node's own values).
        """
        table_shape = self.shape(table)
        updates = [c for c in columns if c not in table_shape.key and c not in keep]
        conflict = ", ".join(quote(c) for c in table_shape.key)
        action = ("DO UPDATE SET " + ", ".join(f"{quote(c)} = excluded.{quote(c)}" for c in updates)
                  if updates else "DO NOTHING")
        self.conn.exec_driver_sql(
            f"INSERT INTO {quote(table)} ({', '.join(quote(c) for c in columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)}) ON CONFLICT ({conflict}) {action}",
            tuple(row[c] for c in columns),
        )

    def delete(self, table: str, pk: str) -> None:
        table_shape = self.shape(table)
        self.conn.exec_driver_sql(f"DELETE FROM {quote(table)} WHERE {table_shape.where_key()}",
                                  tuple(table_shape.key_values(pk)))

    def keys(self, table: str) -> set[str]:
        """Every row's sync key in ``table``."""
        columns = ", ".join(quote(c) for c in self.shape(table).key)
        return {self.key_of(table, row) for row in self.conn.exec_driver_sql(
            f"SELECT {columns} FROM {quote(table)}").mappings()}


def stored_form(table: Table, values: Mapping[str, Any]) -> dict[str, Any]:
    """``values`` (attribute values of an ORM row) as the database stores them.

    What a row read with ``SELECT *`` holds, the form rows travel in: JSON as its
    text, times as the text the column type writes. Only the given columns. None goes
    through the column type as well, as the ORM writes it: a JSON column stores the
    text ``null``, the others SQL NULL.
    """
    stored: dict[str, Any] = {}
    for name, value in values.items():
        processor = table.columns[name].type.dialect_impl(_DIALECT).bind_processor(_DIALECT)
        stored[name] = processor(value) if processor is not None else value
    return stored
