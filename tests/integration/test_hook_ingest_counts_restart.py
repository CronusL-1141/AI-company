"""Hook ingest counters must survive an API restart and be readable by os_health_check.

End to end: a real API process (uvicorn, httptools) on a temporary database. An
outside connection holds the write lock while a burst of hook events queues, so
their clients give up and the server counts hook_client_gone. The API is then
restarted the way os_restart_api does it (POST /api/system/shutdown, which
hard-exits) or by SIGTERM (uvicorn runs the lifespan shutdown), a fresh process
is started on the same database, and os_health_check must still report the count.
"""

from __future__ import annotations

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from aiteam.api.exit_writes import EXIT_WRITE_BUDGET_SECONDS
from aiteam.api.routes import system
from aiteam.mcp.tools import infra
from tests.unit.api.test_hook_ingest_preread import CLIENT_TIMEOUT, _post_and_give_up

ROOT = Path(__file__).resolve().parents[2]
BURST = 12
LOCK_HOLD = 3.0


class _Capture:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


_capture = _Capture()
infra.register(_capture)
health = _capture.tools["os_health_check"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Api:
    """One real API process on the shared temporary database."""

    def __init__(self, tmp_path: Path, port: int) -> None:
        shim = tmp_path / "shim"
        shim.mkdir(exist_ok=True)
        for name in ("claude", "codex"):
            script = shim / name
            script.write_text("#!/bin/sh\nexit 1\n")
            script.chmod(0o755)
        self.port = port
        self.env = {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{shim}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": str(ROOT / "src"),
            "AITEAM_DB_PATH": str(tmp_path / "api.db"),
            "AITEAM_DIAGNOSTICS_DIR": str(tmp_path / "diagnostics"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "*", "no_proxy": "*",
        }
        code = (
            "import uvicorn\n"
            "from aiteam.api.app import create_app\n"
            f"uvicorn.run(create_app(), host='127.0.0.1', port={port}, http='httptools',"
            " log_level='warning')\n"
        )
        self.log = open(tmp_path / f"api-{port}-{uuid.uuid4().hex[:6]}.log", "w")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code], cwd=tmp_path, env=self.env,
            stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 60
        while True:
            assert self.proc.poll() is None, Path(self.log.name).read_text()[-3000:]
            assert time.monotonic() < deadline, "API did not come up"
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.2)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def wait_exit(self, timeout: float = 20) -> None:
        try:
            self.proc.wait(timeout)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(5)
            self.log.close()


def _burst_with_client_gone(api: _Api) -> None:
    locker = sqlite3.connect(api.env["AITEAM_DB_PATH"], isolation_level=None, timeout=10)
    locker.execute("BEGIN IMMEDIATE")
    markers = [f"restart-{uuid.uuid4().hex[:10]}" for _ in range(BURST)]

    def fire(marker: str) -> None:
        _post_and_give_up(api.port, {
            "hook_event_name": "PreToolUse", "session_id": "synthetic-restart-session",
            "tool_name": "Bash", "tool_input": {"command": f"echo {marker}"},
            "tool_use_id": f"toolu_{marker}",
        })

    threads = [threading.Thread(target=fire, args=(m,)) for m in markers]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        time.sleep(max(0.0, LOCK_HOLD - CLIENT_TIMEOUT))
    finally:
        locker.execute("COMMIT")
        locker.close()
    # Let the queued events run to completion before the restart.
    con = sqlite3.connect(f"file:{api.env['AITEAM_DB_PATH']}?mode=ro", uri=True)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            landed = con.execute(
                "SELECT COUNT(*) FROM events WHERE type = 'cc.tool_use' AND data LIKE '%restart-%'",
            ).fetchone()[0]
            if landed >= BURST:
                break
            time.sleep(0.2)
    finally:
        con.close()


@pytest.mark.parametrize("restart", ["http_shutdown", "sigterm", "sigkill"])
def test_client_gone_survives_restart_and_reaches_the_health_check(tmp_path, monkeypatch, restart):
    port = _free_port()
    first = _Api(tmp_path, port)
    try:
        _burst_with_client_gone(first)
        if restart == "http_shutdown":
            request = urllib.request.Request(f"{first.url}/api/system/shutdown", method="POST")
            with urllib.request.urlopen(request, timeout=5):
                pass
        elif restart == "sigterm":
            first.proc.send_signal(signal.SIGTERM)
        else:
            first.proc.send_signal(signal.SIGKILL)
    finally:
        first.wait_exit()

    second = _Api(tmp_path, port)
    try:
        monkeypatch.setenv("AITEAM_API_URL", second.url)
        result = health()
    finally:
        second.proc.send_signal(signal.SIGTERM)
        second.wait_exit()

    assert result["status"] == "healthy"
    ingest = result["hook_ingest"]
    if restart == "sigkill":
        # No exit flush ran: this hour's counts are gone, and the window says so
        # instead of reading as a clean zero.
        assert ingest["complete"] is False, ingest
        assert ingest["processes_without_final_rollup"] == 1
        return
    # The queued part of the burst (everything past the DB slots) lost its receipt.
    assert ingest["window"]["client_gone"] >= BURST - 5, ingest
    assert ingest["window"]["body_lost"] == 0
    assert ingest["complete"] is True  # the previous process flushed on its way out
    assert ingest["current_process"]["counts"]["client_gone"] == 0  # counted by the previous one
    assert "lower bound" in ingest["notes"]["client_gone"]


# os_restart_api gives the old process 10s to exit before it gives up on the restart.
RESTART_EXIT_BUDGET = 10.0


def test_http_shutdown_exits_within_the_restart_budget_while_the_db_is_locked(tmp_path):
    """The exit-path writes (ledger flush, lease release) must not wait out a lock.

    An outside connection holds the write lock past the whole restart budget. The
    writes cannot land; the process must give them up and exit in time instead of
    blocking until the lock frees (SQLAlchemy's cancel cleanup waits for it too).
    """
    api = _Api(tmp_path, _free_port())
    locker = sqlite3.connect(api.env["AITEAM_DB_PATH"], isolation_level=None, timeout=10)
    try:
        locker.execute("BEGIN IMMEDIATE")
        request = urllib.request.Request(f"{api.url}/api/system/shutdown", method="POST")
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=5):
            pass
        try:
            api.proc.wait(RESTART_EXIT_BUDGET + 5)
        except subprocess.TimeoutExpired:
            pass
        exited_after = time.monotonic() - started if api.proc.poll() is not None else None
    finally:
        # Hold the lock past the budget whatever happened, then release it.
        time.sleep(max(0.0, RESTART_EXIT_BUDGET + 2 - (time.monotonic() - started)))
        locker.execute("ROLLBACK")
        locker.close()
        api.wait_exit()
    print(f"http shutdown under a held lock: exited after {exited_after}s")
    # No background job: the response flush plus the exit-write budget (the checkpoint
    # is skipped once those writes are left), with a second of slack.
    bound = (
        system.RESPONSE_FLUSH_SECONDS + EXIT_WRITE_BUDGET_SECONDS
        + system.DIAGNOSTICS_FLUSH_SECONDS + 1.0
    )
    assert exited_after is not None and exited_after < bound, (exited_after, bound)
    assert api.proc.returncode == 0  # the os._exit(0) path, not a crash that exited early
