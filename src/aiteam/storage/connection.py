"""AI Team OS — Async database connection management.

Provides SQLAlchemy async engine and session management with automatic table creation.
Supports per-project database isolation via EnginePool.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
)

from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.models import Base

logger = logging.getLogger(__name__)


def _migrate_old_db_if_needed(new_db_path: Path) -> None:
    """Detect old DB and auto-migrate to new path.

    Only copies the database file from the old location when the new DB
    does not exist or is very small (<10KB).
    After migration, the old file is renamed to .db.migrated to avoid repeated migrations.
    All errors are silently handled to not block startup.
    """
    try:
        if new_db_path.exists() and new_db_path.stat().st_size > 10000:
            return  # New DB already has data, skip migration

        old_candidates = [
            Path.cwd() / "aiteam.db",
        ]

        for old_path in old_candidates:
            if old_path.exists() and old_path.stat().st_size > 10000:
                import shutil

                shutil.copy2(str(old_path), str(new_db_path))
                old_path.rename(old_path.with_suffix(".db.migrated"))
                break
    except Exception:
        pass  # Silent handling, do not block startup


def _default_db_url() -> str:
    """Build the default database URL, using fixed path ~/.claude/data/ai-team-os/aiteam.db."""
    data_dir = Path.home() / ".claude" / "data" / "ai-team-os"
    data_dir.mkdir(parents=True, exist_ok=True)
    new_db_path = data_dir / "aiteam.db"
    _migrate_old_db_if_needed(new_db_path)
    return f"sqlite+aiosqlite:///{new_db_path}"


# Default database URL
DEFAULT_DB_URL = _default_db_url()


def get_engine(db_url: str | None = None) -> AsyncEngine:
    """Get or create an async database engine.

    Uses the global EnginePool to support multiple concurrent databases
    (per-project isolation).

    Args:
        db_url: Database connection URL; uses default when empty.

    Returns:
        AsyncEngine instance.
    """
    url = db_url or DEFAULT_DB_URL
    return engine_pool.get_engine(url)


@asynccontextmanager
async def get_session(
    db_url: str | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Context manager for obtaining an async database session.

    Uses the EnginePool to route to the correct database.

    Usage:
        async with get_session() as session:
            result = await session.execute(...)

    Args:
        db_url: Optional database URL.

    Yields:
        AsyncSession instance.
    """
    url = db_url or DEFAULT_DB_URL
    factory = engine_pool.get_session_factory(url)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


