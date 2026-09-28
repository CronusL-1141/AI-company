"""FastAPI auto-start, PID management, and port/health utilities.

Handles automatic starting of the FastAPI subprocess when the MCP server
launches, including version-aware restart, stale process cleanup, and
cross-platform port management.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from aiteam.api.lifecycle_diagnostics import record_lifecycle_event as _record_event

try:
    import psutil
except ImportError:
    psutil = None

logger = logging.getLogger(__name__)

# Debug log file for MCP/API startup diagnostics
_DEBUG_LOG_DIR = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os")
_DEBUG_LOG_FILE = os.path.join(_DEBUG_LOG_DIR, "mcp-debug.log")

# Port file — shared across all MCP sessions so they know which port the API is on
_PORT_FILE = os.path.join(_DEBUG_LOG_DIR, "api_port.txt")
_DEFAULT_PORT = 8000

# API subprocess stderr sink — a file, deliberately NOT a PIPE: nothing drains the
# pipe after startup, so accumulated tracebacks would eventually fill the ~64KB
# buffer and block uvicorn's stderr writes, freezing the entire API (audit H22).
_API_STDERR_LOG = os.path.join(_DEBUG_LOG_DIR, "api-stderr.log")


def _debug_log(message: str) -> None:
    """Append timestamped message to debug log for post-mortem diagnostics."""
    try:
        os.makedirs(_DEBUG_LOG_DIR, exist_ok=True)
        with open(_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


_api_process: subprocess.Popen | None = None
_PID_FILE = os.path.join(tempfile.gettempdir(), "aiteam-api.pid")


# ============================================================
# Port file management
# ============================================================


def _find_free_port() -> int:
    """Find an available port by binding to port 0 and letting the OS assign one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get_api_port() -> int:
    """Read port from port file. Returns default 8000 if file missing or invalid."""
    try:
        return int(open(_PORT_FILE).read().strip())
    except (FileNotFoundError, ValueError):
        return _DEFAULT_PORT


def _save_api_port(port: int) -> None:
    """Write port to port file so all sessions share the same API URL."""
    os.makedirs(os.path.dirname(_PORT_FILE), exist_ok=True)
    with open(_PORT_FILE, "w") as f:
        f.write(str(port))


def _get_api_url_for_port(port: int) -> str:
    return f"http://localhost:{port}"


# ============================================================
# Port / health checks
# ============================================================


