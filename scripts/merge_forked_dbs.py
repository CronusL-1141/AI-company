#!/usr/bin/env python3
"""Merge, once, two OS databases that forked from one snapshot, before the multi-host cut-over.

Two machines ran on copies of one database (the snapshot taken when the second
machine was set up) and both kept writing. Before multi-host sync starts, the two
are merged into one database: the baseline both nodes start from. The edge (the
machine whose database the snapshot was taken from) owns the baseline: its rows keep
the empty origin. The center keeps its own rows under its node id, so after the
cut-over each machine governs, numbers and derives from its own sessions.

The snapshot itself is gone, so the merge is two-way. What tells the sides apart:

* The fork time: the latest event both databases hold (events are only appended and
  their ids are random, so an event in both was in the snapshot). ``--fork-at``
  overrides it.
* A row in one database only (a lone row) was created there after the fork, or it
  was in the snapshot and the other side deleted it. Only shared tables with a delete
  path ask which (``born`` column against the fork time); every other lone row is new.

Rules, table by table (every ORM table falls in one group of aiteam.sync.tables):

* Session face (events, agents, teams...): the union. The center's lone rows take the
  center's node id. A row both changed is taken whole from the side the session last
  ran on (the activity clock of the table, ties to the edge); when that is the center,
  the row moves to the center's origin. Deletions of session rows are not carried
  over: the owner's own governance removes them again after the cut-over.
* Shared face (tasks, memos, reports...): the union. A row both changed is merged
  field by field with the conflict rules of the sync (aiteam.sync.merge): a status
  only moves forward (STATUS_RANKS), together with its companion fields; an
  invalidation stays (the first one wins); meeting participants are joined; water
  marks take the later value; any other field goes to the later writer (the row's
  clock; for tasks their revision count; ties to the edge). Two completions with
  different results keep one and file the other as a memo on the task. A lone row
  deleted on the other side stays deleted, unless the side that kept it changed it
  (or added rows under it) after the fork: a project deletion takes the rows its
  cascade removed with it; a deletion that came from a cascade whose project survived
  is not honoured.
* Numbered per database: the center's codex usage observations get numbers after the
  edge's (``seq``), and the journal positions recorded in the center's plan
  snapshots (payload ``activity_journal_seq``) are moved to the new numbers.
  knowledge_links get fresh integer ids (nothing refers to them).
* Local tables (this machine's own state): the union, the edge's row where both have
  one. Derived caches and the governance lease are emptied: each node rebuilds its own
  summaries from its own partition on first read.
* Dead tables: the union. Sync bookkeeping: reset to a standalone baseline (the
  tombstones are joined).

Every losing value, every deletion decision and every lone row a unique value keeps
out is listed (report JSON and a readable summary). A unique value held by different
rows on the two sides (the same project root, team name...) stops the merge: no id
is rewritten here.

Without ``--apply`` (the default) the merge runs on a working copy and writes its
report and the merged database under ``OUT/preview``; nothing else is touched. With
``--apply``: both input databases are first archived read-only under ``OUT/archive``
(with sha256 and MANIFEST.json), the merge reads the archives, and the result goes to
``OUT/merged-<edge sha>-<center sha>.db`` with its report and manifest. The inputs are
never written. A run that is killed leaves only its working directory, removed by the
next run; running again on the same inputs finds the finished result and stops.

Usage (the center's node id comes from its node.json, written by
``python3 -m aiteam.sync enable --role center --name mini --apply`` on the center
before its database is copied; a dry run may name an id instead):

    python3 scripts/merge_forked_dbs.py --edge AIR.db --center MINI.db \
        --center-node-file MINI-node.json --out-dir OUT [--apply]

Exit codes: 0 done, 2 bad input, 3 clock convention or schema differs, 4 a unique
value collides, 5 a database already belongs to a sync, 6 an input changed during the
run.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote as url_quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import DateTime  # noqa: E402

from aiteam.ids import content_id  # noqa: E402
from aiteam.storage.models import Base, TaskMemoModel  # noqa: E402
from aiteam.sync.merge import STATUS_RANKS, decode, revision_of  # noqa: E402
from aiteam.sync.rows import quote, stored_form  # noqa: E402
from aiteam.sync.tables import (  # noqa: E402
    DEAD_TABLES,
    LOCAL_COLUMNS,
    LOCAL_TABLES,
    ORIGIN_COLUMN,
    SESSION_RENUMBERED,
    SESSION_TABLES,
    SHARED_TABLES,
    SYNC_BOOKKEEPING_TABLES,
    sync_key,
)
from aiteam.types import NODE_ID_PATTERN, NodeIdentity, NodeRole, TaskMemo  # noqa: E402

SCRIPT_VERSION = 1

EXIT_OK, EXIT_USAGE, EXIT_MISMATCH, EXIT_COLLISION, EXIT_SYNCED, EXIT_CHANGED = 0, 2, 3, 4, 5, 6

EDGE, CENTER = "edge", "center"
_SCHEMA = {EDGE: "e", CENTER: "c"}

# -- table rules -------------------------------------------------------------------

# Local state emptied in the merged database: derived per node from its own partition
# (rebuilt on first read, aiteam.storage.plan_summary_store), or a lease of one process.
CLEARED = {
    "account_plan_summaries": "派生缓存，各节点首次读取时按自己的分区重建",
    "account_plan_cycle_requests": "派生缓存，随额度汇总一起重建",
    "governance_lease": "某个 API 进程的治理租约，启动时重新认领",
}

# Sync bookkeeping that must be empty on both sides: a database that already took part
# in a sync is not merged by this one-time step.
_MUST_BE_EMPTY = ("sync_changes", "sync_outbox", "sync_tentative", "sync_peers", "sync_op_receipts",
                  "sync_conflicts", "sync_intake", "sync_field_clock", "sync_guard", "sync_suppress")

# Fields that only advance: the later value, never a conflict.
WATERMARKS: dict[str, frozenset[str]] = {
    "channel_read_cursors": frozenset({"last_read_at"}),
    "scheduled_tasks": frozenset({"last_run_at", "next_run_at"}),
    **LOCAL_COLUMNS,
}

# A status and the fields that move with it: decided together, so the row stays coherent.
STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "tasks": ("status", "started_at", "completed_at", "result"),
    "leader_briefings": ("status", "resolution", "resolved_at"),
    "meetings": ("status", "concluded_at"),
}

# An invalidation is only ever set: either side's stays, the earlier of two wins.
INVALIDATION_TABLES = frozenset({"memories", "task_memos"})
_INVALIDATION = ("invalid_at", "invalidated_by")

# JSON lists merged as a union.
UNION_LISTS: dict[str, frozenset[str]] = {"meetings": frozenset({"participants"})}

# Shared tables a row can disappear from: deleted one by one (a route), or by the
# cascade of a project deletion (aiteam.storage.repository.delete_project).
INDIVIDUALLY_DELETED = frozenset({
    "reports", "phases", "memories", "scheduled_tasks",
    "ecosystem_repo_tags", "ecosystem_relations", "ecosystem_deep_reviews",
})
PROJECT_CASCADE = frozenset({
    "tasks", "task_memos", "meetings", "meeting_messages", "task_create_batches",
    "phases", "reports", "leader_briefings", "memories",
})
DELETABLE = INDIVIDUALLY_DELETED | PROJECT_CASCADE | {"projects"}

# When a row of a deletable table was created (its insertion time).
BORN = {table: "created_at" for table in DELETABLE}
BORN["meeting_messages"] = "timestamp"

# Rows added under a project after the fork mean its side still used it.
PROJECT_CHILDREN = ("tasks", "task_memos", "meetings", "reports", "leader_briefings", "phases",
                    "task_create_batches", "teams")

# Tables decided before the rest: later ones depend on their decisions (project
# deletions, renumbered observations).
_FIRST = ("projects", "codex_usage_observations")

_VALUE_LIMIT = 2000


class MergeAbortError(Exception):
    """The merge cannot go on; ``code`` is the exit code, ``details`` go into the report."""

    def __init__(self, code: int, message: str, details: list[Any] | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or []


class _BusyError(MergeAbortError):
    """Another run holds the output directory: its files are left alone."""


# -- values ------------------------------------------------------------------------

def parse_time(value: Any) -> datetime | None:
    """A stored time (text, naive meaning UTC) as an aware datetime; None when it is none."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _later(a: Any, b: Any) -> Any:
    """The later of two stored values of one column (times by time, others by value)."""
    if a is None or b is None:
        return b if a is None else a
    ta, tb = parse_time(a), parse_time(b)
    if ta is not None and tb is not None:
        return b if tb > ta else a
    return b if b > a else a


