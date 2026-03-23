"""Migrate global AI Team OS database to per-project SQLite databases.

Phase 2 migration script: reads the global aiteam.db, groups data by project_id,
and writes each project's data into its own isolated database at:
  ~/.claude/data/ai-team-os/projects/{project_id}/data.db

Migration order respects foreign key dependencies:
  projects → phases → teams → agents → tasks → meetings → meeting_messages
  memories, events, agent_activities, scheduled_tasks (no FK to above)

Usage:
    python scripts/migrate_to_project_dbs.py [--dry-run] [--source PATH]

Options:
    --dry-run       Print plan without writing any data.
    --source PATH   Path to source aiteam.db (default: ~/.claude/data/ai-team-os/aiteam.db)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate")

# ---------------------------------------------------------------------------
# Path helpers (mirrors project_context.py logic)
# ---------------------------------------------------------------------------

DEFAULT_SOURCE_DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"
PROJECTS_BASE_DIR = Path.home() / ".claude" / "data" / "ai-team-os" / "projects"


def get_project_db_path(project_id: str) -> Path:
    """Return the path for a project's isolated SQLite database."""
    return PROJECTS_BASE_DIR / project_id / "data.db"


def compute_project_id_from_path(root_path: str) -> str:
    """Compute a stable project_id from a project directory path.

    Mirrors aiteam/api/project_context.py::compute_project_id.
    Uses MD5 hash of the normalized absolute path, truncated to 12 hex chars.
    """
    normalized = str(Path(root_path).resolve())
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Schema DDL — replicated from models.py / Base.metadata
# We use raw SQLite DDL to avoid importing async SQLAlchemy in a sync script.
# ---------------------------------------------------------------------------

