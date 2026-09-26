"""The read-only scan for lone surrogates in stored JSON text, and its os_health_check section.

Rows are created through the production repository, then poisoned with a raw
SQLite write: the storage layer now replaces lone surrogates, so a poisoned cell
can only be one written before that, or by a path outside it. The scan then reads
the file read-only.
"""

from __future__ import annotations

import asyncio
import socket
import sqlite3
from pathlib import Path

from aiteam.json_integrity import scan_lone_surrogates
from aiteam.mcp.tools import infra
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemRepoEvent

POISON = '["x\\ud800y"]'  # JSON text whose decoded value holds a lone surrogate
POISON_OBJECT = '{"v": "x\\ud800y"}'
PAIR = '["\\ud83d\\ude00"]'  # a valid emoji pair: must not count
LITERAL = '["\\\\ud800"]'  # the escape as text, backslash escaped: must not count


async def _seed(path: Path) -> dict:
    url = f"sqlite+aiosqlite:///{path}"
    repo = StorageRepository(db_url=url)
    await repo.init_db()
    ids = {
        "pair": (await repo.create_task(None, "emoji")).id,
        "literal": (await repo.create_task(None, "literal")).id,
        "task": (await repo.create_task(None, "poisoned")).id,
        "memory": (await repo.create_memory("global", "system", "fine")).id,
        "old_event": (await repo.create_event("team.created", "test", {"v": "ok"})).id,
        "repo_event": (await repo.create_repo_event(
            EcosystemRepoEvent(repo_id="r1", event_type="discovered", payload_json={"v": "ok"}))).id,
    }
    for _ in range(3):
        await repo.create_event("team.created", "test", {"v": "filler"})
    ids["recent"] = (await repo.create_event("team.created", "test", {"v": "ok"})).id
    await get_engine(url).dispose()
    return ids


def _poison(path: Path, ids: dict) -> None:
    con = sqlite3.connect(path)
    with con:
        con.execute("UPDATE tasks SET tags = ? WHERE id = ?", (POISON, ids["task"]))
        con.execute("UPDATE tasks SET tags = ? WHERE id = ?", (PAIR, ids["pair"]))
        con.execute("UPDATE tasks SET tags = ? WHERE id = ?", (LITERAL, ids["literal"]))
        con.execute("UPDATE memories SET source_refs = ? WHERE id = ?", (POISON, ids["memory"]))
        con.execute("UPDATE events SET data = ? WHERE id = ?", (POISON_OBJECT, ids["old_event"]))
        con.execute("UPDATE events SET data = ? WHERE id = ?", (POISON_OBJECT, ids["recent"]))
        # A TEXT column filled with json.dumps output, not a JSON column.
        con.execute("UPDATE ecosystem_repo_events SET payload_json = ? WHERE id = ?",
                    (POISON_OBJECT, ids["repo_event"]))
    con.close()


def _poisoned_db(tmp_path: Path) -> tuple[Path, dict]:
    database = tmp_path / "poisoned.db"
    ids = asyncio.run(_seed(database))
    _poison(database, ids)
    return database, ids


def test_the_scan_reports_poisoned_cells_by_column_and_nothing_else(tmp_path):
    database, ids = _poisoned_db(tmp_path)
    result = scan_lone_surrogates(database, row_window=2)
    assert result["status"] == "poisoned"
    found = {hit["column"]: hit for hit in result["hits"]}
    assert set(found) == {"tasks.tags", "memories.source_refs", "events.data",
                          "ecosystem_repo_events.payload_json"}, found
    assert found["tasks.tags"]["ids"] == [ids["task"]]
    assert found["memories.source_refs"]["ids"] == [ids["memory"]]
    assert found["ecosystem_repo_events.payload_json"]["ids"] == [ids["repo_event"]]
    # The event outside the two-row window is not read; the recent one is.
    assert found["events.data"]["ids"] == [ids["recent"]]


def test_a_zero_window_reads_every_row(tmp_path):
    database, ids = _poisoned_db(tmp_path)
    found = {hit["column"]: hit for hit in scan_lone_surrogates(database, row_window=0)["hits"]}
    assert sorted(found["events.data"]["ids"]) == sorted([ids["old_event"], ids["recent"]])
    # Every task row read: the emoji pair and the literal escape still do not count.
    assert found["tasks.tags"]["ids"] == [ids["task"]]


def test_a_clean_database_is_clean(tmp_path):
    database = tmp_path / "clean.db"
    asyncio.run(_seed(database))
    assert scan_lone_surrogates(database)["status"] == "clean"


def test_a_missing_database_is_unavailable_not_clean(tmp_path):
    assert scan_lone_surrogates(tmp_path / "absent.db")["status"] == "unavailable"


class _Capture:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _health(monkeypatch, database: Path):
    monkeypatch.setenv("AITEAM_DB_PATH", str(database))
    monkeypatch.setenv("AITEAM_API_URL", f"http://127.0.0.1:{_closed_port()}")
    capture = _Capture()
    infra.register(capture)
    return capture.tools["os_health_check"]


def test_os_health_check_carries_the_section_while_the_api_is_down(tmp_path, monkeypatch):
    database, ids = _poisoned_db(tmp_path)
    result = _health(monkeypatch, database)()
    assert result["status"] == "unhealthy"
    section = result["json_integrity"]
    assert section["status"] == "poisoned"
    assert ids["memory"] in {i for hit in section["hits"] for i in hit["ids"]}
    assert "400" in section["hint"]
    assert section["row_window"] == 10_000


def test_os_health_check_passes_the_window_through(tmp_path, monkeypatch):
    database, ids = _poisoned_db(tmp_path)
    section = _health(monkeypatch, database)(json_scan_rows=0)["json_integrity"]
    assert section["row_window"] == 0
    events = next(hit for hit in section["hits"] if hit["column"] == "events.data")
    assert ids["old_event"] in events["ids"]