COLUMNS_TO_ENSURE: list[tuple[str, str, str]] = [
    ("meetings", "meta_json", "JSON DEFAULT NULL"),
    ("meeting_messages", "metadata_json", "JSON DEFAULT NULL"),
    # Stage J: per-project isolation for ecosystem tables (5 data tables;
    # ecosystem_tags dictionary stays global).
    ("ecosystem_repo_profiles", "project_id", "VARCHAR(36)"),
    ("ecosystem_deep_reviews", "project_id", "VARCHAR(36)"),
    ("ecosystem_repo_tags", "project_id", "VARCHAR(36)"),
    ("ecosystem_relations", "project_id", "VARCHAR(36)"),
    ("ecosystem_scan_runs", "project_id", "VARCHAR(36)"),
    # K5: split dispatch prompt out of demo_log_excerpt
    ("ecosystem_deep_reviews", "dispatch_prompt", "TEXT DEFAULT ''"),
    # v1.5.0-A: EcosystemDeepReview 渐进式漏斗 stage 状态 + 关联会议/集成任务
    ("ecosystem_deep_reviews", "stage_status", "VARCHAR(30) DEFAULT 'queued'"),
    ("ecosystem_deep_reviews", "integration_md", "TEXT DEFAULT ''"),
    ("ecosystem_deep_reviews", "shallow_completed_at", "DATETIME"),
    ("ecosystem_deep_reviews", "architecture_completed_at", "DATETIME"),
    ("ecosystem_deep_reviews", "debated_at", "DATETIME"),
    ("ecosystem_deep_reviews", "stage3_completed_at", "DATETIME"),
    ("ecosystem_deep_reviews", "debate_meeting_id", "VARCHAR(36)"),
    ("ecosystem_deep_reviews", "integration_task_id", "VARCHAR(36)"),
    # v1.5.0-A: EcosystemRepoProfile 浅扫 + 失败追踪 + 活跃集
    ("ecosystem_repo_profiles", "shallow_summary", "TEXT DEFAULT ''"),
    ("ecosystem_repo_profiles", "last_shallow_refreshed_at", "DATETIME"),
    ("ecosystem_repo_profiles", "is_deleted", "BOOLEAN DEFAULT 0"),
    ("ecosystem_repo_profiles", "is_private_now", "BOOLEAN DEFAULT 0"),
    ("ecosystem_repo_profiles", "last_fetch_error", "TEXT DEFAULT ''"),
    ("ecosystem_repo_profiles", "fetch_failure_count", "INTEGER DEFAULT 0"),
    ("ecosystem_repo_profiles", "is_active", "BOOLEAN DEFAULT 1"),
    ("ecosystem_repo_profiles", "active_rank", "INTEGER"),
    # v1.5.3: worker pool claim 字段
    ("ecosystem_deep_reviews", "claimed_by", "TEXT"),
    ("ecosystem_deep_reviews", "claimed_at", "DATETIME"),
    ("ecosystem_deep_reviews", "quality_score", "INTEGER"),
    ("ecosystem_deep_reviews", "quality_notes", "TEXT"),
    ("ecosystem_deep_reviews", "reviewed_by", "TEXT"),
    ("ecosystem_deep_reviews", "reviewed_at", "DATETIME"),
    # v1.6.0-P0: multi-source fields on ecosystem_repo_profiles
    ("ecosystem_repo_profiles", "canonical_id", "VARCHAR(200)"),
    ("ecosystem_repo_profiles", "source_kind", "VARCHAR(20) DEFAULT 'github'"),
    ("ecosystem_repo_profiles", "last_active_status", "VARCHAR(20)"),
    ("ecosystem_repo_profiles", "last_status_change_at", "DATETIME"),
    # v1.6.0-P0.4: NormalizedSignal fields on ecosystem_repo_profiles
    ("ecosystem_repo_profiles", "popularity_percentile", "FLOAT"),
    ("ecosystem_repo_profiles", "activity_score", "FLOAT"),
    # v1.6.0-P1.A: manual status fields for human-flagged no-value repos
    ("ecosystem_repo_profiles", "manual_status", "VARCHAR(20)"),
    ("ecosystem_repo_profiles", "manual_status_reason", "TEXT"),
    ("ecosystem_repo_profiles", "manual_status_set_at", "DATETIME"),
    ("ecosystem_repo_profiles", "manual_status_set_by", "VARCHAR(100)"),
    # v1.6.0-P1 hotfix: new semantically-correct columns for index_diffs schema
    ("ecosystem_index_diffs", "github_archived_changed_count", "INTEGER DEFAULT 0"),
    ("ecosystem_index_diffs", "removed_from_query_count", "INTEGER DEFAULT 0"),
    # v1.6.0-P1.C-1: which search queries first/subsequently discovered this repo
    ("ecosystem_repo_profiles", "discovered_via_queries", "TEXT DEFAULT NULL"),
    # v1.6.1 multi-source: sources JSON list + primary_source enum
    # sources = [{kind, id, stars/likes, url, last_seen_at}, ...]
    # primary_source = "github" / "hf_space" / "gitlab" — decides canonical_id & UI primary link
    ("ecosystem_repo_profiles", "sources", "TEXT DEFAULT NULL"),
    ("ecosystem_repo_profiles", "primary_source", "VARCHAR(20) DEFAULT 'github'"),
    # v1.6.0 event sourcing: ecosystem_repo_events columns (table created by create_all,
    # these entries guard against future column additions on existing DBs)
    ("ecosystem_repo_events", "payload_json", "TEXT"),
    ("ecosystem_repo_events", "source", "VARCHAR(20) DEFAULT 'scanner'"),
    ("ecosystem_repo_events", "scan_run_id", "VARCHAR(36)"),
    ("ecosystem_repo_events", "from_status", "VARCHAR(20)"),
    ("ecosystem_repo_events", "to_status", "VARCHAR(20)"),
    ("ecosystem_repo_events", "reason", "TEXT"),
    # v1.7.0: shallow batch management
    ("ecosystem_deep_reviews", "batch_id", "VARCHAR(36)"),
    # v1.6.1 Phase 2: alert threshold migrated from scan_profile to settings
    ("ecosystem_project_settings", "alert_max_new_per_scan", "INTEGER DEFAULT 50"),
    # v1.6.1 Phase 2: scan run real metadata change counter
    ("ecosystem_scan_runs", "metadata_changed_count", "INTEGER DEFAULT 0"),
    # v1.6.2 Workflow 观测层 Phase2: live 追踪水位列（既有文件库 ALTER 补齐；
    # 新库/内存库由 models.py Mapped 列经 create_all 直接建齐）
    ("workflow_runs", "journal_offset", "INTEGER DEFAULT 0"),
    ("workflow_runs", "source_fingerprint", "VARCHAR(64) DEFAULT ''"),
    ("workflow_runs", "live_tokens", "INTEGER DEFAULT 0"),
    ("workflow_runs", "last_activity_at", "DATETIME"),
    # 跨项目修复A：回执 transcript_dir 持久化（live/终态直接寻址）
    ("workflow_runs", "transcript_dir", "VARCHAR(500) DEFAULT ''"),
    ("workflow_agents", "last_activity_at", "DATETIME"),
    # 记忆系统 v2 P1：方向层激活——memories 表加 4 列（既有库 ALTER 补齐；
    # 新库/内存库由 models.py Mapped 列经 create_all 直接建齐）
    ("memories", "kind", "VARCHAR(20) DEFAULT 'preference'"),
    ("memories", "invalid_at", "DATETIME"),
    ("memories", "invalidated_by", "VARCHAR(36)"),
    ("memories", "source_refs", "JSON DEFAULT '[]'"),
    # agent 复用治理 P1（批次 1B）：sub-agent 上下文水位台账列（既有库 ALTER 补齐；
    # 新库/内存库由 models.py Mapped 列经 create_all 直接建齐）。设计见
    # docs/agent-reuse-design.md §4.3。reuse_domain 为 P2 决策层预留，P1 不写入。
    ("agents", "ctx_tokens", "INTEGER"),
    ("agents", "ctx_window", "INTEGER"),
    ("agents", "ctx_pct", "FLOAT"),
    ("agents", "transcript_path", "TEXT"),
    ("agents", "ctx_measured_at", "DATETIME"),
    ("agents", "reuse_domain", "VARCHAR(200)"),
]