SCHEMA_DDL = [
    # projects
    """CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        root_path TEXT DEFAULT '' UNIQUE,
        description TEXT DEFAULT '',
        config TEXT DEFAULT '{}',
        created_at TEXT,
        updated_at TEXT
    )""",
    # phases
    """CREATE TABLE IF NOT EXISTS phases (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        status TEXT DEFAULT 'planning',
        "order" INTEGER DEFAULT 0,
        config TEXT DEFAULT '{}',
        created_at TEXT,
        updated_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_phases_project_id ON phases (project_id)""",
    # teams
    """CREATE TABLE IF NOT EXISTS teams (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        mode TEXT NOT NULL DEFAULT 'coordinate',
        project_id TEXT,
        leader_agent_id TEXT,
        status TEXT DEFAULT 'active',
        summary TEXT DEFAULT '',
        config TEXT DEFAULT '{}',
        created_at TEXT,
        updated_at TEXT,
        completed_at TEXT
    )""",
    # agents
    """CREATE TABLE IF NOT EXISTS agents (
        id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        system_prompt TEXT DEFAULT '',
        model TEXT DEFAULT 'claude-opus-4-6',
        status TEXT DEFAULT 'waiting',
        config TEXT DEFAULT '{}',
        source TEXT DEFAULT 'api',
        session_id TEXT,
        cc_tool_use_id TEXT,
        current_task TEXT,
        project_id TEXT,
        current_phase_id TEXT,
        created_at TEXT,
        last_active_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_agents_team_id ON agents (team_id)""",
    # tasks
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        team_id TEXT,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        assigned_to TEXT,
        result TEXT,
        parent_id TEXT,
        project_id TEXT,
        depends_on TEXT DEFAULT '[]',
        depth INTEGER DEFAULT 0,
        "order" INTEGER DEFAULT 0,
        template_id TEXT,
        priority TEXT DEFAULT 'medium',
        horizon TEXT DEFAULT 'short',
        tags TEXT DEFAULT '[]',
        config TEXT DEFAULT '{}',
        created_at TEXT,
        started_at TEXT,
        completed_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_tasks_team_id ON tasks (team_id)""",
    # memories
    """CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        content TEXT NOT NULL,
        metadata TEXT DEFAULT '{}',
        created_at TEXT,
        accessed_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_memories_scope ON memories (scope)""",
    """CREATE INDEX IF NOT EXISTS ix_memories_scope_id ON memories (scope_id)""",
    # events
    """CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        source TEXT NOT NULL,
        data TEXT DEFAULT '{}',
        timestamp TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_events_type ON events (type)""",
    """CREATE INDEX IF NOT EXISTS ix_events_source ON events (source)""",
    # meetings
    """CREATE TABLE IF NOT EXISTS meetings (
        id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        topic TEXT NOT NULL,
        status TEXT DEFAULT 'active',
        participants TEXT DEFAULT '[]',
        project_id TEXT,
        created_at TEXT,
        concluded_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_meetings_team_id ON meetings (team_id)""",
    # meeting_messages
    """CREATE TABLE IF NOT EXISTS meeting_messages (
        id TEXT PRIMARY KEY,
        meeting_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        agent_name TEXT NOT NULL,
        content TEXT NOT NULL,
        round_number INTEGER DEFAULT 1,
        timestamp TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_meeting_messages_meeting_id ON meeting_messages (meeting_id)""",
    """CREATE INDEX IF NOT EXISTS ix_meeting_messages_agent_id ON meeting_messages (agent_id)""",
    # agent_activities
    """CREATE TABLE IF NOT EXISTS agent_activities (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        input_summary TEXT DEFAULT '',
        output_summary TEXT DEFAULT '',
        timestamp TEXT,
        duration_ms INTEGER,
        status TEXT DEFAULT 'completed',
        error TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_agent_activities_agent_id ON agent_activities (agent_id)""",
    """CREATE INDEX IF NOT EXISTS ix_agent_activities_session_id ON agent_activities (session_id)""",
    # scheduled_tasks
    """CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id TEXT PRIMARY KEY,
        team_id TEXT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        interval_seconds INTEGER NOT NULL,
        action_type TEXT NOT NULL,
        action_config TEXT DEFAULT '{}',
        enabled INTEGER DEFAULT 1,
        last_run_at TEXT,
        next_run_at TEXT NOT NULL,
        created_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_scheduled_tasks_team_id ON scheduled_tasks (team_id)""",
    # task_memos (if present)
    """CREATE TABLE IF NOT EXISTS task_memos (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        content TEXT NOT NULL,
        memo_type TEXT DEFAULT 'progress',
        author TEXT DEFAULT 'leader',
        created_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_task_memos_task_id ON task_memos (task_id)""",
]

# ---------------------------------------------------------------------------
# Source DB inspection helpers
# ---------------------------------------------------------------------------


def get_table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names for a table."""
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists in the database."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def fetch_rows(
    conn: sqlite3.Connection,
    table: str,
    where: str = "",
    params: tuple = (),
) -> list[dict]:
    """Fetch all matching rows from a table as dicts."""
    if not table_exists(conn, table):
        return []
    columns = get_table_columns(conn, table)
    quoted_cols = ", ".join(f'"{c}"' for c in columns)
    query = f'SELECT {quoted_cols} FROM "{table}"'
    if where:
        query += f" WHERE {where}"
    cur = conn.execute(query, params)
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def count_rows(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> int:
    """Count matching rows in a table."""
    if not table_exists(conn, table):
        return 0
    query = f"SELECT COUNT(*) FROM {table}"
    if where:
        query += f" WHERE {where}"
    cur = conn.execute(query, params)
    return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Target DB initialization
# ---------------------------------------------------------------------------


def init_target_db(db_path: Path) -> sqlite3.Connection:
    """Create and initialize a target project database.

    Creates the file and all tables. Returns an open connection.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")  # Off during bulk insert
    for ddl in SCHEMA_DDL:
        conn.execute(ddl)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Row insertion helper
# ---------------------------------------------------------------------------


def insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    """Insert rows into a table. Returns number of rows inserted."""
    if not rows:
        return 0
    columns = list(rows[0].keys())
    placeholders = ", ".join("?" for _ in columns)
    col_list = ", ".join(f'"{c}"' for c in columns)
    sql = f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
    values = [tuple(row[c] for c in columns) for row in rows]
    conn.executemany(sql, values)
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Project migration logic
# ---------------------------------------------------------------------------


def collect_project_data(
    src: sqlite3.Connection, project_id: str
) -> dict[str, list[dict]]:
    """Collect all data belonging to a project from the source DB.

    Returns a dict mapping table name to list of row dicts.
    Migration order follows foreign key dependencies.
    """
    data: dict[str, list[dict]] = {}

    # 1. The project record itself
    data["projects"] = fetch_rows(src, "projects", "id = ?", (project_id,))

    # 2. Phases linked directly to project
    data["phases"] = fetch_rows(src, "phases", "project_id = ?", (project_id,))

    # 3. Teams with this project_id
    data["teams"] = fetch_rows(src, "teams", "project_id = ?", (project_id,))
    team_ids = tuple(t["id"] for t in data["teams"])

    # 4. Agents — linked via team_id or project_id
    if team_ids:
        placeholders = ",".join("?" for _ in team_ids)
        agents_by_team = fetch_rows(
            src, "agents", f"team_id IN ({placeholders})", team_ids
        )
    else:
        agents_by_team = []
    agents_by_project = fetch_rows(src, "agents", "project_id = ?", (project_id,))
    # Deduplicate by id
    seen_agent_ids: set[str] = set()
    data["agents"] = []
    for a in agents_by_team + agents_by_project:
        if a["id"] not in seen_agent_ids:
            data["agents"].append(a)
            seen_agent_ids.add(a["id"])
    agent_ids = tuple(a["id"] for a in data["agents"])

    # 5. Tasks linked to project or team
    if team_ids:
        placeholders = ",".join("?" for _ in team_ids)
        tasks_by_team = fetch_rows(
            src, "tasks", f"team_id IN ({placeholders})", team_ids
        )
    else:
        tasks_by_team = []
    tasks_by_project = fetch_rows(src, "tasks", "project_id = ?", (project_id,))
    seen_task_ids: set[str] = set()
    data["tasks"] = []
    for t in tasks_by_team + tasks_by_project:
        if t["id"] not in seen_task_ids:
            data["tasks"].append(t)
            seen_task_ids.add(t["id"])
    task_ids = tuple(t["id"] for t in data["tasks"])

    # 6. Meetings linked to project or team
    if team_ids:
        placeholders = ",".join("?" for _ in team_ids)
        meetings_by_team = fetch_rows(
            src, "meetings", f"team_id IN ({placeholders})", team_ids
        )
    else:
        meetings_by_team = []
    meetings_by_project = fetch_rows(src, "meetings", "project_id = ?", (project_id,))
    seen_meeting_ids: set[str] = set()
    data["meetings"] = []
    for m in meetings_by_team + meetings_by_project:
        if m["id"] not in seen_meeting_ids:
            data["meetings"].append(m)
            seen_meeting_ids.add(m["id"])
    meeting_ids = tuple(m["id"] for m in data["meetings"])

    # 7. Meeting messages — linked via meeting_id
    if meeting_ids:
        placeholders = ",".join("?" for _ in meeting_ids)
        data["meeting_messages"] = fetch_rows(
            src, "meeting_messages", f"meeting_id IN ({placeholders})", meeting_ids
        )
    else:
        data["meeting_messages"] = []

    # 8. Memories — scope_id matches project_id, team_id, or agent_id
    scope_ids = {project_id} | set(team_ids) | set(agent_ids)
    if scope_ids:
        placeholders = ",".join("?" for _ in scope_ids)
        data["memories"] = fetch_rows(
            src, "memories", f"scope_id IN ({placeholders})", tuple(scope_ids)
        )
    else:
        data["memories"] = []

    # 9. Agent activities — linked via agent_id
    if agent_ids:
        placeholders = ",".join("?" for _ in agent_ids)
        data["agent_activities"] = fetch_rows(
            src, "agent_activities", f"agent_id IN ({placeholders})", agent_ids
        )
    else:
        data["agent_activities"] = []

    # 10. Scheduled tasks — linked via team_id
    if team_ids:
        placeholders = ",".join("?" for _ in team_ids)
        data["scheduled_tasks"] = fetch_rows(
            src, "scheduled_tasks", f"team_id IN ({placeholders})", team_ids
        )
    else:
        data["scheduled_tasks"] = []

    # 11. Task memos — linked via task_id
    if task_ids and table_exists(src, "task_memos"):
        placeholders = ",".join("?" for _ in task_ids)
        data["task_memos"] = fetch_rows(
            src, "task_memos", f"task_id IN ({placeholders})", task_ids
        )
    else:
        data["task_memos"] = []

    return data


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_migration(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    project_id: str,
    project_data: dict[str, list[dict]],
) -> bool:
    """Verify that destination DB has same row counts as collected source data.

    Returns True if all counts match.
    """
    ok = True
    for table, rows in project_data.items():
        expected = len(rows)
        actual = count_rows(dst, table)
        status = "OK" if actual >= expected else "MISMATCH"
        if actual < expected:
            ok = False
        log.info("  %-25s expected=%d  actual=%d  [%s]", table, expected, actual, status)
    return ok


# ---------------------------------------------------------------------------
# Main migration
# ---------------------------------------------------------------------------


def migrate_project(
    src: sqlite3.Connection,
    project_id: str,
    project_name: str,
    dry_run: bool,
) -> bool:
    """Migrate a single project from source DB to its own DB.

    Returns True on success, False on failure.
    """
    target_path = get_project_db_path(project_id)
    log.info("--- Project: %s (id=%s)", project_name, project_id)
    log.info("    Target: %s", target_path)

    # Collect source data
    project_data = collect_project_data(src, project_id)

    # Print summary
    total_rows = sum(len(rows) for rows in project_data.values())
    for table, rows in project_data.items():
        if rows:
            log.info("    %-25s %d rows", table, len(rows))
    log.info("    Total rows to migrate: %d", total_rows)

    if dry_run:
        log.info("    [DRY RUN] Skipping write.")
        return True

    # Check if target already has data (to avoid double migration)
    if target_path.exists() and target_path.stat().st_size > 1024:
        log.warning("    Target DB already exists with data. Skipping to avoid overwrite.")
        log.warning("    Delete %s manually to re-run migration for this project.", target_path)
        return True

    # Create and initialize target DB
    dst = None
    try:
        dst = init_target_db(target_path)

        # Migration order: projects → phases → teams → agents → tasks →
        #   meetings → meeting_messages → memories → agent_activities →
        #   scheduled_tasks → task_memos
        migration_order = [
            "projects",
            "phases",
            "teams",
            "agents",
            "tasks",
            "meetings",
            "meeting_messages",
            "memories",
            "agent_activities",
            "scheduled_tasks",
            "task_memos",
        ]
        for table in migration_order:
            rows = project_data.get(table, [])
            if rows:
                n = insert_rows(dst, table, rows)
                log.info("    Inserted %d rows into %s", n, table)

        # Verify
        log.info("    Verifying...")
        ok = verify_migration(src, dst, project_id, project_data)
        if ok:
            log.info("    Verification PASSED.")
        else:
            log.error("    Verification FAILED — row count mismatch!")
            dst.close()
            # Rollback: delete the partially-created target DB
            target_path.unlink(missing_ok=True)
            log.error("    Rolled back: deleted %s", target_path)
            return False

        dst.close()
        return True

    except Exception as exc:
        log.error("    Migration FAILED: %s", exc)
        if dst:
            try:
                dst.close()
            except Exception:
                pass
        # Rollback: remove partially-created DB
        if target_path.exists():
            target_path.unlink()
            log.error("    Rolled back: deleted %s", target_path)
        return False


def run(source_path: Path, dry_run: bool) -> None:
    """Main entry point for the migration."""
    log.info("=" * 60)
    log.info("AI Team OS — migrate_to_project_dbs.py")
    log.info("Started: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if dry_run:
        log.info("Mode: DRY RUN (no data will be written)")
    else:
        log.info("Mode: LIVE")
    log.info("Source: %s", source_path)
    log.info("=" * 60)

    if not source_path.exists():
        log.error("Source DB not found: %s", source_path)
        sys.exit(1)

    src = sqlite3.connect(str(source_path))
    src.row_factory = None  # Use default tuple rows (we handle dicts ourselves)

    if not table_exists(src, "projects"):
        log.error("Source DB has no 'projects' table. Is this the right file?")
        src.close()
        sys.exit(1)

    # Fetch all projects
    projects = fetch_rows(src, "projects")
    log.info("Found %d project(s) in source DB.", len(projects))

    if not projects:
        log.info("No projects to migrate.")
        src.close()
        return

    # Determine project_id to use for each project.
    # Phase 1 uses MD5(root_path) as the directory name.
    # The 'id' column in the projects table is a UUID (from types.py),
    # but the *file-system* project_id used by get_project_db_url is
    # compute_project_id(root_path) — 12-char MD5 of root_path.
    #
    # We use root_path when available; fall back to the stored id if not.
    results: dict[str, bool] = {}
    for project in projects:
        uuid_id: str = project["id"]
        root_path: str = project.get("root_path") or ""
        project_name: str = project.get("name", uuid_id)

        if root_path:
            fs_project_id = compute_project_id_from_path(root_path)
            log.info(
                "Project '%s': uuid=%s  root_path=%s  fs_id=%s",
                project_name, uuid_id, root_path, fs_project_id,
            )
        else:
            # No root_path — use uuid as fallback directory name
            fs_project_id = uuid_id
            log.warning(
                "Project '%s' has no root_path. Using uuid=%s as fs_id.",
                project_name, uuid_id,
            )

        # Collect data using the UUID (that's what all FK columns store)
        success = migrate_project(src, uuid_id, project_name, dry_run)
        results[project_name] = success

        if not dry_run and success and root_path:
            # If the target was created using uuid_id but the system expects
            # the MD5-based fs_project_id, create a symlink-style duplicate
            # by also writing to the MD5-named directory.
            uuid_path = get_project_db_path(uuid_id)
            md5_path = get_project_db_path(fs_project_id)
            if uuid_id != fs_project_id and uuid_path.exists() and not md5_path.exists():
                md5_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(uuid_path), str(md5_path))
                log.info(
                    "    Copied to MD5-named DB path: %s", md5_path
                )

    src.close()

    # Final summary
    log.info("")
    log.info("=" * 60)
    log.info("Migration Summary")
    log.info("=" * 60)
    for name, ok in results.items():
        status = "SUCCESS" if ok else "FAILED"
        log.info("  %-40s %s", name, status)

    failed = [n for n, ok in results.items() if not ok]
    if failed:
        log.error("\n%d project(s) failed: %s", len(failed), failed)
        sys.exit(1)
    else:
        if dry_run:
            log.info("\nDry run complete. Re-run without --dry-run to execute migration.")
        else:
            log.info("\nAll projects migrated successfully.")
            log.info("Source data has NOT been deleted. Verify, then remove manually.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate AI Team OS global DB to per-project databases."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without writing any data.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE_DB,
        help=f"Path to source aiteam.db (default: {DEFAULT_SOURCE_DB})",
    )
    args = parser.parse_args()
    run(source_path=args.source, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
