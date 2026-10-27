"""The hook-rendered data_dir_* notices (docs/config-dir-compat-design.md §5.3).

The hooks render them locally: a missing or split data directory is exactly when
the API cannot be relied on. Distributed scripts run as subprocesses with an
isolated HOME; the API is down (refused) unless a test says otherwise.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from ._notice_fakes import ROOT, hook_env, output, run_hook

CONFLICT = "OS 数据有两份"
ENV_CONFLICT = "AITEAM_DATA_DIR 指向的目录里没有数据库"
MISSING = "找不到 OS 数据目录"
STARTING = "OS 服务正在启动"


def _db(path: Path, version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (x)")
        connection.execute(f"PRAGMA user_version = {version}")
    connection.close()


def _start(home: Path, env: dict) -> dict:
    payload = {"session_id": "s1", "source": "startup", "cwd": str(home)}
    result = run_hook("session_bootstrap.py", payload, env, cwd=home)
    assert result.returncode == 0, result.stderr
    return output(result.stdout)


def _register_chain(home: Path) -> None:
    settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
        "command": f'"{sys.executable}" "{home}/.claude/hooks/ai-team-os/session_bootstrap.py"'}]}]}}
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def test_two_databases_add_a_conflict_line_and_the_start_goes_on(tmp_path):
    home = tmp_path / "home"
    env = hook_env(home)
    _db(home / ".aiteam" / "aiteam.db", 9)
    _db(home / ".claude" / "data" / "ai-team-os" / "aiteam.db", 8)
    doc = _start(home, env)
    assert CONFLICT in doc["systemMessage"] and STARTING in doc["systemMessage"]
    note = doc["hookSpecificOutput"]["additionalContext"]
    assert "user_version 9" in note and "user_version 8" in note
    assert str(home / ".claude" / "data" / "ai-team-os") in note


def test_an_override_without_a_database_is_named(tmp_path):
    home = tmp_path / "home"
    env = hook_env(home, AITEAM_DATA_DIR=str(tmp_path / "scratch"))
    (tmp_path / "scratch").mkdir()
    _db(home / ".aiteam" / "aiteam.db", 9)
    doc = _start(home, env)
    assert ENV_CONFLICT in doc["systemMessage"]
    assert str(tmp_path / "scratch") in doc["hookSpecificOutput"]["additionalContext"]


def test_missing_without_an_installed_chain_stays_silent_and_creates_nothing(tmp_path):
    home = tmp_path / "home"
    env = hook_env(home)
    shutil.rmtree(home / ".aiteam")
    doc = _start(home, env)
    assert MISSING not in json.dumps(doc, ensure_ascii=False)
    assert sorted(p.name for p in home.iterdir()) == []


def test_missing_with_an_installed_chain_replaces_the_start(tmp_path):
    home = tmp_path / "home"
    env = hook_env(home)
    shutil.rmtree(home / ".aiteam")
    _register_chain(home)
    doc = _start(home, env)
    assert MISSING in doc["systemMessage"] and STARTING not in doc["systemMessage"]
    assert "aiteam paths --locate" in doc["hookSpecificOutput"]["additionalContext"]
    assert sorted(p.name for p in home.iterdir()) == [".claude"]
    assert not (home / ".claude" / "data").exists()


def test_codex_start_with_its_receipt_reports_missing(tmp_path):
    home = tmp_path / "home"
    env = hook_env(home)
    shutil.rmtree(home / ".aiteam")
    observer = tmp_path / "codex" / "hooks" / "ai-team-os-observer"
    shutil.copytree(ROOT / "plugin" / "harness" / "codex" / "hooks", observer,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (observer / ".aiteam-codex-install.json").write_text("{}", encoding="utf-8")
    env.update(CODEX_HOME=str(tmp_path / "codex"), LC_ALL="en_US.UTF-8")
    result = subprocess.run([sys.executable, str(observer / "session_bootstrap_codex.py")],
                            input=json.dumps({"session_id": "c1", "source": "startup", "cwd": str(home)}),
                            text=True, capture_output=True, env=env, cwd=home, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "OS data folder not found" in output(result.stdout)["systemMessage"]
    assert sorted(p.name for p in home.iterdir()) == []


# ── a migration in progress: no API call, one line, the start owed ──

MIGRATING = "OS 数据目录正在迁移"


def _lock(home: Path) -> None:
    import os
    import socket
    import time

    body = {"pid": os.getppid(), "host": socket.gethostname(), "started_at": time.time()}
    (home / ".aiteam" / "MIGRATING").write_text(json.dumps(body), encoding="utf-8")


def test_a_migration_in_progress_keeps_the_start_and_prompt_off_the_api(tmp_path):
    from ._notice_fakes import FakeApi, owed_starts

    home = tmp_path / "home"
    with FakeApi() as api:
        env = hook_env(home, api.url)
        _lock(home)
        doc = _start(home, env)
        assert MIGRATING in doc["systemMessage"] and "os_restart_api" in doc["hookSpecificOutput"]["additionalContext"]
        assert len(owed_starts(home)) == 1, "the briefing is delivered after the migration"
        prompt = run_hook("channel_unread.py", {"session_id": "s1", "cwd": str(home), "prompt": "hi"}, env,
                          "leader-cc", cwd=home)
        assert prompt.returncode == 0 and prompt.stdout == "", "said once per session"
        assert api.requests == [], "no API call while the data moves"


def test_codex_start_during_a_migration_says_so_without_calling_the_api(tmp_path):
    from ._notice_fakes import FakeApi

    home = tmp_path / "home"
    observer = tmp_path / "codex" / "hooks" / "ai-team-os-observer"
    shutil.copytree(ROOT / "plugin" / "harness" / "codex" / "hooks", observer,
                    ignore=shutil.ignore_patterns("__pycache__"))
    with FakeApi() as api:
        env = hook_env(home, api.url, language="en_US.UTF-8")
        env.update(CODEX_HOME=str(tmp_path / "codex"))
        _lock(home)
        result = subprocess.run([sys.executable, str(observer / "session_bootstrap_codex.py")],
                                input=json.dumps({"session_id": "c1", "source": "startup", "cwd": str(home)}),
                                text=True, capture_output=True, env=env, cwd=home, timeout=60)
        assert result.returncode == 0, result.stderr
        assert "OS data is being moved" in output(result.stdout)["systemMessage"]
        assert api.requests == []
