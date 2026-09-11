"""Real uvicorn signals with isolated state, no shared API or database."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "src"
CANARY = "lifecycle-private-canary-not-for-diagnostics"
CHILD = r"""
import asyncio
import signal
import socket
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
import uvicorn
from aiteam import diagnostics
from aiteam.api import app as application

mode = sys.argv[2]
canary = sys.argv[3]
baseline = sys.argv[4] == "baseline"
flush_calls = []
original_flush = diagnostics.flush_diagnostics

def checked_flush(*, timeout):
    flush_calls.append(timeout)
    return original_flush(timeout=timeout)

diagnostics.flush_diagnostics = checked_flush

async def initialize():
    if mode == "startup_failure":
        raise RuntimeError(canary)

async def cleanup():
    if mode == "shutdown_failure":
        raise RuntimeError(canary)

application.init_dependencies = initialize
application.cleanup_dependencies = cleanup
application._get_mcp_http_app = lambda: None

@asynccontextmanager
async def checked_lifespan(app):
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    manager = application._application_lifespan if baseline else application.lifespan
    try:
        async with manager(app):
            yield
    finally:
        if not baseline:
            assert flush_calls == [0.25], flush_calls
        assert all(signal.getsignal(signum) == handler for signum, handler in handlers.items())
        print("handlers-restored", flush=True)

app = FastAPI(lifespan=checked_lifespan)

@app.get("/api/health")
async def health():
    if mode in ("normal", "shutdown_failure"):
        server.should_exit = True
    return {"status": "ok"}

server = uvicorn.Server(uvicorn.Config(app, log_level="info", lifespan="on", loop="asyncio"))
listener = socket.socket(fileno=int(sys.argv[1]))
asyncio.run(server.serve(sockets=[listener]))
"""


def run_server(tmp_path, *, mode="signal", signum=None, baseline=False, broken_sink=False):
    with tempfile.TemporaryDirectory(prefix="lifecycle-", dir=tmp_path) as temporary:
        root = Path(temporary)
        events_dir = root / "events"
        if broken_sink:
            events_dir.write_text("not a directory", encoding="utf-8")
        environment = {
            "PATH": os.environ.get("PATH", ""), "HOME": temporary, "TMPDIR": temporary,
            "TMP": temporary, "TEMP": temporary, "PYTHONPATH": str(SOURCE),
            "PYTHONDONTWRITEBYTECODE": "1", "AITEAM_DIAGNOSTICS_ENABLED": "1",
            "AITEAM_DIAGNOSTICS_DIR": str(events_dir), "PRIVATE_CANARY": CANARY,
        }
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", CHILD, str(listener.fileno()), mode, CANARY,
                 "baseline" if baseline else "observed"],
                pass_fds=(listener.fileno(),), env=environment, cwd=temporary,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        try:
            if mode != "startup_failure":
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        stdout, stderr = process.communicate(timeout=5)
                        pytest.fail(f"Isolated fixture exited before readiness: {stdout}\n{stderr}")
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=0.2) as response:
                            assert response.status == 200
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    pytest.fail("Isolated fixture did not become ready")
                if signum is not None:
                    process.send_signal(signum)
            stdout, stderr = process.communicate(timeout=10)
            logs = sorted(events_dir.glob("*.jsonl")) if events_dir.is_dir() else []
            events = [json.loads(line) for path in logs for line in path.read_text().splitlines()]
            assert CANARY not in json.dumps(events)
            for event in events:
                assert event["pid"] == process.pid
                assert event["ppid"] == os.getpid()
                assert datetime.fromisoformat(event["timestamp"]).utcoffset().total_seconds() == 0
            return process.returncode, stdout, stderr, events
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
            assert process.poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited socket and signal fixture")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_real_signals_preserve_uvicorn_exit_and_emit_events(tmp_path, signum):
    baseline_code, _, baseline_stderr, _ = run_server(tmp_path, signum=signum, baseline=True)
    code, stdout, stderr, events = run_server(tmp_path, signum=signum)
    assert code == baseline_code
    assert "Application shutdown complete" in baseline_stderr
    assert "Application shutdown complete" in stderr
    assert "handlers-restored" in stdout
    assert [event["event"] for event in events] == [
        "api.startup.begin", "api.startup.complete", "api.signal.received",
        "api.shutdown.begin", "api.shutdown.complete",
    ]
    assert events[2]["signal"] == signum.name
    assert events[2]["signal_number"] == int(signum)
    assert events[2]["sender_pid"] == "unknown"
    assert events[2]["startup_process"]["ppid"] == os.getpid()
    assert events[2]["startup_process"]["pgid"] == os.getpgrp()
    assert events[2]["signal_process"]["ppid"] == os.getpid()
    assert events[2]["signal_process"]["pgid"] == os.getpgrp()
    assert all(event["startup_process"] == events[0]["startup_process"] for event in events)


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited socket fixture")
def test_normal_shutdown_has_no_invented_signal(tmp_path):
    code, stdout, stderr, events = run_server(tmp_path, mode="normal")
    assert code == 0
    assert "handlers-restored" in stdout
    assert "Application shutdown complete" in stderr
    assert [event["event"] for event in events] == [
        "api.startup.begin", "api.startup.complete", "api.shutdown.begin", "api.shutdown.complete",
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited socket fixture")
@pytest.mark.parametrize("phase", ["startup", "shutdown"])
def test_real_lifespan_failure_records_only_exception_class(tmp_path, phase):
    _, stdout, _, events = run_server(tmp_path, mode=f"{phase}_failure")
    assert "handlers-restored" in stdout
    assert events[-1]["event"] == "api.lifecycle.failed"
    assert events[-1]["phase"] == phase
    assert events[-1]["exception_type"] == "RuntimeError"
    assert not any(event["event"] == f"api.{phase}.complete" for event in events)


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited socket fixture")
@pytest.mark.parametrize("signum", [None, signal.SIGTERM])
def test_real_broken_sink_keeps_startup_and_shutdown_working(tmp_path, signum):
    code, stdout, stderr, events = run_server(
        tmp_path, mode="normal" if signum is None else "signal", signum=signum, broken_sink=True,
    )
    assert code == (0 if signum is None else -int(signum))
    assert "handlers-restored" in stdout
    assert "Application shutdown complete" in stderr
    assert events == []
