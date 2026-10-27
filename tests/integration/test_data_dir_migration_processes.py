"""Data directory migration against real processes (design §7 T8, U4 gate 3, the old-path link).

Real here: the processes holding the database or the API port, lsof (or procfs)
for gates 3-4, SIGTERM and the wait, the symbolic link left at the old path and
code that opens the database by that path from another process. The resolver
and the API start stay injected (see tests/unit/_data_dir_migration_fixtures.py).
"""

from __future__ import annotations

import functools
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from aiteam import data_dir_migration as mig
from aiteam.mcp.tools.infra import _restart_command
from tests.unit._data_dir_migration_fixtures import (
    DB,
    FakeApi,
    FilePostcheck,
    canonical_of,
    databases_under,
    make_legacy,
    snapshot,
    spec_resolver,
    task_count,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process and open-file probes")

# A process whose command line is one of our API entry points for its own port:
# BOOT binds port 0, then execs the SERVE script saved under the entry point's
# program name, so ps and procfs show "<python> <dir>/uvicorn <app> --host ... --port
# N --factory" (uvicorn's console script) or "<python> <dir>/aiteam up --api-port N";
# the same command lines autostart treats as ours (_autostart._is_api_command).
BOOT = r"""
import json, os, socket, sys
tail = json.loads(sys.argv[1])
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen(16)
port = listener.getsockname()[1]
os.set_inheritable(listener.fileno(), True)
os.environ["FAKE_API_FD"] = str(listener.fileno())
os.environ["FAKE_API_PORT"] = str(port)
os.execv(sys.executable, [sys.executable, *[w.replace("{port}", str(port)) for w in tail]])
"""
SERVE = r"""
import os, signal, socket, sqlite3, sys
listener = socket.socket(fileno=int(os.environ["FAKE_API_FD"]))
if os.environ.get("FAKE_API_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.environ.get("FAKE_API_DB"):
    con = sqlite3.connect(os.environ["FAKE_API_DB"])
    con.execute("SELECT COUNT(*) FROM tasks").fetchone()
print(os.environ["FAKE_API_PORT"], flush=True)
sys.stdin.read()
"""
_SENTINEL = 65432
# _restart_command runs "-m uvicorn <app> ..."; its console script takes the same arguments.
API_TAIL = ["{bin}/uvicorn",
            *["{port}" if word == str(_SENTINEL) else word for word in _restart_command(_SENTINEL)[3:]]]
FOREIGN_TAIL = ["{bin}/fake_service.py", "--serve", "--port", "{port}"]


@pytest.fixture()
def spawn(tmp_path_factory):
    """Start processes from this test only and reap every one of them by its own handle."""
    started: list[subprocess.Popen] = []
    programs = tmp_path_factory.mktemp("bin")
    for name in ("uvicorn", "aiteam", "fake_service.py"):
        (programs / name).write_text(SERVE, encoding="utf-8")

    def start(tail: list[str], *, ignore_term: bool = False, db: Path | None = None) -> tuple[subprocess.Popen, int]:
        tail = [word.replace("{bin}", str(programs)) for word in tail]
        env = dict(os.environ)
        if ignore_term:
            env["FAKE_API_IGNORE_TERM"] = "1"
        if db is not None:
            env["FAKE_API_DB"] = str(db)
        proc = subprocess.Popen([sys.executable, "-c", BOOT, json.dumps(tail), ""], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, env=env)
        started.append(proc)
        return proc, int(proc.stdout.readline())

    yield start
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdin.close()
        proc.stdout.close()


def stopper(tmp_path: Path, **kwargs):
    return functools.partial(mig.stop_api_processes, pid_file=tmp_path / "api.pid", interval=0.05, **kwargs)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ============================================================
# Gate 3 with real processes
# ============================================================


def test_our_api_is_stopped_and_the_port_waited_for(tmp_path, spawn):
    proc, port = spawn(API_TAIL)
    command = mig.process_command(proc.pid)  # argv exactly, as autostart reads it
    assert os.path.basename(command[1]) == "uvicorn" and command[2:] == _restart_command(port)[3:], command
    (tmp_path / mig.PORT_FILE_NAME).write_text(str(port))
    stop = stopper(tmp_path)(tmp_path)
    assert stop.ok and stop.port == port and stop.stopped == [proc.pid], stop
    assert proc.wait(timeout=10) == -signal.SIGTERM
    assert not mig.port_open(port)


UP_TAIL = ["{bin}/aiteam", "up", "--api-port", "{port}"]


def test_an_api_started_with_aiteam_up_is_stopped_too(tmp_path, spawn):
    """aiteam up runs uvicorn in-process; its command line is not the restart command, yet it is ours."""
    proc, port = spawn(UP_TAIL)
    (tmp_path / mig.PORT_FILE_NAME).write_text(str(port))
    stop = stopper(tmp_path)(tmp_path)
    assert stop.ok and stop.stopped == [proc.pid], stop
    assert proc.wait(timeout=10) == -signal.SIGTERM
    assert not mig.port_open(port)


def test_dry_run_identifies_our_api_without_signalling(tmp_path, spawn):
    proc, port = spawn(API_TAIL)
    stop = stopper(tmp_path, port=port)(tmp_path, dry_run=True)
    assert stop.ok and stop.was_running and stop.pids == [proc.pid] and stop.stopped == []
    assert proc.poll() is None and mig.port_open(port)


def test_api_alive_after_sigterm_refuses(tmp_path, spawn):
    proc, port = spawn(API_TAIL, ignore_term=True)
    stop = stopper(tmp_path, port=port, polls=3, sleep=lambda seconds: None)(tmp_path)
    assert not stop.ok and stop.reason == mig.API_STILL_RUNNING
    assert stop.detail["alive"] == [proc.pid] and stop.detail["port_open"] is True
    assert proc.poll() is None


def test_foreign_listener_on_the_port_is_not_signalled(tmp_path, spawn):
    proc, port = spawn(FOREIGN_TAIL)
    stop = stopper(tmp_path, port=port)(tmp_path)
    assert not stop.ok and stop.reason == mig.PID_MISMATCH
    assert stop.detail["pid"] == proc.pid and "--serve" in stop.detail["command"]
    assert proc.poll() is None and mig.port_open(port)


def test_pid_file_naming_another_program_is_a_stale_record(tmp_path, spawn):
    proc, _ = spawn(FOREIGN_TAIL)
    (tmp_path / "api.pid").write_text(str(proc.pid))
    stop = stopper(tmp_path, port=free_port())(tmp_path)
    assert stop.ok and not stop.was_running and stop.detail["stale_pid_file"] == proc.pid
    assert proc.poll() is None


# ============================================================
# T8: another process holds the database
# ============================================================

HOLDER = r"""
import sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(db)
con.execute("SELECT COUNT(*) FROM tasks").fetchone()
print("ready", flush=True)
sys.stdin.readline()
con.close()
con = sqlite3.connect(db)  # like a leftover service: reconnect by the original path
con.execute("INSERT INTO tasks VALUES ('late', 'written by path after the refusal')")
con.commit()
con.close()
print("done", flush=True)
"""


def test_open_database_blocks_the_move_and_the_old_path_keeps_working(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    legacy = make_legacy(home)
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(legacy / DB)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        volatile = frozenset({DB + "-shm"})  # the WAL index every reader updates
        before = snapshot(legacy, volatile=volatile)
        api = FakeApi(running=False)
        resolve = spec_resolver(home)
        result = mig.migrate(apply=True, legacy_dir=legacy, target_dir=canonical_of(home), resolve=resolve,
                             stop_api=api.stop, start_api=api.start, postcheck=FilePostcheck(resolve))
        assert result.reason == mig.DB_IN_USE and not result.ok
        gate = result.gates[3]
        assert gate.detail["probe"] in ("lsof", "proc")
        assert [entry["pid"] for entry in gate.detail["holders"]] == [holder.pid]
        assert "sqlite3" in gate.detail["holders"][0]["command"]
        assert [g.status for g in result.gates[4:]] == ["skip", "skip", "skip"]
        # nothing moved: no target, no partial, no backup, no link or stub, no lock
        assert snapshot(legacy, volatile=volatile) == before
        assert not legacy.is_symlink() and not os.path.lexists(canonical_of(home))
        assert sorted(p.name for p in legacy.parent.iterdir()) == ["ai-team-os"]
        assert sorted(p.name for p in home.iterdir()) == [".claude"]
        assert api.names() == ["stop"]
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
    assert task_count(legacy / DB) == 4
    assert databases_under(home) == [legacy / DB]
    assert spec_resolver(home)().mode == "legacy"


# ============================================================
# The link left at the old path
# ============================================================

# Old code does what the released API does on start: open by the fixed path, make sure the schema exists.
OLD_CODE = r"""
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT)")
con.execute("INSERT INTO tasks VALUES ('old', 'written through the old path')")
con.commit()
print(con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
con.close()
"""


def test_old_code_opening_the_old_path_reaches_the_new_database(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    legacy = make_legacy(home)
    target = canonical_of(home)
    resolve = spec_resolver(home)
    api = FakeApi(running=False)
    result = mig.migrate(apply=True, legacy_dir=legacy, target_dir=target, resolve=resolve,
                         stop_api=api.stop, start_api=api.start, postcheck=FilePostcheck(resolve))
    assert result.ok, result.gates
    out = subprocess.run([sys.executable, "-c", OLD_CODE, str(legacy / DB)], capture_output=True, text=True,
                         timeout=60, check=True)
    # the row joined the three migrated tasks, in the new database
    assert out.stdout.strip() == "4"
    assert task_count(target / DB) == 4
    # and no second real database appeared at the old location
    assert databases_under(home) == [target / DB]
    assert result.legacy_marker == "symlink" and legacy.is_symlink() and os.path.samefile(legacy, target)
    assert sorted(p.name for p in legacy.parent.iterdir()) == ["ai-team-os"]


# ============================================================
# Gates 3 and 4 together: the API is the holder until it is stopped
# ============================================================


def test_api_holding_the_database_is_stopped_before_the_holder_check(tmp_path, spawn):
    home = tmp_path / "home"
    home.mkdir()
    legacy = make_legacy(home)
    proc, port = spawn(API_TAIL, db=legacy / DB)
    (legacy / mig.PORT_FILE_NAME).write_text(str(port))
    target = canonical_of(home)
    resolve = spec_resolver(home)
    dry = mig.migrate(legacy_dir=legacy, target_dir=target, resolve=resolve, stop_api=stopper(tmp_path),
                      start_api=None, postcheck=FilePostcheck(resolve))
    assert dry.ok and dry.gates[2].detail["would_stop"] == [proc.pid], dry.gates
    assert proc.poll() is None
    started = []
    result = mig.migrate(apply=True, legacy_dir=legacy, target_dir=target, resolve=resolve,
                         stop_api=stopper(tmp_path), start_api=lambda p: started.append(p) or {"success": True},
                         postcheck=FilePostcheck(resolve))
    assert result.ok, result.gates
    assert proc.wait(timeout=10) == -signal.SIGTERM
    assert result.gates[2].detail["pids"] == [proc.pid] and result.gates[3].detail["probe"] in ("lsof", "proc")
    assert started == [port]
    assert task_count(target / DB) == 3 and legacy.is_symlink()


def test_holder_probe_sees_nothing_on_a_closed_database(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    legacy = make_legacy(home)
    probe = mig.find_db_holders(mig.db_paths(legacy))
    assert probe.ok and probe.holders == [] and probe.method in ("lsof", "proc")
