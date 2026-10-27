"""The Claude Code event entry after the config folder moved and the data went neutral.

docs/config-dir-compat-design.md §6 batch 3 and §7.1 T2: the data lives in the
neutral directory, the whole Claude Code config folder has been moved elsewhere
and CLAUDE_CONFIG_DIR points at it. The installed copy of ``send_event.py`` (and
the ``hook_delivery.py`` beside it) must then find

* the API port in the data directory's ``api_port.txt`` (no AITEAM_API_URL),
* the session's team under ``$CLAUDE_CONFIG_DIR/teams``,
* its delivery ledger and replay queue in the data directory,

and create nothing in the old places. A real API on a free port serves a real
database; every effect is read back over HTTP or from that database.

A stale port file naming a closed port is left in the old place on purpose.
Without AITEAM_API_URL, a hook that cannot find the port file falls back to
port 8000, which on a developer machine is the real OS service; an entry that
still reads the old place reads this file instead and posts nowhere.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from .test_config_dir_relocation import Machine, _free_port, _installer, _tree
from .test_config_dir_relocation import machine as machine  # noqa: F401 - pytest fixture

SESSION = "00000000-0000-4000-8000-00000000c0de"
TEAM = "relocated-team"
TOOL_USE_ID = "toolu_relocated_0001"
COMMAND = "echo relocated-0001"


def _relocated(machine: Machine, monkeypatch) -> tuple[Path, Path, Path]:
    """T2 layout with the API running: (data directory, moved config folder, old data place)."""
    monkeypatch.setenv("HOME", str(machine.home))
    data_dir = _installer().prepare_data_dir()  # install.py step 5 on an empty machine
    assert data_dir == machine.home / ".aiteam"
    machine.start()
    (data_dir / "api_port.txt").write_text(str(machine.port), encoding="utf-8")  # what the MCP autostart writes
    machine.install(monkeypatch)
    profile = machine.move_config()
    old_place = machine.home / ".claude" / "data" / "ai-team-os"
    old_place.mkdir(parents=True)
    (old_place / "api_port.txt").write_text(str(_free_port()), encoding="utf-8")  # see module docstring
    team = profile / "teams" / "team-relocated"
    team.mkdir(parents=True)
    (team / "config.json").write_text(json.dumps({"name": TEAM, "leadSessionId": SESSION}), encoding="utf-8")
    return data_dir, profile, old_place


def _send(machine: Machine, profile: Path, event: str, payload: dict) -> subprocess.CompletedProcess[str]:
    """The installed main-chain copy, as Claude Code runs it: no AITEAM_API_URL."""
    env = {key: value for key, value in machine.env.items() if key != "AITEAM_API_URL"}
    result = subprocess.run(
        [sys.executable, str(profile / "hooks" / "ai-team-os" / "send_event.py"), event],
        input=json.dumps({"session_id": SESSION, "cwd": str(machine.tmp), **payload}),
        text=True, capture_output=True, env=env, cwd=machine.tmp, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result


def _ledger(data_dir: Path) -> list[dict]:
    path = data_dir / "hook-delivery" / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _queued(data_dir: Path) -> list[str]:
    spool = data_dir / "hook-delivery" / "spool"
    return [name for box in ("new", "cur") if (spool / box).is_dir() for name in os.listdir(spool / box)]


def _tool_events(data_dir: Path) -> list[dict]:
    """cc.tool_use events for this test's tool call, straight from the API's database."""
    connection = sqlite3.connect(f"file:{data_dir / 'aiteam.db'}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT data FROM events WHERE type = 'cc.tool_use'").fetchall()
    finally:
        connection.close()
    return [data for data in (json.loads(row[0]) for row in rows) if data.get("tool_input_summary") == COMMAND]


def _wait_for(check, seconds: float = 15) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.1)


def test_entry_finds_the_port_and_the_team_through_the_path_section(machine, monkeypatch):
    """Port from the neutral data directory, team from the moved config folder."""
    _, profile, old_place = _relocated(machine, monkeypatch)

    result = _send(machine, profile, "SubagentStart", {"agent_id": "a-relocated", "agent_type": "general-purpose"})

    assert "post_failed" not in result.stderr, result.stderr
    teams = [team["name"] for team in machine.client.get("/api/teams").json()["data"]]
    assert TEAM in teams, teams
    assert _tree(old_place) == ["api_port.txt"]


def test_failed_post_is_ledgered_and_replayed_from_the_data_directory(machine, monkeypatch):
    """A refused tool event is queued in the data directory and lands once the API is back."""
    data_dir, profile, old_place = _relocated(machine, monkeypatch)
    machine.project_with_task()  # a project at the hooks' cwd ...
    _send(machine, profile, "SessionStart", {"source": "startup"})
    _send(machine, profile, "Stop", {"stop_hook_active": False})  # ... and a first turn: the session has its leader
    machine.stop()

    refused = _send(machine, profile, "PreToolUse", {
        "tool_name": "Bash", "tool_input": {"command": COMMAND}, "tool_use_id": TOOL_USE_ID})

    assert "post_failed cls=refused" in refused.stderr, refused.stderr
    assert [(e["ev"], e["cls"], e["spool"]) for e in _ledger(data_dir)] == [("PreToolUse", "refused", "queued")]
    assert len(_queued(data_dir)) == 1
    assert _tree(old_place) == ["api_port.txt"]

    machine.start()
    assert _tool_events(data_dir) == []
    for _ in range(10):  # a hook drains only when its own POST came back quickly
        _send(machine, profile, "Stop", {"stop_hook_active": False})
        if not _queued(data_dir):
            break
    assert _queued(data_dir) == []
    assert [e["replay"] for e in _ledger(data_dir) if "replay" in e] == ["delivered"]
    _wait_for(lambda: len(_tool_events(data_dir)) == 1)  # ingestion finishes after the receipt
    assert _tool_events(data_dir)[0]["hook_replay"]["attempt"] == 1  # it arrived as the redelivery
    assert _tree(old_place) == ["api_port.txt"]