def _shown(value: Any) -> Any:
    """A value as the report shows it: decoded JSON, long text cut."""
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str) and len(value) > _VALUE_LIMIT:
        return value[:_VALUE_LIMIT] + f"…（共 {len(value)} 字）"
    return value


@functools.cache
def _time_columns(table: str) -> tuple[str, ...]:
    """The table's columns that hold times, from the ORM types (UtcDateTime wraps DateTime)."""
    return tuple(column.name for column in Base.metadata.tables[table].columns
                 if isinstance(getattr(column.type, "impl", column.type), DateTime))


def _latest_time(table: str, row: dict[str, Any], skip: frozenset[str] = frozenset()) -> datetime | None:
    times = [parse_time(row.get(name)) for name in _time_columns(table) if name not in skip]
    present = [moment for moment in times if moment is not None]
    return max(present) if present else None


# -- clocks: which of two versions of a row was written later ------------------------------

def _shared_clock(table: str, row: dict[str, Any]) -> Any:
    """The order of a shared row's versions: a task's revision count, an anchor's reset,
    otherwise its latest time (water marks and this node's own columns left out)."""
    if table == "tasks":
        return revision_of(row)
    if table == "account_plan_price_anchors":
        payload = decode(row.get("payload"))
        return parse_time(payload.get("reset_at")) if isinstance(payload, dict) else None
    return _latest_time(table, row, WATERMARKS.get(table, frozenset()))


def _session_clock(table: str, row: dict[str, Any], team_activity: Callable[[str], Any]) -> Any:
    """Where a session row was last written by its session (not by governance): the
    later value names the machine the session ran on. None: no evidence (immutable rows)."""
    if table == "agents":
        return max((t for t in (parse_time(row.get(c)) for c in ("last_active_at", "tokens_measured_at",
                                                                     "ctx_measured_at")) if t), default=None)
    if table == "teams":
        return team_activity(str(row["id"]))
    if table in ("workflow_runs", "workflow_agents"):
        return parse_time(row.get("last_activity_at"))
    if table == "agent_activities":
        return 1 if row.get("duration_ms") is not None else 0
    if table == "wake_sessions":
        return parse_time(row.get("finished_at"))
    if table == "external_exec_invocations":
        return row.get("revision")
    return None


def _config_sans_revision(row: dict[str, Any]) -> Any:
    """A task's config without its revision count (an edit counter, not a field anyone set)."""
    config = decode(row.get("config"))
    if isinstance(config, dict) and isinstance(config.get("task_management"), dict):
        config = {**config, "task_management": {k: v for k, v in config["task_management"].items()
                                                if k != "revision"}}
    return config


def _center_later(edge_clock: Any, center_clock: Any) -> bool:
    """True when the center's version is strictly later; a tie and no evidence go to the edge."""
    if center_clock is None:
        return False
    if edge_clock is None:
        return True
    return center_clock > edge_clock


# -- the report --------------------------------------------------------------------

@dataclass
class Conflict:
    table: str
    pk: str
    field: str
    winner: str
    winner_value: Any
    loser_value: Any
    rule: str


@dataclass
class Deletion:
    table: str
    pk: str
    deleted_on: str
    honoured: bool
    rule: str


@dataclass
class TableStats:
    group: str
    edge: int = 0
    center: int = 0
    both_differ: int = 0
    lone_edge: int = 0
    lone_center: int = 0
    inserted: int = 0
    stamped_center: int = 0
    moved_to_center: int = 0
    deleted: int = 0
    skipped: int = 0
    renumbered: int = 0
    cleared: int = 0
    merged: int = 0


@dataclass
class Report:
    edge: dict[str, Any] = field(default_factory=dict)
    center: dict[str, Any] = field(default_factory=dict)
    fork_at: str = ""
    fork_source: str = ""
    center_node: str = ""
    mode: str = "dry-run"
    tables: dict[str, TableStats] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    deletions: list[Deletion] = field(default_factory=list)
    collisions: list[dict[str, Any]] = field(default_factory=list)
    memos: list[dict[str, Any]] = field(default_factory=list)
    renumbering: dict[str, Any] = field(default_factory=dict)
    quiet: Counter = field(default_factory=Counter)
    other_tables: list[str] = field(default_factory=list)
    merged_path: str = ""
    abort: dict[str, Any] = field(default_factory=dict)

    def stats(self, table: str, group: str) -> TableStats:
        return self.tables.setdefault(table, TableStats(group=group))

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["quiet"] = dict(self.quiet)
        data["script_version"] = SCRIPT_VERSION
        return data


# -- reading both sides --------------------------------------------------------------

