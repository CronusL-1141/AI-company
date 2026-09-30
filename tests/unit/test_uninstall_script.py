"""scripts/uninstall.py keeps user data, removes the right package and stops only our API.

Home is a temporary directory. No real process is ever signalled: the module's
subprocess is replaced by a recorder that answers like the real tools, and the
autostart helpers that find and stop the API are stubbed.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiteam.mcp import _autostart

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "uninstall.py"


class FakeSubprocess:
    """Answers lsof, kill, taskkill, powershell and pip like the real tools, and runs none of them."""

    def __init__(self, port_pids: str = ""):
        self.calls: list[list[str]] = []
        self.installed = {"ai-team-os": "1.15.0"}
        self.sticky = False  # pip reports success but the distribution stays (e.g. a second copy)
        self.port_pids = port_pids

    def run(self, args, **_kwargs):
        args = [str(arg) for arg in args]
        self.calls.append(args)
        if args[0] == "lsof" or args[0] == "powershell":
            return SimpleNamespace(returncode=0, stdout=self.port_pids, stderr="")
        if args[0] in ("kill", "taskkill"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == sys.executable and args[1] == "-c":  # importlib.metadata probe
            version = self.installed.get(args[-1])
            return SimpleNamespace(returncode=0 if version else 1, stdout=f"{version or ''}\n", stderr="")
        if args[0] == sys.executable and args[1:4] == ["-m", "pip", "uninstall"]:
            name = args[4]
            if name not in self.installed:
                # Real pip exits 0 when asked to remove something that is not installed.
                return SimpleNamespace(returncode=0, stdout="",
                                       stderr=f"WARNING: Skipping {name} as it is not installed.")
            if not self.sticky:
                del self.installed[name]
            return SimpleNamespace(returncode=0, stdout=f"Successfully uninstalled {name}", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    def signalled(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] in ("kill", "taskkill")]


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "0")
    monkeypatch.delenv("AITEAM_API_URL", raising=False)
    return home


@pytest.fixture()
def fake(monkeypatch):
    return FakeSubprocess()


@pytest.fixture()
def api(monkeypatch):
    """Stub the autostart's listener discovery, identity check and termination."""
    state = SimpleNamespace(listeners={}, ours=set(), terminated=[], port_file=8000)

    def pin(pid, listeners):
        return [SimpleNamespace(pid=pid)] if pid in state.ours else None

    def terminate(family, *, reason, port=None):
        state.terminated.append(([member.pid for member in family], reason, port))
        state.listeners.pop(port, None)

    monkeypatch.setattr(_autostart, "_listener_pids", lambda port: set(state.listeners.get(port, ())))
    monkeypatch.setattr(_autostart, "_pin_api_family", pin)
    monkeypatch.setattr(_autostart, "_terminate_api_family", terminate)
    monkeypatch.setattr(_autostart, "_get_api_port", lambda: state.port_file)
    return state


