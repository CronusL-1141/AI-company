"""Real MCP/API lifetime checks with isolated state and owned process groups."""

from __future__ import annotations

import json
import os
import runpy
import selectors
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created_at: float
    command: tuple[str, ...]
    cwd: str

    @classmethod
    def capture(cls, pid: int, cwd: Path) -> ProcessIdentity:
        process = psutil.Process(pid)
        assert process.uids().real == os.getuid()
        assert process.cwd() == str(cwd)
        return cls(pid, process.create_time(), tuple(process.cmdline()), str(cwd))

    def live(self) -> psutil.Process | None:
        try:
            process = psutil.Process(self.pid)
            assert process.create_time() == self.created_at, "PID was reused; do not signal"
            if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                return None
            assert process.uids().real == os.getuid()
            assert tuple(process.cmdline()) == self.command
            assert process.cwd() == self.cwd
            return process
        except psutil.NoSuchProcess:
            return None


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


def _request(port: int, path: str, *, post: bool = False) -> dict | None:
    assert port != 8000, "The shared API is outside this test"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"{}" if post else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(request, timeout=1) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def _stop_owned(identity: ProcessIdentity | None) -> None:
    if identity is None or identity.live() is None:
        return
    identity.live().send_signal(signal.SIGTERM)
    if not _wait_for(lambda: identity.live() is None, 8):
        process = identity.live()
        if process is not None:
            process.send_signal(signal.SIGKILL)
        assert _wait_for(lambda: identity.live() is None, 5)


def _group_members(pgid: int) -> set[int]:
    members = set()
    for process in psutil.process_iter():
        try:
            if os.getpgid(process.pid) == pgid:
                if process.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                    members.add(process.pid)
        except (psutil.NoSuchProcess, ProcessLookupError):
            pass
    return members


def _initialize_mcp(parent: subprocess.Popen) -> None:
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "process-group-test", "version": "1"},
        },
    }
    parent.stdin.write((json.dumps(request) + "\n").encode())
    parent.stdin.flush()
    with selectors.DefaultSelector() as selector:
        selector.register(parent.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=10), "MCP initialize timed out"
        response = json.loads(parent.stdout.readline())
    assert response["id"] == 1 and "result" in response, response


def _launch_mcp(port: int, start_path: str) -> None:
    from aiteam.mcp import _autostart

    isolated = Path.cwd()
    for value in (_autostart._PID_FILE, _autostart._STARTUP_LOCK_FILE,
                  _autostart._DEBUG_LOG_DIR, os.environ["AITEAM_DB_PATH"]):
        assert Path(value).is_relative_to(isolated)
    assert Path.home().is_relative_to(isolated)
    assert port != 8000 and "AITEAM_API_URL" not in os.environ
    _autostart._DEFAULT_PORT = port
    _autostart._save_api_port(port)

    def refuse_occupant_kill(*args, **kwargs):
        raise AssertionError("Never signal an unowned port occupant")

    _autostart._kill_port_occupant = refuse_occupant_kill
    if start_path == "restart":
        from aiteam.mcp.tools.infra import _restart_spawn_on_port

        result = _restart_spawn_on_port(_autostart, port)
        assert result["success"], result
    runpy.run_module("aiteam.mcp.server", run_name="__main__")


def _finish_parent(parent: subprocess.Popen, identity: ProcessIdentity | None) -> None:
    try:
        try:
            parent.stdin.close()
        except OSError:
            pass
    finally:
        try:
            parent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                _stop_owned(identity)
            finally:
                parent.wait(timeout=5)