def _primary_key(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    info = conn.execute(f"PRAGMA main.table_info({quote(table)})").fetchall()
    return tuple(row[1] for row in sorted(info, key=lambda row: row[5]) if row[5])


def _key_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """The key rows are matched by across the two sides: the sync key (aiteam.sync.tables)."""
    return sync_key(table, _primary_key(conn, table))


def _node_local(conn: sqlite3.Connection, table: str, key: tuple[str, ...]) -> frozenset[str]:
    """A primary key that is not the matching key numbers rows per database: never copied."""
    primary = _primary_key(conn, table)
    return frozenset(primary) - frozenset(key) if primary != key else frozenset()


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA main.table_info({quote(table)})").fetchall()]


def _unique_indexes(conn: sqlite3.Connection, table: str, key: tuple[str, ...]) -> list[tuple[list[str], bool]]:
    """Unique indexes other than the key: (columns, partial)."""
    found = []
    for row in conn.execute(f"PRAGMA main.index_list({quote(table)})").fetchall():
        if row[2] and row[3] != "pk":
            columns = [info[2] for info in conn.execute(f"PRAGMA main.index_info({quote(row[1])})").fetchall()]
            if columns and all(columns) and tuple(columns) != key:
                found.append((columns, bool(row[4])))
    return found


def _join(key: tuple[str, ...], left: str, right: str) -> str:
    return " AND ".join(f"{left}.{quote(c)} = {right}.{quote(c)}" for c in key)


def _key_text(key: tuple[str, ...], row: dict[str, Any]) -> str:
    if len(key) == 1:
        return str(row[key[0]])
    return json.dumps([row[c] for c in key], ensure_ascii=False, separators=(",", ":"))


class Merger:
    """One merge: reads the edge (``e``) and the center (``c``), writes ``main``.

    ``main`` starts as a byte copy of the edge; every decision reads the two attached
    inputs only, so the order tables are written in never changes what is decided.
    """

    def __init__(self, conn: sqlite3.Connection, report: Report, fork_at: datetime, names: dict[str, str]) -> None:
        self.conn = conn
        self.report = report
        self.fork_at = fork_at
        self.names = names  # how the report names each side; the center's name is its node id
        self.center_node = names[CENTER]
        # Project deletions decided: project id -> (side that deleted it, honoured).
        self.project_fate: dict[str, tuple[str, bool]] = {}
        # Renumbered observations: the center's old seq -> new seq, and the edge's last seq.
        self.seq_map: list[tuple[int, int]] = []
        self.seq_floor = 0

    # -- driving ----------------------------------------------------------------

    def run(self) -> None:
        # Journal positions up to the edge's last number are not the center's.
        self.seq_floor = int(self.conn.execute(
            "SELECT coalesce(max(seq), 0) FROM e.codex_usage_observations").fetchone()[0])
        orm = set(Base.metadata.tables)
        order = [t for t in _FIRST if t in orm] + sorted(orm - set(_FIRST))
        for table in order:
            if table in SYNC_BOOKKEEPING_TABLES:
                continue
            if table in CLEARED:
                self._clear(table)
            elif table in SESSION_TABLES:
                self._merge_synced(table, "session")
            elif table in SHARED_TABLES:
                self._merge_synced(table, "shared")
            elif table in LOCAL_TABLES or table in DEAD_TABLES:
                self._union_plain(table, "local" if table in LOCAL_TABLES else "dead")
            else:
                raise MergeAbortError(EXIT_MISMATCH,
                                      f"{table} 没有归入任何同步分组（aiteam.sync.tables），不知道怎么合并")
        self._reset_sync_bookkeeping()
        self._note_other_tables(orm)
        for table, stats in self.report.tables.items():
            stats.merged = self._count("main", table)

    def _count(self, schema: str, table: str) -> int:
        return int(self.conn.execute(f"SELECT count(*) FROM {schema}.{quote(table)}").fetchone()[0])

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cursor = self.conn.execute(sql, params)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    # -- synced tables ------------------------------------------------------------------

    def _merge_synced(self, table: str, group: str) -> None:
        stats = self.report.stats(table, group)
        key = _key_columns(self.conn, table)
        columns = _columns(self.conn, table)
        node_local = _node_local(self.conn, table, key)
        compared = [c for c in columns if c != ORIGIN_COLUMN and c not in node_local]
        stats.edge, stats.center = self._count("e", table), self._count("c", table)

        for edge_row, center_row in self._differing(table, key, compared):
            stats.both_differ += 1
            if group == "session":
                self._settle_session_row(table, key, compared, edge_row, center_row, stats)
            else:
                self._settle_shared_row(table, key, compared, edge_row, center_row)

        lone_center = self._lone(table, key, "c", "e")
        stats.lone_center = len(lone_center)
        if table in DELETABLE:
            lone_edge = self._lone(table, key, "e", "c")
            stats.lone_edge = len(lone_edge)
            self._settle_deletions(table, key, lone_edge, lone_center, stats)
        else:
            # New on the edge: already in the merged copy. New on the center: its node's.
            stats.lone_edge = self._lone_count(table, key, "e", "c")
            self._insert_center(table, key, columns, node_local, [(row, self.center_node) for row in lone_center],
                                stats)

    def _differing(self, table: str, key: tuple[str, ...], compared: list[str]) -> Iterator[tuple[dict, dict]]:
        """Pairs (edge row, center row) under one key whose compared columns differ."""
        differs = " OR ".join(f"NOT (e.{quote(c)} IS x.{quote(c)})" for c in compared)
        order = ", ".join(f"e.{quote(c)}" for c in key)
        on = _join(key, "e", "x")
        edge_rows = self._rows(f"SELECT e.* FROM e.{quote(table)} e JOIN c.{quote(table)} x ON {on} "
                               f"WHERE {differs} ORDER BY {order}")
        center_rows = self._rows(f"SELECT x.* FROM e.{quote(table)} e JOIN c.{quote(table)} x ON {on} "
                                 f"WHERE {differs} ORDER BY {order}")
        return zip(edge_rows, center_rows, strict=True)

    @staticmethod
    def _lone_sql(table: str, key: tuple[str, ...], here: str, there: str, what: str) -> str:
        return (f"SELECT {what} FROM {here}.{quote(table)} h WHERE NOT EXISTS "
                f"(SELECT 1 FROM {there}.{quote(table)} o WHERE {_join(key, 'o', 'h')})")

    def _lone(self, table: str, key: tuple[str, ...], here: str, there: str) -> list[dict[str, Any]]:
        """Rows of ``here`` whose key ``there`` does not hold."""
        return self._rows(self._lone_sql(table, key, here, there, "h.*"))

    def _lone_count(self, table: str, key: tuple[str, ...], here: str, there: str) -> int:
        return int(self.conn.execute(self._lone_sql(table, key, here, there, "count(*)")).fetchone()[0])

    def _write(self, table: str, key: tuple[str, ...], row: dict[str, Any], values: dict[str, Any]) -> None:
        """Set ``values`` on the merged row under ``row``'s key."""
        if not values:
            return
        assignments = ", ".join(f"{quote(c)} = ?" for c in values)
        where = " AND ".join(f"{quote(c)} = ?" for c in key)
        self.conn.execute(f"UPDATE main.{quote(table)} SET {assignments} WHERE {where}",
                          (*values.values(), *(row[c] for c in key)))

    def _conflict(self, table: str, key: tuple[str, ...], row: dict[str, Any], name: str, winner: str,
                  won: Any, lost: Any, rule: str) -> None:
        self.report.conflicts.append(Conflict(table=table, pk=_key_text(key, row), field=name,
                                              winner=self.names[winner], winner_value=_shown(won),
                                              loser_value=_shown(lost), rule=rule))

    # -- a row both sides changed: session face ----------------------------------------

    def _settle_session_row(self, table: str, key: tuple[str, ...], compared: list[str], edge_row: dict,
                            center_row: dict, stats: TableStats) -> None:
        """Taken whole from the side the session last ran on; the row follows it to the center."""
        clocks = (_session_clock(table, edge_row, lambda team: self._team_activity("e", team)),
                  _session_clock(table, center_row, lambda team: self._team_activity("c", team)))
        center_wins = _center_later(*clocks)
        winner, chosen, other = (CENTER, center_row, edge_row) if center_wins else (EDGE, edge_row, center_row)
        for name in compared:
            if chosen[name] != other[name]:
                self._conflict(table, key, edge_row, name, winner, chosen[name], other[name], "session_owner")
        if center_wins:
            stats.moved_to_center += 1
            self._write(table, key, edge_row, {**{c: center_row[c] for c in compared},
                                               ORIGIN_COLUMN: self.center_node})

    def _team_activity(self, schema: str, team_id: str) -> datetime | None:
        """The latest activity of a team's members on one side: governance (closing a
        team, marking members offline) does not move it, only the session does."""
        value = self.conn.execute(f"SELECT max(last_active_at) FROM {schema}.agents WHERE team_id = ?",
                                  (team_id,)).fetchone()[0]
        return parse_time(value)

    # -- a row both sides changed: shared face ---------------------------------------------

    def _settle_shared_row(self, table: str, key: tuple[str, ...], compared: list[str], edge_row: dict,
                           center_row: dict) -> None:
        # JSON text that only differs in its spelling is the same value.
        differing = [c for c in compared if decode(edge_row[c]) != decode(center_row[c])]
        merged = dict(edge_row)
        clock_winner = CENTER if _center_later(_shared_clock(table, edge_row),
                                               _shared_clock(table, center_row)) else EDGE
        sides = {EDGE: edge_row, CENTER: center_row}
        handled: set[str] = set()

        if table == "tasks" and "config" in differing and _config_sans_revision(edge_row) == \
                _config_sans_revision(center_row):
            merged["config"] = sides[clock_winner]["config"]  # the count is settled by _bump_revision
            self.report.quiet["tasks.config:revision"] += 1
            handled.add("config")
        for name in WATERMARKS.get(table, frozenset()) & set(differing):
            merged[name] = _later(edge_row[name], center_row[name])
            self.report.quiet[f"{table}.{name}:later"] += 1
            handled.add(name)
        if table in INVALIDATION_TABLES and set(_INVALIDATION) & set(differing):
            handled |= self._settle_invalidation(table, key, edge_row, center_row, merged)
        for name in UNION_LISTS.get(table, frozenset()) & set(differing):
            merged[name] = self._union_list(edge_row[name], center_row[name])
            self.report.quiet[f"{table}.{name}:union"] += 1
            handled.add(name)
        group = STATUS_GROUPS.get(table, ())
        if set(group) & set(differing):
            handled |= self._settle_status(table, key, group, edge_row, center_row, merged, clock_winner)
        for name in differing:
            if name in handled:
                continue
            merged[name] = sides[clock_winner][name]
            loser = CENTER if clock_winner == EDGE else EDGE
            self._conflict(table, key, edge_row, name, clock_winner, sides[clock_winner][name], sides[loser][name],
                           "last_writer")
        if table == "tasks":
            self._bump_revision(edge_row, center_row, merged, compared)
        self._write(table, key, edge_row, {c: merged[c] for c in compared if merged[c] != edge_row[c]})

    def _settle_invalidation(self, table: str, key: tuple[str, ...], edge_row: dict, center_row: dict,
                             merged: dict) -> set[str]:
        """Either side's invalidation stays; of two, the earlier one (with its replacement)."""
        edge_at, center_at = parse_time(edge_row["invalid_at"]), parse_time(center_row["invalid_at"])
        if edge_at is None and center_at is None:
            return set()  # only invalidated_by differs, without an invalidation: last writer
        if edge_at is None or center_at is None:
            source = center_row if edge_at is None else edge_row
            self.report.quiet[f"{table}.invalid_at:set"] += 1
        else:
            source = center_row if center_at < edge_at else edge_row
            winner = CENTER if source is center_row else EDGE
            other = edge_row if source is center_row else center_row
            for name in _INVALIDATION:
                if source[name] != other[name]:
                    self._conflict(table, key, edge_row, name, winner, source[name], other[name],
                                   "invalidated_first")
        for name in _INVALIDATION:
            merged[name] = source[name]
        return set(_INVALIDATION)

    @staticmethod
    def _union_list(edge_value: Any, center_value: Any) -> Any:
        ours, theirs = decode(edge_value), decode(center_value)
        if not isinstance(ours, list) or not isinstance(theirs, list):
            return edge_value
        joined = ours + [item for item in theirs if item not in ours]
        return json.dumps(joined, ensure_ascii=False) if isinstance(edge_value, str) else joined

    def _settle_status(self, table: str, key: tuple[str, ...], group: tuple[str, ...], edge_row: dict,
                       center_row: dict, merged: dict, clock_winner: str) -> set[str]:
        """The status further along wins, with the fields that move with it; at equal rank
        the later writer. Two different completion results: the loser's becomes a memo."""
        ranks = STATUS_RANKS[table]
        edge_rank = ranks.get(str(edge_row["status"]), -1)
        center_rank = ranks.get(str(center_row["status"]), -1)
        if edge_rank != center_rank:
            winner, rule = (CENTER if center_rank > edge_rank else EDGE), "status_rank"
        else:
            winner, rule = clock_winner, "last_writer"
        chosen = center_row if winner == CENTER else edge_row
        other = edge_row if winner == CENTER else center_row
        for name in group:
            merged[name] = chosen[name]
            if chosen[name] != other[name]:
                self._conflict(table, key, edge_row, name, winner, chosen[name], other[name], rule)
        if table == "tasks" and chosen["status"] == other["status"] == "completed" \
                and other["result"] and other["result"] != chosen["result"]:
            self._result_memo(edge_row, other, CENTER if winner == EDGE else EDGE)
        return set(group)

    def _result_memo(self, task: dict, loser_row: dict, loser: str) -> None:
        """File the losing completion result as a memo on the task (as the sync does)."""
        memo_id = content_id("db-merge.task-result", str(task["id"]), loser)
        memo = TaskMemo(
            id=memo_id, task_id=str(task["id"]), project_id=task.get("project_id"), author="sync",
            memo_type="issue",
            content=f"来自 {self.names[loser]} 的完成结果（两库合并时未采用）：{loser_row['result']}",
            created_at=parse_time(loser_row.get("completed_at")) or self.fork_at,
        )
        model = TaskMemoModel.from_pydantic(memo)
        table = Base.metadata.tables["task_memos"]
        values = stored_form(table, {c.name: getattr(model, c.key) for c in table.columns
                                     if c.name != ORIGIN_COLUMN})
        values[ORIGIN_COLUMN] = ""
        names = list(values)
        self.conn.execute(f"INSERT OR IGNORE INTO main.task_memos ({', '.join(quote(n) for n in names)}) "
                          f"VALUES ({', '.join('?' for _ in names)})", tuple(values[n] for n in names))
        self.report.memos.append({"task_id": task["id"], "memo_id": memo_id, "loser": self.names[loser],
                                  "result": _shown(loser_row["result"])})

    @staticmethod
    def _bump_revision(edge_row: dict, center_row: dict, merged: dict, compared: list[str]) -> None:
        """A merged task that equals neither side gets a revision past both: a writer holding
        either side's revision is checked against the merge."""
        if all(merged[c] == edge_row[c] for c in compared) or all(merged[c] == center_row[c] for c in compared):
            return
        config = decode(merged.get("config"))
        if not isinstance(config, dict):
            return
        revisions = [r for r in (revision_of(edge_row), revision_of(center_row)) if r is not None]
        management = config.setdefault("task_management", {})
        if isinstance(management, dict):
            management["revision"] = max(revisions, default=0) + 1
            merged["config"] = stored_form(Base.metadata.tables["tasks"], {"config": config})["config"]

    # -- lone rows -----------------------------------------------------------------

    def _settle_deletions(self, table: str, key: tuple[str, ...], lone_edge: list[dict], lone_center: list[dict],
                          stats: TableStats) -> None:
        """A shared row with a delete path that one side lacks: new, or deleted there."""
        born = BORN[table]
        columns = _columns(self.conn, table)
        inserts: list[tuple[dict, str]] = []
        for side, rows in ((EDGE, lone_edge), (CENTER, lone_center)):
            for row in rows:
                created = parse_time(row.get(born))
                if created is not None and created > self.fork_at:
                    if side == CENTER:
                        inserts.append((row, self.center_node))
                    continue
                deleter = CENTER if side == EDGE else EDGE
                honoured, rule = self._deletion_honoured(table, side, deleter, row)
                self.report.deletions.append(Deletion(table=table, pk=_key_text(key, row),
                                                      deleted_on=self.names[deleter], honoured=honoured, rule=rule))
                if honoured and side == EDGE:
                    where = " AND ".join(f"{quote(c)} = ?" for c in key)
                    self.conn.execute(f"DELETE FROM main.{quote(table)} WHERE {where}", tuple(row[c] for c in key))
                    stats.deleted += 1
                elif honoured:
                    stats.skipped += 1
                elif side == CENTER:
                    inserts.append((row, row.get(ORIGIN_COLUMN) or ""))
        self._insert_center(table, key, columns, frozenset(), inserts, stats)

    def _deletion_honoured(self, table: str, survivor: str, deleter: str, row: dict) -> tuple[bool, str]:
        if table == "projects":
            changed = self._changed_after_fork(table, row) or self._children_after_fork(survivor, str(row["id"]))
            self.project_fate[str(row["id"])] = (deleter, not changed)
            return (False, "delete_vs_update") if changed else (True, "deleted")
        project = self._project_of(table, survivor, row)
        fate = self.project_fate.get(project or "")
        if fate is not None and fate[0] == deleter:
            return (True, "project_deleted") if fate[1] else (False, "project_kept")
        if table not in INDIVIDUALLY_DELETED:
            return False, "lone_unexplained"
        if self._changed_after_fork(table, row):
            return False, "delete_vs_update"
        return True, "deleted"

    def _changed_after_fork(self, table: str, row: dict) -> bool:
        latest = _latest_time(table, row, WATERMARKS.get(table, frozenset()))
        return latest is not None and latest > self.fork_at

    def _children_after_fork(self, side: str, project_id: str) -> bool:
        schema = _SCHEMA[side]
        for child in PROJECT_CHILDREN:
            found = self.conn.execute(
                f"SELECT 1 FROM {schema}.{quote(child)} WHERE project_id = ? AND created_at > ? LIMIT 1",
                (project_id, self._stored_fork())).fetchone()
            if found:
                return True
        return False

    def _stored_fork(self) -> str:
        """The fork time in the stored text form of times (naive UTC, microseconds)."""
        return self.fork_at.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")

    def _project_of(self, table: str, side: str, row: dict) -> str | None:
        """The project a row was filed under, as its cascade finds it."""
        schema = _SCHEMA[side]
        if table == "memories":
            return row.get("scope_id") if row.get("scope") == "project" else None
        if table == "meeting_messages":
            found = self.conn.execute(f"SELECT project_id FROM {schema}.meetings WHERE id = ?",
                                      (row.get("meeting_id"),)).fetchone()
            return found[0] if found else None
        if table == "task_memos" and not row.get("project_id"):
            found = self.conn.execute(f"SELECT project_id FROM {schema}.tasks WHERE id = ?",
                                      (row.get("task_id"),)).fetchone()
            return found[0] if found else None
        return row.get("project_id")

    def _insert_center(self, table: str, key: tuple[str, ...], columns: list[str], node_local: frozenset[str],
                       rows: list[tuple[dict, str]], stats: TableStats) -> None:
        """Insert the center's rows with the origin given for each; a row whose unique value
        another row holds is not inserted but listed (it stops the merge)."""
        if not rows:
            return
        rows = self._without_collisions(table, key, rows)
        renumbered = SESSION_RENUMBERED.get(table)
        if renumbered:
            rows = self._renumber(table, renumbered, rows)
        if table == "account_plan_snapshots":
            rows = [(self._repositioned(row) if origin == self.center_node else row, origin) for row, origin in rows]
        names = [c for c in columns if c not in node_local]
        statement = (f"INSERT INTO main.{quote(table)} ({', '.join(quote(c) for c in names)}) "
                     f"VALUES ({', '.join('?' for _ in names)})")
        self.conn.executemany(statement, [tuple(origin if c == ORIGIN_COLUMN else row[c] for c in names)
                                          for row, origin in rows])
        stats.inserted += len(rows)
        stats.stamped_center += sum(1 for _, origin in rows if origin == self.center_node)

    def _without_collisions(self, table: str, key: tuple[str, ...], rows: list[tuple[dict, str]]
                            ) -> list[tuple[dict, str]]:
        renumbered = set(SESSION_RENUMBERED.get(table, ()))
        indexes = [(columns, partial) for columns, partial in _unique_indexes(self.conn, table, key)
                   if not set(columns) & renumbered]
        kept = []
        for row, origin in rows:
            holder = None
            for columns, partial in indexes:
                values = [row[c] for c in columns]
                if any(v is None or (partial and v == "") for v in values):
                    continue
                where = " AND ".join(f"{quote(c)} = ?" for c in columns)
                holder = self._rows(f"SELECT * FROM main.{quote(table)} WHERE {where} LIMIT 1", tuple(values))
                if holder:
                    self.report.collisions.append({
                        "table": table, "columns": columns, "value": values,
                        "center_row": _key_text(key, row), "edge_row": _key_text(key, holder[0])})
                    break
            if not holder:
                kept.append((row, origin))
        return kept

    def _renumber(self, table: str, renumbered: tuple[str, str], rows: list[tuple[dict, str]]
                  ) -> list[tuple[dict, str]]:
        """The center's rows take numbers after the edge's, in their own order."""
        local, kept = renumbered
        ordered = sorted(rows, key=lambda item: item[0][local])
        out = []
        for position, (row, origin) in enumerate(ordered, start=1):
            new = self.seq_floor + position
            self.seq_map.append((int(row[local]), new))
            out.append(({**row, local: new, kept: None}, origin))
        self.report.stats(table, "session").renumbered = len(out)
        if out:
            self.report.renumbering[table] = {
                "column": local, "edge_last": self.seq_floor, "center_old": [self.seq_map[0][0], self.seq_map[-1][0]],
                "center_new": [self.seq_floor + 1, self.seq_floor + len(out)]}
        return out

    def _repositioned(self, row: dict) -> dict:
        """A center plan snapshot's journal position, moved to the renumbered journal."""
        payload = decode(row.get("payload"))
        if not isinstance(payload, dict) or payload.get("activity_journal_seq") is None:
            return row
        old = int(payload["activity_journal_seq"])
        new = max((n for o, n in self.seq_map if o <= old), default=self.seq_floor)
        if new == old:
            return row
        payload["activity_journal_seq"] = new
        self.report.renumbering.setdefault("account_plan_snapshots.payload.activity_journal_seq", 0)
        self.report.renumbering["account_plan_snapshots.payload.activity_journal_seq"] += 1
        return {**row, "payload": stored_form(Base.metadata.tables["account_plan_snapshots"],
                                              {"payload": payload})["payload"]}

    # -- local, dead, cleared and bookkeeping tables ----------------------------------------

    def _union_plain(self, table: str, group: str) -> None:
        """The edge's rows, plus the center's under keys (and unique values) the edge does not hold."""
        stats = self.report.stats(table, group)
        stats.edge, stats.center = self._count("e", table), self._count("c", table)
        columns = ", ".join(quote(c) for c in _columns(self.conn, table))
        before = self._count("main", table)
        self.conn.execute(f"INSERT OR IGNORE INTO main.{quote(table)} ({columns}) "
                          f"SELECT {columns} FROM c.{quote(table)}")
        stats.inserted = self._count("main", table) - before
        stats.skipped = stats.center - stats.inserted - self._common(table)

    def _common(self, table: str) -> int:
        key = _key_columns(self.conn, table)
        return int(self.conn.execute(f"SELECT count(*) FROM e.{quote(table)} x JOIN c.{quote(table)} y "
                                     f"ON {_join(key, 'x', 'y')}").fetchone()[0])

    def _clear(self, table: str) -> None:
        stats = self.report.stats(table, "cleared")
        stats.edge, stats.center = self._count("e", table), self._count("c", table)
        stats.cleared = self.conn.execute(f"DELETE FROM main.{quote(table)}").rowcount

    def _reset_sync_bookkeeping(self) -> None:
        """A standalone baseline: no change log, no node, the later clock floor, both sides' tombstones."""
        floors = [row[0] for row in self.conn.execute(
            "SELECT hlc FROM e.sync_state UNION ALL SELECT hlc FROM c.sync_state").fetchall() if row[0]]
        present = set(_columns(self.conn, "sync_state"))
        values = {"node_id": "", "capture": 0, "hlc": max(floors, default=""), "baseline_seq": 0, "pruned_seq": 0,
                  "registered_node": "", "registered_role": "", "hardware_uuid": ""}
        values = {k: v for k, v in values.items() if k in present}
        self.conn.execute(f"UPDATE main.sync_state SET {', '.join(f'{quote(k)} = ?' for k in values)} WHERE id = 1",
                          tuple(values.values()))
        self.conn.execute("DELETE FROM main.sync_changes")
        columns = ", ".join(quote(c) for c in _columns(self.conn, "sync_tombstones"))
        self.conn.execute(f"INSERT OR IGNORE INTO main.sync_tombstones ({columns}) "
                          f"SELECT {columns} FROM c.sync_tombstones")

    def _note_other_tables(self, orm: set[str]) -> None:
        """Tables outside the ORM (old leftovers): the edge's stay as they are."""
        names = {row[0] for row in self.conn.execute(
            "SELECT name FROM e.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        self.report.other_tables = sorted(names - orm)


# -- inputs, checks and the fork time ------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _uri(path: Path, *, readonly: bool) -> str:
    return f"file:{url_quote(str(path.resolve()))}" + ("?mode=ro&immutable=1" if readonly else "?mode=rw")


def _check_input(path: Path, label: str) -> None:
    if not path.is_file():
        raise MergeAbortError(EXIT_USAGE, f"{label} 库 {path} 不存在")
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise MergeAbortError(EXIT_USAGE, f"{label} 库旁有未合并的 WAL（{wal}）：像是仍在被写。"
                                          "先停掉该机 API，或用备份接口取一份快照")


def _affinity(declared: str) -> str:
    """SQLite's type affinity of a declared column type (what decides how values are stored)."""
    text = (declared or "").upper()
    if "INT" in text:
        return "INTEGER"
    if any(part in text for part in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in text or not text:
        return "BLOB"
    if any(part in text for part in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _comparable(affinity: str) -> str:
    """NUMERIC and INTEGER store the integers OS writes the same way, so they compare equal.

    A column the ORM once declared Boolean (NUMERIC) and later Integer keeps NUMERIC in a
    database migrated through both versions (SQLite cannot change a column's type), and gets
    INTEGER in one created by the later version: sync_state.capture, between phase 0 and 1.
    """
    return "INTEGER" if affinity == "NUMERIC" else affinity


def schema_signature(conn: sqlite3.Connection, schema: str) -> dict[str, Any]:
    """What two databases must share to be merged column by column: each table's columns
    (name, affinity, key position), its unique and plain indexes, and the trigger and view
    names. Column order and the text of the CREATE statements are left out: a database
    migrated step by step (ALTER TABLE) and one created at once differ there and nowhere else."""
    signature: dict[str, Any] = {}
    objects = conn.execute(f"SELECT type, name FROM {schema}.sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    for kind, name in objects:
        if kind == "table":
            info = conn.execute(f"PRAGMA {schema}.table_info({quote(name)})").fetchall()
            signature[f"table {name}"] = sorted((row[1], _comparable(_affinity(row[2])), row[5]) for row in info)
            indexes = []
            for row in conn.execute(f"PRAGMA {schema}.index_list({quote(name)})").fetchall():
                columns = [i[2] for i in conn.execute(f"PRAGMA {schema}.index_info({quote(row[1])})").fetchall()]
                indexes.append((tuple(c or "" for c in columns), bool(row[2]), bool(row[4])))
            signature[f"indexes {name}"] = sorted(indexes)
        elif kind in ("trigger", "view"):
            signature[f"{kind} {name}"] = True
    return signature


def preflight(conn: sqlite3.Connection) -> dict[str, Any]:
    """Both inputs share one clock convention and one schema, the ORM's, and neither takes part in a sync."""
    versions = {side: conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0] for side, schema in _SCHEMA.items()}
    if versions[EDGE] != versions[CENTER]:
        raise MergeAbortError(EXIT_MISMATCH, f"PRAGMA user_version 不同（边缘 {versions[EDGE]}，"
                                             f"中心 {versions[CENTER]}）：时钟制式不一致，中止")
    edge_schema, center_schema = schema_signature(conn, "e"), schema_signature(conn, "c")
    if edge_schema != center_schema:
        differences = [{"object": name, "edge": edge_schema.get(name), "center": center_schema.get(name)}
                       for name in sorted(edge_schema.keys() | center_schema.keys())
                       if edge_schema.get(name) != center_schema.get(name)]
        raise MergeAbortError(EXIT_MISMATCH, "两库 schema 不同：先把两台升级到同一版本并各启动一次 API，再取快照",
                         differences[:50])
    missing = []
    for name, table in Base.metadata.tables.items():
        present = {row[1] for row in conn.execute(f"PRAGMA e.table_info({quote(name)})")}
        if not present:
            missing.append(f"{name}（整表）")
        else:
            missing += [f"{name}.{c}" for c in table.columns.keys() if c not in present]
    if missing:
        raise MergeAbortError(EXIT_MISMATCH, "库的 schema 落后于本脚本所在的代码：先以同一版本启动一次 API",
                              missing[:100])
    problems = []
    for side, schema in _SCHEMA.items():
        state = conn.execute(f"SELECT capture, registered_node FROM {schema}.sync_state WHERE id = 1").fetchone()
        if state is not None and (state[0] or state[1]):
            problems.append(f"{side}: sync_state capture={state[0]} registered_node={state[1]!r}")
        for table in _MUST_BE_EMPTY:
            count = conn.execute(f"SELECT count(*) FROM {schema}.{quote(table)}").fetchone()[0]
            if count:
                problems.append(f"{side}: {table} 有 {count} 行")
    if problems:
        raise MergeAbortError(EXIT_SYNCED, "有库已参与过同步：本脚本只合并切换前的两份单机库", problems)
    return {"user_version": versions[EDGE], "schema_objects": len(edge_schema)}


def derive_fork_time(conn: sqlite3.Connection) -> datetime:
    """The latest moment both databases hold an event (or activity) of: the snapshot was taken after it."""
    moments = []
    for table, column in (("events", "timestamp"), ("agent_activities", "timestamp")):
        value = conn.execute(f"SELECT max(x.{quote(column)}) FROM e.{quote(table)} x "
                             f"JOIN c.{quote(table)} y ON y.id = x.id").fetchone()[0]
        if parse_time(value) is not None:
            moments.append(parse_time(value))
    if not moments:
        raise MergeAbortError(EXIT_USAGE, "两库没有共同的事件，推不出分叉时刻：用 --fork-at 指定（UTC，ISO 格式）")
    return max(moments)


# -- running ---------------------------------------------------------------------------

def _center_node(args: argparse.Namespace) -> str:
    if args.center_node_file:
        try:
            identity = NodeIdentity.model_validate_json(Path(args.center_node_file).read_bytes())
        except (OSError, ValueError) as exc:
            raise MergeAbortError(EXIT_USAGE, f"读不了中心的 node.json：{exc}") from exc
        if identity.role is not NodeRole.CENTER:
            raise MergeAbortError(EXIT_USAGE, f"{args.center_node_file} 声明的角色是 {identity.role}，不是 center")
        return identity.node_id
    if args.apply:
        raise MergeAbortError(EXIT_USAGE, "正式执行要用 --center-node-file（中心 enable 后写下的 node.json），"
                                          "不收手填的 id")
    if not re.match(NODE_ID_PATTERN, args.center_node or ""):
        raise MergeAbortError(EXIT_USAGE, f"中心节点 id {args.center_node!r} 不合格式")
    return str(args.center_node)


def _describe(path: Path, digest: str) -> dict[str, Any]:
    return {"path": str(path), "sha256": digest, "size": path.stat().st_size}


@contextlib.contextmanager
def _locked(out: Path) -> Iterator[None]:
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _BusyError(EXIT_USAGE, f"{out} 里另一个合并正在运行") from exc
        yield


def _archive(source: Path, digest: str, target: Path) -> None:
    """A read-only byte copy of ``source`` at ``target``, verified by its sha256."""
    if target.exists():
        if sha256_file(target) != digest:
            raise MergeAbortError(EXIT_CHANGED, f"归档 {target} 已存在但 sha256 不符：有人改过它，先查明再重跑")
        return
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    shutil.copyfile(source, temporary)
    if sha256_file(temporary) != digest:
        temporary.unlink()
        raise MergeAbortError(EXIT_CHANGED, f"复制 {source} 时内容在变：先停掉该机 API")
    os.chmod(temporary, 0o444)
    os.replace(temporary, target)


def _write_text(path: Path, text: str) -> None:
    """Write ``path`` whole or not at all."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(path: Path, data: Any) -> None:
    _write_text(path, json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")


def merge_into(work_db: Path, edge: Path, center: Path, report: Report, fork_at: datetime | None,
               names: dict[str, str]) -> None:
    """Run the merge in ``work_db`` (a byte copy of ``edge``), reading ``edge`` and ``center``;
    ``names[CENTER]`` is the center's node id."""
    conn = sqlite3.connect(_uri(work_db, readonly=False), uri=True, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute(f"ATTACH DATABASE '{_uri(edge, readonly=True)}' AS e")
        conn.execute(f"ATTACH DATABASE '{_uri(center, readonly=True)}' AS c")
        report.edge.update(preflight(conn))
        if fork_at is None:
            fork_at, report.fork_source = derive_fork_time(conn), "两库共有的最后一条事件"
        else:
            report.fork_source = "--fork-at"
        report.fork_at = fork_at.astimezone(UTC).isoformat()
        conn.execute("BEGIN")
        Merger(conn, report, fork_at, names).run()
        if report.collisions:
            conn.execute("ROLLBACK")
            raise MergeAbortError(EXIT_COLLISION, "两边各有一行占着同一个唯一值（同一项目根、队名等）："
                                                  "本脚本不改写 id，需人工处理", report.collisions)
        conn.execute("COMMIT")
        conn.execute("DETACH DATABASE e")
        conn.execute("DETACH DATABASE c")
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise MergeAbortError(EXIT_CHANGED, f"合并库自检不过：{check}")
    finally:
        conn.close()
    _read_back(work_db, report)


def _read_back(path: Path, report: Report) -> None:
    """Counts from a fresh connection must match what the merge recorded."""
    conn = sqlite3.connect(_uri(path, readonly=True), uri=True)
    try:
        wrong = [table for table, stats in report.tables.items()
                 if conn.execute(f"SELECT count(*) FROM {quote(table)}").fetchone()[0] != stats.merged]
        changes = conn.execute("SELECT count(*) FROM sync_changes").fetchone()[0]
    finally:
        conn.close()
    if wrong or changes:
        raise MergeAbortError(EXIT_CHANGED, f"合并库回读与记录不符：{wrong}，sync_changes={changes}")


def summary_text(report: Report) -> str:
    """The report for a person: what was merged, what lost, what to do next."""
    lines = [f"两库合并（{'正式执行' if report.mode == 'apply' else '预演'}）", ""]
    for side in (EDGE, CENTER):
        info = getattr(report, side)
        lines.append(f"- {side}（{info.get('name', '')}）：{info.get('path')}  sha256 {info.get('sha256', '')[:12]}")
    if report.fork_at:
        local = datetime.fromisoformat(report.fork_at).astimezone()
        lines.append(f"- 分叉时刻：{report.fork_at}（UTC），本机时间 {local:%Y-%m-%d %H:%M:%S %Z}；"
                     f"来源：{report.fork_source}")
    lines.append(f"- 中心节点 id：{report.center_node}")
    if report.abort:
        lines += ["", f"中止：{report.abort['message']}（退出码 {report.abort['code']}）"]
        lines += [f"  {json.dumps(item, ensure_ascii=False, default=str)}" for item in report.abort["details"][:30]]
    if report.tables:
        lines += ["", "逐表（边缘/中心/两边都改/只在边缘/只在中心/插入/标中心/改归中心/删除/未收/合并后）："]
        for table, s in sorted(report.tables.items()):
            if s.edge or s.center or s.merged:
                lines.append(f"  {table} [{s.group}] {s.edge}/{s.center}/{s.both_differ}/{s.lone_edge}/{s.lone_center}/"
                             f"{s.inserted}/{s.stamped_center}/{s.moved_to_center}/{s.deleted}/{s.skipped}/{s.merged}"
                             + (f" 重编号 {s.renumbered}" if s.renumbered else "")
                             + (f" 清空 {s.cleared}" if s.cleared else ""))
    by_rule = Counter((c.table, c.rule) for c in report.conflicts)
    lines += ["", f"冲突（输掉的值）{len(report.conflicts)} 条："]
    lines += [f"  {table} / {rule}: {count}" for (table, rule), count in sorted(by_rule.items())]
    shared = [c for c in report.conflicts if c.rule != "session_owner"][:30]
    for conflict in shared:
        lines.append(f"    {conflict.table} {conflict.pk} {conflict.field}: 取 {conflict.winner} 的 "
                     f"{str(conflict.winner_value)[:120]!r}，舍 {str(conflict.loser_value)[:120]!r}（{conflict.rule}）")
    honoured = [d for d in report.deletions if d.honoured]
    kept = len(report.deletions) - len(honoured)
    lines += ["", f"删除裁决 {len(report.deletions)} 条（照删 {len(honoured)}，保留 {kept}）："]
    lines += [f"  {d.table} {d.pk}：{d.deleted_on} 删的，{'照删' if d.honoured else '保留'}（{d.rule}）"
              for d in report.deletions[:50]]
    if report.memos:
        lines += ["", f"未采用的完成结果转成 memo：{len(report.memos)} 条"]
    if report.renumbering:
        lines += ["", f"重新编号：{json.dumps(report.renumbering, ensure_ascii=False)}"]
    if report.quiet:
        lines += ["", "按规则合并、不算冲突：" + "，".join(f"{k} {v}" for k, v in sorted(report.quiet.items()))]
    if report.other_tables:
        lines += ["", f"ORM 之外的表（保留边缘的）：{', '.join(report.other_tables)}"]
    if report.merged_path:
        lines += ["", f"合并后的库：{report.merged_path}"]
    return "\n".join(lines) + "\n"


def _inputs(args: argparse.Namespace, report: Report) -> tuple[dict[str, Path], dict[str, str], datetime | None]:
    """The two databases (checked and hashed), the names the report gives the sides, the fork time given."""
    paths = {EDGE: Path(args.edge).expanduser(), CENTER: Path(args.center).expanduser()}
    if not re.match(NODE_ID_PATTERN, args.edge_name):
        raise MergeAbortError(EXIT_USAGE, f"--edge-name {args.edge_name!r} 不合节点名格式")
    names = {EDGE: args.edge_name, CENTER: _center_node(args)}
    report.center_node, report.mode = names[CENTER], "apply" if args.apply else "dry-run"
    fork_at = parse_time(args.fork_at) if args.fork_at else None
    if args.fork_at and fork_at is None:
        raise MergeAbortError(EXIT_USAGE, f"--fork-at {args.fork_at!r} 不是 ISO 时间")
    for side, label in ((EDGE, "边缘"), (CENTER, "中心")):
        _check_input(paths[side], label)
        setattr(report, side, {**_describe(paths[side], sha256_file(paths[side])), "name": names[side]})
    return paths, names, fork_at


def _archive_inputs(out: Path, paths: dict[str, Path], names: dict[str, str], digests: dict[str, str]
                    ) -> dict[str, Path]:
    """Read-only byte copies of both inputs with their MANIFEST; the merge then reads these."""
    archive = out / "archive"
    archive.mkdir(exist_ok=True)
    copies = {side: archive / f"{side}-{names[side]}-{digests[side][:12]}.db" for side in (EDGE, CENTER)}
    for side in (EDGE, CENTER):
        _archive(paths[side], digests[side], copies[side])
    _write_json(archive / "MANIFEST.json", {
        "note": "两库合并前的只读归档：原样字节，sha256 可复核",
        "files": [{"role": side, "name": names[side], "original": str(paths[side]), "archive": copies[side].name,
                   "sha256": digests[side], "size": copies[side].stat().st_size} for side in (EDGE, CENTER)]})
    return copies


def _publish(work_db: Path, final: Path, report: Report) -> None:
    """The report and summary next to the merged database, which moves in last: a run
    killed before that leaves no merged database to mistake for a finished one."""
    report.merged_path = str(final)
    stem = final.with_suffix("")
    _write_json(stem.with_name(stem.name + ".report.json"), report.as_json())
    _write_text(stem.with_name(stem.name + ".summary.txt"), summary_text(report))
    os.replace(work_db, final)


def _run(args: argparse.Namespace, report: Report) -> int:
    out = Path(args.out_dir).expanduser()
    with _locked(out):
        # What a killed run left, and (dry run) the last preview: never mistaken for this run's.
        for stale in [*out.glob(".work-*"), *([] if args.apply else [out / "preview"])]:
            shutil.rmtree(stale, ignore_errors=True)
        paths, names, fork_at = _inputs(args, report)
        digests = {EDGE: report.edge["sha256"], CENTER: report.center["sha256"]}
        stem = f"merged-{digests[EDGE][:12]}-{digests[CENTER][:12]}"
        identity = {"edge_sha256": digests[EDGE], "center_sha256": digests[CENTER], "center_node": names[CENTER],
                    "fork_at": args.fork_at or "", "edge_name": names[EDGE], "script_version": SCRIPT_VERSION}
        manifest = out / f"{stem}.manifest.json"
        if args.apply and manifest.exists() and (out / f"{stem}.db").exists():
            done = json.loads(manifest.read_text(encoding="utf-8"))
            if {k: done.get(k) for k in identity} == identity:
                print(f"这两份库已合并过，结果在 {out / f'{stem}.db'}（{manifest.name}），不再重做")
                return EXIT_OK
        work = out / f".work-{os.getpid()}"
        work.mkdir()
        try:
            sources = _archive_inputs(out, paths, names, digests) if args.apply else paths
            work_db = work / "merged.db"
            shutil.copyfile(sources[EDGE], work_db)
            os.chmod(work_db, 0o644)
            merge_into(work_db, sources[EDGE], sources[CENTER], report, fork_at, names)
            if any(sha256_file(paths[side]) != digests[side] for side in (EDGE, CENTER)):
                raise MergeAbortError(EXIT_CHANGED, "合并期间输入库被改动：先停掉两台的 API 再重跑")
            if args.apply:
                final = out / f"{stem}.db"
                _publish(work_db, final, report)
                _write_json(manifest, {**identity, "merged": final.name, "merged_sha256": sha256_file(final),
                                       "archive": "archive/MANIFEST.json"})
            else:
                (out / "preview").mkdir()
                _publish(work_db, out / "preview" / "merged.db", report)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print(summary_text(report))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="merge_forked_dbs.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--edge", required=True, help="the edge's database (Air): it owns the baseline")
    parser.add_argument("--center", required=True, help="the center's database (mini)")
    node = parser.add_mutually_exclusive_group(required=True)
    node.add_argument("--center-node-file", help="the center's node.json (required with --apply)")
    node.add_argument("--center-node", help="the center's node id (dry run only)")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--edge-name", default="air", help="how the report names the edge")
    parser.add_argument("--fork-at", help="fork time (ISO, UTC when no zone); derived from the data by default")
    parser.add_argument("--apply", action="store_true", help="archive the inputs and produce the merged database")
    args = parser.parse_args(argv)
    report = Report()
    try:
        return _run(args, report)
    except MergeAbortError as exc:
        report.abort = {"code": exc.code, "message": exc.message, "details": exc.details}
        out = Path(args.out_dir).expanduser()
        if not isinstance(exc, _BusyError):
            target = out if args.apply else out / "preview"
            target.mkdir(parents=True, exist_ok=True)
            _write_json(target / "aborted.report.json", report.as_json())
        print(summary_text(report), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
