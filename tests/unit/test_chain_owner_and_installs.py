"""The installer side of the path section (hook_core): command spelling, owner record, install registry.

docs/config-dir-compat-design.md §4.2 γ (the ``${CLAUDE_CONFIG_DIR:-...}`` command
form and §7 U3, every matcher recognises both spellings), §4.3 (.chain-owner.json)
and §4.2 / §5.3 (installs.json, whose last migration reason the data_dir_legacy
notice quotes). HOME is a throwaway directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiteam import paths
from aiteam.hooks import hook_core

ROOT = Path(__file__).resolve().parents[2]
SHELL_FORM = '"/py" "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/ai-team-os/send_event.py" Stop'
ABSOLUTE_FORM = '"/py" "/home/x/.claude/hooks/ai-team-os/send_event.py" Stop'
USERS_OWN = '"/py" "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/ai-team-os/mail_reminder.py"'


@pytest.fixture()
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    (path / ".aiteam").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AITEAM_DATA_DIR", raising=False)
    return path


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The command spelling of the main chain directory
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell form")
@pytest.mark.parametrize("configured", [False, True], ids=["default-folder", "configured-folder"])
def test_the_command_form_expands_to_the_chain_dir_of_the_session(home, tmp_path, monkeypatch, configured):
    """What sh -c makes of the spelling is what the resolver says, with or without the variable."""
    if configured:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile"))
    spelled = hook_core.cc_main_chain_command_dir()
    assert spelled == "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/ai-team-os"
    expanded = subprocess.run(["/bin/sh", "-c", f'printf %s "{spelled}"'], capture_output=True, text=True,
                              env=dict(os.environ), timeout=10, check=True).stdout
    assert expanded == str(hook_core.cc_main_chain_dir())


@pytest.mark.parametrize(("command", "ours"), [
    pytest.param(SHELL_FORM, True, id="variable-form"),
    pytest.param(ABSOLUTE_FORM, True, id="absolute-form"),
    pytest.param(ABSOLUTE_FORM.replace("/", "\\"), True, id="windows-backslashes"),
    pytest.param(USERS_OWN, False, id="users-own-script-in-the-dir"),
    pytest.param('"/py" "/opt/guards/send_event.py"', False, id="outside-the-dir"),
])
def test_u3_the_installers_recognise_both_spellings(command, ours):
    installer = _load(ROOT / "install.py", "_u3_install")
    assert installer._is_our_hook(command) is ours


@pytest.mark.parametrize("command", [SHELL_FORM, ABSOLUTE_FORM], ids=["variable-form", "absolute-form"])
def test_u3_every_main_chain_check_sees_both_spellings(command):
    settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}
    bootstrap = _load(ROOT / "plugin" / "hooks" / "session_bootstrap.py", "_u3_bootstrap")
    uninstaller = _load(ROOT / "scripts" / "uninstall.py", "_u3_uninstall")
    assert bootstrap._main_chain_registered(settings)
    assert uninstaller._is_our_hook(command)


# ---------------------------------------------------------------------------
# .chain-owner.json
# ---------------------------------------------------------------------------


def test_owner_record_round_trip_and_what_does_not_count(home):
    chain = hook_core.cc_main_chain_dir()
    chain.mkdir(parents=True)
    assert paths.chain_owner() is None
    for broken in ("{not json", json.dumps(["source"]), json.dumps({"installed_by": "someone"})):
        (chain / hook_core.CHAIN_OWNER_FILE).write_text(broken)
        assert paths.chain_owner() is None, broken
    written = hook_core.write_chain_owner("source", baseline="/src/tree", version="1.15.2")
    assert written == chain / ".chain-owner.json"
    record = paths.chain_owner()
    assert (record["installed_by"], record["baseline"], record["version"]) == ("source", "/src/tree", "1.15.2")
    assert record["installed_at"].endswith("Z")
    assert sorted(p.name for p in chain.iterdir()) == [".chain-owner.json"], "no temporary file left"
    with pytest.raises(ValueError):
        hook_core.write_chain_owner("someone", baseline="", version="")


def test_owner_record_follows_the_config_folder(home, tmp_path, monkeypatch):
    hook_core.cc_main_chain_dir().mkdir(parents=True)
    hook_core.write_chain_owner("source", baseline="/a", version="1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile"))
    assert paths.chain_owner() is None


# ---------------------------------------------------------------------------
# installs.json
# ---------------------------------------------------------------------------


def test_install_records_are_stamped_bounded_and_survive_a_broken_file(home, monkeypatch):
    registry = home / ".aiteam" / "installs.json"
    registry.write_text("{broken")
    monkeypatch.setattr(hook_core, "INSTALLS_KEPT", 3)
    for index in range(5):
        assert hook_core.append_install_record({"host": "cc", "n": index}) == registry
    records = json.loads(registry.read_text())
    assert [record["n"] for record in records] == [2, 3, 4]
    assert all(record["at"].endswith("Z") for record in records)


def test_the_last_migration_attempt_is_the_newest_record_that_tried(home):
    assert paths.last_migration_attempt() is None
    hook_core.append_install_record({"migration": {"reason": "locked"}})
    hook_core.append_install_record({"migration": {"reason": "db_in_use"}})
    hook_core.append_install_record({"kind": "source"})  # a later plain install tried nothing
    assert paths.last_migration_attempt() == {"reason": "db_in_use"}


def test_no_data_directory_no_registry(home):
    (home / ".aiteam").rmdir()
    with pytest.raises(hook_core.DataDirMissing):
        hook_core.append_install_record({"host": "cc"})
    assert paths.last_migration_attempt() is None
    assert not (home / ".aiteam").exists(), "run time never creates it"


def test_installers_load_the_same_definitions_by_path():
    """install.py (by path, standard library) and the package see one path section."""
    code = ("import importlib.util, sys; spec = importlib.util.spec_from_file_location('i', sys.argv[1]); "
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
            "print(m._hook_core().CHAIN_OWNER_FILE, m._hook_core().INSTALLS_FILE)")
    out = subprocess.run([sys.executable, "-c", code, str(ROOT / "install.py")], capture_output=True, text=True,
                         timeout=30, check=True).stdout.split()
    assert out == [paths.CHAIN_OWNER_FILE, paths.INSTALLS_FILE]