def _is_port_open(host: str = "127.0.0.1", port: int = _DEFAULT_PORT) -> bool:
    """Check if the specified port is already listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def _is_api_healthy_on_port(port: int, timeout: float = 3.0) -> bool:
    """Return True only when /api/health on the given port responds successfully."""
    return _get_running_api_version_on_port(port, timeout=timeout) is not None


def _get_running_api_version_on_port(port: int, timeout: float = 2.0) -> str | None:
    """Query /api/health on a specific port and return the version string, or None."""
    try:
        url = f"{_get_api_url_for_port(port)}/api/health"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data.get("version")
    except Exception:
        return None


def _is_api_healthy(timeout: float = 3.0) -> bool:
    """Return True only when /api/health responds on the current saved port."""
    return _is_api_healthy_on_port(_get_api_port(), timeout=timeout)


def _get_running_api_version(timeout: float = 2.0) -> str | None:
    """Query /api/health on the current saved port and return version string, or None."""
    return _get_running_api_version_on_port(_get_api_port(), timeout=timeout)


# ============================================================
# PID file management
# ============================================================


def _read_pid_file() -> int | None:
    """Read PID from file and verify the process is alive. Returns None if missing/invalid/dead."""
    try:
        with open(_PID_FILE) as handle:
            pid = int(handle.read().strip())
            recorded_at = os.fstat(handle.fileno()).st_mtime
        identity = _api_process_identity(pid)
        if identity is None or identity > recorded_at:
            return None
        _debug_log(f"PID file: process {pid} alive")
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError, SystemError) as exc:
        # OSError/SystemError on Windows when process doesn't exist (WinError 87)
        _debug_log(f"PID file: stale/missing ({type(exc).__name__}: {exc})")
        return None


def _write_pid_file(pid: int, *, lock_held: bool = False) -> None:
    if pid <= 0:
        raise ValueError("PID must be positive")
    lock_fd = None if lock_held else _acquire_startup_lock()
    if not lock_held and lock_fd is None:
        raise OSError("API startup lock is busy")
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".aiteam-pid-", dir=os.path.dirname(_PID_FILE))
        with os.fdopen(fd, "w") as handle:
            handle.write(str(pid))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _PID_FILE)
    finally:
        try:
            if temporary is not None:
                os.unlink(temporary)
        except OSError:
            pass
        if lock_fd is not None:
            _release_startup_lock(lock_fd)


def _process_exists(pid: int) -> bool | None:
    """Return False for confirmed absence or death; permissions mean unknown."""
    if pid <= 0:
        return None
    if psutil is not None:
        try:
            return psutil.Process(pid).status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
        except psutil.NoSuchProcess:
            return False
        except (psutil.Error, OSError):
            return None
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=3, check=True,
        )
        status = result.stdout.strip()
        return not status.startswith(("Z", "X")) if status else None
    except ProcessLookupError:
        return False
    except (OSError, subprocess.SubprocessError):
        return None


_API_APP = "aiteam.api.app:create_app"


def _program_name(arg: str) -> str:
    name = os.path.basename(arg).lower()
    return name.removesuffix(".exe").removesuffix("-script.py")


def _is_api_command(args: list[str]) -> bool:
    """Match the API entry points, not arbitrary command-line substrings.

    Accepted: uvicorn run as ``python -m uvicorn``, as its console script
    (``uvicorn`` or ``python .../uvicorn``) with the app as any later argument,
    so --reload/--workers may sit on either side; and ``aiteam up`` (console
    script or ``python -m aiteam.cli.app``), which runs uvicorn in-process.
    Reload and worker children run multiprocessing's spawn_main and are not
    matched here; _api_listener_owner accounts for them.
    """
    if args[1:3] == ["-m", "uvicorn"]:
        return _API_APP in args[3:]
    if args[1:4] == ["-m", "aiteam.cli.app", "up"]:
        return True
    for index in (0, 1):
        if index >= len(args):
            break
        program = _program_name(args[index])
        if program == "uvicorn":
            return _API_APP in args[index + 1:]
        if program == "aiteam":
            return args[index + 1:index + 2] == ["up"]
    return False


# Identity verdicts. NOT_OURS requires positive evidence (gone, zombie, other
# owner, or a readable command that is no API entry point); a process that
# could not be inspected is UNKNOWN.
_OURS, _NOT_OURS, _UNKNOWN = "ours", "not_ours", "unknown"


def _classify_api_process_from_ps(pid: int) -> tuple[str, float | None]:
    """Read owner, state, birth time and command without optional dependencies."""
    if os.name != "posix":
        return _UNKNOWN, None
    exists = _process_exists(pid)
    if exists is not True:
        return (_NOT_OURS if exists is False else _UNKNOWN), None
    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "uid=,stat=,lstart=,command="],
            capture_output=True, text=True, timeout=3, check=True,
            env={**os.environ, "LC_ALL": "C"},
        )
        fields = result.stdout.strip().split(None, 7)
        if len(fields) != 8:
            return _UNKNOWN, None
        if int(fields[0]) != os.getuid() or fields[1].startswith(("Z", "X")):
            return _NOT_OURS, None
        command = fields[7]
        try:
            matched = _is_api_command(shlex.split(command))
        except ValueError:
            matched = False
        if not matched:
            # Only reached without psutil. ps joins argv with spaces, so an
            # interpreter path containing a space can hide a real API; only a
            # command without our name counts as foreign.
            return (_UNKNOWN if "aiteam" in command else _NOT_OURS), None
        return _OURS, time.mktime(time.strptime(" ".join(fields[2:7]), "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return _UNKNOWN, None


def _classify_api_process(pid: int) -> tuple[str, float | None]:
    """Return (verdict, creation time); the time is set only for _OURS."""
    if pid <= 0:
        return _UNKNOWN, None
    if psutil is None:
        return _classify_api_process_from_ps(pid)
    try:
        process = psutil.Process(pid)
        if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
            return _NOT_OURS, None
        if hasattr(os, "getuid") and process.uids().real != os.getuid():
            return _NOT_OURS, None
        args = process.cmdline()
        if not args:
            return _UNKNOWN, None
        if not _is_api_command(args):
            return _NOT_OURS, None
        return _OURS, process.create_time()
    except psutil.NoSuchProcess:
        return _NOT_OURS, None
    except (psutil.Error, OSError, ValueError):
        return _UNKNOWN, None


def _api_process_identity(pid: int) -> float | None:
    """Return creation time only for a live API process owned by this user."""
    verdict, created = _classify_api_process(pid)
    return created if verdict == _OURS else None


def _api_listener_owner(listeners: set[int]) -> int | None:
    """Return the process that owns the API listeners, or None when ambiguous.

    A single listener is returned as is; callers still verify its identity.
    With --reload or --workers the main process and its spawned children all
    hold the listening socket: accept that only when exactly one listener is
    an API process and every other listener is its direct child.
    """
    if len(listeners) == 1:
        return next(iter(listeners))
    if psutil is None:
        return None
    mains = [pid for pid in listeners if _api_process_identity(pid) is not None]
    if len(mains) != 1:
        return None
    try:
        if any(psutil.Process(pid).ppid() != mains[0] for pid in listeners - {mains[0]}):
            return None
    except (psutil.Error, OSError):
        return None
    return mains[0]


def _listener_pids(port: int) -> set[int]:
    """Discover loopback/wildcard listeners without elevated privileges."""
    if psutil is None:
        return set()
    try:
        return {
            connection.pid for connection in psutil.net_connections(kind="tcp")
            if connection.status == psutil.CONN_LISTEN and connection.pid
            and connection.laddr.port == port
            and connection.laddr.ip in ("127.0.0.1", "::1", "0.0.0.0", "::")
        }
    except (psutil.Error, OSError):
        try:
            # macOS GUI/CLI launch environments often omit /usr/sbin from PATH.
            lsof = "/usr/sbin/lsof" if sys.platform == "darwin" else "lsof"
            result = subprocess.run(
                [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            if result.returncode != 0:
                return set()
            pids: set[int] = set()
            pid = None
            for line in result.stdout.splitlines():
                if line.startswith("p"):
                    pid = int(line[1:])
                elif line.startswith("n") and pid and line[1:] in (
                    f"127.0.0.1:{port}", f"[::1]:{port}", f"*:{port}",
                ):
                    pids.add(pid)
            return pids
        except (OSError, ValueError, subprocess.SubprocessError):
            return set()


def _reconcile_api_pid(port: int, *, lock_held: bool = False) -> int | None:
    """Adopt a verified healthy listener, or leave all shared state untouched."""
    if psutil is None or not 0 < port < 65536:
        return None
    lock_fd = None if lock_held else _acquire_startup_lock()
    if not lock_held and lock_fd is None:
        return None
    try:
        if port != _get_api_port():
            return None
        global _api_process
        if _api_process is not None and _api_process.poll() is not None:
            _api_process = None
        candidates = _listener_pids(port)
        pid = _api_listener_owner(candidates)
        if pid is None:
            return None
        created = _api_process_identity(pid)
        if created is None or not _is_api_healthy_on_port(port, timeout=2):
            return None
        if (_listener_pids(port) != candidates or _api_process_identity(pid) != created
                or port != _get_api_port()):
            return None
        if _read_pid_file() != pid:
            _write_pid_file(pid, lock_held=True)
        return pid
    except (OSError, psutil.Error):
        return None
    finally:
        if lock_fd is not None:
            _release_startup_lock(lock_fd)


def _remove_owned_pid_file(pid: int) -> None:
    """Caller holds the startup lock; never remove a successor's record."""
    try:
        with open(_PID_FILE) as handle:
            recorded_pid = int(handle.read().strip())
        if recorded_pid == pid and psutil is not None:
            try:
                process = psutil.Process(pid)
                if process.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                    return
            except psutil.NoSuchProcess:
                pass
            except psutil.Error:
                return
            os.unlink(_PID_FILE)
    except (OSError, ValueError):
        pass


