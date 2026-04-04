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
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
        # OSError on Windows when process doesn't exist (WinError 87)
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


def _ensure_api_running() -> None:
    """Auto-start the FastAPI subprocess if it is not already running.

    Uses a PID file (aiteam-api.pid in the system temp directory) instead of
    a startup lock. This prevents duplicate uvicorn launches across multiple
    MCP processes and automatically recovers from stuck/unhealthy processes.

    Recovery logic:
    1. Fast path — if /api/health responds with correct version, return immediately.
    2. PID file exists — wait up to 15s for the process to become healthy.
       If still unhealthy after 15s, the stuck process is killed.
    3. Port occupied by unknown process — kill it via _kill_port_occupant().
    4. Start a fresh uvicorn subprocess, write PID file, wait for health.
    """
    import aiteam as _aiteam_pkg

    current_version = _aiteam_pkg.__version__
    global _api_process

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

    # 2. PID file present — another MCP session may have already started the API
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
                except (ProcessLookupError, PermissionError):
                    pass
        except Exception as exc:
            logger.warning("Failed to kill stuck process %d: %s", existing_pid, exc)
        try:
            os.unlink(_PID_FILE)
        except OSError:
            pass
        time.sleep(1)

    # 3. Port still occupied by an untracked process
    if _is_port_open():
        logger.warning("Port 8000 occupied by untracked process — killing it")
        _kill_port_occupant()
        time.sleep(1)

    # 4. Start fresh API subprocess
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
        logger.warning("Failed to start FastAPI subprocess: %s", exc)
        return

    _api_process = proc
    _write_pid_file(proc.pid)
    atexit.register(_cleanup_api)

    # 5. Wait for health endpoint to respond
    for _i in range(20):
        time.sleep(0.5)
        if _is_api_healthy(timeout=2):
            logger.info("FastAPI subprocess is ready (pid=%d)", proc.pid)
            return
        if proc.poll() is not None:
            logger.warning(
                "FastAPI subprocess exited prematurely (code=%s)", proc.returncode
            )
            _api_process = None
            try:
                os.unlink(_PID_FILE)
            except OSError:
                pass
            return
    logger.warning("FastAPI subprocess did not become healthy within 10s")
