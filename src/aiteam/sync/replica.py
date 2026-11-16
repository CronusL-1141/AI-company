"""An edge's replica of the shared face: the center's rows, written as they come.

Rows travel in their stored form (aiteam.sync.rows). Writing one into the replica
makes the local row equal to the center's: an upsert under the row's sync key, after
removing any local row that holds one of its other unique values (a row staged here
that the center created under another id; NULL, or an empty value under a partial
index, holds none). Columns that only number rows on this node (the integer id of
knowledge_links) and columns that record this node's own use (LOCAL_COLUMNS) keep
their local values. Deletions remove the row.

``Replica`` works on a synchronous SQLAlchemy connection inside the caller's
transaction; the edge's applier uses it with change capture suppressed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Connection

from aiteam.sync.rows import Rows, quote
from aiteam.sync.tables import LOCAL_COLUMNS, SHARED_TABLES


class Replica(Rows):
    """Shared-face rows of one transaction: read, keyed, and written as the center has them."""

    def __init__(self, conn: Connection) -> None:
        super().__init__(conn, SHARED_TABLES)

    def write(self, table: str, pk: str, row: dict[str, Any] | None) -> None:
        """Make the local row under ``pk`` equal to ``row``; None deletes it."""
        table_shape = self.shape(table)
        key = table_shape.key_values(pk)
        if row is None:
            self.delete(table, pk)
            return
        columns = [c for c in table_shape.columns if c in row and c not in table_shape.node_local]
        for unique in table_shape.unique_values_of(row):
            self.conn.exec_driver_sql(
                f"DELETE FROM {quote(table)} WHERE "
                + " AND ".join(f"{quote(c)} IS ?" for c in unique)
                + f" AND NOT ({table_shape.where_key()})",
                (*[row[c] for c in unique], *key),
            )
        self.upsert(table, row, columns, keep=LOCAL_COLUMNS.get(table, frozenset()))