@contextmanager
def _isolated_mcp(tmp_path: Path, start_path: str):
    for directory in ("home", "tmp", "data"):
        (tmp_path / directory).mkdir()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    assert port != 8000
    env = {
        "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp"),
        "AITEAM_DB_PATH": str(tmp_path / "data" / "aiteam.db"),
        "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1", "NO_PROXY": "*",
        "PATH": os.environ.get("PATH", os.defpath), "FASTMCP_CHECK_FOR_UPDATES": "off",
    }
    pid_file = tmp_path / "tmp" / "aiteam-api.pid"
    parent_identity = None
    api_identity = None

    def capture_api() -> bool:
        nonlocal api_identity
        if not pid_file.exists():
            return False
        pid = int(pid_file.read_text())
        process = psutil.Process(pid)
        assert process.ppid() == parent.pid
        assert process.create_time() >= parent_identity.created_at
        command = process.cmdline()
        assert command[1:4] == ["-m", "uvicorn", "aiteam.api.app:create_app"]
        assert command[command.index("--port") + 1] == str(port)
        api_identity = ProcessIdentity.capture(pid, tmp_path)
        child_env = process.environ()
        for key in ("HOME", "TMPDIR", "AITEAM_DB_PATH"):
            assert child_env.get(key) == env[key]
        return True

    def capture_for_cleanup() -> None:
        if api_identity is None and parent.poll() is None and pid_file.exists():
            capture_api()

    with ExitStack() as cleanup:
        # The macOS Python launcher re-execs with a different argv[0]; use the
        # already-running executable so birth-time/command checks remain strict.
        with (tmp_path / "mcp-stderr.log").open("wb") as stderr:
            parent = subprocess.Popen(
                [psutil.Process().exe(), str(Path(__file__).resolve()), str(port), start_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                cwd=tmp_path, env=env, start_new_session=True,
            )
        # Each layer still runs when an earlier capture or verification fails.
        cleanup.callback(parent.stdout.close)
        cleanup.callback(lambda: _stop_owned(api_identity))
        cleanup.callback(lambda: _finish_parent(parent, parent_identity))
        cleanup.callback(capture_for_cleanup)
        parent_identity = ProcessIdentity.capture(parent.pid, tmp_path)
        assert os.getpgid(parent.pid) == parent.pid != os.getpgrp()
        assert parent_identity.live() is not None
        parent.stdin.write(b"START\n")
        parent.stdin.flush()
        assert _wait_for(lambda: bool(_request(port, "/api/health")), 20), (
            tmp_path / "mcp-stderr.log"
        ).read_text()
        _initialize_mcp(parent)
        # Wait until Python has finished any launcher re-exec before freezing
        # command identity or inspecting the child's environment on macOS.
        assert capture_api()
        assert parent_identity.live() is not None
        assert api_identity.live() is not None
        yield parent, parent_identity, api_identity, port, pid_file
    assert parent_identity.live() is None
    assert api_identity is None or api_identity.live() is None
    assert not _group_members(parent.pid)
    assert not _request(port, "/api/health")


@pytest.fixture(params=["autostart", "restart"])
def isolated_mcp(request, tmp_path):
    with _isolated_mcp(tmp_path, request.param) as runtime:
        yield runtime


@pytest.mark.parametrize("exit_mode", ["group_sigterm", "eof"])
def test_api_survives_mcp_exit_and_remains_stoppable(isolated_mcp, exit_mode):
    from aiteam.mcp._autostart import _listener_pids

    parent, parent_identity, api_identity, port, pid_file = isolated_mcp
    if exit_mode == "group_sigterm":
        assert parent_identity.live() is not None and api_identity.live() is not None
        assert os.getpgid(parent.pid) == parent.pid != os.getpgrp()
        members = _group_members(parent.pid)
        assert parent.pid in members and members <= {parent.pid, api_identity.pid}
        os.killpg(parent.pid, signal.SIGTERM)
        parent.wait(timeout=10)
        time.sleep(0.5)
    else:
        parent.stdin.close()
        assert parent.wait(timeout=10) == 0

    assert api_identity.live() is not None, "MCP process-group exit stopped the shared API"
    assert _request(port, "/api/health")["status"] == "ok"
    assert int(pid_file.read_text()) == api_identity.pid
    assert os.getpgid(api_identity.pid) == api_identity.pid
    assert os.getsid(api_identity.pid) == api_identity.pid
    assert api_identity.live() is not None
    assert _listener_pids(port) == {api_identity.pid}
    response = _request(port, "/api/system/shutdown", post=True)
    assert response["success"] and response["pid"] == api_identity.pid
    assert _wait_for(lambda: api_identity.live() is None)
    assert _wait_for(lambda: not _request(port, "/api/health"))


def test_parent_capture_failure_closes_gate_without_starting_api(tmp_path):
    with (
        patch.object(ProcessIdentity, "capture", side_effect=psutil.AccessDenied(0)),
        patch(__name__ + "._finish_parent", wraps=_finish_parent) as finish,
        patch(__name__ + "._stop_owned", wraps=_stop_owned) as stop,
        pytest.raises(psutil.AccessDenied),
    ):
        with _isolated_mcp(tmp_path, "autostart"):
            pytest.fail("The unverified parent must not start the API")

    parent = finish.call_args.args[0]
    assert parent.returncode == 0
    assert parent.stdin.closed and parent.stdout.closed
    assert all(call.args[0] is None for call in stop.call_args_list)
    assert not (tmp_path / "tmp" / "aiteam-api.pid").exists()
    assert not (tmp_path / "data" / "aiteam.db").exists()


def test_cleanup_capture_failure_still_stops_verified_api(tmp_path):
    with (
        patch(__name__ + "._initialize_mcp", side_effect=RuntimeError("injected setup failure")),
        patch.object(psutil.Process, "environ", side_effect=psutil.AccessDenied(0)),
        patch(__name__ + "._finish_parent", wraps=_finish_parent) as finish,
        patch(__name__ + "._stop_owned", wraps=_stop_owned) as stop,
        pytest.raises(psutil.AccessDenied),
    ):
        with _isolated_mcp(tmp_path, "autostart"):
            pytest.fail("The injected setup failure must propagate")

    parent = finish.call_args.args[0]
    assert parent.returncode == 0
    assert parent.stdin.closed and parent.stdout.closed
    identities = [call.args[0] for call in stop.call_args_list if call.args[0] is not None]
    assert identities
    assert all(identity.live() is None for identity in identities)
    assert not _group_members(parent.pid)


def test_parent_recheck_failure_still_waits_and_stops_verified_api(tmp_path, monkeypatch):
    with pytest.raises(AssertionError, match="injected parent identity failure"):
        with _isolated_mcp(tmp_path, "restart") as runtime:
            parent, parent_identity, api_identity, port, _ = runtime
            real_live = ProcessIdentity.live
            real_wait = parent.wait
            waits = 0

            def fail_parent_recheck(identity):
                if identity.pid == parent_identity.pid:
                    raise AssertionError("injected parent identity failure")
                return real_live(identity)

            def first_wait_times_out(*, timeout=None):
                nonlocal waits
                waits += 1
                if waits == 1:
                    raise subprocess.TimeoutExpired(parent.args, timeout)
                return real_wait(timeout=timeout)

            monkeypatch.setattr(ProcessIdentity, "live", fail_parent_recheck)
            monkeypatch.setattr(parent, "wait", first_wait_times_out)

    assert waits >= 2 and parent.poll() is not None
    assert parent.stdin.closed and parent.stdout.closed
    assert real_live(api_identity) is None
    assert not _request(port, "/api/health")


if __name__ == "__main__":
    # EOF before the parent validates this process must not leave an API behind.
    if sys.stdin.buffer.readline() == b"START\n":
        _launch_mcp(int(sys.argv[1]), sys.argv[2])
