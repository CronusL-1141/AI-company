"""The HTTP shutdown worker must preserve evidence before os._exit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_real_hard_exit_drains_logs_without_touching_shared_state(tmp_path):
    source = Path(__file__).resolve().parents[2] / "src"
    code = """
import asyncio
from aiteam.api import deps
from aiteam.api.routes import system
deps._repository = None
system._wal_checkpoint_best_effort = lambda: None
asyncio.run(system._delayed_exit())
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "PYTHONPATH": str(source),
             "AITEAM_DIAGNOSTICS_DIR": str(tmp_path / "events"), "AITEAM_DIAGNOSTICS_ENABLED": "1",
             "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for file in (tmp_path / "events").glob("*.jsonl")
               for line in file.read_text().splitlines()]
    assert len(records) == 1
    event = records[0]
    assert event["event"] == "api.process.exit"
    assert event["reason"] == "http_shutdown"
    assert event["target_pid"] == event["pid"]
    assert event["exit_code"] == 0
    assert not list(tmp_path.rglob("*.db"))
