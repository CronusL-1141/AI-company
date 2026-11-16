"""This node's own rows: the ones it derives data from (multi-host sync).

A node that syncs holds other nodes' session rows too (the center holds its edges').
Data derived from observations (the usage projection's position, the continuity of
quota sampling, quota summaries) is computed where the observations were made and
travels finished: computed again on the center from another node's rows it would
be wrong (positions are numbered per database) or mixed (two machines' samples of
one account would break each other's cycles). So those reads see only this node's
partition, aiteam.sync.context.own_origins: every row on a standalone node, the
node's own rows on the center, its own and the baseline's on an edge.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, TextClause, bindparam, false, text, true

from aiteam.sync import context as sync_context

# Every way of reading a partition below is written once, here: an ORM condition, a
# text clause, a fragment for plain driver SQL, a test of one origin.

Origins = tuple[str, ...] | None  # None: every row


def edge_origins(node_id: str) -> tuple[str, str]:
    """What an edge owns: its own rows and the baseline's (its database before the sync)."""
    return ("", node_id)


def within(column: Any, origins: Origins) -> ColumnElement[bool]:
    """A condition keeping the rows of ``origins`` (``column`` is a table's origin_node)."""
    return true() if origins is None else column.in_(origins)


def own(column: Any) -> ColumnElement[bool]:
    """A condition keeping this node's rows."""
    return within(column, sync_context.own_origins())


def elsewhere(column: Any) -> ColumnElement[bool]:
    """A condition keeping the other nodes' rows (none where every row is this node's)."""
    origins = sync_context.own_origins()
    return false() if origins is None else column.not_in(origins)


def is_own(origin: str) -> bool:
    origins = sync_context.own_origins()
    return origins is None or origin in origins


def own_text(sql: str) -> TextClause:
    """``sql`` with ``{own}`` replaced by the condition, as a SQLAlchemy text clause."""
    origins = sync_context.own_origins()
    if origins is None:
        return text(sql.replace("{own}", "1 = 1"))
    return text(sql.replace("{own}", "origin_node IN :own_origins")).bindparams(
        bindparam("own_origins", value=list(origins), expanding=True))


def driver_sql(origins: Origins) -> tuple[str, tuple[str, ...]]:
    """The condition for plain driver SQL (``?`` parameters): the fragment and its values."""
    if origins is None:
        return "1 = 1", ()
    return f"origin_node IN ({', '.join('?' for _ in origins)})", tuple(origins)