# K1 perf — composite indexes the SQLAlchemy create_all path emits for new
# DBs. Existing DBs need explicit CREATE INDEX IF NOT EXISTS migration.
# Each entry: (index_name, table, columns_csv).
INDEXES_TO_ENSURE: list[tuple[str, str, str]] = [
    (
        "ix_ecosystem_profiles_project_stars",
        "ecosystem_repo_profiles",
        "project_id, stars",
    ),
    (
        "ix_ecosystem_profiles_project_category_stars",
        "ecosystem_repo_profiles",
        "project_id, relevance_category, stars",
    ),
    (
        "ix_ecosystem_profiles_project_lang_stars",
        "ecosystem_repo_profiles",
        "project_id, language, stars",
    ),
    (
        "ix_ecosystem_profiles_project_pushed",
        "ecosystem_repo_profiles",
        "project_id, pushed_at",
    ),
    (
        "ix_ecosystem_profiles_project_archived_stars",
        "ecosystem_repo_profiles",
        "project_id, is_archived, stars",
    ),
    # v1.5.0-A perf — active set hot path (filter is_active + sort by stars)
    (
        "ix_ecosystem_profiles_project_active_stars",
        "ecosystem_repo_profiles",
        "project_id, is_active, stars",
    ),
    (
        "ix_ecosystem_profiles_project_deleted_private",
        "ecosystem_repo_profiles",
        "project_id, is_deleted, is_private_now",
    ),
    # v1.5.0-A: status snapshot table indexes
    (
        "ix_eco_status_snap_repo_time",
        "ecosystem_repo_status_snapshots",
        "repo_id, snapshot_at",
    ),
    (
        "ix_eco_status_snap_scan_run",
        "ecosystem_repo_status_snapshots",
        "scan_run_id",
    ),
    (
        "ix_eco_status_snap_project_repo",
        "ecosystem_repo_status_snapshots",
        "project_id, repo_id",
    ),
    # v1.5.0-A: deep_review stage_status filter
    (
        "ix_ecosystem_deep_reviews_stage_status",
        "ecosystem_deep_reviews",
        "stage_status",
    ),
]


