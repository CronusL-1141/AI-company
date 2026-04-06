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
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from aiteam.mcp._base import API_URL

logger = logging.getLogger(__name__)

# Debug log file for MCP/API startup diagnostics
_DEBUG_LOG_DIR = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os")
_DEBUG_LOG_FILE = os.path.join(_DEBUG_LOG_DIR, "mcp-debug.log")


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
# Port / health checks
# ============================================================


def _is_port_open(host: str = "127.0.0.1", port: int = 8000) -> bool:
    """Check if the specified port is already listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def _is_api_healthy(timeout: float = 3.0) -> bool:
    """Return True only when /api/health responds successfully (not just port open)."""
    return _get_running_api_version(timeout=timeout) is not None


def _get_running_api_version(timeout: float = 2.0) -> str | None:
    """Query /api/health and return the reported version string, or None on failure.

    Returns None if the port is not open, the request times out, or the
    response does not contain a parseable version field.
    """
    try:
        with urllib.request.urlopen(f"{API_URL}/api/health", timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data.get("version")
    except Exception:
        return None


# ============================================================
# PID file management
# ============================================================


def _read_pid_file() -> int | None:
    """Read PID from file and verify the process is alive. Returns None if missing/invalid/dead."""
    try:
        pid = int(open(_PID_FILE).read().strip())
        os.kill(pid, 0)  # signal 0 = existence check only
        _debug_log(f"PID file: process {pid} alive")
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError, SystemError) as exc:
        # OSError/SystemError on Windows when process doesn't exist (WinError 87)
        _debug_log(f"PID file: stale/missing ({type(exc).__name__}: {exc})")
        return None


def _write_pid_file(pid: int) -> None:
    with open(_PID_FILE, "w") as f:
        f.write(str(pid))


def _cleanup_api() -> None:
    """Terminate the FastAPI subprocess on process exit and remove PID file."""
    global _api_process
    if _api_process is not None and _api_process.poll() is None:
        _api_process.terminate()
        try:
            _api_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _api_process.kill()
        _api_process = None
    try:
        os.unlink(_PID_FILE)
    except OSError:
        pass


# ============================================================
# Port occupant management
# ============================================================


def _kill_port_occupant(port: int = 8000) -> None:
    """Kill whichever process is listening on *port*.

    Uses platform-appropriate tools:
    - Windows: ``netstat`` + ``taskkill``
    - Unix/macOS: ``fuser`` or ``lsof`` + ``kill -9``
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
            if pid:
                subprocess.call(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info("Killed stale API process PID=%s (Windows)", pid)
        except Exception as exc:
            logger.warning("Failed to kill stale process on Windows: %s", exc)
    else:
        # Try fuser first (Linux); fall back to lsof (macOS)
        try:
            out = subprocess.check_output(
                ["fuser", f"{port}/tcp"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            for token in out.split():
                try:
                    pid = int(token)
                    break
                except ValueError:
                    continue
        except Exception:
            pass
        if pid is None:
            try:
                out = subprocess.check_output(
                    ["lsof", "-ti", f"tcp:{port}"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                pid = int(out.splitlines()[0]) if out else None
            except Exception:
                pass
        if pid:
            try:
                os.kill(pid, 9)
                logger.info("Killed stale API process PID=%s (Unix)", pid)
            except Exception as exc:
                logger.warning("Failed to kill stale process PID=%s: %s", pid, exc)
        else:
            logger.warning("Could not determine PID for port %s — unable to kill stale process", port)


# ============================================================
# Main auto-start entry point
# ============================================================


_STARTUP_LOCK_FILE = os.path.join(tempfile.gettempdir(), "aiteam-api-startup.lock")


_STARTUP_LOCK_MAX_AGE = 60  # seconds — locks older than this are stale


def _acquire_startup_lock() -> int | None:
    """Atomically create a startup lock file. Returns the fd on success, None if already locked.

    Uses O_CREAT | O_EXCL for atomic creation so only one MCP session can enter the
    startup sequence at a time. The caller must call _release_startup_lock(fd) when done.

    Stale lock detection: if the lock file is older than _STARTUP_LOCK_MAX_AGE seconds,
    it is considered abandoned (e.g. CC crashed) and removed before retrying.
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

    Recovery logic:
    1. Fast path — if /api/health responds with correct version, return immediately.
    2. Acquire atomic startup lock — if locked, wait up to 15s for the other session
       to finish starting the API, then return.
    3. PID file exists — wait up to 15s for the process to become healthy.
       If still unhealthy after 15s, the stuck process is killed.
    4. Port occupied by unknown process — kill it via _kill_port_occupant().
    5. Start a fresh uvicorn subprocess, write PID file, wait for health.
    """
    import aiteam as _aiteam_pkg

    current_version = _aiteam_pkg.__version__
    global _api_process
    _debug_log(f"=== _ensure_api_running start (version={current_version}) ===")

    # 1. Fast path: API already healthy with correct version
    if _is_api_healthy(timeout=2):
        running_version = _get_running_api_version(timeout=2)
        if running_version == current_version:
            logger.info(
                "FastAPI already running on port 8000 (version=%s), skipping auto-start",
                running_version,
            )
            return
        # Version mismatch — kill and restart
        logger.info(
            "Stale API detected (running=%s, current=%s) — restarting",
            running_version,
            current_version,
        )
        _kill_port_occupant()
        time.sleep(1)

    # 2. Acquire startup lock — prevent multiple MCP sessions from racing to start the API
    startup_lock_fd = _acquire_startup_lock()
    if startup_lock_fd is None:
        # Another session is currently in the startup sequence — wait for it to finish
        _debug_log("Startup lock held by another session, waiting up to 20s for API to become healthy")
        logger.info("Another MCP session is starting the API — waiting up to 20s")
        for _ in range(20):
            if _is_api_healthy(timeout=2):
                running_version = _get_running_api_version(timeout=2)
                if running_version == current_version:
                    logger.info("API became healthy while waiting for startup lock (version=%s)", running_version)
                    return
            time.sleep(1)
        # Lock-holding session didn't produce a healthy API; clean up stale lock and continue
        _debug_log("Timeout waiting for locked startup; removing stale lock and continuing")
        try:
            os.unlink(_STARTUP_LOCK_FILE)
        except OSError:
            pass
        startup_lock_fd = _acquire_startup_lock()
        if startup_lock_fd is None:
            logger.warning("Could not acquire startup lock after timeout — proceeding without lock")

    try:
        _ensure_api_running_locked(current_version)
    finally:
        if startup_lock_fd is not None:
            _release_startup_lock(startup_lock_fd)


def _ensure_api_running_locked(current_version: str) -> None:
    """Inner implementation of _ensure_api_running, called while holding the startup lock."""
    global _api_process

    # 3. PID file present — another MCP session may have already started the API
    existing_pid = _read_pid_file()
    if existing_pid is not None:
        logger.info("PID file found (pid=%d) — waiting up to 15s for API to become healthy", existing_pid)
        for _ in range(15):
            if _is_api_healthy(timeout=2):
                logger.info("API became healthy while waiting (pid=%d)", existing_pid)
                return
            time.sleep(1)
        # Process exists but is not healthy after 15s — kill it
        logger.warning("API process %d is not healthy after 15s — killing stuck process", existing_pid)
        try:
            if sys.platform == "win32":
                subprocess.call(
                    ["taskkill", "/F", "/PID", str(existing_pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                try:
                    os.kill(existing_pid, signal.SIGTERM)
                    time.sleep(2)
                    os.kill(existing_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError, SystemError):
                    pass
        except Exception as exc:
            logger.warning("Failed to kill stuck process %d: %s", existing_pid, exc)
        try:
            os.unlink(_PID_FILE)
        except OSError:
            pass
        time.sleep(1)

    # 4. Port still occupied by an untracked process
    if _is_port_open():
        logger.warning("Port 8000 occupied by untracked process — killing it")
        _kill_port_occupant()
        time.sleep(1)

    # 5. Start fresh API subprocess
    _debug_log(f"Starting fresh API subprocess (version={current_version})")
    logger.info("Starting FastAPI subprocess on port 8000 (version=%s)...", current_version)
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "aiteam.api.app:create_app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--factory",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except Exception as exc:
        _debug_log(f"Failed to start API: {exc}")
        logger.warning("Failed to start FastAPI subprocess: %s", exc)
        return

    _api_process = proc
    _write_pid_file(proc.pid)
    atexit.register(_cleanup_api)
    _debug_log(f"API process started PID={proc.pid}, waiting for health...")

    # 6. Wait for health endpoint to respond
    for _i in range(20):
        time.sleep(0.5)
        if _is_api_healthy(timeout=2):
            _debug_log(f"API healthy (PID={proc.pid})")
            logger.info("FastAPI subprocess is ready (pid=%d)", proc.pid)
            return
        if proc.poll() is not None:
            stderr_out = ""
            try:
                stderr_out = proc.stderr.read().decode("utf-8", errors="replace")[:500] if proc.stderr else ""
            except Exception:
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
    _debug_log("API did not become healthy within 10s")
    logger.warning("FastAPI subprocess did not become healthy within 10s")
