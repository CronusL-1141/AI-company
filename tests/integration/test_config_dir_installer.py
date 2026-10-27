"""The installers write into the session's Claude Code config folder (docs/config-dir-compat-design.md §7 T3).

The real install functions (install.py) and the plugin's self-heal
(auto_install.py) run with HOME pointed at a throwaway directory and
CLAUDE_CONFIG_DIR at a folder outside the default one; a fake ``claude`` first on
PATH stands in for the CLI (it records what it was given, and either fails or
writes the entry where the real CLI does). A registered hook command is then run
through ``sh -c``, the way Claude Code runs it, to show which copy it starts.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL_DIR = "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/ai-team-os/"

FAKE_CLAUDE = """#!{python}
import json, os, sys
from pathlib import Path
record = Path(os.environ["FAKE_CLAUDE_RECORD"])
record.write_text(json.dumps({{"argv": sys.argv[1:], "config_dir": os.environ.get("CLAUDE_CONFIG_DIR")}}))
if os.environ.get("FAKE_CLAUDE_MODE") != "writes":
    sys.exit(1)
target = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
document = json.loads(target.read_text()) if target.exists() else {{}}
document.setdefault("mcpServers", {{}})[sys.argv[5]] = {{**json.loads(sys.argv[6]), "by": "cli"}}
target.write_text(json.dumps(document))
"""


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def machine(tmp_path, monkeypatch):
    """HOME, a configured config folder, and the fake CLI first on PATH."""
    home, profile, bin_dir = tmp_path / "home", tmp_path / "profile", tmp_path / "bin"
    for path in (home, profile, bin_dir):
        path.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
    claude.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "claude-call.json"))
    (home / ".aiteam").mkdir()
    return home, profile, tmp_path / "claude-call.json"


def _commands(settings: Path) -> list[str]:
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    return [hook["command"] for groups in hooks.values() for group in groups for hook in group["hooks"]]


@pytest.mark.skipif(os.name == "nt", reason="the ${CLAUDE_CONFIG_DIR:-...} form is POSIX only")
def test_t3_source_install_lands_in_the_configured_folder(machine, tmp_path, capsys):
    home, profile, call = machine
    installer = _load(ROOT / "install.py", "_t3_install")
    project = tmp_path / "project"  # where a fallback .mcp.json would go; never the checkout
    project.mkdir()

    installer.register_global_mcp(project)
    installer.register_hooks(ROOT)
    installer.copy_agent_templates(ROOT)
    installer.copy_skills(ROOT)
    installer.copy_commands(ROOT)
    installer.install_loop_md(ROOT)
    assert installer.verify_installation(ROOT)
    out = capsys.readouterr().out

    servers = json.loads((profile / ".claude.json").read_text())["mcpServers"]
    assert servers["ai-team-os"]["args"] == ["-m", "aiteam.mcp.server"]
    assert not (home / ".claude.json").exists() and not (home / ".claude").exists()
    assert not (project / ".mcp.json").exists()
    seen = json.loads(call.read_text())
    assert seen["argv"][:5] == ["mcp", "add-json", "--scope", "user", "ai-team-os"]
    assert seen["config_dir"] == str(profile), "the CLI is run for the target folder"

    commands = _commands(profile / "settings.json")
    assert len(commands) == sum(len(entries) for _e, _m, entries in installer.HOOK_SURFACE)
    assert all(f'"{sys.executable}" "{SHELL_DIR}' in command for command in commands)
    owner = json.loads((profile / "hooks" / "ai-team-os" / ".chain-owner.json").read_text())
    assert (owner["installed_by"], owner["baseline"]) == ("source", str(ROOT))
    assert f"[OK] Global MCP in {profile / '.claude.json'}" in out
    assert f"[OK] {profile / 'settings.json'} hooks" in out

    # Claude Code runs the command through sh -c: the variable picks this folder's copy.
    tick = next(c for c in commands if c.endswith("session_bootstrap.py\" resume-tick"))
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CONFIG_DIR"}
    ran = subprocess.run(["/bin/sh", "-c", tick], input="{}", text=True, capture_output=True, timeout=30,
                         env={**env, "CLAUDE_CONFIG_DIR": str(profile)})
    assert ran.returncode == 0, ran.stderr
    default = subprocess.run(["/bin/sh", "-c", tick], input="{}", text=True, capture_output=True, timeout=30,
                             env=env)
    assert default.returncode != 0 and str(home / ".claude" / "hooks" / "ai-team-os") in default.stderr


def test_t3b_a_cli_that_wrote_the_entry_is_not_overwritten(machine, tmp_path, monkeypatch):
    home, profile, _call = machine
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "writes")
    _load(ROOT / "install.py", "_t3b_install").register_global_mcp(tmp_path)
    entry = json.loads((profile / ".claude.json").read_text())["mcpServers"]["ai-team-os"]
    assert entry["by"] == "cli", "verified in place: no direct write over it"
    assert not (home / ".claude.json").exists()


@pytest.mark.parametrize("named", ["other", "default"])
def test_t3_config_dir_flag_selects_the_folder(machine, tmp_path, monkeypatch, named):
    """--config-dir becomes CLAUDE_CONFIG_DIR; naming the default folder drops the variable."""
    home, _profile, _call = machine
    installer = _load(ROOT / "install.py", "_t3_flag_install")
    folder = tmp_path / "elsewhere" if named == "other" else home / ".claude"
    folder.mkdir(parents=True, exist_ok=True)
    installer.apply_config_dir(str(folder))  # monkeypatch restores CLAUDE_CONFIG_DIR afterwards
    core = installer._hook_core()
    assert core.cc_config_dir() == folder
    assert core.cc_global_config() == (folder / ".claude.json" if named == "other" else home / ".claude.json")


def test_a_config_dir_that_does_not_exist_stops_the_run(machine, tmp_path, capsys):
    """A mistyped --config-dir (install, update and uninstall share this step) is not a new config folder."""
    installer = _load(ROOT / "install.py", "_t3_missing_install")
    missing = tmp_path / "typo" / "config"
    with pytest.raises(SystemExit) as stopped:
        installer.apply_config_dir(str(missing))
    assert stopped.value.code not in (0, None)
    assert "does not exist" in capsys.readouterr().out
    assert not (tmp_path / "typo").exists()


@pytest.mark.skipif(os.name == "nt", reason="the ${CLAUDE_CONFIG_DIR:-...} form is POSIX only")
def test_t3c_plugin_self_heal_lands_in_the_configured_folder(machine, tmp_path, monkeypatch):
    """The plugin's first run: creates the data directory on an empty machine, chain owned by the plugin."""
    home, profile, _call = machine
    shutil.rmtree(home / ".aiteam")
    plugin = tmp_path / "plugin"
    shutil.copytree(ROOT / "plugin" / "hooks", plugin / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "plugin" / ".claude-plugin", plugin / ".claude-plugin")
    version = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())["version"]
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:9")
    auto = _load(plugin / "hooks" / "auto_install.py", "_t3c_auto_install")
    monkeypatch.setattr(auto, "_installed_version", lambda: version)
    monkeypatch.setattr(auto, "_pip_install", lambda upgrade: pytest.fail("current version: no pip"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    auto.main()

    assert (home / ".aiteam").is_dir(), "an empty machine gets the data directory on the first run"
    commands = _commands(profile / "settings.json")
    assert commands and all(SHELL_DIR in command for command in commands)
    owner = json.loads((profile / "hooks" / "ai-team-os" / ".chain-owner.json").read_text())
    assert (owner["installed_by"], owner["baseline"], owner["version"]) == ("plugin", str(plugin), version)
    assert not (home / ".claude").exists()


def test_t3c_plugin_first_run_leaves_legacy_data_in_place(machine, tmp_path, monkeypatch):
    """Legacy data: the plugin neither creates the new directory nor moves anything (only updates move)."""
    home, _profile, _call = machine
    shutil.rmtree(home / ".aiteam")
    legacy = home / ".claude" / "data" / "ai-team-os"
    legacy.mkdir(parents=True)
    with sqlite3.connect(legacy / "aiteam.db") as connection:
        connection.execute("CREATE TABLE tasks (id TEXT)")
    connection.close()
    plugin = tmp_path / "plugin"
    shutil.copytree(ROOT / "plugin" / "hooks", plugin / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "plugin" / ".claude-plugin", plugin / ".claude-plugin")
    version = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())["version"]
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    auto = _load(plugin / "hooks" / "auto_install.py", "_t3c_legacy_auto_install")
    monkeypatch.setattr(auto, "_installed_version", lambda: version)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    auto.main()
    assert not os.path.lexists(home / ".aiteam")
    assert (legacy / "aiteam.db").is_file() and not legacy.is_symlink()
