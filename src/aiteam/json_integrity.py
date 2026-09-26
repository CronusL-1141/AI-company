"""Read-only scan for lone surrogates stored inside JSON text.

A lone surrogate has no UTF-8 encoding, so a TEXT column refuses it, but a JSON
column stores it as an ASCII escape and the write succeeds. From then on every
read that serializes the row fails: one poisoned ``memories.source_refs`` cell
made ``GET /api/memories`` answer 400 and silently emptied the direction layer
of every session start. The API now refuses such bodies; this scan finds rows
written before that, or by a path that never went through a request body.

Standard library plus aiteam.surrogates only, and no import of the storage
package: that package creates the data directory and migrates old databases at
import time, which a health check must not do.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from aiteam.surrogates import has_lone_surrogate

# Only each table's most recent rows are read. events passes a million rows and the
# telemetry payload tables tens of thousands, where a full scan costs seconds; the
# tables agents write text into are mostly smaller than the window. A new
# poisoning is always among the recent rows.
ROW_WINDOW = 10_000
_SAMPLE_IDS = 5


def default_db_path() -> Path:
    """The database the API uses: same precedence as storage.connection, no side effects."""
    override = os.environ.get("AITEAM_DB_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"


def _poisoned(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return has_lone_surrogate(json.loads(value))
    except ValueError:
        return False


def _text_affinity(declared: object) -> bool:
    """Columns that can hold JSON text: JSON columns and plain TEXT/VARCHAR ones.

    Some TEXT columns are filled with ``json.dumps`` output (ecosystem profile topics,
    repo event payloads, index diff details), so the column type alone misses them.
    """
    kind = str(declared).upper()
    return any(token in kind for token in ("JSON", "TEXT", "CHAR", "CLOB"))


def scan_lone_surrogates(db_path: Path, *, row_window: int = ROW_WINDOW) -> dict[str, Any]:
    """Every cell whose JSON text decodes to a lone surrogate, grouped by column.

    Scans every text-affinity column; a cell counts only when it parses as JSON and
    the decoded value holds a lone surrogate. Returns ``status`` clean / poisoned /
    unavailable, ``hits`` [{column, rows, ids}], ``row_window`` (rows read per table,
    newest first; 0 reads every row) and ``elapsed_ms``. Opens the file read-only;
    never writes.
    """
    started = time.monotonic()
    if not db_path.is_file():
        return {"status": "unavailable", "reason": "database not found"}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    try:
        hits = []
        tables = [row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            info = con.execute(f"PRAGMA table_info('{table}')").fetchall()
            key = "id" if any(col[1] == "id" for col in info) else "rowid"
            for column in [col[1] for col in info if _text_affinity(col[2])]:
                # Cheap prefilter: a stored surrogate is always a \uDxxx escape.
                where = f"instr(lower(CAST(\"{column}\" AS TEXT)), '\\ud') > 0"
                if row_window > 0:
                    where += (f" AND rowid > (SELECT COALESCE(MAX(rowid), 0) FROM \"{table}\")"
                              f" - {int(row_window)}")
                rows = con.execute(f"SELECT {key}, \"{column}\" FROM \"{table}\" WHERE {where}").fetchall()
                bad = [str(row_id) for row_id, value in rows if _poisoned(value)]
                if bad:
                    hits.append({"column": f"{table}.{column}", "rows": len(bad), "ids": bad[:_SAMPLE_IDS]})
    except sqlite3.Error as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
    finally:
        con.close()
    return {
        "status": "poisoned" if hits else "clean",
        "hits": hits,
        "row_window": row_window,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
