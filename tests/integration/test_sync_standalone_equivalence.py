"""A standalone node writes exactly what it wrote before the sync layer existed.

One fixed request set (projects, phases, a deleted project, a hook-driven session
with tools and a sub-agent, tasks with memo, batch and completion, direction memory,
reports with a deletion, channel message and read cursor, a meeting, a briefing, a
scheduled task with a deletion) runs through the real app and lifespan; the
database is then read back row by row through a fresh connection
(sync_equivalence_driver.py). The runs:

* ``standalone`` as shipped: no node.json, so capture is off;
* ``capture``: the same with change capture forced on, so every trigger fires;
* ``notriggers``: the same with every change-capture trigger dropped.

All three must hold identical rows in every table but the sync layer's own
(``sync_*`` tables and the ``origin_node`` column, reported apart), and the
sync layer must have written nothing but the change log, and that only when
capture is on.

Setting ``SYNC_EQUIVALENCE_BASE_SRC`` to the ``src`` directory of a tree from
before the sync layer (``git archive <commit> src scripts plugin``) adds a fourth
run on that code, which must match too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVER = Path(__file__).with_name("sync_equivalence_driver.py")


def _run(tmp_path: Path, variant: str, src: Path = ROOT / "src") -> dict:
    out = tmp_path / f"{variant}.json"
    env = {**os.environ, "PYTHONPATH": str(src), "HOME": str(tmp_path / "home")}
    (tmp_path / "home" / ".aiteam").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(DRIVER), "--work", str(tmp_path / "work"), "--variant", variant, "--out", str(out)],
        capture_output=True, text=True, timeout=180, env=env, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    result = json.loads(out.read_text())
    failed = [entry for entry in result["requests"] if entry[2] >= 300]
    assert not failed, failed
    return result


def _differing_tables(left: dict, right: dict) -> list[str]:
    names = set(left["tables"]) | set(right["tables"])
    return sorted(name for name in names if left["tables"].get(name) != right["tables"].get(name))


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict[str, dict]:
    # One directory for every run: paths that end up in rows are then identical.
    tmp_path = tmp_path_factory.mktemp("sync-equivalence")
    return {variant: _run(tmp_path, variant) for variant in ("standalone", "capture", "notriggers")}


def test_the_request_set_writes_every_face(runs) -> None:
    written = {name for name, table in runs["standalone"]["tables"].items() if table["rows"]}
    assert {"projects", "phases", "tasks", "task_memos", "memories", "reports", "channel_messages",
            "channel_read_cursors", "meetings", "meeting_messages", "leader_briefings", "task_create_batches",
            "teams", "agents", "agent_activities", "events", "hook_event_receipts"} <= written


def test_change_capture_never_alters_a_row(runs) -> None:
    assert _differing_tables(runs["standalone"], runs["notriggers"]) == []
    assert _differing_tables(runs["standalone"], runs["capture"]) == []
    assert runs["standalone"]["requests"] == runs["capture"]["requests"] == runs["notriggers"]["requests"]


def test_standalone_writes_nothing_of_its_own(runs) -> None:
    standalone, captured = runs["standalone"], runs["capture"]
    assert standalone["origins"] == captured["origins"] == [""]
    assert {name: count for name, count in standalone["sync_counts"].items() if count} == {"sync_state": 1}
    # The forced capture shows the triggers are live: every write was logged, nothing else.
    assert captured["sync_counts"]["sync_changes"] > 0
    assert {name: count for name, count in captured["sync_counts"].items() if count} == {
        "sync_state": 1, "sync_changes": captured["sync_counts"]["sync_changes"],
    }


@pytest.mark.skipif(not os.environ.get("SYNC_EQUIVALENCE_BASE_SRC"), reason="no pre-sync source tree given")
def test_standalone_matches_the_code_before_sync(runs, tmp_path_factory) -> None:
    base_src = Path(os.environ["SYNC_EQUIVALENCE_BASE_SRC"])
    tmp_path = tmp_path_factory.mktemp("sync-equivalence-base")
    standalone = _run(tmp_path, "standalone")
    base = _run(tmp_path, "base", base_src)
    assert base["sync_counts"] == {}
    assert _differing_tables(standalone, base) == []
    assert standalone["requests"] == base["requests"]
