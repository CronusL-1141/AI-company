"""Move the OS data directory from its legacy location to the canonical one.

Who may call what: only the update actions a user triggers (installer update,
``scripts/update.py``, the Codex adapter update) and the manual entry
``scripts/migrate_data_dir.py`` call :func:`migrate` or :func:`rollback`.
Runtime code (API, MCP, hooks, CLI queries) never moves data; before starting
the API it asks :func:`migration_lock_active` and refuses while it is true.

Seven gates run in order. Any failure leaves the data where it was, so legacy
mode keeps working, and the next update tries again. The reason codes are the
contract with the callers, which turn them into notices (this module sends
none):

====  ==========  ==============================================  ===================================
gate  name        passes when                                     reason when it does not
====  ==========  ==============================================  ===================================
1     two_sides   the resolver reports legacy for the old dir      conflict, not_legacy,
                  and its database can be read (baseline)          baseline_unreadable
2     lock        the MIGRATING lock is ours                       locked
3     stop_api    the API gate 7 starts runs this code, and our     code_mismatch, pid_mismatch,
                  API (command line checked) has stopped           api_still_running
4     db_holders  no process has the database open                 db_in_use
5     wal         WAL checkpointed down to zero bytes              wal_not_truncated
6     move        rename; across filesystems copy, sha256, rename  rename_failed, checksum_mismatch
7     postcheck   API restarted on the new dir and reconciled      postcheck_failed (then rollback)
====  ==========  ==============================================  ===================================

A dry run judges gates 1-5 only and writes nothing: the lock is checked, not
taken; the API is identified, not stopped; processes found as our API are not
counted as database holders (applying stops them first); the WAL size is
reported without a checkpoint.

After the move the legacy path becomes a symbolic link to the new directory,
so older code still running elsewhere that opens the database by the old path
reaches the real database instead of creating an empty one. Only where no
link can be made (Windows, or the call fails) the legacy path becomes a stub
directory holding ``MOVED-TO`` and the migration record.

Across filesystems the copy takes a while, and hooks keep appending local
records to the old directory meanwhile (they never touch the database, which
gate 4 keeps free). Once the copy is in place and the old directory renamed
aside, what they wrote is carried over (:func:`_carry_over_late_writes`), so the
new directory misses none of it; a rollback across filesystems does the same in
the other direction.

Lock format, kept trivial so hooks can re-implement the check with the
standard library: ``<data dir>/MIGRATING`` holds JSON
``{"pid": int, "host": str, "started_at": float}`` (epoch seconds). It is
active for ``LOCK_TTL_SECONDS`` after it was taken or last refreshed (the
file's mtime, which a copy across filesystems touches while it runs) unless
its process is known to be dead on this host (unparsable content counts from
the file's mtime). The process that
holds the lock is not blocked by it, so it can restart the API itself.

Standard library and sqlite3 only (plus ``aiteam.clock``, itself standard
library, for timestamps). Defaults that need the rest of the package (the path
resolver, the API start, the API command shape, the PID file) are imported
inside the functions that use them, so importing this module needs neither the
resolver nor the MCP package.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from aiteam.clock import from_timestamp

DB_NAME = "aiteam.db"
DB_SIDECARS = ("-wal", "-shm")
LOCK_NAME = "MIGRATING"
LOCK_TTL_SECONDS = 30 * 60
MOVED_TO_NAME = "MOVED-TO"
RECORD_NAME = "MIGRATION.json"
PORT_FILE_NAME = "api_port.txt"
SQLITE_HEADER = b"SQLite format 3\x00"

MANUAL_COMMAND = "python3 -m aiteam.data_dir_migration --apply"
ROLLBACK_COMMAND = "python3 -m aiteam.data_dir_migration --rollback"

# Reason codes (gate 1-7 of the design table, plus the rollback-only ones).
CONFLICT = "conflict"
NOT_LEGACY = "not_legacy"
BASELINE_UNREADABLE = "baseline_unreadable"
LOCKED = "locked"
PID_MISMATCH = "pid_mismatch"
CODE_MISMATCH = "code_mismatch"
API_STILL_RUNNING = "api_still_running"
DB_IN_USE = "db_in_use"
WAL_NOT_TRUNCATED = "wal_not_truncated"
RENAME_FAILED = "rename_failed"
CHECKSUM_MISMATCH = "checksum_mismatch"
POSTCHECK_FAILED = "postcheck_failed"
NOT_MIGRATED = "not_migrated"

GATES: tuple[tuple[int, str, str], ...] = (
    (1, "two_sides", "先看两处"),
    (2, "lock", "迁移锁"),
    (3, "stop_api", "停 API"),
    (4, "db_holders", "无人开库"),
    (5, "wal", "WAL 归零"),
    (6, "move", "搬"),
    (7, "postcheck", "起 API 并对账"),
)
_GATE_NAMES = {number: name for number, name, _ in GATES}
_GATE_LABELS = {number: label for number, _, label in GATES}

# Health fields the API reports about the data directory it resolved
# (aiteam.api.routes.health). task_count is COUNT(*) over the default database's
# tasks, added only when asked for, so the liveness probe never touches the database.
HEALTH_DATA_DIR_KEY = "data_dir"
HEALTH_MODE_KEY = "data_dir_mode"
HEALTH_TASK_COUNT_KEY = "task_count"
HEALTH_COUNT_PATH = "/api/health?task_count=1"
# The port aiteam up listens on without --api-port (aiteam.cli.commands.up_cmd).
UP_DEFAULT_PORT = 8000
# The longest user words kept with a consent record (a 1KB local record line).
MAX_QUOTE_CHARS = 120


# ============================================================
# Result types
# ============================================================


@dataclass
class GateResult:
    gate: int
    name: str
    status: str  # "pass" | "fail" | "skip"
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApiStop:
    """Outcome of gate 3; ``pids`` are the verified API processes, ``stopped`` those terminated."""

    ok: bool
    port: int
    reason: str | None = None
    was_running: bool = False
    pids: list[int] = field(default_factory=list)
    stopped: list[int] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class HolderProbe:
    """Outcome of the open-file probe; ``ok`` False means the probe itself could not run."""

    ok: bool
    method: str
    holders: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class Postcheck:
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class MigrationResult:
    action: str  # "dry_run" | "apply" | "rollback"
    ok: bool  # dry_run: gates 1-5 pass; apply: moved and reconciled; rollback: moved back
    reason: str | None
    legacy_dir: str
    target_dir: str
    gates: list[GateResult]
    method: str | None = None  # "rename" | "copy"
    legacy_marker: str | None = None  # "symlink" | "stub"
    backup_dir: str | None = None
    rolled_back: bool = False
    api: dict[str, Any] = field(default_factory=dict)
    migrated_at: str | None = None
    command: str = MANUAL_COMMAND

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def failed_gate(self) -> GateResult | None:
        return next((gate for gate in self.gates if gate.status == "fail"), None)

    def outcome(self) -> dict[str, Any]:
        """What an installer keeps of the run (installs.json ``migration``; the notice shows ``reason``)."""
        failed = self.failed_gate
        return {
            "action": self.action, "ok": self.ok and failed is None, "reason": self.reason,
            "gate": failed.gate if failed else None,
            "detail": json.loads(json.dumps(failed.detail, default=str)) if failed else {},
            "method": self.method, "legacy_marker": self.legacy_marker, "rolled_back": self.rolled_back,
            "migrated_at": self.migrated_at,
        }


# ============================================================
# Small helpers
# ============================================================


def _now_iso(epoch: float) -> str:
    return from_timestamp(epoch).isoformat(timespec="seconds").replace("+00:00", "Z")


def _local_stamp(epoch: float) -> str:
    return from_timestamp(epoch).astimezone().strftime("%Y%m%d-%H%M%S")


def _same_path(a: str | os.PathLike[str] | None, b: str | os.PathLike[str] | None) -> bool:
    if a is None or b is None:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normpath(os.path.abspath(os.path.expanduser(os.fspath(a)))) == \
            os.path.normpath(os.path.abspath(os.path.expanduser(os.fspath(b))))


def is_sqlite_file(path: Path) -> bool:
    """True when *path* starts with the 16-byte SQLite header (size is not looked at)."""
    try:
        with open(path, "rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def db_paths(data_dir: Path) -> list[Path]:
    db = data_dir / DB_NAME
    return [db, *(db.with_name(db.name + suffix) for suffix in DB_SIDECARS)]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_counts(con: sqlite3.Connection) -> tuple[int, int]:
    user_version = int(con.execute("PRAGMA user_version").fetchone()[0])
    task_count = int(con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    return user_version, task_count


def db_facts(db: Path) -> dict[str, Any]:
    """Size, mtime, ``PRAGMA user_version`` and task count, read without leaving files behind.

    A read-only connection to a WAL database creates ``-wal`` and ``-shm`` when they
    are missing and leaves them after closing. When both exist some connection is
    live and the read is a normal read-only one; otherwise nobody has the database
    open and it is read as immutable, which creates nothing. ``error`` is set when
    the file cannot be read as an OS database.
    """
    facts: dict[str, Any] = {"path": str(db)}
    try:
        stat = db.stat()
    except OSError as exc:
        facts["error"] = f"{type(exc).__name__}: {exc}"
        return facts
    facts.update(size=stat.st_size, mtime=from_timestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"))
    live = all(db.with_name(db.name + suffix).exists() for suffix in DB_SIDECARS)
    uri = Path(os.path.abspath(db)).as_uri() + ("?mode=ro" if live else "?mode=ro&immutable=1")
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as con:
            facts["user_version"], facts["task_count"] = _read_counts(con)
    except sqlite3.Error as exc:
        facts["error"] = f"{type(exc).__name__}: {exc}"
    return facts


def read_api_port(data_dir: Path) -> int | None:
    try:
        port = int((data_dir / PORT_FILE_NAME).read_text().strip())
    except (OSError, ValueError):
        return None
    return port if 0 < port < 65536 else None


# ============================================================
# Migration lock
# ============================================================


def _lock_info(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return {"pid": None, "host": None, "started_at": time.time(), "refreshed_at": time.time()}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    started = data.get("started_at")
    pid = data.get("pid")
    host = data.get("host")
    return {
        "pid": pid if type(pid) is int and pid > 0 else None,
        "host": host if isinstance(host, str) else None,
        "started_at": float(started) if isinstance(started, (int, float)) else stat.st_mtime,
        "refreshed_at": stat.st_mtime,
    }


def _lock_is_live(info: dict[str, Any], now: float) -> bool:
    # A long copy refreshes the lock (the file's mtime) so it does not expire mid-move.
    if now - max(info["started_at"], info.get("refreshed_at", 0.0)) >= LOCK_TTL_SECONDS:
        return False
    if info["pid"] and info["host"] == socket.gethostname() and os.name != "nt":
        return pid_alive(info["pid"])
    return True


def read_lock(data_dir: Path) -> dict[str, Any] | None:
    """The parsed ``MIGRATING`` lock in *data_dir*, or None when there is none."""
    return _lock_info(Path(data_dir) / LOCK_NAME)


def migration_lock_active(data_dir: str | os.PathLike[str], *, now: float | None = None) -> bool:
    """True while a migration holds *data_dir*: starters of the API must refuse then.

    Standard library only. The holder itself is not blocked (it restarts the API
    as part of the migration); a lock older than ``LOCK_TTL_SECONDS`` or left by a
    process that is gone on this host is stale.
    """
    info = _lock_info(Path(data_dir) / LOCK_NAME)
    if info is None:
        return False
    if info["pid"] == os.getpid() and info["host"] == socket.gethostname():
        return False
    return _lock_is_live(info, time.time() if now is None else now)


def _acquire_lock(data_dir: Path, now: float) -> tuple[bool, dict[str, Any]]:
    """Create the lock exclusively; a stale lock is replaced. Returns (acquired, detail)."""
    path = data_dir / LOCK_NAME
    payload = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started_at": now,
                          "started_at_local": from_timestamp(now).astimezone().isoformat(timespec="seconds")})
    detail: dict[str, Any] = {"lock": str(path)}
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            info = _lock_info(path)
            if info is not None and _lock_is_live(info, now):
                return False, {**detail, "holder": info}
            detail["replaced_stale"] = info
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        except OSError as exc:
            return False, {**detail, "error": f"{type(exc).__name__}: {exc}"}
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return True, detail
    return False, {**detail, "holder": _lock_info(path)}


def _release_lock(data_dir: Path | None) -> None:
    """Remove the lock in *data_dir* only when this process holds it."""
    if data_dir is None:
        return
    path = data_dir / LOCK_NAME
    info = _lock_info(path)
    if info is not None and info["pid"] == os.getpid() and info["host"] == socket.gethostname():
        with contextlib.suppress(OSError):
            path.unlink()


# ============================================================
# Processes (gate 3 and gate 4): standard library, ps / lsof / procfs
# ============================================================


def _ps(pid: int, column: str) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", f"{column}=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5, check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = result.stdout.strip()
    return out if result.returncode == 0 and out else None


def _process_state(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            return stat.rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return None
    return _ps(pid, "stat")


def pid_alive(pid: int) -> bool:
    """True for a live process; a zombie (exited, not reaped) counts as gone."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=5, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return True
        return str(pid) in out.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    state = _process_state(pid)
    return not (state or "").startswith(("Z", "X"))


