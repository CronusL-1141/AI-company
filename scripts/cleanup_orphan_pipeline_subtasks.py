"""Clean up orphan pipeline subtasks.

Background: pipeline_create() historically generated 5-6 ceremonial subtasks
(diagnose / reproduce / fix / review / test) per parent. The pipeline_advance
flow was rarely used in practice; parents got marked completed via task_update
directly, leaving subtasks permanently stranded in pending state.

This script finds (parent.status=completed AND child.status=pending) pairs
and closes the children with a clear audit trail. Idempotent; safe to re-run.

Usage:
    python scripts/cleanup_orphan_pipeline_subtasks.py            # dry-run
    python scripts/cleanup_orphan_pipeline_subtasks.py --apply    # actually update
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def get_db_path() -> Path:
    """aiteam.db in the OS data directory, as the one resolver finds it (aiteam.paths)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from aiteam.paths import os_data_dir

    return os_data_dir() / "aiteam.db"


AUTO_CLOSE_RESULT = (
    "auto-closed: parent task was already completed "
    "(Phase 0 cleanup, 2026-04-28)"
)


FIND_ORPHANS_SQL = """
SELECT child.id AS child_id,
       child.title AS child_title,
       child.parent_id AS parent_id,
       parent.title AS parent_title
FROM tasks AS child
JOIN tasks AS parent ON parent.id = child.parent_id
WHERE child.status = 'pending'
  AND parent.status = 'completed'
  AND child.parent_id IS NOT NULL
ORDER BY parent.id, child.id;
"""


UPDATE_ORPHANS_SQL = """
UPDATE tasks
SET status = 'completed',
    result = ?,
    completed_at = CURRENT_TIMESTAMP
WHERE id IN (
    SELECT child.id
    FROM tasks AS child
    JOIN tasks AS parent ON parent.id = child.parent_id
    WHERE child.status = 'pending'
      AND parent.status = 'completed'
      AND child.parent_id IS NOT NULL
);
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Close orphan pipeline subtasks (parent completed, child pending)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the update. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Override DB path (defaults to aiteam.db in the OS data directory; aiteam paths shows it).",
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else get_db_path()
    if not db_path.exists():
        print(f"[ERROR] DB not found: {db_path}", file=sys.stderr)
        return 2

    print(f"[INFO] DB: {db_path}")
    print(f"[INFO] Mode: {'APPLY' if args.apply else 'DRY-RUN'}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()

        cur.execute(FIND_ORPHANS_SQL)
        orphans = cur.fetchall()
        n_orphans = len(orphans)
        parent_ids = {row["parent_id"] for row in orphans}
        n_parents = len(parent_ids)

        print(f"[INFO] Found {n_orphans} orphan subtasks across {n_parents} completed parents")

        if n_orphans == 0:
            print("[INFO] Nothing to do. Exiting.")
            return 0

        sample = orphans[:5]
        print("[INFO] Sample (up to 5):")
        for row in sample:
            print(
                f"  - child={row['child_id'][:8]} "
                f"title={row['child_title'][:60]!r} "
                f"parent={row['parent_id'][:8]}"
            )

        if not args.apply:
            print("[INFO] Dry-run complete. Re-run with --apply to perform update.")
            return 0

        cur.execute("BEGIN;")
        try:
            cur.execute(UPDATE_ORPHANS_SQL, (AUTO_CLOSE_RESULT,))
            updated = cur.rowcount
            conn.commit()
        except Exception as exc:
            conn.rollback()
            print(f"[ERROR] Update failed, rolled back: {exc}", file=sys.stderr)
            return 1

        print(f"[OK] Updated {updated} subtasks (expected {n_orphans})")
        if updated != n_orphans:
            print(
                f"[WARN] Update count {updated} differs from initial scan {n_orphans} — "
                "may indicate concurrent modification.",
                file=sys.stderr,
            )

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
