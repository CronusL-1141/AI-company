"""Updating from v1.15.2 moves the data on the user's own update; a plugin really removed is still an orphan.

docs/config-dir-compat-design.md §7 T7 and T8 through the installer entry the
user runs (``install.py --update``, as a subprocess, ``--offline`` so it neither
pulls the checkout nor reinstalls packages), and the orphan judgement after a
plugin is really removed. A throwaway HOME, a real API process on a free port
(read back over HTTP), a fake ``claude`` CLI first on PATH; nothing touches port
8000 or the real HOME. The harness is test_config_dir_relocation's.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.test_config_dir_relocation import (
    E06,
    HOLDER,
    ROOT,
    Machine,
    _installer,
    _legacy_machine,
    _real_databases,
    _text,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX processes, lsof and the ${...} hook commands")

SHELL_DIR = "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/ai-team-os/"
LEGACY_LINE = "数据仍在旧位置"
FAKE_CLAUDE = f"#!{sys.executable}\nimport sys\nsys.exit(1)\n"  # no CLI here: the installer writes directly


@pytest.fixture()
def machine(tmp_path):
    instance = Machine(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(FAKE_CLAUDE, encoding="utf-8")
    (bin_dir / "claude").chmod(0o755)
    instance.env["PATH"] = f"{bin_dir}{os.pathsep}{instance.env['PATH']}"
    try:
        yield instance
    finally:
        instance.stop()
        instance.stop_spawned_api()
        instance.client.close()


def _v1152_install(machine: Machine) -> Path:
    """What install.py of v1.15.2 left: copies in the default folder, absolute commands, no owner record."""
    config = machine.home / ".claude"
    chain = config / "hooks" / "ai-team-os"
    chain.mkdir(parents=True)
    installer = _installer()
    for name in installer.HOOK_SCRIPTS + installer.HOOK_SUPPORT_MODULES:
        shutil.copy2(ROOT / "plugin" / "hooks" / name, chain / name)
    hooks: dict = {}
    for event, matcher, entries in installer.HOOK_SURFACE:
        group = {"matcher": matcher, "hooks": []} if matcher else {"hooks": []}
        for script, arg, timeout in entries:
            command = f'"{sys.executable}" "{chain}/{script}"' + (f" {arg}" if arg else "")
            group["hooks"].append({"type": "command", "command": command, "timeout": timeout})
        hooks.setdefault(event, []).append(group)
    (config / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return chain


def _update(machine: Machine, *args: str) -> subprocess.CompletedProcess[str]:
    """The user's update: install.py --update, run from the checkout as a separate process."""
    return subprocess.run([sys.executable, str(ROOT / "install.py"), "--update", "--offline", *args],
                          text=True, capture_output=True, env=machine.env, cwd=machine.tmp, timeout=300)