@pytest.fixture()
def uninstall(home, fake, api, monkeypatch):
    spec = importlib.util.spec_from_file_location("uninstall_script_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "subprocess", fake)
    return module


def _run(module, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["uninstall.py", *argv])
    module.main()


def _seed_data(home: Path) -> Path:
    data = home / ".claude" / "data" / "ai-team-os"
    data.mkdir(parents=True)
    with sqlite3.connect(data / "aiteam.db") as db:
        db.execute("create table marker (v text)")
        db.execute("insert into marker values ('keep me')")
    return data


# (a) data directory ------------------------------------------------------------

@pytest.mark.parametrize("argv", [(), ("--keep-data",)], ids=["default", "keep-data"])
def test_data_directory_survives_unless_purge_is_asked(uninstall, home, monkeypatch, capsys, argv):
    data = _seed_data(home)
    (home / ".claude" / "hooks" / "ai-team-os").mkdir(parents=True)
    _run(uninstall, monkeypatch, *argv)
    with sqlite3.connect(data / "aiteam.db") as db:
        assert db.execute("select v from marker").fetchone() == ("keep me",)
    assert not (home / ".claude" / "hooks" / "ai-team-os").exists(), "the install surface still goes"
    assert "--purge-data" in capsys.readouterr().out


def test_purge_data_deletes_the_data_directory(uninstall, home, monkeypatch):
    data = _seed_data(home)
    _run(uninstall, monkeypatch, "--purge-data")
    assert not data.exists()


def test_purge_and_keep_cannot_be_combined(uninstall, home, monkeypatch):
    data = _seed_data(home)
    with pytest.raises(SystemExit):
        _run(uninstall, monkeypatch, "--purge-data", "--keep-data")
    assert (data / "aiteam.db").exists()


# (b) pip package ---------------------------------------------------------------

def test_pip_removes_the_distribution_and_checks_it_is_gone(uninstall, fake, capsys):
    uninstall.pip_uninstall(dry_run=False)
    assert [sys.executable, "-m", "pip", "uninstall", "ai-team-os", "-y"] in fake.calls
    assert "ai-team-os" not in fake.installed
    assert "[OK]     ai-team-os uninstalled" in capsys.readouterr().out


def test_pip_success_is_not_claimed_while_the_package_remains(uninstall, fake, capsys):
    fake.sticky = True
    uninstall.pip_uninstall(dry_run=False)
    out = capsys.readouterr().out
    assert "[WARN]" in out and "still installed" in out and "[OK]" not in out


# (c) stopping the API ------------------------------------------------------------

@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_only_a_verified_listener_is_stopped_through_the_graceful_path(
        uninstall, fake, api, monkeypatch, capsys, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:18999")
    api.listeners[18999] = {4242}
    api.ours.add(4242)
    fake.port_pids = "4242\n5151\n"  # what lsof -ti / PowerShell would list: listener plus a client
    uninstall.kill_api_process(dry_run=False)
    # _terminate_api_family sends SIGTERM and escalates to SIGKILL only after the grace period.
    assert api.terminated == [([4242], "uninstall", 18999)]
    assert fake.signalled() == []
    assert "[OK]     API stopped" in capsys.readouterr().out


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_a_listener_that_is_not_our_api_is_left_running(uninstall, fake, api, monkeypatch, capsys, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:18999")
    api.listeners[18999] = {5151}
    fake.port_pids = "5151\n"
    uninstall.kill_api_process(dry_run=False)
    assert api.terminated == [] and fake.signalled() == []
    assert "not a verifiable AI Team OS API; left running" in capsys.readouterr().out


def test_dry_run_names_the_api_without_stopping_it(uninstall, fake, api, monkeypatch, capsys):
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:18999")
    api.listeners[18999] = {4242}
    api.ours.add(4242)
    fake.port_pids = "4242\n"
    uninstall.kill_api_process(dry_run=True)
    assert api.terminated == [] and fake.signalled() == []
    assert "[STOP]   AI Team OS API PID 4242 on port 18999" in capsys.readouterr().out


@pytest.mark.parametrize(("url", "port_file", "expected"), [
    (None, 8000, 8000),
    (None, 8123, 8123),  # the autostart moved to a free port and wrote it down
    ("http://127.0.0.1:18999", 8123, 18999),
    ("http://localhost:18998/", 8000, 18998),
    ("http://api.example.invalid:8000", 8000, None),  # remote: nothing local to stop
])
def test_the_port_follows_the_configuration(uninstall, fake, api, monkeypatch, url, port_file, expected):
    if url:
        monkeypatch.setenv("AITEAM_API_URL", url)
    api.port_file = port_file
    looked_up = []
    monkeypatch.setattr(_autostart, "_listener_pids", lambda port: looked_up.append(port) or set())
    fake.port_pids = "4242\n"
    uninstall.kill_api_process(dry_run=False)
    assert looked_up == ([expected] if expected else [])
    assert fake.signalled() == []
