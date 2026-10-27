"""The MIGRATING lock keeps every starter of the API away while data moves (design §4.1 gate 2).

Two readers of one file format: aiteam.data_dir_migration (the mover, which
takes the lock) and the path section of hook_core (hooks, which cannot import
aiteam, and the package's API starters through aiteam.paths). Parity first,
then each refusing entry: MCP autostart, os_restart_api, aiteam up.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aiteam import data_dir_migration as mig
from aiteam import paths


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".aiteam").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for name in ("AITEAM_DATA_DIR", "CLAUDE_CONFIG_DIR", "AITEAM_API_URL"):
        monkeypatch.delenv(name, raising=False)
    return home / ".aiteam"


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


def _write_lock(directory: Path, refreshed_at: float | None = None, **fields) -> None:
    body = {"pid": os.getppid(), "host": socket.gethostname(), "started_at": time.time(), **fields}
    lock = directory / paths.MIGRATION_LOCK
    lock.write_text(json.dumps(body), encoding="utf-8")
    if refreshed_at is not None:
        os.utime(lock, (refreshed_at, refreshed_at))


_EXPIRED = time.time() - mig.LOCK_TTL_SECONDS - 5
CASES = {
    "none": None,
    "live-other-process": {},
    "dead-process": {"pid": "dead"},
    "expired": {"started_at": _EXPIRED, "refreshed_at": _EXPIRED},
    "old-but-refreshed": {"started_at": _EXPIRED},  # a long copy keeps touching it
    "other-host": {"host": "elsewhere.invalid", "pid": 1},
    "this-process": {"pid": os.getpid()},
    "garbage": "garbage",
}


@pytest.mark.parametrize("case", list(CASES))
def test_hook_reader_agrees_with_the_mover(data_dir, case):
    spec = CASES[case]
    if spec == "garbage":
        (data_dir / paths.MIGRATION_LOCK).write_text("not json", encoding="utf-8")
    elif spec is not None:
        fields = dict(spec)
        if fields.get("pid") == "dead":
            fields["pid"] = _dead_pid()
        _write_lock(data_dir, **fields)
    expected = mig.migration_lock_active(data_dir)
    assert (paths.migration_lock() is not None) is expected
    assert expected is (case in ("live-other-process", "old-but-refreshed", "other-host", "garbage"))


def test_the_lock_is_found_wherever_the_directory_is_mid_move(data_dir, tmp_path):
    """Before the rename it sits in the legacy directory, after it in the canonical one."""
    home = data_dir.parent
    data_dir.rmdir()
    legacy = home / ".claude" / "data" / "ai-team-os"
    legacy.mkdir(parents=True)
    _write_lock(legacy)
    found = paths.migration_lock()
    assert found is not None and found["path"] == str(legacy / paths.MIGRATION_LOCK)


def test_autostart_does_not_start_the_api_during_a_migration(data_dir, monkeypatch):
    from aiteam.mcp import _autostart

    _write_lock(data_dir)
    events = []
    monkeypatch.setattr(_autostart, "_record_event", lambda name, **fields: events.append((name, fields)))

    def refuse(*args, **kwargs):
        raise AssertionError("autostart went on during a migration")

    monkeypatch.setattr(_autostart, "_get_api_port", refuse)
    monkeypatch.setattr(_autostart, "_ensure_api_running_locked", refuse)
    _autostart._ensure_api_running()
    assert ("api.autostart.skipped", {"reason": "data_dir_migrating"}) in events


def test_os_restart_api_refuses_during_a_migration(data_dir, monkeypatch):
    import aiteam.mcp.tools.infra as infra
    from aiteam.mcp import _autostart

    _write_lock(data_dir)

    class Capture:
        tools: dict = {}

        def tool(self, *args, **kwargs):
            def decorator(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorator

    capture = Capture()
    infra.register(capture)

    def refuse(*args, **kwargs):
        raise AssertionError("os_restart_api went on during a migration")

    monkeypatch.setattr(_autostart, "_get_api_port", refuse)
    result = capture.tools["os_restart_api"](force=True)
    assert result["success"] is False and result["error"] == "data_dir_migrating"
    assert result["lock"] == str(data_dir / paths.MIGRATION_LOCK)


def test_aiteam_up_refuses_during_a_migration(data_dir, monkeypatch):
    import uvicorn
    from typer.testing import CliRunner

    from aiteam.cli.app import app

    _write_lock(data_dir)

    def refuse(*args, **kwargs):
        raise AssertionError("aiteam up started the API during a migration")

    monkeypatch.setattr(uvicorn, "run", refuse)
    result = CliRunner().invoke(app, ["up", "--api-port", "8765"])
    assert result.exit_code == 1, result.output
    assert "正在迁移" in result.output