def process_command(pid: int) -> list[str] | None:
    """The command line of *pid* as its argv, or None when it cannot be read.

    psutil (a dependency of the package, the source autostart reads too) and procfs
    give argv exactly. Only without either does ps answer, joining argv with spaces:
    an interpreter path holding a space then splits, the command no longer reads as
    our API, and gate 3 refuses (pid_mismatch) rather than guess.
    """
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return psutil.Process(pid).cmdline() or None
        except psutil.NoSuchProcess:
            return None
        except (psutil.Error, OSError):
            pass  # not readable through psutil (another user's process): ask the system below
    if os.name == "nt":
        return None
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        words = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        return words or None
    out = _ps(pid, "args")
    return out.split() if out else None


def _option(command: list[str], name: str) -> str | None:
    """The value of ``name`` (``name VALUE`` or ``name=VALUE``) in *command*; None without it."""
    for position, word in enumerate(command):
        if word == name:
            return command[position + 1] if position + 1 < len(command) else ""
        if word.startswith(name + "="):
            return word[len(name) + 1:]
    return None


def command_matches_api(command: list[str], port: int) -> bool:
    """Whether *command* is our API process on *port*.

    Which commands are our API is ``aiteam.mcp._autostart._is_api_command``'s
    answer, the one definition autostart and os_restart_api go by (uvicorn as a
    module or a console script, and ``aiteam up``). On top of it the port must
    agree: uvicorn's ``--port``, when present, names *port* (a process serving a
    handed-over socket with ``--fd`` carries none: it was found listening on the
    port or named by the PID file); ``aiteam up`` serves ``--api-port``, by
    default ``UP_DEFAULT_PORT``.
    """
    from aiteam.mcp import _autostart

    if not _autostart._is_api_command(command):
        return False
    if _autostart._API_APP in command:  # uvicorn, as a module or a console script
        named = _option(command, "--port")
        return named is None or named == str(port)
    named = _option(command, "--api-port")  # aiteam up runs uvicorn in-process
    return (named if named is not None else str(UP_DEFAULT_PORT)) == str(port)