def _cleanup_api() -> None:
    """MCP 退出时的收尾——保留共享 API 守护进程，只清理已死子进程的残留。

    API 是跨会话共享守护进程（端口文件发现 + adopt 语义）：启动方会话先退出时
    绝不能把其它活跃会话正在用的 API 拉闸（审计 M56 —— 旧实现无条件 terminate
    并删 PID 文件，杀掉被 adopt 的实例还破坏其余会话的发现链）。健康的 API 刻意
    留给后续会话；真正的停止入口是版本升级换新 / os_restart_api / 卸载脚本。
    """
    global _api_process
    proc = _api_process
    _api_process = None
    if proc is None:
        return
    if proc.poll() is not None:
        _record_event("api.autostart.child_exited", target_pid=proc.pid, returncode=proc.returncode)
        # 子进程已死：清掉指向死 PID 的文件，避免下个会话对着尸体探活 15s。
        lock_fd = _acquire_startup_lock()
        if lock_fd is not None:
            try:
                _remove_owned_pid_file(proc.pid)
            finally:
                _release_startup_lock(lock_fd)
    else:
        _record_event("api.autostart.retained", target_pid=proc.pid, reason="mcp_exit_shared_api_retained")


# ============================================================
# Port occupant management
# ============================================================


def _pid_is_aiteam_api(pid: int) -> bool:
    """Kill 前验明正身：该 PID 是否真的是我们的 uvicorn/aiteam API 进程。

    PID 文件残留 + 操作系统 PID 复用会让「按存 PID 杀」误伤无辜进程（审计
    M55）；端口占用与健康检查之间也存在重绑竞态窗口。校验失败/不确定一律按
    「不是我们的」处理——宁可不杀（后续流程会自选空闲端口或留给用户处置）。
    """
    # The ps fallback has only second-resolution birth time: reuse is safe,
    # but destructive recovery requires the full process identity provider.
    return psutil is not None and _api_process_identity(pid) is not None