def _column_exists(con: object, table: str, column: str) -> bool:
    """Return True if *column* exists in *table* (uses PRAGMA table_info)."""

    rows = con.execute(f"PRAGMA table_info({table})")  # type: ignore[union-attr]
    return any(row[1] == column for row in rows)


def _table_exists(con: object, table: str) -> bool:
    """Return True if *table* exists in the SQLite DB."""
    rows = list(
        con.execute(  # type: ignore[union-attr]
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
    )
    return bool(rows)


def _sqlite_migrate(db_path: str) -> None:
    """Idempotent schema migrations for SQLite DBs using stdlib sqlite3.

    SQLAlchemy create_all only creates missing tables, not missing columns.
    Each entry in COLUMNS_TO_ENSURE is checked via PRAGMA table_info and
    ALTER TABLE'd only when absent, making all runs safe to repeat.

    Stage J: also rebuild the legacy column-level UNIQUE on
    ecosystem_repo_profiles.repo_full_name into a (project_id, repo_full_name)
    composite unique index, so the same repo can coexist across projects.
    """
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        # Concurrent first-start/upgrade: wait for the write lock instead of
        # failing immediately (stdlib connection does not inherit the
        # busy_timeout configured on the SQLAlchemy engine side).
        con.execute("PRAGMA busy_timeout=30000")
        for table, col, ddl in COLUMNS_TO_ENSURE:
            # Skip when the table itself does not exist yet (e.g. create_all
            # hasn't been called when migration runs against an empty DB).
            if not _table_exists(con, table):
                continue
            if not _column_exists(con, table, col):
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    con.commit()
                except sqlite3.OperationalError as exc:
                    # e.g. lock contention or a concurrent process already
                    # added the column — do not crash startup, next run retries.
                    logger.warning(
                        "Skip ALTER TABLE %s ADD COLUMN %s: %s", table, col, exc
                    )

        if _table_exists(con, "ecosystem_repo_profiles"):
            _ensure_ecosystem_profile_project_unique(con)
            _ensure_ecosystem_perf_indexes(con)

        # B1: agents.cc_tool_use_id partial UNIQUE (dedup existing rows first).
        if _table_exists(con, "agents"):
            _ensure_agents_cc_tool_use_id_unique(con)

        # v1.6.0-P0: backfill canonical_id + source_kind for existing github repos
        if _table_exists(con, "ecosystem_repo_profiles"):
            _backfill_v160_repo_profile_fields(con)

        # v1.6.0 event sourcing: backfill existing status_changes into events table
        if _table_exists(con, "ecosystem_status_changes") and _table_exists(con, "ecosystem_repo_events"):
            _backfill_status_changes_to_events(con)

        # v1.6.0 P2: backfill discovered events for old repos (first_seen_at set but no event)
        if _table_exists(con, "ecosystem_repo_profiles") and _table_exists(con, "ecosystem_repo_events"):
            _backfill_discovered_events(con)
    finally:
        con.close()


def _ensure_ecosystem_perf_indexes(con: object) -> None:
    """K1 — ensure composite perf indexes exist on ecosystem_repo_profiles.

    Idempotent: ``CREATE INDEX IF NOT EXISTS`` is a no-op when the index
    already exists. Skips silently when the table is missing (covered by
    caller) or any individual statement fails (defensive).
    """
    import sqlite3

    if not isinstance(con, sqlite3.Connection):
        return  # pragma: no cover

    for idx_name, table, cols in INDEXES_TO_ENSURE:
        try:
            con.execute(
                f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols})"
            )
        except sqlite3.OperationalError:
            # e.g. column missing in legacy schema — skip silently
            continue
    con.commit()


