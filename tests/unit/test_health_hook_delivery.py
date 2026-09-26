"""os_health_check 的 hook_delivery 段：API 活着和挂了都要给出，数据来自 hook 写的本机账。

账由真 send_event 子进程写（拒连），再由健康检查读：跨进程、跨文件，不拿内存对象拼。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from aiteam.clock import utc_now
from aiteam.mcp import _autostart
from aiteam.mcp.tools import infra

ROOT = Path(__file__).resolve().parents[2]
SEND_EVENT = ROOT / "plugin" / "hooks" / "send_event.py"


class Capture:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


capture = Capture()
infra.register(capture)
health = capture.tools["os_health_check"]


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture()
def ledger_home(tmp_path, monkeypatch):
    """Two refused Stop events and one refused PreToolUse, written by the real hook."""
    env = {**os.environ, "HOME": str(tmp_path),
           "AITEAM_API_URL": f"http://127.0.0.1:{_closed_port()}"}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    for event, payload in (
        ("Stop", {"session_id": "s1", "cwd": "/w"}),
        ("Stop", {"session_id": "s1", "cwd": "/w"}),
        ("PreToolUse", {"session_id": "s1", "cwd": "/w", "tool_name": "Bash",
                        "tool_input": {"command": "ls"}, "tool_use_id": "toolu_1"}),
    ):
        subprocess.run([sys.executable, str(SEND_EVENT), event], input=json.dumps(payload),
                       text=True, capture_output=True, env=env, timeout=30, check=True)
    directory = tmp_path / ".claude" / "data" / "ai-team-os" / "hook-delivery"
    stale = {"t": (utc_now() - timedelta(hours=30)).isoformat(), "ev": "Stop", "cls": "http_5xx"}
    with open(directory / "ledger.1.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(stale) + "\n")
    with open(directory / "ledger.jsonl", "a", encoding="utf-8") as f:
        f.write("not json\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _default_config_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _expected(home: Path) -> dict:
    return {
        "window_hours": 24,
        "complete": True,  # the rotated generation reaches back past the window
        "failed_posts": 3,
        "by_class": {"refused": 3},
        "by_event": {"Stop": 2, "PreToolUse": 1},
        "ledger": str(home / ".claude/data/ai-team-os/hook-delivery/ledger.jsonl"),
        # The keyed PreToolUse was queued for redelivery; the two Stops only recorded.
        "replay": {
            "pending": 1,
            "queued": 1,
            "queued_shrunk": 0,
            "not_queued": {"unkeyed": 2, "not_replayable_event": 0, "not_replayable_class": 0},
            "outcomes": {"delivered": 0, "duplicate": 0, "failed": 0, "orphan_recovered": 0},
            "by_origin_class": {},
            "drops": {"spool_full": 0, "spool_error": 0, "too_large": 0, "expired": 0,
                      "exhausted": 0, "rejected": 0, "corrupt": 0},
        },
        "installed_hooks": {
            "dir": str(home / ".claude/hooks/ai-team-os"),
            "send_event": False,
            "hook_delivery": False,
            "recording": "no_source_install",
        },
        "unreadable_lines": 1,
    }


def test_healthy_branch_reports_hook_delivery(ledger_home, monkeypatch):
    monkeypatch.setenv("AITEAM_API_URL", "http://localhost:8765")
    with (
        patch.object(infra, "_api_call", return_value={"success": True, "total": 2}),
        patch.object(infra, "_usage_coverage_line", return_value="no data"),
        patch.object(_autostart, "_get_api_port", return_value=8765),
        patch.object(_autostart, "_reconcile_api_pid", return_value=123),
    ):
        result = health()
    section = result["hook_delivery"]
    assert section.pop("last_failure_at") is not None
    assert section.pop("covered_since") is not None
    assert 0 <= section["replay"].pop("oldest_age_s") < 120
    assert section == _expected(ledger_home)


def test_unhealthy_branch_still_reports_hook_delivery(ledger_home):
    """The API being down is exactly when the local ledger matters."""
    with patch.object(infra, "_api_call", return_value={"success": False, "error": "refused"}):
        result = health()
    assert result["status"] == "unhealthy"
    section = result["hook_delivery"]
    section.pop("last_failure_at")
    section.pop("covered_since")
    assert 0 <= section["replay"].pop("oldest_age_s") < 120
    assert section == _expected(ledger_home)


def test_no_ledger_means_zero_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch.object(infra, "_api_call", return_value={"success": False, "error": "x"}):
        section = health()["hook_delivery"]
    assert section["failed_posts"] == 0 and section["by_class"] == {}
    assert section["last_failure_at"] is None


def test_a_broken_summary_does_not_break_the_check(monkeypatch):
    monkeypatch.setattr(infra, "_hook_delivery_summary", lambda: 1 / 0)
    with patch.object(infra, "_api_call", return_value={"success": False, "error": "x"}):
        result = health()
    assert result["status"] == "unhealthy"
    assert result["hook_delivery"]["error"].startswith("ZeroDivisionError")


def _write_ledger(home: Path, name: str, hours_ago: list[float]) -> None:
    directory = home / ".claude" / "data" / "ai-team-os" / "hook-delivery"
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / name, "a", encoding="utf-8") as f:
        for hours in hours_ago:
            at = (utc_now() - timedelta(hours=hours)).isoformat()
            f.write(json.dumps({"t": at, "ev": "Stop", "cls": "refused"}) + "\n")


def _section(home: Path, monkeypatch) -> dict:
    monkeypatch.setenv("HOME", str(home))
    with patch.object(infra, "_api_call", return_value={"success": False, "error": "x"}):
        return health()["hook_delivery"]


def test_rotated_within_the_window_is_marked_a_lower_bound(tmp_path, monkeypatch):
    """Two rotations in a busy day: the oldest surviving line is younger than the window."""
    _write_ledger(tmp_path, "ledger.1.jsonl", [2.0, 1.5])
    _write_ledger(tmp_path, "ledger.jsonl", [1.0])
    section = _section(tmp_path, monkeypatch)
    assert section["complete"] is False
    assert section["failed_posts"] == 3
    oldest = datetime.fromisoformat(section["covered_since"])
    assert timedelta(hours=1.9) < utc_now() - oldest < timedelta(hours=2.1)


def test_unrotated_ledger_covers_the_whole_window(tmp_path, monkeypatch):
    """No rotation yet: nothing older was ever recorded, so the counts are complete."""
    _write_ledger(tmp_path, "ledger.jsonl", [1.0])
    section = _section(tmp_path, monkeypatch)
    assert section["complete"] is True
    start = datetime.fromisoformat(section["covered_since"])
    assert timedelta(hours=23.9) < utc_now() - start < timedelta(hours=24.1)


@pytest.mark.parametrize(
    ("installed", "recording"),
    [
        (("send_event.py", "hook_delivery.py"), "active"),
        (("send_event.py",), "fallback_unrecorded"),
        ((), "no_source_install"),
    ],
)
def test_installed_hooks_say_whether_failures_are_recorded(tmp_path, monkeypatch,
                                                           installed, recording):
    """"No failed POSTs" and "the installed hooks cannot record any" must look different."""
    hooks_dir = tmp_path / ".claude" / "hooks" / "ai-team-os"
    hooks_dir.mkdir(parents=True)
    for name in installed:
        (hooks_dir / name).write_text("", encoding="utf-8")
    section = _section(tmp_path, monkeypatch)
    assert section["failed_posts"] == 0
    assert section["installed_hooks"] == {
        "dir": str(hooks_dir),
        "send_event": "send_event.py" in installed,
        "hook_delivery": "hook_delivery.py" in installed,
        "recording": recording,
    }


def test_replay_outcomes_are_grouped_by_the_first_failure_class(tmp_path, monkeypatch):
    """The landing rate of a class = duplicates among its answered redeliveries."""
    directory = tmp_path / ".claude" / "data" / "ai-team-os" / "hook-delivery"
    directory.mkdir(parents=True)
    now = utc_now().isoformat()
    lines = (
        [{"t": now, "replay": "duplicate", "origin_cls": "timeout_after_send"}] * 3
        + [{"t": now, "replay": "delivered", "origin_cls": "timeout_after_send"}]
        + [{"t": now, "replay": "delivered", "origin_cls": "refused"}] * 5
        + [{"t": now, "replay": "failed", "origin_cls": "http_5xx"}]
        + [{"t": now, "ev": "Write", "cls": "refused", "spool": "too_large"}]
        + [{"t": now, "ev": "Write", "cls": "refused", "spool": "queued", "shrunk_from": 900000}]
    )
    (directory / "ledger.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch.object(infra, "_api_call", return_value={"success": False, "error": "x"}):
        replay = health()["hook_delivery"]["replay"]
    assert replay["by_origin_class"] == {
        "http_5xx": {"failed": 1},
        "refused": {"delivered": 5, "landed_share": 0.0},
        "timeout_after_send": {"duplicate": 3, "delivered": 1, "landed_share": 0.75},
    }
    assert replay["drops"]["too_large"] == 1
    assert (replay["queued"], replay["queued_shrunk"]) == (1, 1)
