"""Repair historical ``agents`` rows for usage attribution. Dry run by default.

Entry point: ``aiteam attribution repair`` (or ``python3 -m aiteam attribution repair``
with the interpreter that runs the OS). Design notes: docs/token-attribution-v1-design.md
section 9.

Why
---
Rows written before v1.15.0 carry history the usage-attribution page cannot correct by
itself:

* ``harness`` is NULL on rows created before the server labelled every hook row, so
  they sit in the "harness not labelled" bucket;
* ``claude --resume`` shells (a Leader row with no transcript and no tool call) and
  several Leader rows registered on one transcript are counted as Leaders;
* finished rows whose transcript holds no billed usage line read as "a backfill can
  recover this".

Every verdict comes from :mod:`aiteam.services.attribution_marks`, the only copy of the
rules (the statistics read the marks and never decide again). This module only gathers
the inputs (database rows, transcript and rollout probes), orders the two steps and
enforces the write discipline. It never deletes a row and only fills NULL cells, so a
rerun finds nothing to do.

Steps, in one plan
------------------
1. ``harness`` for rows whose source the evidence recognises (``infer_harness``).
2. ``attribution_exclusion`` (with ``attribution_exclusion_of``) for resume shells and
   duplicate-transcript Leaders (``identify_exclusions``), and ``usage_unmeasurable``
   for finished, unmeasured rows (``identify_unmeasurable``; a row still in flight per
   ``usage_coverage.is_in_flight`` gets no verdict). Step 2 sees the labels of step 1:
   a Codex row whose harness was NULL must not be judged by the Claude Code rules.

Clock
-----
The rules compare ``created_at`` and ``last_active_at`` with now (resume-shell grace,
in-flight window) and rank a duplicate group by ``tokens_measured_at`` and
``created_at`` (a relative order, the same under either reading while every value is
naive); nothing this module writes is a timestamp. ``PRAGMA user_version`` names the
clock the database stores (docs/utc-unification-design.md):

* ``20260728``: UTC, timestamps are read as UTC;
* ``0``: unknown. Databases created between v1.11.1 and v1.15.1 store UTC without the
  marker, older unmigrated ones store this host's wall clock, and nothing tells them
  apart. The rules run under both readings and only verdicts both readings reach are
  written, so a row in flight or inside the grace period under either reading gets none;
* any other value: refused.

A stored time that does not parse is unknown: its row gets no verdict that reads time
and its duplicate group is left whole for a later run (``harness`` reads no time).

Apply
-----
Check the clock, take a full backup (SQLite online backup), re-read the rows (the file
probes of the first pass are reused, so only database reads remain), write a journal
with every planned cell and the before-image of every row the verdicts read, then in
one ``BEGIN IMMEDIATE`` transaction compare each before-image field with the database
and write. A changed field, a vanished row, a changed duplicate group or a cell that is
no longer NULL rolls the whole transaction back; the journal records why. When nothing
gets written (the re-read plan is empty, or the transaction rolled back) the backup is
deleted: the database is as it was, so the copy has nothing to restore.

Undo
----
Puts NULL back into the cells of each journaled decision whose cells still hold what
the journal wrote. A decision changed since (for instance a row the API measured later)
is left alone and listed. Dry run unless asked to apply.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aiteam import __version__
from aiteam.clock import utc_now
from aiteam.services.attribution_marks import (
    AgentSnapshot,
    identify_exclusions,
    identify_unmeasurable,
    infer_harness,
)
from aiteam.services.codex_activity import codex_home
from aiteam.services.usage_coverage import LEADER_ROLE, is_in_flight
from aiteam.types import AttributionExclusion

TOOL = "aiteam attribution repair"
JOURNAL_FORMAT = 1

# PRAGMA user_version of a database whose timestamps are stored as UTC. Same value as
# MIGRATION_MARKER in scripts/migrate_timestamps_utc.py and UTC_CLOCK_MARKER in
# aiteam.storage.connection (tests pin them together). Kept here rather than imported
# from storage: that module pulls in SQLAlchemy and resolves the default database path
# on import, which this command does not need (the CLI resolves it through
# json_integrity.default_db_path).
UTC_CLOCK_MARKER = 20260728
UNMARKED_CLOCK = 0

READING_UTC = "utc"
READING_LOCAL = "local"

KIND_HARNESS = "harness"
KIND_EXCLUSION = "exclusion"
KIND_UNMEASURABLE = "unmeasurable"
_KIND_ORDER = {KIND_HARNESS: 0, KIND_EXCLUSION: 1, KIND_UNMEASURABLE: 2}

# The only columns this module ever writes.
TARGET_COLUMNS = ("harness", "attribution_exclusion", "attribution_exclusion_of", "usage_unmeasurable")
# agents columns behind AgentSnapshot, plus team_id and the written columns.
ROW_COLUMNS = (
    "role", "source", "status", "harness", "session_id", "cc_tool_use_id", "transcript_path",
    "tokens_measured_at", "created_at", "last_active_at", "team_id",
    "attribution_exclusion", "attribution_exclusion_of", "usage_unmeasurable",
)
# Before-image of a row that gets a decision: the columns above and the two values
# derived from other tables. Taken together the rules read every one of them (the
# in-flight window reads status and last_active_at, the resume-shell rule reads
# activity_count, harness inference reads team_name), so all are compared.
IMAGE_FIELDS = (*ROW_COLUMNS, "team_name", "activity_count")
# Before-image of a row that is read but not written: the representative of a duplicate
# group. identify_exclusions groups Leaders by role and transcript_path and ranks them
# by attribution_marks._representative_key (tokens_measured_at, session_id, created_at,
# id); the written columns are compared too. status, last_active_at and activity_count
# move with every hook call of a live Leader and decide nothing about its duplicates,
# so comparing them would roll a correct apply back for nothing.
REPRESENTATIVE_FIELDS = ("role", "transcript_path", "session_id", "tokens_measured_at", "created_at",
                         *TARGET_COLUMNS)
TIME_COLUMNS = ("tokens_measured_at", "created_at", "last_active_at")

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_ROLLED_BACK = "rolled_back"

LOCK_TIMEOUT_SECONDS = 10.0
_CHUNK = 500
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class RepairRefusedError(Exception):
    """Refused before anything was written; the message says why."""


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------


def canonical(db: Path) -> Path:
    """The path journals record and compare (``~`` expanded, symlinks resolved)."""
    return db.expanduser().resolve()


def open_readonly(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise RepairRefusedError(f"数据库不存在：{db}")
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _open_writer(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise RepairRefusedError(f"数据库不存在：{db}")
    con = sqlite3.connect(db, timeout=LOCK_TIMEOUT_SECONDS, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def user_version(con: sqlite3.Connection) -> int:
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def clock_readings(version: int) -> tuple[str, ...]:
    """How timestamps are read for a database with this ``user_version``."""
    if version == UTC_CLOCK_MARKER:
        return (READING_UTC,)
    if version == UNMARKED_CLOCK:
        return (READING_UTC, READING_LOCAL)
    raise RepairRefusedError(
        f"PRAGMA user_version = {version}，不是已知的时钟制式"
        f"（{UTC_CLOCK_MARKER} 表示 UTC，0 表示未标注），不处理"
    )


def missing_columns(con: sqlite3.Connection) -> tuple[str, ...]:
    present = {row[1] for row in con.execute("PRAGMA table_info(agents)")}
    if not present:
        raise RepairRefusedError("数据库里没有 agents 表")
    return tuple(column for column in ROW_COLUMNS if column not in present)


@contextmanager
def _one_snapshot(con: sqlite3.Connection) -> Iterator[None]:
    """Run the reads inside one read transaction, so they see one database state."""
    if con.in_transaction:
        yield
        return
    con.execute("BEGIN")
    try:
        yield
    finally:
        con.execute("ROLLBACK")


def _chunks(items: list[str]) -> Iterator[list[str]]:
    for start in range(0, len(items), _CHUNK):
        yield items[start:start + _CHUNK]


def load_images(con: sqlite3.Connection, ids: Iterable[str] | None = None) -> dict[str, dict]:
    """Raw values of every agents row (or only ``ids``), keyed by id.

    A column the database does not have yet reads as NULL, so a dry run works before
    the API has restarted on new code; apply refuses that state (:func:`apply_repair`).
    """
    absent = set(missing_columns(con))
    columns = ", ".join(f"NULL AS {c}" if c in absent else f"a.{c}" for c in ROW_COLUMNS)
    base = f"SELECT a.id, {columns}, t.name AS team_name FROM agents a LEFT JOIN teams t ON t.id = a.team_id"  # noqa: S608
    rows: list[sqlite3.Row] = []
    activity: dict[str, int] = {}
    if ids is None:
        rows = con.execute(base).fetchall()
        activity = dict(con.execute("SELECT agent_id, count(*) FROM agent_activities GROUP BY agent_id").fetchall())
    else:
        for chunk in _chunks(sorted(set(ids))):
            marks = ", ".join("?" * len(chunk))
            rows += con.execute(f"{base} WHERE a.id IN ({marks})", chunk).fetchall()  # noqa: S608
            activity.update(con.execute(
                f"SELECT agent_id, count(*) FROM agent_activities WHERE agent_id IN ({marks}) GROUP BY agent_id",  # noqa: S608
                chunk,
            ).fetchall())
    images: dict[str, dict] = {}
    for row in rows:
        image = {column: row[column] for column in ROW_COLUMNS}
        image["team_name"] = row["team_name"]
        image["activity_count"] = activity.get(row["id"], 0)
        images[row["id"]] = image
    return images


def _parse_time(raw: object) -> datetime:
    """Raises ValueError for a value that is not an ISO timestamp."""
    return datetime.fromisoformat(str(raw).strip())


def unreadable_times(image: dict) -> tuple[str, ...]:
    """The time columns of a row holding a value that does not parse.

    Such a value is unknown, neither absent nor now: read as absent it makes a measured
    row look unmeasured, read as now it makes the row the freshest measurement of its
    duplicate group. The caller gives the row no verdict that reads time.
    """
    unreadable = []
    for column in TIME_COLUMNS:
        raw = image[column]
        if raw is None or raw == "":
            continue
        try:
            _parse_time(raw)
        except ValueError:
            unreadable.append(column)
    return tuple(unreadable)


def read_time(raw: object, reading: str) -> datetime | None:
    """A stored timestamp as aware UTC under one clock reading; None when absent.

    Raises ValueError for a value that does not parse (:func:`unreadable_times` finds
    those rows first).
    """
    if raw is None or raw == "":
        return None
    value = _parse_time(raw)
    if value.tzinfo is not None:
        return value.astimezone(UTC)
    if reading == READING_UTC:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)  # naive read as this host's wall clock


def snapshot(agent_id: str, image: dict, reading: str | None) -> AgentSnapshot:
    """The rules' view of a row. ``reading=None`` leaves every time field empty, for
    infer_harness, which reads none."""
    def time(column: str) -> datetime | None:
        return None if reading is None else read_time(image[column], reading)

    return AgentSnapshot(
        id=agent_id,
        role=image["role"] or "",
        source=image["source"] or "",
        status=image["status"] or "",
        harness=image["harness"],
        session_id=image["session_id"],
        cc_tool_use_id=image["cc_tool_use_id"],
        transcript_path=image["transcript_path"],
        tokens_measured_at=time("tokens_measured_at"),
        created_at=time("created_at"),
        team_name=image["team_name"] or "",
        activity_count=image["activity_count"],
        last_active_at=time("last_active_at"),
    )


# ---------------------------------------------------------------------------
# File probes
# ---------------------------------------------------------------------------


class Probes:
    """File-system evidence the rules ask for, read once per run and cached.

    The first pass fills the caches; the re-read just before writing reuses them, so
    transcript parsing never happens near the write lock.
    """

    def __init__(self, *, cc_projects: Path | None = None, codex: Path | None = None) -> None:
        if cc_projects is None:
            from aiteam.services.notices.transcript import projects_root

            cc_projects = projects_root()
        self.cc_projects = cc_projects
        self.codex_home = codex if codex is not None else codex_home()
        self._cc_sessions: set[str] | None = None
        self._rollouts: set[str] | None = None
        self._exists: dict[str, bool] = {}
        self._reasons: dict[str, str | None] = {}
        self._codex: dict[tuple[str, str | None], str | None] = {}

    def cc_session_exists(self, session_id: str) -> bool:
        """A main transcript ``<projects>/<slug>/<session_id>.jsonl`` exists."""
        if self._cc_sessions is None:
            self._cc_sessions = {path.stem for path in self.cc_projects.glob("*/*.jsonl")}
        return session_id in self._cc_sessions

    def codex_rollout_exists(self, session_id: str) -> bool:
        """A Codex rollout (live or archived) is named after this thread id."""
        if self._rollouts is None:
            ids: set[str] = set()
            for sub in ("sessions", "archived_sessions"):
                base = self.codex_home / sub
                if base.is_dir():
                    for path in base.rglob("rollout-*.jsonl"):
                        ids.update(_UUID.findall(path.name))
            self._rollouts = ids
        return session_id in self._rollouts

    def transcript_exists(self, path: str) -> bool:
        if path not in self._exists:
            try:
                self._exists[path] = Path(path).is_file()
            except OSError:
                self._exists[path] = False
        return self._exists[path]

    def reason_of(self, path: str) -> str | None:
        if path not in self._reasons:
            from aiteam.services.token_attribution import unmeasurable_reason

            self._reasons[path] = unmeasurable_reason(path)
        return self._reasons[path]

    def codex_reason_of(self, row: AgentSnapshot) -> str | None:
        """``no_usage_lines`` when the parser finds the thread's rollout and it has no
        usage event; None when nothing is found or the parser refuses."""
        thread = row.session_id if row.role == LEADER_ROLE else row.cc_tool_use_id
        if not thread:
            return None
        key = (thread, row.transcript_path)
        if key not in self._codex:
            from aiteam.services.codex_rollout_usage import (
                CodexRolloutUsageError,
                parse_codex_rollout_usage_state,
            )

            try:
                usage, found = parse_codex_rollout_usage_state(
                    row.transcript_path, expected_session_id=thread, codex_home=self.codex_home
                )
            except CodexRolloutUsageError:
                usage, found = None, False
            self._codex[key] = "no_usage_lines" if usage is None and found else None
        return self._codex[key]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """One verdict on one row: one cell, or the exclusion pair. Old values are NULL."""

    agent_id: str
    kind: str
    cells: tuple[tuple[str, str], ...]
    evidence: str

    @property
    def key(self) -> tuple:
        return (self.agent_id, self.kind, self.cells)


@dataclass
class Plan:
    db: Path
    user_version: int
    readings: tuple[str, ...]
    now: datetime
    decisions: list[Decision]
    # Before-image of every row a verdict read: IMAGE_FIELDS of the targets,
    # REPRESENTATIVE_FIELDS of the representatives that are not targets themselves.
    before: dict[str, dict]
    # transcript_path -> Leader ids sharing it, for every duplicate verdict.
    groups: dict[str, list[str]]
    in_flight: int
    unlabelled: int
    # Rows with a time value that does not parse (no verdict that reads time).
    unreadable: int = 0
    missing_columns: tuple[str, ...] = ()

    @property
    def cells(self) -> int:
        return sum(len(d.cells) for d in self.decisions)

    @property
    def rows(self) -> int:
        return len({d.agent_id for d in self.decisions})

    def counts(self) -> dict[str, int]:
        """Cells per ``column=value``; ``attribution_exclusion_of`` counted as one key."""
        counter: Counter[str] = Counter()
        for decision in self.decisions:
            for column, value in decision.cells:
                counter[column if column == "attribution_exclusion_of" else f"{column}={value}"] += 1
        return dict(sorted(counter.items()))

    def harness_evidence(self) -> dict[str, int]:
        counter = Counter(
            f"{d.cells[0][1]} {d.evidence}" for d in self.decisions if d.kind == KIND_HARNESS
        )
        return dict(sorted(counter.items()))


def _open_cells(image: dict, cells: tuple[tuple[str, str], ...]) -> bool:
    return all(image[column] is None for column, _ in cells)


def _mark_decisions(images: dict[str, dict], reading: str, probes: Probes, now: datetime,
                    unreadable: set[str]) -> dict[tuple, Decision]:
    """Exclusion and unmeasurable verdicts under one clock reading.

    Rows in ``unreadable`` are left out of the rules; a duplicate verdict on a transcript
    one of them shares is dropped as well, since that row might have represented it.
    """
    snapshots = [snapshot(agent_id, image, reading) for agent_id, image in images.items()
                 if agent_id not in unreadable]
    unsettled_groups = {
        images[agent_id]["transcript_path"] for agent_id in unreadable
        if (images[agent_id]["role"] or "") == LEADER_ROLE and images[agent_id]["transcript_path"]
    }
    found: dict[tuple, Decision] = {}
    for mark in identify_exclusions(snapshots, session_transcript_exists=probes.cc_session_exists, now=now):
        if mark.kind is AttributionExclusion.DUPLICATE_TRANSCRIPT and \
                images[mark.agent_id]["transcript_path"] in unsettled_groups:
            continue
        cells: tuple[tuple[str, str], ...] = (("attribution_exclusion", mark.kind.value),)
        if mark.of:
            cells += (("attribution_exclusion_of", mark.of),)
        if _open_cells(images[mark.agent_id], cells):
            decision = Decision(mark.agent_id, KIND_EXCLUSION, cells, mark.evidence)
            found[decision.key] = decision
    for mark in identify_unmeasurable(
        [s for s in snapshots if images[s.id]["usage_unmeasurable"] is None],
        reason_of=probes.reason_of,
        transcript_exists=probes.transcript_exists,
        now=now,
        codex_reason_of=probes.codex_reason_of,
    ):
        decision = Decision(mark.agent_id, KIND_UNMEASURABLE, (("usage_unmeasurable", mark.reason.value),),
                            mark.evidence)
        found[decision.key] = decision
    return found


def build_plan(con: sqlite3.Connection, db: Path, probes: Probes, now: datetime) -> Plan:
    """What the rules would write on this database now. Reads only."""
    with _one_snapshot(con):
        version = user_version(con)
        readings = clock_readings(version)
        absent = missing_columns(con)
        images = load_images(con)

    decisions: list[Decision] = []
    labels: dict[str, str] = {}
    for agent_id, image in images.items():
        mark = infer_harness(
            snapshot(agent_id, image, None),  # reads no timestamp
            cc_session_transcript_exists=probes.cc_session_exists,
            codex_rollout_exists=probes.codex_rollout_exists,
        )
        if mark is not None:
            labels[agent_id] = mark.harness.value
            decisions.append(Decision(agent_id, KIND_HARNESS, (("harness", mark.harness.value),), mark.evidence))

    # Step 2 judges the rows as step 1 leaves them (module docstring, "Steps").
    labelled = {aid: {**img, "harness": labels[aid]} if aid in labels else img for aid, img in images.items()}
    unreadable = {aid for aid, image in images.items() if unreadable_times(image)}
    per_reading = [_mark_decisions(labelled, reading, probes, now, unreadable) for reading in readings]
    agreed = set(per_reading[0]).intersection(*per_reading[1:])
    decisions += [decision for key, decision in per_reading[0].items() if key in agreed]
    decisions.sort(key=lambda d: (_KIND_ORDER[d.kind], d.agent_id))

    targets = {d.agent_id for d in decisions}
    representatives: set[str] = set()
    by_transcript: dict[str, list[str]] = defaultdict(list)
    for agent_id, image in images.items():
        if (image["role"] or "") == LEADER_ROLE and image["transcript_path"]:
            by_transcript[image["transcript_path"]].append(agent_id)
    groups: dict[str, list[str]] = {}
    for decision in decisions:
        representative = dict(decision.cells).get("attribution_exclusion_of")
        if representative:
            representatives.add(representative)
            path = images[decision.agent_id]["transcript_path"]
            groups[path] = sorted(by_transcript[path])
    before = {aid: dict(images[aid]) for aid in targets}
    before.update({aid: {name: images[aid][name] for name in REPRESENTATIVE_FIELDS}
                   for aid in representatives - targets})

    in_flight = sum(
        1
        for aid, image in images.items()
        if aid not in unreadable and image["tokens_measured_at"] is None and image["usage_unmeasurable"] is None
        and any(is_in_flight(image["status"], read_time(image["last_active_at"], reading), now) for reading in readings)
    )
    unlabelled = sum(1 for aid, image in images.items() if image["harness"] is None and aid not in labels)
    return Plan(
        db=db,
        user_version=version,
        readings=readings,
        now=now,
        decisions=decisions,
        before=dict(sorted(before.items())),
        groups=groups,
        in_flight=in_flight,
        unlabelled=unlabelled,
        unreadable=len(unreadable),
        missing_columns=absent,
    )


def preview(db: Path, probes: Probes, now: datetime | None = None) -> Plan:
    db = canonical(db)
    with closing(open_readonly(db)) as con:
        return build_plan(con, db, probes, now or utc_now())


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@dataclass
class ApplyOutcome:
    status: str  # "nothing" | STATUS_APPLIED | STATUS_ROLLED_BACK
    plan: Plan
    backup: Path | None = None
    journal: Path | None = None
    reason: str = ""
    mismatches: list[dict] = field(default_factory=list)
    # The backup was taken, nothing got written, and the copy is deleted again.
    backup_discarded: bool = False


def _stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%S%fZ")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_new(path: Path, record: dict) -> None:
    """Create ``path`` (never overwrite) and make it durable."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_dir(path.parent)