def _backfill_v160_repo_profile_fields(con: object) -> None:
    """v1.6.0-P0: backfill canonical_id and source_kind for existing github repos.

    Idempotent: only updates rows where canonical_id IS NULL.
    Maps is_active to last_active_status (True->'active', False->'inactive').
    """
    import sqlite3

    if not isinstance(con, sqlite3.Connection):
        return  # pragma: no cover

    try:
        # backfill canonical_id = 'github/' + repo_full_name where NULL
        con.execute(
            "UPDATE ecosystem_repo_profiles "
            "SET canonical_id = 'github/' || repo_full_name "
            "WHERE canonical_id IS NULL AND repo_full_name IS NOT NULL"
        )
        # backfill source_kind = 'github' where NULL or empty
        con.execute(
            "UPDATE ecosystem_repo_profiles "
            "SET source_kind = 'github' "
            "WHERE source_kind IS NULL OR source_kind = ''"
        )
        # backfill last_active_status from is_active where NULL
        con.execute(
            "UPDATE ecosystem_repo_profiles "
            "SET last_active_status = CASE WHEN is_active = 1 THEN 'active' ELSE 'inactive' END "
            "WHERE last_active_status IS NULL"
        )
        con.commit()
    except sqlite3.OperationalError:
        pass  # columns may not exist yet in empty DB — create_all handles that


def _backfill_status_changes_to_events(con: object) -> None:
    """v1.6.0 event sourcing: migrate existing ecosystem_status_changes rows into ecosystem_repo_events.

    Idempotent: only inserts rows where the source id does not already appear in events.
    Existing status_changes are preserved (not deleted) for backward compat.
    """
    import json
    import sqlite3

    if not isinstance(con, sqlite3.Connection):
        return  # pragma: no cover

    try:
        rows = con.execute(
            "SELECT id, repo_id, project_id, from_status, to_status, scan_run_id, reason, triggered_at "
            "FROM ecosystem_status_changes"
        ).fetchall()
        for row in rows:
            sc_id, repo_id, project_id, from_status, to_status, scan_run_id, reason, triggered_at = row
            # Check if already backfilled (reason field stores source id reference)
            existing = con.execute(
                "SELECT 1 FROM ecosystem_repo_events WHERE id = ?", (sc_id,)
            ).fetchone()
            if existing:
                continue
            payload = json.dumps({"from": from_status, "to": to_status})
            con.execute(
                "INSERT INTO ecosystem_repo_events "
                "(id, repo_id, project_id, event_type, payload_json, source, "
                "scan_run_id, from_status, to_status, reason, triggered_at) "
                "VALUES (?, ?, ?, 'status_changed', ?, 'scanner', ?, ?, ?, ?, ?)",
                (sc_id, repo_id, project_id, payload, scan_run_id, from_status, to_status, reason or "", triggered_at),
            )
        con.commit()
    except sqlite3.OperationalError:
        pass  # graceful degradation — missing columns handled by create_all