def _registered(machine: Machine) -> list[str]:
    hooks = json.loads((machine.home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
    return [hook["command"] for groups in hooks.values() for group in groups for hook in group["hooks"]]


def _assert_surface_installed(machine: Machine) -> None:
    """The rest of the update happened whatever the data directory did."""
    commands = _registered(machine)
    assert len(commands) == sum(len(entries) for _e, _m, entries in _installer().HOOK_SURFACE)
    assert all(SHELL_DIR in command for command in commands), "absolute commands rewritten to the variable form"
    owner = json.loads((machine.home / ".claude" / "hooks" / "ai-team-os" / ".chain-owner.json").read_text())
    assert (owner["installed_by"], owner["baseline"]) == ("source", str(ROOT))
    servers = json.loads((machine.home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "ai-team-os" in servers


def _records(data_dir: Path) -> list[dict]:
    return json.loads((data_dir / "installs.json").read_text(encoding="utf-8"))


def test_t7_update_from_v1152_moves_the_data_and_rewires_the_chain(machine):
    """T7: the data leaves the config folder during the update; the old place keeps working as a link."""
    legacy, ids, user_version = _legacy_machine(machine, 3)
    chain = _v1152_install(machine)
    old_api = machine.process

    result = _update(machine)
    assert result.returncode == 0, result.stdout + result.stderr
    assert old_api.wait(timeout=30) is not None  # gate 3 stopped the old API
    machine.process = None

    canonical = machine.home / ".aiteam"
    assert legacy.is_symlink() and os.path.samefile(legacy, canonical)
    assert _real_databases(machine.home) == [canonical / "aiteam.db"]
    assert "[6] 搬: 通过" in result.stdout and "[7] 起 API 并对账: 通过" in result.stdout
    with sqlite3.connect(f"file:{canonical / 'aiteam.db'}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == user_version
    connection.close()
    assert not (canonical / "MIGRATING").exists()
    health = machine.wait_healthy()
    assert (health["data_dir"], health["data_dir_mode"]) == (str(canonical), "canonical")
    assert machine.task_count() == 3 and all(machine.task_title(task_id) for task_id in ids)
    _assert_surface_installed(machine)
    record = _records(canonical)[-1]
    assert record["migration"]["ok"] and record["data_migrated_at"] and record["data_dir_mode"] == "canonical"

    line, note = _text(machine.session_start(chain))
    assert LEGACY_LINE not in line and E06 not in line + note


def test_t8_update_with_the_database_held_open_keeps_the_data_and_says_why(machine):
    """T8 through the installer: db_in_use, nothing moved, the update finishes, the notice gives the reason."""
    legacy, ids, _ = _legacy_machine(machine, 3)
    chain = _v1152_install(machine)
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(legacy / "aiteam.db"), ids[0]],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        result = _update(machine)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "(db_in_use)" in result.stdout and str(holder.pid) in result.stdout

        canonical = machine.home / ".aiteam"
        assert not os.path.lexists(canonical)
        assert legacy.is_dir() and not legacy.is_symlink() and (legacy / "aiteam.db").is_file()
        assert sorted(p.name for p in legacy.parent.iterdir()) == ["ai-team-os"], "no .migrated-*, no stub"
        assert not (legacy / "MOVED-TO").exists() and not (legacy / "MIGRATING").exists()
        health = machine.wait_healthy()  # restored on the old place by the migration
        assert (health["data_dir"], health["data_dir_mode"]) == (str(legacy), "legacy")
        assert machine.task_count() == 3
        _assert_surface_installed(machine)
        migration = _records(legacy)[-1]["migration"]
        assert (migration["ok"], migration["reason"], migration["gate"]) == (False, "db_in_use", 4)
        assert [entry["pid"] for entry in migration["detail"]["holders"]] == [holder.pid]

        line, note = _text(machine.session_start(chain))
        assert LEGACY_LINE in line and "db_in_use" in line, line
        assert "python3 -m aiteam.data_dir_migration" in note and E06 not in line + note

        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "done"
        assert holder.wait(timeout=30) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)
        holder.stdin.close()
        holder.stdout.close()
    assert machine.task_count() == 4  # the write by the old path landed in the one database served
    assert _real_databases(machine.home) == [legacy / "aiteam.db"]


def _run_hook(machine: Machine, script: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    payload = {"session_id": "s-plugin", "source": "startup", "cwd": str(machine.tmp)}
    result = subprocess.run([sys.executable, str(script)], input=json.dumps(payload), text=True,
                            capture_output=True, env={**machine.env, **extra}, cwd=machine.tmp, timeout=120)
    assert result.returncode == 0, result.stderr
    return result


def test_a_plugin_really_removed_is_still_reported_as_an_orphan(machine):
    """The plugin's self-heal installs and owns the chain; once the plugin is removed, the chain is an orphan."""
    (machine.home / ".aiteam").mkdir()
    plugin = machine.tmp / "plugin"
    shutil.copytree(ROOT / "plugin" / "hooks", plugin / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "plugin" / ".claude-plugin", plugin / ".claude-plugin")
    config = machine.home / ".claude"
    (config / "plugins").mkdir(parents=True)
    key = "ai-team-os@ai-team-os"
    (config / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {key: [{"scope": "user", "installPath": str(plugin)}]}}))
    (config / "settings.json").write_text(json.dumps({"enabledPlugins": {key: True}}))
    machine.env["AITEAM_API_URL"] = "http://127.0.0.1:9"  # the judgement reads files only

    _run_hook(machine, plugin / "hooks" / "auto_install.py", CLAUDE_PLUGIN_ROOT=str(plugin))
    chain = config / "hooks" / "ai-team-os"
    owner = json.loads((chain / ".chain-owner.json").read_text())
    assert owner["installed_by"] == "plugin" and all(SHELL_DIR in c for c in _registered(machine))
    line, _note = _text(machine.session_start(chain))
    assert E06 not in line, "the plugin is installed: not an orphan"

    (config / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {}}))
    settings = json.loads((config / "settings.json").read_text())
    settings.pop("enabledPlugins")
    (config / "settings.json").write_text(json.dumps(settings))
    line, note = _text(machine.session_start(chain))
    assert E06 in line and "uninstall_main_chain.py" in note