def _lsof() -> str | None:
    found = shutil.which("lsof")
    if found:
        return found
    for candidate in ("/usr/sbin/lsof", "/usr/bin/lsof", "/sbin/lsof"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _proc_listening_inodes(port: int) -> set[str]:
    inodes: set[str] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(fields[9])
    return inodes


def _proc_fd_owners(match: Callable[[str], bool]) -> set[int] | None:
    """PIDs with an open descriptor whose link target satisfies *match*; None without procfs."""
    if not os.path.isdir("/proc/self/fd"):
        return None
    owners: set[int] = set()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        fd_dir = f"/proc/{entry.name}/fd"
        try:
            descriptors = os.listdir(fd_dir)
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(f"{fd_dir}/{descriptor}")
            except OSError:
                continue
            if match(target):
                owners.add(int(entry.name))
                break
    return owners


def listening_pids(port: int) -> set[int] | None:
    """PIDs listening on TCP *port*; None when no probe is available or it failed."""
    lsof = _lsof()
    if lsof:
        try:
            result = subprocess.run([lsof, "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
                                    capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode not in (0, 1):
            return None
        return {int(line) for line in result.stdout.split() if line.isdigit()}
    if sys.platform.startswith("linux"):
        sockets = {f"socket:[{inode}]" for inode in _proc_listening_inodes(port)}
        return _proc_fd_owners(sockets.__contains__) if sockets else set()
    return None


def find_db_holders(paths: Iterable[Path]) -> HolderProbe:
    """Processes holding any of *paths* open (gate 4).

    lsof on macOS and Linux; Linux without lsof scans ``/proc/*/fd``. Windows has
    no equivalent in the standard library, so the probe reports itself unusable
    and the gate does not pass there.
    """
    existing = [os.path.realpath(path) for path in paths if os.path.exists(path)]
    if not existing:
        return HolderProbe(True, "none")
    if os.name == "nt":
        return HolderProbe(False, "unavailable", error="no open-file probe on this platform")
    lsof = _lsof()
    if lsof:
        try:
            result = subprocess.run([lsof, "-w", "-F", "pc", "--", *existing], capture_output=True, text=True,
                                    timeout=60, check=False, env={**os.environ, "LC_ALL": "C"})
        except (OSError, subprocess.SubprocessError) as exc:
            return HolderProbe(False, "lsof", error=f"{type(exc).__name__}: {exc}")
        noise = [line for line in result.stderr.splitlines()
                 if line.strip() and "No such file or directory" not in line]
        if result.returncode not in (0, 1) or (result.returncode == 1 and noise):
            return HolderProbe(False, "lsof", error=(result.stderr.strip() or f"exit {result.returncode}")[:500])
        names: dict[int, str] = {}
        current = None
        for line in result.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current = int(line[1:])
                names.setdefault(current, "")
            elif line.startswith("c") and current is not None:
                names[current] = line[1:]
        holders = [{"pid": pid, "command": " ".join(process_command(pid) or [name])}
                   for pid, name in sorted(names.items())]
        return HolderProbe(True, "lsof", holders)
    targets = set(existing)
    owners = _proc_fd_owners(targets.__contains__)
    if owners is None:
        return HolderProbe(False, "unavailable", error="neither lsof nor /proc is available")
    return HolderProbe(True, "proc", [{"pid": pid, "command": " ".join(process_command(pid) or [])}
                                      for pid in sorted(owners)])


def _read_pid_file(path: Path) -> int | None:
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _default_api_port() -> int:
    from aiteam.mcp import _autostart

    return _autostart._DEFAULT_PORT


def _default_pid_file() -> Path:
    from aiteam.mcp import _autostart

    return Path(_autostart._PID_FILE)


def stop_api_processes(
    data_dir: Path,
    dry_run: bool = False,
    *,
    port: int | None = None,
    pid_file: Path | None = None,
    polls: int = 150,
    interval: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
) -> ApiStop:
    """Gate 3: find our API by the port file and the PID file, verify, SIGTERM, wait.

    Every process listening on the port must be our API by command line, or
    nothing is signalled (pid_mismatch); a port that is open with no
    identifiable listener counts the same. The PID file adds a process only when
    its command line is ours; a recorded PID now used by something else is a
    stale record and is ignored. After SIGTERM the gate waits (counted polls)
    until every process is gone and the port is closed (api_still_running).
    """
    if port is None:
        port = read_api_port(data_dir) or _default_api_port()
    detail: dict[str, Any] = {"port": port}
    listening = port_open(port)
    if os.name == "nt":
        if listening:
            return ApiStop(False, port, PID_MISMATCH, was_running=True,
                           detail={**detail, "error": "command line not verifiable on this platform"})
        return ApiStop(True, port, detail=detail)
    listeners = listening_pids(port) if listening else set()
    if listening and not listeners:
        return ApiStop(False, port, PID_MISMATCH, was_running=True,
                       detail={**detail, "error": "port open but its listener is unidentified"})
    targets: list[int] = []
    for pid in sorted(listeners or ()):
        command = process_command(pid)
        if command is None or not command_matches_api(command, port):
            return ApiStop(False, port, PID_MISMATCH, was_running=True,
                           detail={**detail, "pid": pid, "command": " ".join(command or ["?"])})
        targets.append(pid)
    recorded = _read_pid_file(Path(pid_file) if pid_file is not None else _default_pid_file())
    if recorded and recorded not in targets and pid_alive(recorded):
        command = process_command(recorded)
        if command is not None and command_matches_api(command, port):
            targets.append(recorded)
        else:
            detail["stale_pid_file"] = recorded
    if not targets:
        return ApiStop(True, port, detail=detail)
    detail["pids"] = targets
    if dry_run:
        return ApiStop(True, port, was_running=True, pids=targets, detail=detail)
    for pid in targets:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    for _ in range(polls):
        if not any(pid_alive(pid) for pid in targets) and not port_open(port):
            return ApiStop(True, port, was_running=True, pids=targets, stopped=targets, detail=detail)
        sleep(interval)
    alive = [pid for pid in targets if pid_alive(pid)]
    if not alive and not port_open(port):
        return ApiStop(True, port, was_running=True, pids=targets, stopped=targets, detail=detail)
    return ApiStop(False, port, API_STILL_RUNNING, was_running=True, pids=targets,
                   detail={**detail, "alive": alive, "port_open": port_open(port)})


# ============================================================
# API start and reconciliation (gate 7)
# ============================================================


def start_api_default(port: int) -> dict[str, Any]:
    """Start the API on *port* the same way os_restart_api does."""
    from aiteam.mcp import _autostart
    from aiteam.mcp.tools import infra

    return infra._restart_spawn_on_port(_autostart, port)


def _http_json(port: int, path: str, timeout: float) -> Any:
    # Loopback only: never route through a proxy configured in the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def postcheck_api(
    port: int,
    expected_dir: Path,
    expected_mode: str,
    baseline: dict[str, Any],
    *,
    polls: int = 60,
    interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 10.0,
) -> Postcheck:
    """Gate 7 by HTTP: health names *expected_dir* in *expected_mode*; counts match *baseline*.

    The task count comes over HTTP (proves the API serves this database); the
    ``PRAGMA user_version`` is read from the file at *expected_dir*.
    """
    health = None
    for _ in range(polls):
        health = _http_json(port, HEALTH_COUNT_PATH, timeout)
        if isinstance(health, dict):
            break
        sleep(interval)
    if not isinstance(health, dict):
        return Postcheck(False, {"error": "health_timeout", "port": port})
    reported_dir, reported_mode = health.get(HEALTH_DATA_DIR_KEY), health.get(HEALTH_MODE_KEY)
    detail: dict[str, Any] = {"port": port, "data_dir": reported_dir, "mode": reported_mode}
    if not isinstance(reported_dir, str) or not isinstance(reported_mode, str):
        return Postcheck(False, {**detail, "error": "health_missing_data_dir"})
    if reported_mode != expected_mode or not _same_path(reported_dir, expected_dir):
        return Postcheck(False, {**detail, "error": "data_dir_mismatch",
                                 "expected_dir": str(expected_dir), "expected_mode": expected_mode})
    task_count = health.get(HEALTH_TASK_COUNT_KEY)
    detail["task_count"] = task_count
    if type(task_count) is not int or task_count != baseline.get("task_count"):
        return Postcheck(False, {**detail, "error": "task_count_mismatch", "expected": baseline.get("task_count")})
    facts = db_facts(expected_dir / DB_NAME)
    detail["user_version"] = facts.get("user_version")
    if facts.get("user_version") != baseline.get("user_version"):
        return Postcheck(False, {**detail, "error": "user_version_mismatch",
                                 "expected": baseline.get("user_version")})
    return Postcheck(True, detail)


# ============================================================
# The runs
# ============================================================


def _default_dirs() -> tuple[Path, Path]:
    from aiteam import paths

    def value(name: str) -> Path:
        item = getattr(paths, name)
        return Path(item() if callable(item) else item)

    return value("LEGACY_DATA_DIR"), value("DEFAULT_DATA_DIR")


def _default_resolve() -> Any:
    from aiteam.paths import resolve_data_dir

    return resolve_data_dir()


def _default_symlink(target: Path, link: Path) -> None:
    if os.name == "nt":
        raise OSError(errno.EPERM, "the legacy location keeps a stub directory on Windows")
    os.symlink(os.fspath(target), os.fspath(link), target_is_directory=True)


def _default_copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, symlinks=True)


# A copy across filesystems touches the lock this often, so it never expires mid-move.
LOCK_REFRESH_SECONDS = 60.0


@contextlib.contextmanager
def _lock_kept_fresh(lock_dirs: list[Path], every: float | None = None) -> Iterator[None]:
    """Touch the lock in *lock_dirs* every *every* seconds until the block ends."""
    stop = threading.Event()
    interval = LOCK_REFRESH_SECONDS if every is None else every

    def beat() -> None:
        while not stop.wait(interval):
            for data_dir in list(lock_dirs):
                with contextlib.suppress(OSError):
                    os.utime(data_dir / LOCK_NAME)

    thread = threading.Thread(target=beat, name="aiteam-migration-lock", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


# The package this module belongs to: what the restarted API has to run (gate 3).
THIS_PACKAGE = Path(__file__).resolve().parent


def default_api_package() -> Path | None:
    """The aiteam package the API that :func:`start_api_default` starts would import.

    Same interpreter and environment as that start (the session's variables
    scrubbed, nothing of this process's import path added). An update that did
    not install the package (``--offline``), or a checkout run by hand, can leave
    another copy of aiteam installed: the API would then come back on other code,
    and gate 7 would fail on it. None when it cannot be told.
    """
    from aiteam.session_env import scrub_session_env

    probe = "import aiteam, os; print(os.path.dirname(os.path.realpath(aiteam.__file__)))"
    try:
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
                                check=True, env=scrub_session_env())
        return Path(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


@dataclass
class _Deps:
    legacy: Path
    target: Path
    resolve: Callable[[], Any]
    stop_api: Callable[..., ApiStop]
    start_api: Callable[[int], dict[str, Any]]
    db_holders: Callable[[list[Path]], HolderProbe]
    postcheck: Callable[[int, Path, str, dict[str, Any]], Postcheck]
    device_of: Callable[[Path], int]
    copy_tree: Callable[[Path, Path], None]
    make_symlink: Callable[[Path, Path], None]
    now: Callable[[], float]
    checkpoint_timeout: float
    api_package: Callable[[], Path | None]


def _build_deps(legacy_dir, target_dir, resolve, stop_api, start_api, db_holders, postcheck,
                device_of, copy_tree, make_symlink, now, checkpoint_timeout, api_package=None) -> _Deps:
    if legacy_dir is None or target_dir is None:
        default_legacy, default_target = _default_dirs()
        legacy_dir = default_legacy if legacy_dir is None else legacy_dir
        target_dir = default_target if target_dir is None else target_dir
    return _Deps(
        legacy=Path(os.path.abspath(os.path.expanduser(os.fspath(legacy_dir)))),
        target=Path(os.path.abspath(os.path.expanduser(os.fspath(target_dir)))),
        resolve=resolve or _default_resolve,
        stop_api=stop_api or stop_api_processes,
        start_api=start_api or start_api_default,
        db_holders=db_holders or find_db_holders,
        postcheck=postcheck or postcheck_api,
        device_of=device_of or (lambda path: os.stat(path).st_dev),
        copy_tree=copy_tree or _default_copy_tree,
        make_symlink=make_symlink or _default_symlink,
        now=now or time.time,
        checkpoint_timeout=checkpoint_timeout,
        # Which code the restarted API runs matters for the default start only; an
        # injected start runs whatever its caller decided.
        api_package=api_package or (default_api_package if start_api is None else (lambda: THIS_PACKAGE)),
    )


class _Run:
    """Shared machinery: gate bookkeeping, gates 2-5, the API restore and the lock release."""

    action = "apply"

    def __init__(self, deps: _Deps, *, apply: bool, command: str) -> None:
        self.deps = deps
        self.apply = apply
        self.gates: list[GateResult] = []
        self.result = MigrationResult(action=self.action if apply else "dry_run", ok=False, reason=None,
                                      legacy_dir=str(deps.legacy), target_dir=str(deps.target),
                                      gates=self.gates, command=command)
        self.lock_dirs: list[Path] = []  # where our lock currently sits
        self.port: int | None = None
        self.api_was_running = False
        self.api_stopped = False
        self.api_pids: list[int] = []
        self.baseline: dict[str, Any] = {}

    # -- bookkeeping --------------------------------------------------------

    def _pass(self, gate: int, **detail: Any) -> bool:
        self.gates.append(GateResult(gate, _GATE_NAMES[gate], "pass", None, detail))
        return True

    def _fail(self, gate: int, reason: str, **detail: Any) -> bool:
        self.gates.append(GateResult(gate, _GATE_NAMES[gate], "fail", reason, detail))
        if self.result.reason is None:
            self.result.reason = reason
        return False

    def _skip_rest(self, why: str) -> None:
        done = {gate.gate for gate in self.gates}
        for number, name, _ in GATES:
            if number not in done:
                self.gates.append(GateResult(number, name, "skip", None, {"why": why}))

    # -- gates shared by migrate and rollback --------------------------------

    def _gate_lock(self, data_dir: Path) -> bool:
        now = self.deps.now()
        if not self.apply:
            info = _lock_info(data_dir / LOCK_NAME)
            if info is not None and _lock_is_live(info, now):
                return self._fail(2, LOCKED, holder=info)
            return self._pass(2, lock="none" if info is None else "stale", would_write=str(data_dir / LOCK_NAME))
        acquired, detail = _acquire_lock(data_dir, now)
        if not acquired:
            return self._fail(2, LOCKED, **detail)
        self.lock_dirs = [data_dir]
        return self._pass(2, **detail)

    def _gate_stop_api(self, data_dir: Path) -> bool:
        package = self.deps.api_package()
        if package is None or not _same_path(package, THIS_PACKAGE):
            return self._fail(3, CODE_MISMATCH, this_code=str(THIS_PACKAGE),
                              api_would_run=None if package is None else str(package),
                              hint="install this checkout's package first (python3 -m pip install -e .), "
                                   "then run the update again")
        try:
            stop = self.deps.stop_api(data_dir, dry_run=not self.apply)
        except Exception as exc:  # noqa: BLE001 - any failure to verify means "do not proceed"
            return self._fail(3, PID_MISMATCH, error=f"{type(exc).__name__}: {exc}")
        self.port = stop.port
        self.api_was_running = stop.was_running
        self.api_stopped = bool(stop.stopped)
        self.api_pids = list(stop.pids)
        self.result.api["stop"] = asdict(stop)
        if not stop.ok:
            return self._fail(3, stop.reason or API_STILL_RUNNING, **stop.detail)
        detail = dict(stop.detail)
        if stop.pids and not self.apply:
            detail["would_stop"] = stop.pids
        return self._pass(3, running=stop.was_running, **detail)

    def _gate_holders(self, data_dir: Path) -> bool:
        try:
            probe = self.deps.db_holders(db_paths(data_dir))
        except Exception as exc:  # noqa: BLE001
            return self._fail(4, DB_IN_USE, probe="failed", error=f"{type(exc).__name__}: {exc}")
        holders = probe.holders
        if not self.apply:
            # Applying stops these first: they are our API, verified by gate 3.
            holders = [holder for holder in holders if holder.get("pid") not in self.api_pids]
        if not probe.ok:
            return self._fail(4, DB_IN_USE, probe=probe.method, error=probe.error,
                              command=self.result.command)
        if holders:
            return self._fail(4, DB_IN_USE, probe=probe.method, holders=holders)
        return self._pass(4, probe=probe.method)

    def _gate_wal(self, data_dir: Path) -> bool:
        db = data_dir / DB_NAME
        wal = db.with_name(db.name + "-wal")
        if not self.apply:
            size = wal.stat().st_size if wal.exists() else 0
            return self._pass(5, wal_bytes=size, checkpoint="not run in a dry run")
        try:
            with contextlib.closing(sqlite3.connect(os.fspath(db), timeout=self.deps.checkpoint_timeout,
                                                    isolation_level=None)) as con:
                busy, log_frames, checkpointed = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                user_version, task_count = _read_counts(con)
        except sqlite3.Error as exc:
            return self._fail(5, WAL_NOT_TRUNCATED, error=f"{type(exc).__name__}: {exc}")
        size = wal.stat().st_size if wal.exists() else 0
        detail = {"busy": busy, "log_frames": log_frames, "checkpointed": checkpointed, "wal_bytes": size}
        if busy != 0 or size != 0:
            return self._fail(5, WAL_NOT_TRUNCATED, **detail)
        # The database is quiet now: this is the baseline gate 7 reconciles against.
        self.baseline = {"user_version": user_version, "task_count": task_count}
        return self._pass(5, **detail, baseline=self.baseline)

    # -- recovery -------------------------------------------------------------

    def _start_api(self) -> dict[str, Any]:
        try:
            outcome = self.deps.start_api(self.port)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        return outcome if isinstance(outcome, dict) else {"success": bool(outcome)}

    def _restore_api(self) -> None:
        """After a failed gate: start the API again when this run stopped it."""
        if self.apply and self.api_stopped and self.port is not None:
            self.result.api["restart"] = self._start_api()

    def _release(self) -> None:
        for data_dir in self.lock_dirs:
            _release_lock(data_dir)
        self.lock_dirs = []

    # -- moving one directory to a new path ----------------------------------

    def _relocate(self, gate: int, src: Path, dst: Path) -> dict[str, Any] | None:
        """Move *src* to *dst*: rename on one filesystem, else copy to ``.partial``, sha256, rename.

        Returns ``{"method": "rename"}`` or ``{"method": "copy", "sha256": ...}``; after
        a copy *src* is still in place and the caller renames or keeps it. Returns
        None after recording a failure: then *src* is untouched and no partial copy
        is left.

        A copy takes a while. The lock is kept fresh meanwhile, and once the copy is
        verified the database holders are probed again: a process that opened the
        database during the copy would go on writing into *src* after it is renamed
        aside, and the copy would miss it, so then the copy is dropped (db_in_use).
        """
        if os.path.lexists(dst):
            self._fail(gate, RENAME_FAILED, error="destination exists", destination=str(dst))
            return None
        try:
            same = self.deps.device_of(src) == self.deps.device_of(dst.parent)
        except OSError as exc:
            self._fail(gate, RENAME_FAILED, error=f"{type(exc).__name__}: {exc}")
            return None
        if same:
            try:
                os.rename(src, dst)
                return {"method": "rename"}
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    self._fail(gate, RENAME_FAILED, error=f"{type(exc).__name__}: {exc}")
                    return None
        partial = dst.with_name(dst.name + ".partial")
        try:
            if os.path.lexists(partial):
                # Only this module creates it, and we hold the lock: a dead run's leftover.
                shutil.rmtree(partial)
            with _lock_kept_fresh(self.lock_dirs):
                self.deps.copy_tree(src, partial)
        except OSError as exc:
            shutil.rmtree(partial, ignore_errors=True)
            self._fail(gate, RENAME_FAILED, error=f"{type(exc).__name__}: {exc}", method="copy")
            return None
        try:
            source_sum, copy_sum = _sha256(src / DB_NAME), _sha256(partial / DB_NAME)
        except OSError as exc:
            shutil.rmtree(partial, ignore_errors=True)
            self._fail(gate, CHECKSUM_MISMATCH, error=f"{type(exc).__name__}: {exc}")
            return None
        if source_sum != copy_sum:
            shutil.rmtree(partial, ignore_errors=True)
            self._fail(gate, CHECKSUM_MISMATCH, source_sha256=source_sum, copy_sha256=copy_sum)
            return None
        try:
            probe = self.deps.db_holders(db_paths(src))
        except Exception as exc:  # noqa: BLE001 - unknown holders mean "do not proceed"
            probe = HolderProbe(False, "failed", error=f"{type(exc).__name__}: {exc}")
        if not probe.ok or probe.holders:
            shutil.rmtree(partial, ignore_errors=True)
            self._fail(gate, DB_IN_USE, probe=probe.method, error=probe.error, holders=probe.holders,
                       when="after the copy")
            return None
        try:
            os.rename(partial, dst)
        except OSError as exc:
            shutil.rmtree(partial, ignore_errors=True)
            self._fail(gate, RENAME_FAILED, error=f"{type(exc).__name__}: {exc}", method="copy")
            return None
        return {"method": "copy", "sha256": copy_sum}


class _Migration(_Run):
    action = "apply"

    def __init__(self, deps: _Deps, *, apply: bool, explicit_source: bool) -> None:
        command = MANUAL_COMMAND + (f" --from {shlex.quote(str(deps.legacy))}" if explicit_source else "")
        super().__init__(deps, apply=apply, command=command)
        self.explicit_source = explicit_source
        self.method: str | None = None
        self.backup: Path | None = None
        self.marker: str | None = None
        self.symlink_error: str | None = None
        self.recreated: dict[str, list[str]] | None = None

    def run(self) -> MigrationResult:
        legacy = self.deps.legacy
        try:
            if not (self._gate_two_sides() and self._gate_lock(legacy)):
                self._skip_rest("an earlier gate did not pass")
                return self.result
            passed = self._gate_stop_api(legacy) and self._gate_holders(legacy) and self._gate_wal(legacy)
            if not self.apply:
                self.result.ok = passed
                self._skip_rest("dry run" if passed else "an earlier gate did not pass")
                return self.result
            if not passed or not self._gate_move():
                self._restore_api()
                self._skip_rest("an earlier gate did not pass")
                return self.result
            self._gate_postcheck()
            return self.result
        finally:
            self._release()

    # -- gate 1 ----------------------------------------------------------------

    def _gate_two_sides(self) -> bool:
        legacy, target = self.deps.legacy, self.deps.target
        try:
            resolution = self.deps.resolve()
        except Exception as exc:  # noqa: BLE001
            return self._fail(1, NOT_LEGACY, error=f"resolver failed: {type(exc).__name__}: {exc}")
        mode, resolved = getattr(resolution, "mode", None), getattr(resolution, "path", None)
        detail: dict[str, Any] = {"mode": mode, "resolved": None if resolved is None else str(resolved)}
        if mode == "conflict":
            return self._fail(1, CONFLICT, **detail, legacy=db_facts(legacy / DB_NAME),
                              canonical=db_facts(target / DB_NAME))
        if self.explicit_source:
            accepted = mode == "missing" or (mode == "legacy" and _same_path(resolved, legacy))
        else:
            accepted = mode == "legacy" and _same_path(resolved, legacy)
        if not accepted:
            return self._fail(1, NOT_LEGACY, **detail, expected=str(legacy))
        db = legacy / DB_NAME
        if legacy.is_symlink():
            return self._fail(1, NOT_LEGACY, **detail, error="the legacy directory is a symbolic link")
        if not legacy.is_dir() or not is_sqlite_file(db):
            return self._fail(1, NOT_LEGACY, **detail, error="no SQLite database in the legacy directory")
        if os.path.lexists(target):
            return self._fail(1, NOT_LEGACY, **detail, error="the target already exists", target=str(target))
        facts = db_facts(db)
        if "error" in facts:
            return self._fail(1, BASELINE_UNREADABLE, **detail, legacy=facts)
        self.baseline = {"user_version": facts["user_version"], "task_count": facts["task_count"]}
        return self._pass(1, **detail, legacy=facts)

    # -- gate 6 ----------------------------------------------------------------

    def _gate_move(self) -> bool:
        legacy, target = self.deps.legacy, self.deps.target
        moved = self._relocate(6, legacy, target)
        if moved is None:
            return False
        self.method = moved["method"]
        self.lock_dirs = [target]
        if self.method == "copy":
            backup = legacy.with_name(f"{legacy.name}.migrated-{_local_stamp(self.deps.now())}")
            try:
                os.rename(legacy, backup)
            except OSError as exc:
                # The verified copy goes; the original never moved.
                shutil.rmtree(target, ignore_errors=True)
                self.lock_dirs = [legacy]
                return self._fail(6, RENAME_FAILED, error=f"{type(exc).__name__}: {exc}", method="copy")
            self.backup = backup
            self.lock_dirs = [target, backup]
        try:
            self.marker = self._place_marker()
            self._write_record()
        except OSError as exc:
            undo = self._undo_move()
            return self._fail(6, RENAME_FAILED, error=f"{type(exc).__name__}: {exc}", undo=undo)
        self.result.method, self.result.legacy_marker = self.method, self.marker
        self.result.backup_dir = None if self.backup is None else str(self.backup)
        extra = {"symlink_error": self.symlink_error} if self.symlink_error else {}
        if self.recreated is not None:
            extra["recreated_and_carried"] = self.recreated
        if self.backup is not None:
            extra["carried"] = _carry_over_late_writes(self.backup, target)
        return self._pass(6, **moved, legacy_marker=self.marker, target=str(target), backup=self.result.backup_dir,
                          **extra)

    def _record(self) -> dict[str, Any]:
        return {
            "from": str(self.deps.legacy), "to": str(self.deps.target), "method": self.method,
            "backup": None if self.backup is None else str(self.backup), "legacy_marker": self.marker,
            "migrated_at": _now_iso(self.deps.now()), "pid": os.getpid(), **self.baseline,
        }

    def _place_marker(self) -> str:
        legacy, target = self.deps.legacy, self.deps.target
        for attempt in (1, 2):
            try:
                self.deps.make_symlink(target, legacy)
                return "symlink"
            except FileExistsError as exc:
                if attempt == 2 or not self._absorb_recreated(legacy, target):
                    self.symlink_error = f"{type(exc).__name__}: {exc}"
                    break
            except OSError as exc:
                self.symlink_error = f"{type(exc).__name__}: {exc}"
                break
        legacy.mkdir()
        self.marker = "stub"
        (legacy / MOVED_TO_NAME).write_text(f"{target}\n", encoding="utf-8")
        (legacy / RECORD_NAME).write_text(json.dumps(self._record(), ensure_ascii=False, indent=2), encoding="utf-8")
        return "stub"

    def _absorb_recreated(self, legacy: Path, target: Path) -> bool:
        """Older code still running elsewhere recreated the old directory after the move
        (hooks of earlier versions mkdir it): bring what it wrote over and clear the way.

        Only notes, logs and records are brought over. A database there means a
        service of that older code started on an empty one: that is not merged by
        guessing, so False, and the move is undone or reported as it stands.
        """
        if legacy.is_symlink() or not legacy.is_dir():
            return False
        if any((legacy / name).exists() for name in (DB_NAME, *(DB_NAME + suffix for suffix in DB_SIDECARS))):
            return False
        self.recreated = _carry_over_late_writes(legacy, target)
        shutil.rmtree(legacy)
        return True

    def _write_record(self) -> None:
        (self.deps.target / RECORD_NAME).write_text(json.dumps(self._record(), ensure_ascii=False, indent=2),
                                                    encoding="utf-8")

    def _undo_move(self) -> dict[str, Any]:
        """Put the legacy directory back exactly; returns what was done or what failed."""
        legacy, target = self.deps.legacy, self.deps.target
        steps: dict[str, Any] = {}
        try:
            _remove_marker(legacy, target)
            steps["marker"] = "removed"
            if self.method == "rename":
                os.rename(target, legacy)
                self.lock_dirs = [legacy]
                with contextlib.suppress(FileNotFoundError):
                    (legacy / RECORD_NAME).unlink()
            else:
                # The original is the untouched backup; the copy, which the API may
                # have written to during gate 7, is kept aside rather than deleted.
                os.rename(self.backup, legacy)
                self.lock_dirs = [target, legacy]
                kept = target.with_name(f"{target.name}.rolled-back-{_local_stamp(self.deps.now())}")
                os.rename(target, kept)
                steps["copy_kept_at"] = str(kept)
                self.result.backup_dir = str(kept)
                self.lock_dirs = [kept, legacy]
            steps["data"] = "back in the legacy directory"
        except OSError as exc:
            steps["error"] = f"{type(exc).__name__}: {exc}"
        return steps

    # -- gate 7 ----------------------------------------------------------------

    def _gate_postcheck(self) -> None:
        started = self._start_api()
        self.result.api["start"] = started
        if started.get("success"):
            try:
                check = self.deps.postcheck(self.port, self.deps.target, "canonical", self.baseline)
            except Exception as exc:  # noqa: BLE001
                check = Postcheck(False, {"error": f"{type(exc).__name__}: {exc}"})
        else:
            check = Postcheck(False, {"error": "start_failed", "start": started})
        if check.ok:
            self.result.ok = True
            self.result.migrated_at = _now_iso(self.deps.now())
            self._pass(7, **check.detail)
            return
        self._fail(7, POSTCHECK_FAILED, **check.detail)
        self._rollback_after_postcheck()

    def _rollback_after_postcheck(self) -> None:
        rollback: dict[str, Any] = {}
        try:
            stop = self.deps.stop_api(self.deps.target, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            stop = ApiStop(False, self.port or 0, PID_MISMATCH, detail={"error": f"{type(exc).__name__}: {exc}"})
        rollback["stop"] = asdict(stop)
        if not stop.ok:
            # Moving the directory under a live API would split it; leave it migrated.
            rollback["error"] = "the API started on the new directory could not be stopped; left migrated"
        else:
            rollback.update(self._undo_move())
            self.result.rolled_back = "error" not in rollback
            if self.result.rolled_back:
                self.result.method = self.result.legacy_marker = None
                if self.api_was_running:
                    rollback["restart"] = self._start_api()
        self.result.api["rollback"] = rollback


# Files a copy-mode move never carries over: the verified database (gate 4 kept
# every holder away, so nothing wrote it) and this module's own bookkeeping.
_NOT_CARRIED = frozenset({DB_NAME, *(DB_NAME + suffix for suffix in DB_SIDECARS), LOCK_NAME, RECORD_NAME})
# Append-only line files (local notice records, delivery and debug logs): when
# both sides grew, the lines only the old side has are appended.
_LINE_FILES = (".jsonl", ".log")
_LISTED = 50


def _carry_over_late_writes(stale: Path, live: Path) -> dict[str, list[str]]:
    """After a copy-mode move: bring over what hooks wrote to the old directory during the copy.

    The API is stopped and no process holds the database, but hooks keep running
    in every session: while the copy runs they resolve the old directory and
    append local notice records, delivery records and logs there. Once the copy
    is renamed into place they resolve the new one. So the old side (*stale*,
    renamed aside by now) may hold writes the copy missed, and the new side
    (*live*) may hold writes made since. Per file: missing on the live side ->
    copied; the live side unchanged since the copy -> the rest appended; both
    grew -> for line files the lines only *stale* has are appended, any other
    file keeps the live side (every process writes there from now on).
    """
    done: dict[str, list[str]] = {"copied": [], "appended": [], "kept_live": [], "failed": []}
    for dirpath, _dirs, files in os.walk(stale):
        for name in files:
            source = Path(dirpath) / name
            relative = source.relative_to(stale)
            if relative.as_posix() in _NOT_CARRIED or source.is_symlink():
                continue
            destination = live / relative
            try:
                old = source.read_bytes()
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    done["copied"].append(relative.as_posix())
                    continue
                new = destination.read_bytes()
                if new == old or new.startswith(old):
                    continue
                if old.startswith(new):
                    extra = old[len(new):]
                elif name.endswith(_LINE_FILES):
                    present = set(new.splitlines())
                    extra = b"".join(line for line in old.splitlines(keepends=True)
                                     if line.rstrip(b"\r\n") not in present)
                    if extra and not new.endswith(b"\n"):
                        extra = b"\n" + extra
                else:
                    done["kept_live"].append(relative.as_posix())
                    continue
                if extra:
                    with open(destination, "ab") as handle:
                        handle.write(extra)
                    done["appended"].append(relative.as_posix())
            except OSError:
                done["failed"].append(relative.as_posix())
    return {key: names[:_LISTED] for key, names in done.items() if names}


def _remove_marker(legacy: Path, target: Path) -> str | None:
    """Remove the symlink or stub left at *legacy* for *target*; refuse anything else."""
    if legacy.is_symlink():
        legacy.unlink()
        return "symlink"
    if not legacy.exists():
        return None
    extra = sorted(entry.name for entry in legacy.iterdir() if entry.name not in (MOVED_TO_NAME, RECORD_NAME))
    if extra:
        raise OSError(errno.ENOTEMPTY, f"stub at {legacy} holds other entries: {', '.join(extra[:10])}")
    for name in (MOVED_TO_NAME, RECORD_NAME):
        with contextlib.suppress(FileNotFoundError):
            (legacy / name).unlink()
    legacy.rmdir()
    return "stub"


def marker_kind(legacy: Path, target: Path) -> tuple[str | None, dict[str, Any]]:
    """What a migration left at *legacy* for *target*: ("symlink" | "stub", {}) or (None, {"error": ...}).

    The one reading of the marker: the rollback checks it, and the uninstaller's
    --purge-data removes it along with the data directory.
    """
    if legacy.is_symlink():
        return ("symlink", {}) if _same_path(os.path.realpath(legacy), target) else \
            (None, {"error": "the legacy path links elsewhere", "link": os.readlink(legacy)})
    if legacy.is_dir():
        try:
            moved_to = (legacy / MOVED_TO_NAME).read_text(encoding="utf-8").strip()
        except OSError:
            return None, {"error": "the legacy directory is no stub (no MOVED-TO)"}
        if not _same_path(moved_to, target):
            return None, {"error": "the stub points elsewhere", "moved_to": moved_to}
        extra = sorted(entry.name for entry in legacy.iterdir() if entry.name not in (MOVED_TO_NAME, RECORD_NAME))
        if extra:
            return None, {"error": "the stub holds other entries", "entries": extra[:20]}
        return "stub", {}
    return None, {"error": "nothing at the legacy path"}


class _Rollback(_Run):
    action = "rollback"

    def __init__(self, deps: _Deps) -> None:
        super().__init__(deps, apply=True, command=ROLLBACK_COMMAND)
        self.marker: str | None = None

    def run(self) -> MigrationResult:
        target = self.deps.target
        try:
            if not (self._gate_state() and self._gate_lock(target)):
                self._skip_rest("an earlier gate did not pass")
                return self.result
            if not (self._gate_stop_api(target) and self._gate_holders(target) and self._gate_wal(target)
                    and self._gate_move_back()):
                self._restore_api()
                self._skip_rest("an earlier gate did not pass")
                return self.result
            self.result.ok = True
            started = self._start_api()
            self.result.api["start"] = started
            if not started.get("success"):
                self._fail(7, POSTCHECK_FAILED, error="start_failed", start=started)
                return self.result
            try:
                check = self.deps.postcheck(self.port, self.deps.legacy, "legacy", self.baseline)
            except Exception as exc:  # noqa: BLE001
                check = Postcheck(False, {"error": f"{type(exc).__name__}: {exc}"})
            if check.ok:
                self._pass(7, **check.detail)
            else:
                self._fail(7, POSTCHECK_FAILED, **check.detail)
            return self.result
        finally:
            self._release()

    def _gate_state(self) -> bool:
        legacy, target = self.deps.legacy, self.deps.target
        try:
            resolution = self.deps.resolve()
        except Exception as exc:  # noqa: BLE001
            return self._fail(1, NOT_MIGRATED, error=f"resolver failed: {type(exc).__name__}: {exc}")
        mode, resolved = getattr(resolution, "mode", None), getattr(resolution, "path", None)
        detail: dict[str, Any] = {"mode": mode, "resolved": None if resolved is None else str(resolved)}
        if mode != "canonical" or not _same_path(resolved, target):
            return self._fail(1, NOT_MIGRATED, **detail, expected=str(target))
        if target.is_symlink() or not is_sqlite_file(target / DB_NAME):
            return self._fail(1, NOT_MIGRATED, **detail, error="no SQLite database in the target directory")
        marker, problem = marker_kind(legacy, target)
        if marker is None:
            return self._fail(1, NOT_MIGRATED, **detail, **problem)
        self.marker = marker
        try:
            record = json.loads((target / RECORD_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        return self._pass(1, **detail, legacy_marker=marker, record=record)

    def _gate_move_back(self) -> bool:
        legacy, target = self.deps.legacy, self.deps.target
        try:
            _remove_marker(legacy, target)
        except OSError as exc:
            return self._fail(6, RENAME_FAILED, error=f"{type(exc).__name__}: {exc}")
        moved = self._relocate(6, target, legacy)
        if moved is None:
            self._put_marker_back()
            return False
        self.lock_dirs = [legacy]
        if moved["method"] == "copy":
            kept = target.with_name(f"{target.name}.rolled-back-{_local_stamp(self.deps.now())}")
            try:
                os.rename(target, kept)
            except OSError as exc:
                # The verified copy goes; the data never left the new directory.
                shutil.rmtree(legacy, ignore_errors=True)
                self.lock_dirs = [target]
                self._fail(6, RENAME_FAILED, error=f"{type(exc).__name__}: {exc}", method="copy")
                self._put_marker_back()
                return False
            self.result.backup_dir = str(kept)
            self.lock_dirs = [legacy, kept]
            moved["carried"] = _carry_over_late_writes(kept, legacy)
        with contextlib.suppress(FileNotFoundError):
            (legacy / RECORD_NAME).unlink()
        self.result.method = moved["method"]
        self.result.rolled_back = True
        return self._pass(6, **moved, removed_marker=self.marker, kept=self.result.backup_dir)

    def _put_marker_back(self) -> None:
        legacy, target = self.deps.legacy, self.deps.target
        try:
            if self.marker == "symlink":
                self.deps.make_symlink(target, legacy)
            else:
                legacy.mkdir()
                (legacy / MOVED_TO_NAME).write_text(f"{target}\n", encoding="utf-8")
        except OSError as exc:
            # Recorded on the gate 6 failure this follows.
            self.gates[-1].detail["marker_restore_error"] = f"{type(exc).__name__}: {exc}"


def migrate(
    *,
    apply: bool = False,
    legacy_dir: str | os.PathLike[str] | None = None,
    target_dir: str | os.PathLike[str] | None = None,
    explicit_source: bool = False,
    resolve: Callable[[], Any] | None = None,
    stop_api: Callable[..., ApiStop] | None = None,
    start_api: Callable[[int], dict[str, Any]] | None = None,
    db_holders: Callable[[list[Path]], HolderProbe] | None = None,
    postcheck: Callable[[int, Path, str, dict[str, Any]], Postcheck] | None = None,
    device_of: Callable[[Path], int] | None = None,
    copy_tree: Callable[[Path, Path], None] | None = None,
    make_symlink: Callable[[Path, Path], None] | None = None,
    now: Callable[[], float] | None = None,
    checkpoint_timeout: float = 5.0,
    api_package: Callable[[], Path | None] | None = None,
) -> MigrationResult:
    """Run the seven gates; ``apply=False`` (the default) judges gates 1-5 and changes nothing.

    Injected callables (each defaults to the production behaviour):

    * ``resolve() -> object with .path, .mode, .evidence`` (``aiteam.paths.resolve_data_dir``)
    * ``stop_api(data_dir, dry_run=bool) -> ApiStop`` (:func:`stop_api_processes`)
    * ``start_api(port) -> {"success": bool, ...}`` (:func:`start_api_default`)
    * ``db_holders([paths]) -> HolderProbe`` (:func:`find_db_holders`)
    * ``postcheck(port, expected_dir, expected_mode, baseline) -> Postcheck`` (:func:`postcheck_api`)
    * ``device_of(path) -> int`` (``os.stat().st_dev``), ``copy_tree(src, dst)``,
      ``make_symlink(target, link)``, ``now() -> epoch seconds``
    * ``api_package() -> Path | None``: the aiteam package the restarted API runs
      (:func:`default_api_package` with the default start; this package otherwise)

    ``explicit_source`` marks *legacy_dir* as given by the user (``--from``): it is
    accepted when the resolver finds nothing (mode ``missing``) as well.
    """
    deps = _build_deps(legacy_dir, target_dir, resolve, stop_api, start_api, db_holders, postcheck,
                       device_of, copy_tree, make_symlink, now, checkpoint_timeout, api_package)
    return _Migration(deps, apply=apply, explicit_source=explicit_source).run()


def rollback(
    *,
    legacy_dir: str | os.PathLike[str] | None = None,
    target_dir: str | os.PathLike[str] | None = None,
    resolve: Callable[[], Any] | None = None,
    stop_api: Callable[..., ApiStop] | None = None,
    start_api: Callable[[int], dict[str, Any]] | None = None,
    db_holders: Callable[[list[Path]], HolderProbe] | None = None,
    postcheck: Callable[[int, Path, str, dict[str, Any]], Postcheck] | None = None,
    device_of: Callable[[Path], int] | None = None,
    copy_tree: Callable[[Path, Path], None] | None = None,
    make_symlink: Callable[[Path, Path], None] | None = None,
    now: Callable[[], float] | None = None,
    checkpoint_timeout: float = 5.0,
    api_package: Callable[[], Path | None] | None = None,
) -> MigrationResult:
    """Move a migrated data directory back to the legacy location.

    Gate 1 checks the state is one this module left (resolver says canonical, the
    legacy path is our symlink or stub); gates 2-5 are the same as for migrating,
    on the new directory; gate 6 removes the symlink or stub and moves the current
    data back (rename, or copy and sha256 across filesystems, keeping the new
    directory renamed aside); gate 7 starts the API and checks it reports legacy.
    """
    deps = _build_deps(legacy_dir, target_dir, resolve, stop_api, start_api, db_holders, postcheck,
                       device_of, copy_tree, make_symlink, now, checkpoint_timeout, api_package)
    return _Rollback(deps).run()


# ============================================================
# CLI
# ============================================================

_STATUS = {"pass": "通过", "fail": "不过", "skip": "跳过"}
_ACTION = {"dry_run": "预演（只判定第 1-5 闸，不改动任何文件）", "apply": "迁移", "rollback": "回滚"}
_MARKER = {"symlink": "指向新位置的符号链接", "stub": "MOVED-TO 桩目录"}


def _render(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def format_result(result: MigrationResult) -> str:
    """One line per gate, then the conclusion."""
    lines = [f"OS 数据目录{_ACTION[result.action]}", f"  旧位置: {result.legacy_dir}",
             f"  新位置: {result.target_dir}"]
    for gate in result.gates:
        line = f"[{gate.gate}] {_GATE_LABELS[gate.gate]}: {_STATUS[gate.status]}"
        if gate.reason:
            line += f" ({gate.reason})"
        if gate.detail:
            line += "  " + "; ".join(f"{key}={_render(value)}" for key, value in gate.detail.items())
        lines.append(line)
    failed = result.failed_gate
    if failed is not None and failed.reason in (NOT_LEGACY, NOT_MIGRATED):
        mode = failed.detail.get("mode")
        lines.append(f"结论: 解析器报 {mode}，" + ("不是旧位置原地沿用的状态，不迁" if failed.reason == NOT_LEGACY
                                               else "不是本工具迁移后的状态，不回滚"))
    elif result.action == "dry_run":
        lines.append(f"结论: 第 1-5 闸都能过，可以迁移: {MANUAL_COMMAND}" if result.ok else
                     f"结论: 第 {failed.gate} 闸不过 ({failed.reason})，现在迁不了；数据留在旧位置照常可用")
    elif result.action == "apply":
        if result.ok:
            lines.append(f"结论: 已迁移 ({result.method})，旧位置留了{_MARKER.get(result.legacy_marker, '?')}；"
                         f"回滚: {ROLLBACK_COMMAND}")
        elif result.rolled_back:
            lines.append(f"结论: 第 7 闸不过 ({result.reason})，已自动回滚；数据留在旧位置照常可用")
        elif failed is not None and failed.gate == 7:
            lines.append(f"结论: 第 7 闸不过 ({result.reason})，自动回滚没有完成，数据在新位置；"
                         f"排查后可执行 {ROLLBACK_COMMAND}")
        elif failed is not None and "error" in (failed.detail.get("undo") or {}):
            lines.append(f"结论: 迁移没有走完 ({result.reason})，也没能撤回：数据在新位置 {result.target_dir}，"
                         f"旧位置不是本工具留下的链接或桩（undo 一项写明原因）。查看旧位置里的内容并移走后，"
                         f"运行 aiteam paths 核对")
        else:
            lines.append(f"结论: 未迁移 ({result.reason})，数据留在旧位置照常可用；排除原因后重试: {result.command}")
    else:
        if result.ok and failed is None:
            lines.append("结论: 已搬回旧位置")
        elif result.rolled_back:
            lines.append(f"结论: 已搬回旧位置，但 API 核对不过 ({result.reason})，请检查 API")
        else:
            lines.append(f"结论: 未回滚 ({result.reason})，数据仍在新位置")
    if result.backup_dir:
        lines.append(f"  另留一份: {result.backup_dir}")
    return "\n".join(lines)


def _caller_host() -> str:
    """cc when a Claude Code session runs this (it sets its session id), else codex."""
    return "cc" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "codex"


def record_consent(result: MigrationResult, quote: str, *, resolve: Callable[[], Any] | None = None) -> str:
    """Record that the user asked for this run (``--user-quote``): the API first, else the local file.

    Same ``consent`` record as the other conversation-authorised changes
    (``decision.user_config_write``), written where the data now is: through the
    API that gate 7 or the restore brought back, else appended to the data
    directory's local record file, which the next hook fetch imports. Returns
    "api", "local" or "none"; never raises.
    """
    from aiteam.hooks.user_notice import append_record_line, consent_record

    host = _caller_host()
    status = "ok" if result.ok and result.failed_gate is None else (
        "rolled_back" if result.rolled_back else "failed")
    record = consent_record(
        "data_dir_migration", host=host, tool="data_dir_migration",
        change="rollback_data_dir" if result.action == "rollback" else "migrate_data_dir",
        notice_key="data_dir_legacy", status=status, user_quote=quote[:MAX_QUOTE_CHARS],
        **({"error": str(result.reason)[:300]} if result.reason else {}),
        **({"backup": result.backup_dir} if result.backup_dir else {}),
    )
    try:
        data_dir = Path((resolve or _default_resolve)().path)
    except Exception:  # noqa: BLE001 - the run itself already reported its outcome
        return "none"
    port = read_api_port(data_dir)
    if port is not None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/notices/consent",
                                         data=json.dumps(record).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=5) as response:
                if response.status == 200:
                    return "api"
        except OSError:
            pass
    local = {key: value for key, value in record.items() if key != "host"}  # the importer stamps it
    return "local" if append_record_line(data_dir / f"notice-local.{host}.jsonl", local) else "none"


def main(argv: list[str] | None = None, **overrides: Any) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aiteam.data_dir_migration",
        description="把 OS 数据目录从旧位置迁到新位置；七道安全闸，任一不过就不迁。缺省只预演。",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="只判定第 1-5 闸，不改动任何文件（缺省）")
    action.add_argument("--apply", action="store_true", help="过闸后迁移；第 7 闸不过自动回滚")
    action.add_argument("--rollback", action="store_true", help="把已迁移的数据目录搬回旧位置")
    parser.add_argument("--from", dest="source", type=Path,
                        help="旧数据目录不在默认旧位置时显式给出（解析器报 missing 的情形）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--user-quote", dest="user_quote",
                        help="由助手代为执行时附上用户确认的原话（记为用户授权的配置改动）；"
                             "只与 --apply / --rollback 同用")
    args = parser.parse_args(argv)
    if args.user_quote is not None and not (args.apply or args.rollback):
        parser.error("--user-quote 只与 --apply 或 --rollback 同用：预演不改动任何东西，无需授权")
    if args.source is not None:
        overrides["legacy_dir"] = args.source
    if args.rollback:
        result = rollback(**overrides)
    else:
        result = migrate(apply=args.apply, explicit_source=args.source is not None, **overrides)
    consent = None
    if args.user_quote is not None:
        consent = record_consent(result, args.user_quote, resolve=overrides.get("resolve"))
    if args.json:
        document = result.to_dict()
        if consent is not None:
            document["consent_recorded"] = consent
        print(json.dumps(document, ensure_ascii=False, indent=2))
    else:
        print(format_result(result))
        if consent is not None:
            print(f"  用户确认: {'已记录' if consent != 'none' else '未能记录'}（{consent}）")
    return 0 if result.ok and result.failed_gate is None else 1


if __name__ == "__main__":
    sys.exit(main())
