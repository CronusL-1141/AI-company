"""Drive one fixed set of requests through the real app and dump the database.

Used by test_sync_standalone_equivalence.py: run once per variant, each in its own
process (the base variant runs another source tree via PYTHONPATH), then compare
the dumps. Usage::

    PYTHONPATH=<src> python3 sync_equivalence_driver.py --work DIR --variant V --out DUMP.json

Variants: ``standalone`` as shipped (no node.json), ``capture`` with change capture
forced on, ``notriggers`` with every change-capture trigger dropped, ``base`` for a
tree that has no sync layer at all.

The app runs with its real lifespan (init_db, startup migrations, node identity,
dependency wiring) over httpx's ASGI transport. Randomness and the clock are pinned
so two runs write identical rows: ids come from a seeded ``os.urandom``, every
``utc_now()`` is one fixed instant, the request ledger names a fixed process. The
background loops (state reaper, watchdog, account monitor) are not started: they
act on timers, and their effect on the same rows is covered by the governance tests.
Every run uses the same directory, so paths written into rows are identical too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

FIXED_NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
SEED = 20261115


def pin_randomness_and_clock() -> None:
    generator = random.Random(SEED)
    os.urandom = generator.randbytes  # uuid.uuid4 reads os.urandom at call time

    import aiteam.clock as clock

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - mirrors datetime.now
            return FIXED_NOW.astimezone(tz) if tz is not None else FIXED_NOW.replace(tzinfo=None)

    clock.datetime = FrozenDatetime


def quiet_background() -> None:
    """Keep timer-driven loops and the account monitor out of a short scripted run."""
    from aiteam.api import app as app_module
    from aiteam.api import state_reaper
    from aiteam.api.request_ledger import request_ledger
    from aiteam.loop import watchdog

    state_reaper.StateReaper.start = lambda self: None
    watchdog.WatchdogRunner.start = lambda self: None
    app_module._get_mcp_http_app = lambda: None

    @asynccontextmanager
    async def no_account_monitor(app, db_url):  # noqa: ANN001
        yield None

    app_module.account_monitor_lifespan = no_account_monitor
    request_ledger.pid = 4242
    request_ledger.started_at = FIXED_NOW


async def _after_startup(variant: str, db_url: str) -> None:
    if variant not in ("capture", "notriggers"):
        return
    from aiteam.storage.connection import get_engine

    async with get_engine(db_url).begin() as conn:
        if variant == "capture":
            await conn.exec_driver_sql("UPDATE sync_state SET capture = 3, node_id = 'air-test'")
        else:
            rows = await conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            for (name,) in rows.fetchall():
                if name.startswith("sync_"):
                    await conn.exec_driver_sql(f'DROP TRIGGER "{name}"')


def _hook(event: str, session: str, work: Path, **fields) -> dict:
    return {"hook_event_name": event, "session_id": session, "cwd": str(work),
            "transcript_path": "", **fields}


async def _requests(client, work: Path, log: list) -> None:
    from aiteam.api import deps

    async def call(method: str, path: str, body=None, headers=None):
        response = await client.request(method, path, json=body, headers=headers or {})
        await deps.get_hook_translator().drain(10)
        log.append([method, path, response.status_code])
        try:
            return response.json()
        except ValueError:
            return None

    def data(payload):
        # Most routes wrap the object in "data"; reports answer with it directly.
        payload = payload or {}
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload

    project = data(await call("POST", "/api/projects", {"name": "sync-eq", "root_path": str(work / "proj")}))
    pid = project.get("id", "")
    scope = {"X-Project-Id": pid}
    await call("PUT", f"/api/projects/{pid}", {"description": "edited"})
    await call("POST", f"/api/projects/{pid}/phases", {"name": "phase one"})
    doomed = data(await call("POST", "/api/projects", {"name": "doomed", "root_path": str(work / "doomed")}))
    await call("DELETE", f"/api/projects/{doomed.get('id', '')}")

    session = "sync-eq-session"
    await call("POST", "/api/hooks/event", _hook("SessionStart", session, work / "proj", source="startup"))
    await call("POST", "/api/hooks/event", _hook("UserPromptSubmit", session, work / "proj", prompt="hello"))
    for n in range(3):
        tool = {"tool_name": "Bash", "tool_input": {"command": f"echo {n}"}, "tool_use_id": f"tu-{n}"}
        await call("POST", "/api/hooks/event", _hook("PreToolUse", session, work / "proj", **tool))
        await call("POST", "/api/hooks/event", _hook("PostToolUse", session, work / "proj", **tool,
                                                     tool_response={"stdout": f"{n}\n"}))
    await call("POST", "/api/hooks/event", _hook("SubagentStart", session, work / "proj",
                                                 agent_id="agent-1", agent_type="team-member"))
    await call("POST", "/api/hooks/event", _hook("SubagentStop", session, work / "proj",
                                                 agent_id="agent-1", agent_type="team-member"))

    task = data(await call("POST", f"/api/projects/{pid}/tasks", {"title": "first task", "priority": "high"}))
    tid = task.get("id", "")
    await call("PUT", f"/api/tasks/{tid}", {"status": "running", "description": "under way"})
    await call("POST", f"/api/tasks/{tid}/memo", {"content": "progress note", "type": "progress"})
    await call("PUT", f"/api/tasks/{tid}/complete", {})
    await call("POST", f"/api/projects/{pid}/tasks/batch", {
        "idempotency_key": "batch-1",
        "tasks": [{"key": "a", "title": "batch a"}, {"key": "b", "title": "batch b", "depends_on": ["@a"]}],
    })

    memory = data(await call("POST", "/api/memories", {"content": "a direction", "kind": "directive",
                                                       "scope": "project"}, scope))
    await call("GET", "/api/memories", None, scope)
    await call("POST", f"/api/memories/{memory.get('id', '')}/invalidate", {}, scope)

    report = data(await call("POST", "/api/reports", {"author": "eq", "topic": "kept", "content": "body"}, scope))
    gone = data(await call("POST", "/api/reports", {"author": "eq", "topic": "gone", "content": "body"}, scope))
    await call("DELETE", f"/api/reports/{gone.get('id', '')}")
    await call("GET", f"/api/reports/{report.get('id', '')}")

    await call("POST", "/api/channels/team:eq/messages", {"sender": "leader", "content": "hi"}, scope)
    await call("POST", "/api/channels/team:eq/read-cursor",
               {"reader": "leader-cc", "project_id": pid, "last_read_at": FIXED_NOW.isoformat()}, scope)

    team_id = ""
    teams = (await call("GET", "/api/teams")) or {}
    for team in teams.get("data") or []:
        team_id = team.get("id", "")
    meeting = data(await call("POST", f"/api/teams/{team_id}/meetings", {"topic": "sync", "participants": ["a"]}))
    mid = meeting.get("id", "")
    await call("POST", f"/api/meetings/{mid}/messages", {"agent_id": "a", "agent_name": "a", "content": "point",
                                                        "round_number": 1})
    await call("PUT", f"/api/meetings/{mid}/conclude", {})

    briefing = data(await call("POST", "/api/leader-briefings", {"title": "decide", "project_id": pid}))
    await call("PUT", f"/api/leader-briefings/{briefing.get('id', '')}/resolve", {"resolution": "done"})

    scheduled = data(await call("POST", "/api/scheduler", {"name": "tick", "interval_seconds": 3600,
                                                          "action_type": "wake_agent", "team_id": team_id}))
    await call("PUT", f"/api/scheduler/{scheduled.get('id', '')}", {"enabled": False})
    await call("DELETE", f"/api/scheduler/{scheduled.get('id', '')}")

    await call("POST", "/api/hooks/event", _hook("Stop", session, work / "proj"))
    await call("POST", "/api/hooks/event", _hook("SessionEnd", session, work / "proj", reason="exit"))


async def _drive(variant: str, work: Path, db_url: str) -> list:
    import httpx

    from aiteam.api.app import create_app

    app = create_app()
    log: list = []
    async with app.router.lifespan_context(app):
        await _after_startup(variant, db_url)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await _requests(client, work, log)
    return log


def _jsonable(value):
    return value.hex() if isinstance(value, bytes) else value


def dump(db: Path) -> dict:
    """Every row of every table, read back through a fresh connection.

    Tables and columns the sync layer added are reported apart: ``sync_*`` tables by
    row count, ``origin_node`` by its distinct values. The rest is the comparison.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))
        rows, sync_counts, origins = {}, {}, set()
        for table in tables:
            if table.startswith("sync_"):
                sync_counts[table] = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                continue
            columns = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
            kept = [c for c in columns if c != "origin_node"]
            if "origin_node" in columns:
                origins |= {r[0] for r in con.execute(f'SELECT DISTINCT origin_node FROM "{table}"')}
            selected = ", ".join(f'"{c}"' for c in kept)
            table_rows = [[_jsonable(v) for v in row] for row in con.execute(f'SELECT {selected} FROM "{table}"')]
            rows[table] = {"columns": kept, "rows": sorted(table_rows, key=lambda r: json.dumps(r, default=str))}
    finally:
        con.close()
    return {"tables": rows, "sync_counts": sync_counts, "origins": sorted(origins)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--variant", choices=("standalone", "capture", "notriggers", "base"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    work = args.work.resolve()
    data_dir, db = work / "data", work / "data" / "aiteam.db"
    (work / "proj").mkdir(parents=True, exist_ok=True)
    (work / "doomed").mkdir(exist_ok=True)
    data_dir.mkdir(exist_ok=True)
    for stale in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"), data_dir / "node.json"):
        stale.unlink(missing_ok=True)
    os.environ.update({"AITEAM_DATA_DIR": str(data_dir), "AITEAM_DB_PATH": str(db),
                       "AITEAM_DIAGNOSTICS_ENABLED": "0"})
    pin_randomness_and_clock()
    quiet_background()
    log = asyncio.run(_drive(args.variant, work, f"sqlite+aiosqlite:///{db}"))
    args.out.write_text(json.dumps({"requests": log, **dump(db)}, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