def _backfill_discovered_events(con: object) -> None:
    """v1.6.0 P2: give old repos a 'discovered' event synthesised from first_seen_at.

    Only inserts for repos where first_seen_at IS NOT NULL and no 'discovered'
    event exists yet.  Idempotent — safe to call on every startup.
    """
    import json
    import sqlite3
    import uuid

    if not isinstance(con, sqlite3.Connection):
        return  # pragma: no cover

    try:
        rows = con.execute(
            "SELECT id, project_id, scan_run_id, topics, stars, first_seen_at "
            "FROM ecosystem_repo_profiles "
            "WHERE first_seen_at IS NOT NULL "
            "AND id NOT IN ("
            "  SELECT repo_id FROM ecosystem_repo_events WHERE event_type = 'discovered'"
            ")"
        ).fetchall()

        for repo_id, project_id, scan_run_id, topics_raw, stars, first_seen_at in rows:
            try:
                topics = json.loads(topics_raw) if topics_raw else []
            except (json.JSONDecodeError, TypeError):
                topics = []

            payload = json.dumps({
                "backfilled": True,
                "first_topics": topics,
                "first_stars": stars,
                "source_query": "backfill_from_first_seen_at",
            })
            event_id = str(uuid.uuid4())
            con.execute(
                "INSERT INTO ecosystem_repo_events "
                "(id, repo_id, project_id, event_type, payload_json, source, scan_run_id, triggered_at) "
                "VALUES (?, ?, ?, 'discovered', ?, 'migration', ?, ?)",
                (event_id, repo_id, project_id, payload, scan_run_id, first_seen_at),
            )
        con.commit()
    except sqlite3.OperationalError:
        pass  # graceful degradation — table may not exist in empty DB


def _ensure_ecosystem_profile_project_unique(con: object) -> None:
    """Replace legacy single-column UNIQUE on repo_full_name with composite
    (project_id, repo_full_name) UNIQUE so the same repo can exist in multiple
    projects.

    Idempotent: skips when the legacy unique index is already gone or the
    composite index already exists.
    """
    import sqlite3

    if not isinstance(con, sqlite3.Connection):
        return  # pragma: no cover

    rows = list(
        con.execute(
            "SELECT name, \"unique\" FROM sqlite_master "
            "WHERE type='index' AND tbl_name='ecosystem_repo_profiles'"
        )
    )
    # Detect legacy unique index over repo_full_name only
    legacy_unique_name = None
    composite_present = False
    for row in rows:
        idx_name = row[0]
        if idx_name == "uq_ecosystem_profiles_project_repo":
            composite_present = True
            continue
        # PRAGMA index_list says this index is UNIQUE
        info = list(con.execute("PRAGMA index_list(ecosystem_repo_profiles)"))
        idx_meta = {item[1]: item for item in info}
        meta = idx_meta.get(idx_name)
        if meta is None:
            continue
        if int(meta[2]) != 1:  # not unique
            continue
        cols = list(con.execute(f"PRAGMA index_info({idx_name})"))
        col_names = [c[2] for c in cols]
        if col_names == ["repo_full_name"]:
            legacy_unique_name = idx_name

    if legacy_unique_name and not composite_present:
        # Drop column-level unique (rebuilt as composite below + non-unique
        # index for fast lookup retains).
        try:
            con.execute(f"DROP INDEX {legacy_unique_name}")
            con.execute(
                "CREATE INDEX IF NOT EXISTS ix_ecosystem_repo_profiles_repo_full_name "
                "ON ecosystem_repo_profiles (repo_full_name)"
            )
            con.commit()
        except sqlite3.OperationalError:
            pass

    if not composite_present:
        try:
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_ecosystem_profiles_project_repo "
                "ON ecosystem_repo_profiles (project_id, repo_full_name)"
            )
            con.commit()
        except sqlite3.OperationalError:
            pass