# Grace between SIGTERM and SIGKILL: the budget os_restart_api gives the old
# process. The API's SIGTERM exit (lifespan shutdown, lease release on a
# bounded lock wait) stays well inside it: 4.3s measured under a held lock.
_TERMINATE_GRACE_SECONDS = 10.0
# Grace for workers a hung main never stopped: covers the same measured exit.
_WORKER_GRACE_SECONDS = 5.0


def _pin_api_family(main_pid: int, listeners: set[int]) -> list | None:
    """Handles for a verified API process and its workers, or None.

    Workers are direct children that listen on the API port or run as
    multiprocessing spawn children (--reload / --workers). The handles are
    taken before the identity check and carry the creation time, so a PID
    reused afterwards is never signalled: psutil compares it before every signal.
    """
    if psutil is None:
        return None
    try:
        main = psutil.Process(main_pid)
        children = main.children()
    except psutil.Error:
        return None
    if not _pid_is_aiteam_api(main_pid):
        return None
    family = [main]
    for child in children:
        try:
            if child.pid in listeners or "--multiprocessing-fork" in child.cmdline():
                family.append(child)
        except psutil.Error:
            continue
    return family


def _family_alive(family: list) -> list:
    alive = []
    for process in family:
        try:
            # A zombie holds no port and never exits by itself.
            if process.is_running() and process.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                alive.append(process)
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            alive.append(process)
    return alive


def _signal_members(members: list, *, kill: bool, reason: str, port: int | None) -> None:
    signum = signal.SIGKILL if kill else signal.SIGTERM
    for process in members:
        _record_event(
            "api.termination.requested", target_pid=process.pid, port=port,
            reason=reason, signal=signum.name, signal_number=int(signum),
        )
        try:
            if kill:
                process.kill()
            else:
                process.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            logger.warning("Failed to signal API process PID=%s: %s", process.pid, exc)


def _wait_members(members: list, seconds: float) -> list:
    """Poll until *members* are gone or *seconds* pass; return those still alive."""
    alive = _family_alive(members)
    # Counted polls rather than a deadline: termination must end even under a
    # frozen or mocked clock.
    for _ in range(int(seconds / 0.1)):
        if not alive:
            break
        time.sleep(0.1)
        alive = _family_alive(alive)
    return alive


def _terminate_api_family(family: list, *, reason: str, port: int | None = None) -> None:
    """SIGTERM the API process, allow the exit grace, then SIGKILL what is left.

    SIGTERM lets the API run its own exit (lease release); a --reload or
    --workers main process stops its workers. A main still alive after the
    grace is killed. Workers still alive were never told to stop (a hung or
    vanished main forwards nothing), so they get their own SIGTERM and
    _WORKER_GRACE_SECONDS for their exit before SIGKILL.
    """
    main = family[0]
    _signal_members([main], kill=False, reason=reason, port=port)
    alive = _wait_members(family, _TERMINATE_GRACE_SECONDS)
    if not alive:
        return
    escalation = f"{reason}_escalation"
    _signal_members([process for process in alive if process is main], kill=True, reason=escalation, port=port)
    workers = [process for process in alive if process is not main]
    if workers:
        _signal_members(workers, kill=False, reason=escalation, port=port)
        _signal_members(_wait_members(workers, _WORKER_GRACE_SECONDS), kill=True, reason=escalation, port=port)
    _wait_members(alive, 3.0)


def _is_orphaned_worker(pid: int) -> bool:
    """A multiprocessing worker of ours whose parent is no API process any more."""
    try:
        process = psutil.Process(pid)
        if hasattr(os, "getuid") and process.uids().real != os.getuid():
            return False
        if "--multiprocessing-fork" not in process.cmdline():
            return False
        parent = process.parent()
        return parent is None or _api_process_identity(parent.pid) is None
    except (psutil.Error, OSError):
        return False