def _rewrite(path: Path, record: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _discard_backup(backup: Path) -> str:
    """Delete the backup of a database nothing was written to; '' or why it stays."""
    try:
        backup.unlink(missing_ok=True)
    except OSError as exc:
        return f"没有写入，但备份没能删除（{exc}），可以手工删掉：{backup}"
    return ""


def take_backup(db: Path, destination: Path) -> None:
    """Full copy through SQLite's online backup (consistent while the API writes)."""
    os.close(os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    with closing(open_readonly(db)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)


def journal_record(plan: Plan, backup: Path) -> dict:
    return {
        "tool": TOOL,
        "format": JOURNAL_FORMAT,
        "aiteam_version": __version__,
        "db": str(plan.db),
        "user_version": plan.user_version,
        "readings": list(plan.readings),
        "planned_at": _iso(plan.now),
        "backup": str(backup),
        "status": STATUS_PENDING,
        "cells": plan.cells,
        "counts": plan.counts(),
        "changes": [
            {
                "agent_id": d.agent_id,
                "kind": d.kind,
                "evidence": d.evidence,
                "cells": [{"column": column, "old": None, "new": value} for column, value in d.cells],
            }
            for d in plan.decisions
        ],
        "before": plan.before,
        "groups": plan.groups,
    }


def compare_before(con: sqlite3.Connection, plan: Plan) -> list[dict]:
    """Every field of the plan's before-images that no longer matches the database.

    Each before-image holds exactly the fields to compare (IMAGE_FIELDS for a target,
    REPRESENTATIVE_FIELDS for a representative only read).
    """
    current = load_images(con, plan.before)
    mismatches: list[dict] = []
    for agent_id, image in plan.before.items():
        now_image = current.get(agent_id)
        if now_image is None:
            mismatches.append({"agent_id": agent_id, "field": None, "before": "row", "now": None})
            continue
        for name, value in image.items():
            if now_image[name] != value:
                mismatches.append({"agent_id": agent_id, "field": name, "before": value, "now": now_image[name]})
    for path, members in plan.groups.items():
        now_members = sorted(
            row[0] for row in con.execute(
                "SELECT id FROM agents WHERE role = ? AND transcript_path = ?", (LEADER_ROLE, path)
            )
        )
        if now_members != members:
            mismatches.append({"group": path, "before": members, "now": now_members})
    return mismatches


class _ConflictError(Exception):
    def __init__(self, reason: str, mismatches: list[dict] | None = None) -> None:
        super().__init__(reason)
        self.mismatches = mismatches or []


def commit_plan(db: Path, plan: Plan, journal: Path, record: dict) -> ApplyOutcome:
    """Compare every before-image field and write every cell in one transaction."""
    backup = Path(record["backup"])
    con = _open_writer(db)
    try:
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            return _finish(journal, record, plan, backup, STATUS_ROLLED_BACK, f"写锁未拿到（{exc}），未写入")
        try:
            if user_version(con) != plan.user_version:
                raise _ConflictError("PRAGMA user_version 在计划之后变了")
            if missing_columns(con):
                raise _ConflictError("agents 缺列")
            mismatches = compare_before(con, plan)
            if mismatches:
                raise _ConflictError(f"计划之后有 {len(mismatches)} 处 before-image 对不上", mismatches)
            for decision in plan.decisions:
                for column, value in decision.cells:
                    if column not in TARGET_COLUMNS:
                        raise _ConflictError(f"计划里出现了不允许写的列 {column}")
                    cursor = con.execute(
                        f"UPDATE agents SET {column} = ? WHERE id = ? AND {column} IS NULL",  # noqa: S608
                        (value, decision.agent_id),
                    )
                    if cursor.rowcount != 1:
                        raise _ConflictError(f"{decision.agent_id}.{column} 应写 1 行，实际 {cursor.rowcount} 行")
            con.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - any failure rolls everything back
            if con.in_transaction:
                con.execute("ROLLBACK")
            mismatches = exc.mismatches if isinstance(exc, _ConflictError) else []
            return _finish(journal, record, plan, backup, STATUS_ROLLED_BACK, f"{exc}；整事务已回滚，未写入",
                           mismatches)
    finally:
        con.close()
    return _finish(journal, record, plan, backup, STATUS_APPLIED, "")


def _finish(journal: Path, record: dict, plan: Plan, backup: Path, status: str, reason: str,
            mismatches: list[dict] | None = None) -> ApplyOutcome:
    discarded = False
    if status == STATUS_ROLLED_BACK:
        # The transaction never committed, so the database is the backup's twin.
        note = _discard_backup(backup)
        discarded = not note
        record["backup_discarded"] = discarded
        reason = f"{reason}；{note}" if note else reason
    record["status"] = status
    record["finished_at"] = _iso(utc_now())
    if reason:
        record["reason"] = reason
    if mismatches:
        record["mismatches"] = mismatches
    try:
        _rewrite(journal, record)
    except OSError as exc:
        # The database outcome stands; a journal left at "pending" is still undoable.
        reason = f"{reason}（journal 状态没能更新：{exc}）" if reason else f"journal 状态没能更新为 {status}：{exc}"
    return ApplyOutcome(status, plan, backup, journal, reason, list(mismatches or []), discarded)


def apply_repair(db: Path, *, out_dir: Path, probes: Probes, now: datetime | None = None) -> ApplyOutcome:
    """Clock check, backup, journal, then the guarded transaction (module docstring).

    ``now`` pins the clock for both passes (tests); by default each pass reads it.
    """
    db = canonical(db)
    with closing(open_readonly(db)) as con:
        first = build_plan(con, db, probes, now or utc_now())
    if first.missing_columns:
        raise RepairRefusedError(
            f"agents 缺少列 {', '.join(first.missing_columns)}：先让 API 在新版本上重启"
            "（启动迁移会补列），再运行 --apply"
        )
    if not first.decisions:
        return ApplyOutcome("nothing", first)
    stamp = _stamp()
    backup = out_dir / f"{db.name}.bak-attribution-{stamp}"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        take_backup(db, backup)
    except (OSError, sqlite3.Error) as exc:
        backup.unlink(missing_ok=True)  # a partial copy must not pass for a backup
        raise RepairRefusedError(f"备份失败（{exc}），未写入") from exc
    # Rows may have moved while the backup ran: plan again from fresh rows. The probes
    # are cached, so this pass only reads the database.
    with closing(open_readonly(db)) as con:
        plan = build_plan(con, db, probes, now or utc_now())
    if not plan.decisions:
        # Nothing left to write after the rows moved: the copy has nothing to restore.
        note = _discard_backup(backup)
        return ApplyOutcome("nothing", plan, backup, reason=note, backup_discarded=not note)
    journal = out_dir / f"attribution-repair-{stamp}.journal.json"
    record = journal_record(plan, backup)
    try:
        write_journal(journal, record)
    except OSError as exc:
        raise RepairRefusedError(f"journal 写不了（{exc}），未写入；备份在 {backup}") from exc
    return commit_plan(db, plan, journal, record)


def write_journal(path: Path, record: dict) -> None:
    """The journal is on disk before the transaction starts."""
    _write_new(path, record)


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


@dataclass
class UndoOutcome:
    status: str  # "preview" | "nothing" | "undone" | STATUS_ROLLED_BACK
    journal: dict
    revert: list[dict]
    already: list[dict]
    changed: list[dict]
    record: Path | None = None
    reason: str = ""


def load_journal(path: Path, db: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepairRefusedError(f"读不了 journal {path}：{exc}") from exc
    if not isinstance(data, dict) or data.get("tool") != TOOL or data.get("format") != JOURNAL_FORMAT:
        raise RepairRefusedError(f"{path} 不是本命令写的 journal（或格式版本不符）")
    if data.get("db") != str(db):
        raise RepairRefusedError(f"journal 属于另一个数据库：{data.get('db')}（当前 {db}）；用 --db 指向它")
    if data.get("status") == STATUS_ROLLED_BACK:
        raise RepairRefusedError("这份 journal 对应的写入已整事务回滚，没有可撤回的内容")
    if data.get("status") not in (STATUS_APPLIED, STATUS_PENDING):
        raise RepairRefusedError(f"journal 状态 {data.get('status')!r} 不可撤回")
    changes = data.get("changes")
    if not isinstance(changes, list) or not all(_valid_change(change) for change in changes):
        raise RepairRefusedError("journal 内容越界（只允许把四个标记列从 NULL 改成字符串）")
    return data


def _valid_change(change: object) -> bool:
    if not isinstance(change, dict) or not isinstance(change.get("agent_id"), str):
        return False
    cells = change.get("cells")
    return isinstance(cells, list) and bool(cells) and all(
        isinstance(cell, dict) and cell.get("column") in TARGET_COLUMNS and cell.get("old") is None
        and isinstance(cell.get("new"), str)
        for cell in cells
    )


def classify(con: sqlite3.Connection, changes: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split journaled decisions into revertible, already reverted, and changed since."""
    current: dict[str, sqlite3.Row] = {}
    columns = ", ".join(TARGET_COLUMNS)
    for chunk in _chunks(sorted({c["agent_id"] for c in changes})):
        marks = ", ".join("?" * len(chunk))
        for row in con.execute(f"SELECT id, {columns} FROM agents WHERE id IN ({marks})", chunk):  # noqa: S608
            current[row["id"]] = row
    revert, already, changed = [], [], []
    for change in changes:
        row = current.get(change["agent_id"])
        cells = change["cells"]
        if row is not None and all(row[cell["column"]] == cell["new"] for cell in cells):
            revert.append(change)
        elif row is not None and all(row[cell["column"]] == cell["old"] for cell in cells):
            already.append(change)
        else:
            now = {cell["column"]: row[cell["column"]] for cell in cells} if row is not None else None
            changed.append({**change, "now": now})
    return revert, already, changed


def undo_repair(db: Path, journal_path: Path, *, apply: bool) -> UndoOutcome:
    db = canonical(db)
    data = load_journal(journal_path, db)
    changes = data["changes"]
    with closing(open_readonly(db)) as con:
        revert, already, changed = classify(con, changes)
    if not apply:
        return UndoOutcome("preview", data, revert, already, changed)
    if not revert:
        return UndoOutcome("nothing", data, revert, already, changed)
    con = _open_writer(db)
    try:
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            return UndoOutcome(STATUS_ROLLED_BACK, data, revert, already, changed,
                               reason=f"写锁未拿到（{exc}），未写入")
        try:
            revert, already, changed = classify(con, changes)  # again, under the lock
            for change in revert:
                for cell in change["cells"]:
                    column = cell["column"]
                    cursor = con.execute(
                        f"UPDATE agents SET {column} = ? WHERE id = ? AND {column} = ?",  # noqa: S608
                        (cell["old"], change["agent_id"], cell["new"]),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"{change['agent_id']}.{column} 应改 1 行，实际 {cursor.rowcount} 行")
            con.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001
            if con.in_transaction:
                con.execute("ROLLBACK")
            return UndoOutcome(STATUS_ROLLED_BACK, data, revert, already, changed, reason=f"{exc}；整事务已回滚")
    finally:
        con.close()
    record_path: Path | None = journal_path.with_name(f"{journal_path.name}.undo-{_stamp()}.json")
    try:
        _write_new(record_path, {
            "tool": TOOL,
            "format": JOURNAL_FORMAT,
            "journal": str(journal_path),
            "db": str(db),
            "undone_at": _iso(utc_now()),
            "reverted": revert,
            "already_reverted": len(already),
            "changed_since": changed,
        })
    except OSError:
        record_path = None  # the revert is committed; only its record is missing
    return UndoOutcome("undone", data, revert, already, changed, record_path)