def _ensure_agents_cc_tool_use_id_unique(con: object) -> None:
    """B1 — add a partial UNIQUE index on ``agents.cc_tool_use_id`` (WHERE NOT NULL).

    Two ``find_agent_by_cc_id`` == None → create paths (SubagentStart coroutine vs
    reaper live-tail member reap) can currently both insert a row for the same CC
    tool_use id, inflating member counts / leaving an orphan busy (audit B1). The
    partial unique index makes the second concurrent insert raise IntegrityError,
    which both create sites now swallow/re-fetch.

    Before creating the index, dedup any pre-existing rows sharing a non-null
    cc_tool_use_id: keep the most-complete / newest row, delete the redundant ones.
    Rows with role='leader' are NEVER deleted — a parasitic leader row may share a
    workflow team and must survive cleanup (round-35 blood lesson: manual dedup once
    误扫 a leader row). SELECT-audit the group before deleting anything.

    Idempotent: ``CREATE UNIQUE INDEX IF NOT EXISTS`` is a no-op on repeat, and the
    dedup only fires while duplicate groups still exist.
    """
    import sqlite3

    if not isinstance(con, sqlite3.Connection):
        return  # pragma: no cover

    try:
        groups = con.execute(
            "SELECT cc_tool_use_id FROM agents "
            "WHERE cc_tool_use_id IS NOT NULL AND cc_tool_use_id != '' "
            "GROUP BY cc_tool_use_id HAVING COUNT(*) > 1"
        ).fetchall()
    except sqlite3.OperationalError:
        return  # legacy/partial schema without the column — nothing to do

    for (cc_id,) in groups:
        # SELECT-audit the whole group (role composition) before touching a row.
        rows = con.execute(
            "SELECT id, role, status, current_task, model, last_active_at, created_at "
            "FROM agents WHERE cc_tool_use_id = ?",
            (cc_id,),
        ).fetchall()
        # Leaders are sacrosanct — exclude them from the deletable set entirely.
        deletable = [r for r in rows if (r[1] or "") != "leader"]
        if len(deletable) <= 1:
            # Only leaders duplicate (pathological) or a single non-leader remains:
            # nothing safe to remove. If a residual conflict survives, the CREATE
            # below fails gracefully and next startup retries.
            continue
        # Keeper = most-complete / newest: has current_task, has model, then newest
        # last_active_at, then newest created_at (ISO strings sort chronologically;
        # None → "" sorts before any timestamp, so a touched row wins).
        def _rank(r: tuple) -> tuple:
            return (
                1 if (r[3] or "") else 0,   # current_task present
                1 if (r[4] or "") else 0,   # model present
                r[5] or "",                 # last_active_at
                r[6] or "",                 # created_at
            )

        deletable.sort(key=_rank, reverse=True)
        keeper, losers = deletable[0], deletable[1:]
        for loser in losers:
            con.execute("DELETE FROM agents WHERE id = ?", (loser[0],))
        logger.info(
            "agents cc_tool_use_id dedup: cc=%s kept=%s removed=%d (leaders preserved)",
            cc_id,
            keeper[0],
            len(losers),
        )
    con.commit()

    try:
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_cc_tool_use_id "
            "ON agents (cc_tool_use_id) "
            "WHERE cc_tool_use_id IS NOT NULL AND cc_tool_use_id != ''"
        )
        con.commit()
    except sqlite3.OperationalError as exc:
        # e.g. a residual duplicate (leader-involved) blocks the unique build —
        # do not crash startup; the next run retries after more churn.
        logger.warning(
            "Skip CREATE UNIQUE INDEX uq_agents_cc_tool_use_id: %s", exc
        )


async def init_db(db_url: str | None = None) -> None:
    """Initialize the database and create all tables.

    If using SQLite and the parent directory of the database file does not exist,
    the directory is created automatically.

    Args:
        db_url: Database connection URL.
    """
    url = db_url or DEFAULT_DB_URL

    db_path_str = ""
    # Ensure the directory for the SQLite database file exists
    if "sqlite" in url:
        # Extract file path from URL: sqlite+aiosqlite:///path/to/db
        db_path_str = url.split("///", 1)[-1] if "///" in url else ""
        if db_path_str:
            db_path = Path(db_path_str)
            db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Run idempotent column migrations (create_all won't add missing columns)
    if db_path_str:
        _sqlite_migrate(db_path_str)


async def close_db() -> None:
    """Close all database connections and release resources."""
    await engine_pool.dispose_all()