def _kill_port_occupant(port: int = 8000, *, keep_version: str | None = None) -> str | None:
    """Kill whichever process is listening on *port*.

    With *keep_version*, the API is left alone unless it still answers with
    another version once its processes are pinned: a concurrent session may
    have replaced it since the caller looked.

    Returns "terminated", the reason it was left alone, or None when no
    listener was found.

    Uses platform-appropriate tools:
    - Windows: ``netstat`` + ``taskkill``
    - Unix/macOS: _listener_pids, then _terminate_api_family (SIGTERM, grace,
      SIGKILL) over the API process and its worker listeners
    """
    pid: int | None = None
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for line in out.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    pid = int(line.split()[-1])
                    break
            if pid and not _pid_is_aiteam_api(pid):
                _record_event(
                    "api.termination.skipped", target_pid=pid, port=port, reason="ownership_unverified",
                )
                logger.warning(
                    "Port %s occupant PID=%s is not an aiteam API — refusing to kill (M55)",
                    port,
                    pid,
                )
                return "ownership_unverified"
            elif pid:
                _record_event(
                    "api.termination.requested", target_pid=pid, port=port,
                    reason="stale_port_occupant", signal="TerminateProcess", mechanism="taskkill_force",
                )
                subprocess.call(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info("Killed stale API process PID=%s (Windows)", pid)
                return "terminated"
        except Exception as exc:
            logger.warning("Failed to kill stale process on Windows: %s", exc)
        return None
    else:
        # Listeners only: fuser and lsof -ti also list clients connected to the port.
        listeners = _listener_pids(port)
        pid = _api_listener_owner(listeners) if listeners else None
        family = _pin_api_family(pid, listeners) if pid else None
        if listeners and family is None:
            # Workers whose API main died keep serving; nothing proves them ours.
            orphaned = all(_is_orphaned_worker(listener) for listener in listeners)
            skipped = "orphaned_workers" if orphaned else "ownership_unverified"
            _record_event(
                "api.termination.skipped", target_pid=pid, port=port, reason=skipped,
                listener_pids=sorted(listeners),
            )
            logger.warning(
                "Port %s listeners %s are not a verifiable aiteam API - refusing to kill (M55)",
                port,
                sorted(listeners),
            )
            return skipped
        if family:
            if keep_version is not None:
                running_version = _get_running_api_version_on_port(port, timeout=2)
                if running_version in (None, keep_version):
                    skipped = "replaced_concurrently" if running_version else "not_answering"
                    _record_event("api.termination.skipped", target_pid=pid, port=port, reason=skipped)
                    logger.info("API on port %s is no longer a stale version; leaving it", port)
                    return skipped
            _terminate_api_family(family, reason="stale_port_occupant", port=port)
            return "terminated"
        logger.warning("Could not determine PID for port %s - unable to kill stale process", port)
        return None


# ============================================================
# Main auto-start entry point
# ============================================================


_STARTUP_LOCK_FILE = os.path.join(tempfile.gettempdir(), "aiteam-api-startup.lock")


_STARTUP_LOCK_MAX_AGE = 60  # seconds — locks older than this are stale


def _acquire_startup_lock() -> int | None:
    """Atomically create a startup lock file. Returns the fd on success, None if already locked.

    Uses O_CREAT | O_EXCL for atomic creation so only one MCP session can enter the
    startup sequence at a time. The caller must call _release_startup_lock(fd) when done.

    An aged lock is reclaimed only after its owner is confirmed absent.
    """
    try:
        fd = os.open(_STARTUP_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except (FileExistsError, OSError):
        # Lock exists — check if it's stale (older than max age)
        try:
            lock_age = time.time() - os.path.getmtime(_STARTUP_LOCK_FILE)
            if lock_age > _STARTUP_LOCK_MAX_AGE:
                # Age alone cannot establish abandonment during a slow startup.
                try:
                    with open(_STARTUP_LOCK_FILE) as handle:
                        owner = int(handle.read().strip())
                        recorded = os.fstat(handle.fileno())
                    if _process_exists(owner) is not False:
                        return None
                    current = os.stat(_STARTUP_LOCK_FILE)
                    if (current.st_ino, current.st_mtime_ns) != (
                        recorded.st_ino, recorded.st_mtime_ns,
                    ):
                        return None
                except (OSError, ValueError):
                    return None
                _debug_log(f"Stale startup lock detected (age={lock_age:.0f}s > {_STARTUP_LOCK_MAX_AGE}s), removing")
                try:
                    os.unlink(_STARTUP_LOCK_FILE)
                except OSError:
                    pass
                # Retry acquisition after removing stale lock
                try:
                    fd = os.open(_STARTUP_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode())
                    return fd
                except (FileExistsError, OSError):
                    pass
        except OSError:
            pass
        return None


def _release_startup_lock(fd: int) -> None:
    """Release the startup lock by closing and removing the lock file."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(_STARTUP_LOCK_FILE)
    except OSError:
        pass


def _ensure_api_running() -> None:
    """Auto-start the FastAPI subprocess if it is not already running.

    Uses a PID file (aiteam-api.pid in the system temp directory) and an atomic
    startup lock file to prevent duplicate uvicorn launches when multiple MCP
    sessions start concurrently. The lock is held only during the startup sequence
    and released immediately afterwards.

    Port discovery logic:
    0. If AITEAM_API_URL env var is set, trust it completely (manual override).
    1. Fast path — check port file's saved port; if /api/health responds with
       correct version, return immediately (reuse existing session).
    2. Check default port 8000 — if healthy, adopt it and update port file.
    3. Acquire atomic startup lock before starting a new process.
    4. PID file exists — wait up to 15s for the process to become healthy.
    5. Port 8000 occupied by unknown (non-OS) process — find a free port instead.
    6. Start a fresh uvicorn subprocess on the chosen port, write port file.
    """
    import aiteam as _aiteam_pkg

    current_version = _aiteam_pkg.__version__
    global _api_process
    _debug_log(f"=== _ensure_api_running start (version={current_version}) ===")
    _record_event("api.autostart.begin")

    # 0. If user manually set AITEAM_API_URL, do not interfere with port selection
    if os.environ.get("AITEAM_API_URL"):
        _record_event("api.autostart.skipped", reason="manual_api_url")
        _debug_log("AITEAM_API_URL set by environment, skipping auto port discovery")
        return

    # 1. Fast path: check saved port file — another session may already have started
    saved_port = _get_api_port()
    if _is_api_healthy_on_port(saved_port, timeout=2):
        running_version = _get_running_api_version_on_port(saved_port, timeout=2)
        if running_version == current_version:
            logger.info(
                "FastAPI already running on port %d (version=%s), skipping auto-start",
                saved_port,
                running_version,
            )
            _reconcile_api_pid(saved_port)
            _record_event("api.autostart.reused", port=saved_port, reason="healthy_saved_port")
            return
        if psutil is None:
            _record_event("api.autostart.skipped", port=saved_port, reason="ownership_unverified")
            logger.warning("Cannot verify old API ownership without psutil; leaving runtime unchanged")
            return
        # Version mismatch — kill stale process and restart
        _record_event("api.autostart.restart_requested", port=saved_port, reason="version_mismatch")
        logger.info(
            "Stale API detected on port %d (running=%s, current=%s) — restarting",
            saved_port,
            running_version,
            current_version,
        )
        _kill_port_occupant(saved_port, keep_version=current_version)
        time.sleep(1)

    # 2. Check default port 8000 (covers first-run without a port file)
    if saved_port != _DEFAULT_PORT and _is_api_healthy_on_port(_DEFAULT_PORT, timeout=2):
        running_version = _get_running_api_version_on_port(_DEFAULT_PORT, timeout=2)
        if running_version == current_version:
            logger.info(
                "FastAPI found on default port %d (version=%s), adopting",
                _DEFAULT_PORT,
                running_version,
            )
            _save_api_port(_DEFAULT_PORT)
            _reconcile_api_pid(_DEFAULT_PORT)
            _record_event("api.autostart.reused", port=_DEFAULT_PORT, reason="healthy_default_port")
            return

    # 3. Acquire startup lock — prevent multiple MCP sessions from racing to start the API
    startup_lock_fd = _acquire_startup_lock()
    if startup_lock_fd is None:
        _record_event("api.autostart.waiting", reason="startup_lock_busy")
        # Another session is currently in the startup sequence — wait for it to finish
        _debug_log("Startup lock held by another session, waiting up to 20s for API to become healthy")
        logger.info("Another MCP session is starting the API — waiting up to 20s")
        for _ in range(20):
            current_saved_port = _get_api_port()
            if _is_api_healthy_on_port(current_saved_port, timeout=2):
                running_version = _get_running_api_version_on_port(current_saved_port, timeout=2)
                if running_version == current_version:
                    logger.info(
                        "API became healthy on port %d while waiting for startup lock (version=%s)",
                        current_saved_port,
                        running_version,
                    )
                    _reconcile_api_pid(current_saved_port)
                    _record_event("api.autostart.reused", port=current_saved_port, reason="healthy_after_lock_wait")
                    return
            time.sleep(1)
        # Only the lock acquisition helper may establish that an owner is gone.
        _debug_log("Timeout waiting for locked startup; checking abandoned lock")
        startup_lock_fd = _acquire_startup_lock()
        if startup_lock_fd is None:
            _record_event("api.autostart.skipped", reason="startup_lock_timeout")
            logger.warning("Could not acquire startup lock after timeout; leaving API unchanged")
            return

    try:
        _ensure_api_running_locked(current_version)
    finally:
        if startup_lock_fd is not None:
            _release_startup_lock(startup_lock_fd)


def _ensure_api_running_locked(current_version: str) -> None:
    """Inner implementation of _ensure_api_running, called while holding the startup lock."""
    global _api_process

    # Without process inspection, an occupied managed/default port cannot be
    # classified as unrelated. Do not create a second API on another port.
    if psutil is None and any(
        _is_port_open(port=port) for port in {_get_api_port(), _DEFAULT_PORT}
    ):
        _record_event("api.autostart.skipped", reason="ownership_unverified")
        logger.warning("Port ownership cannot be verified without psutil; skipping auto-start")
        return

    # 4. PID file present — another MCP session may have already started the API
    existing_pid = _read_pid_file()
    if existing_pid is None:
        try:
            with open(_PID_FILE) as handle:
                recorded_pid = int(handle.read().strip())
            if recorded_pid > 0 and _process_exists(recorded_pid) is not False:
                if _classify_api_process(recorded_pid)[0] != _NOT_OURS:
                    _record_event("api.autostart.skipped", target_pid=recorded_pid, reason="recorded_pid_unverified")
                    logger.warning("Recorded API process cannot be verified; leaving runtime unchanged")
                    return
                # The command check is an allow-list (python -u -m uvicorn is a real
                # API it misses); a process serving an API port may be ours.
                if any(recorded_pid in _listener_pids(port) for port in {_get_api_port(), _DEFAULT_PORT}):
                    _record_event("api.autostart.skipped", target_pid=recorded_pid, reason="recorded_pid_on_api_port")
                    logger.warning("Recorded PID serves an API port; leaving runtime unchanged")
                    return
                # The recorded API is gone and its PID was reused: drop the
                # record (we hold the startup lock) and start normally. A record
                # we cannot remove could not be replaced after the spawn either.
                try:
                    os.unlink(_PID_FILE)
                except FileNotFoundError:
                    pass
                except OSError:
                    _record_event("api.autostart.skipped", target_pid=recorded_pid, reason="stale_pid_not_removable")
                    logger.warning("Stale API PID record cannot be removed; leaving runtime unchanged")
                    return
                _record_event("api.autostart.stale_pid_cleared", target_pid=recorded_pid, reason="recorded_pid_not_api")
                logger.info("Recorded API PID=%d now belongs to another process; cleared it", recorded_pid)
        except (OSError, ValueError):
            pass
    if existing_pid is not None:
        saved_port = _get_api_port()
        _record_event("api.autostart.waiting", target_pid=existing_pid, port=saved_port, reason="existing_pid")
        logger.info(
            "PID file found (pid=%d) — waiting up to 15s for API to become healthy on port %d",
            existing_pid,
            saved_port,
        )
        for _ in range(15):
            if _is_api_healthy_on_port(saved_port, timeout=2):
                logger.info("API became healthy while waiting (pid=%d, port=%d)", existing_pid, saved_port)
                _reconcile_api_pid(saved_port, lock_held=True)
                _record_event(
                    "api.autostart.reused", target_pid=existing_pid, port=saved_port, reason="healthy_existing_pid",
                )
                return
            time.sleep(1)
        # Process exists but is not healthy after 15s — kill it.
        # D3 阶段B（审计 M55）：按存 PID 杀之前先验明正身——PID 文件残留 + 操作
        # 系统 PID 复用会把无辜进程当"卡死的 API"杀掉（考古线亦点名此处按存 PID
        # 盲杀最危险）。身份不确定时保留进程与台账，不启动替代实例。
        family = (_pin_api_family(existing_pid, _listener_pids(saved_port))
                  if _read_pid_file() == existing_pid else None)
        if family is None:
            _record_event("api.autostart.skipped", target_pid=existing_pid, reason="existing_pid_identity_changed")
            logger.warning(
                "API identity for PID=%d is uncertain; leaving process and PID file unchanged",
                existing_pid,
            )
            _debug_log(f"Uncertain API identity for PID {existing_pid}; leaving runtime unchanged")
            return
        else:
            logger.warning("API process %d is not healthy after 15s - stopping stuck process", existing_pid)
            try:
                if sys.platform == "win32":
                    _record_event(
                        "api.termination.requested", target_pid=existing_pid,
                        reason="existing_pid_health_timeout", signal="TerminateProcess", mechanism="taskkill_force",
                    )
                    subprocess.call(
                        ["taskkill", "/F", "/PID", str(existing_pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    _terminate_api_family(family, reason="existing_pid_health_timeout", port=saved_port)
            except Exception as exc:
                logger.warning("Failed to kill stuck process %d: %s", existing_pid, exc)
        try:
            os.unlink(_PID_FILE)
        except OSError:
            pass
        time.sleep(1)

    # 5. Determine which port to use
    #    - If default 8000 is free → use it (normal case, no other projects)
    #    - If 8000 is occupied but NOT our API → find a free port (multi-project conflict)
    #    - If 8000 is occupied by our API (healthy) → this was caught in fast path already
    if _is_port_open(port=_DEFAULT_PORT):
        # Port is occupied — check if it's an unrelated process
        if not _is_api_healthy_on_port(_DEFAULT_PORT, timeout=2):
            # Not our API — another project owns port 8000; find a free port
            port = _find_free_port()
            logger.info(
                "Port %d occupied by unrelated process — auto-selecting free port %d",
                _DEFAULT_PORT,
                port,
            )
            _debug_log(f"Port {_DEFAULT_PORT} occupied by non-OS process, using port {port}")
        else:
            # A healthy API step 2 did not adopt; replace it and reuse the port,
            # unless it now answers with the current version (replaced meanwhile).
            _record_event("api.autostart.restart_requested", port=_DEFAULT_PORT, reason="version_mismatch")
            logger.warning("Port %d occupied by our API (wrong version) — killing it", _DEFAULT_PORT)
            if _kill_port_occupant(_DEFAULT_PORT, keep_version=current_version) == "replaced_concurrently":
                _save_api_port(_DEFAULT_PORT)
                _reconcile_api_pid(_DEFAULT_PORT, lock_held=True)
                _record_event("api.autostart.reused", port=_DEFAULT_PORT, reason="replaced_concurrently")
                return
            time.sleep(1)
            port = _DEFAULT_PORT
    else:
        # Port 8000 is free
        port = _DEFAULT_PORT

    # 6. Start fresh API subprocess on the chosen port
    _debug_log(f"Starting fresh API subprocess on port {port} (version={current_version})")
    logger.info("Starting FastAPI subprocess on port %d (version=%s)...", port, current_version)
    _record_event("api.autostart.spawn_attempt", port=port)
    try:
        os.makedirs(_DEBUG_LOG_DIR, exist_ok=True)
        # stderr → append-mode file (parent's handle closed right after Popen; the
        # child keeps its own dup'd fd). Keeps startup errors inspectable without
        # the never-drained-PIPE freeze. stdout stays DEVNULL (protects MCP stdio).
        with open(_API_STDERR_LOG, "ab") as _stderr_fh:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "aiteam.api.app:create_app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--factory",
                ],
                # Do not keep the MCP host's input pipe open in the shared API.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=_stderr_fh,
                # The shared API must outlive signals to the launching MCP group.
                start_new_session=os.name == "posix",
            )
    except Exception as exc:
        _record_event(
            "api.autostart.failed", port=port, reason="spawn_error", exception_type=type(exc).__name__,
        )
        _debug_log(f"Failed to start API: {exc}")
        logger.warning("Failed to start FastAPI subprocess: %s", exc)
        return

    _api_process = proc
    _record_event("api.autostart.spawned", target_pid=proc.pid, port=port)
    try:
        _write_pid_file(proc.pid, lock_held=True)
        _save_api_port(port)
    except Exception as exc:
        _record_event(
            "api.autostart.failed", target_pid=proc.pid, port=port,
            reason="runtime_state_write_error", exception_type=type(exc).__name__,
        )
        raise
    atexit.register(_cleanup_api)
    _debug_log(f"API process started PID={proc.pid} port={port}, waiting for health...")

    # 7. Wait for health endpoint to respond
    for _i in range(20):
        time.sleep(0.5)
        if _is_api_healthy_on_port(port, timeout=2):
            _record_event("api.autostart.ready", target_pid=proc.pid, port=port)
            _debug_log(f"API healthy (PID={proc.pid}, port={port})")
            logger.info("FastAPI subprocess is ready (pid=%d, port=%d)", proc.pid, port)
            return
        if proc.poll() is not None:
            _record_event(
                "api.autostart.failed", target_pid=proc.pid, port=port,
                reason="child_exited", returncode=proc.returncode,
            )
            # stderr now goes to _API_STDERR_LOG (not a PIPE) — tail the file
            # to preserve the premature-exit post-mortem snapshot.
            stderr_out = ""
            try:
                with open(_API_STDERR_LOG, "rb") as _f:
                    _f.seek(0, os.SEEK_END)
                    _size = _f.tell()
                    _f.seek(max(0, _size - 2000))
                    stderr_out = _f.read().decode("utf-8", errors="replace")
            except OSError:
                pass
            _debug_log(f"API exited prematurely code={proc.returncode} stderr={stderr_out}")
            logger.warning(
                "FastAPI subprocess exited prematurely (code=%s)", proc.returncode
            )
            _api_process = None
            try:
                os.unlink(_PID_FILE)
            except OSError:
                pass
            return
    _debug_log(f"API did not become healthy within 10s on port {port}")
    _record_event("api.autostart.failed", target_pid=proc.pid, port=port, reason="health_timeout")
    logger.warning("FastAPI subprocess did not become healthy within 10s on port %d", port)
