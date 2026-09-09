"""Runtime PID reconciliation with real loopback HTTP and isolated files."""

import os
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

import aiteam
from aiteam.mcp import _autostart as api
from aiteam.mcp import _base
from aiteam.mcp.tools import infra


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    for name, filename in (("_PID_FILE", "api.pid"), ("_PORT_FILE", "port"),
                           ("_STARTUP_LOCK_FILE", "startup.lock")):
        monkeypatch.setattr(api, name, str(tmp_path / filename))
    monkeypatch.setattr(api, "_debug_log", lambda message: None)
    monkeypatch.setattr(api, "_api_process", None)
    monkeypatch.setattr(_base, "_PORT_FILE", str(tmp_path / "port"))
    monkeypatch.delenv("AITEAM_API_URL", raising=False)
    return tmp_path


@pytest.mark.parametrize("is_api", [True, False])
@pytest.mark.parametrize("minimal_path", [True, False])
def test_ensure_reconciles_real_listener_only_with_api_identity(isolated, monkeypatch, is_api, minimal_path):
    import os

    if minimal_path and sys.platform == "darwin":
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
    module = "uvicorn" if is_api else "unrelated_server"
    (isolated / f"{module}.py").write_text(
        "import http.server, json, sys\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers()\n"
        f"  self.wfile.write(json.dumps({{'version': {aiteam.__version__!r}}}).encode())\n"
        " def log_message(self, *args): pass\n"
        "http.server.HTTPServer(('127.0.0.1', int(sys.argv[-1])), Handler).serve_forever()\n"
    )
    port = api._find_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", module, "aiteam.api.app:create_app", "--port", str(port)],
        cwd=isolated, env={**os.environ, "PYTHONPATH": str(isolated)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if api._is_api_healthy_on_port(port, timeout=0.1):
                break
            time.sleep(0.02)
        else:
            pytest.fail("Local HTTP fixture did not start")
        api._save_api_port(port)
        (isolated / "api.pid").write_text("99999999")
        api._ensure_api_running()
        assert (isolated / "api.pid").read_text() == (str(proc.pid) if is_api else "99999999")
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.parametrize("failure", ["multiple", "unknown", "unhealthy", "reused", "rebound", "denied"])
def test_reconcile_ambiguity_never_writes(isolated, monkeypatch, failure):
    (isolated / "api.pid").write_text("111")
    listener = Mock(return_value={222})
    identity = Mock(return_value=1.0)
    monkeypatch.setattr(api, "_listener_pids", listener)
    monkeypatch.setattr(api, "_api_process_identity", identity)
    monkeypatch.setattr(api, "_is_api_healthy_on_port", lambda *a, **kw: failure != "unhealthy")
    if failure == "multiple":
        listener.return_value = {222, 333}
    elif failure == "unknown":
        identity.return_value = None
    elif failure == "reused":
        identity.side_effect = [1.0, 2.0]
    elif failure == "rebound":
        listener.side_effect = [{222}, {333}]
    elif failure == "denied":
        monkeypatch.setattr(api, "_write_pid_file", Mock(side_effect=PermissionError))
    assert api._reconcile_api_pid(8000) is None
    assert (isolated / "api.pid").read_text() == "111"
    assert not (isolated / "startup.lock").exists()


def test_existing_lock_prevents_reconcile(isolated, monkeypatch):
    (isolated / "startup.lock").write_text("123")
    listener = Mock()
    monkeypatch.setattr(api, "_listener_pids", listener)
    assert api._reconcile_api_pid(8000) is None
    listener.assert_not_called()


def test_cleanup_reaps_own_child_but_preserves_successor(isolated, monkeypatch):
    proc = Mock(pid=111)
    proc.poll.return_value = 0
    monkeypatch.setattr(api, "_api_process", proc)
    (isolated / "api.pid").write_text("222")
    api._cleanup_api()
    proc.poll.assert_called_once()
    proc.terminate.assert_not_called()
    assert (isolated / "api.pid").read_text() == "222"


@pytest.mark.parametrize("value", ["0", "-1", "bad", "222"])
def test_read_rejects_invalid_or_reused_pid(isolated, monkeypatch, value):
    (isolated / "api.pid").write_text(value)
    monkeypatch.setattr(api, "_api_process_identity", lambda pid: time.time() + 10)
    assert api._read_pid_file() is None


def test_missing_psutil_preserves_health_reuse(isolated, monkeypatch):
    monkeypatch.setattr(api, "psutil", None)
    monkeypatch.setattr(api, "_get_running_api_version_on_port", lambda *a, **kw: aiteam.__version__)
    api._ensure_api_running()
    assert not (isolated / "api.pid").exists()


def test_lsof_fallback_filters_nonlocal_listener(isolated, monkeypatch):
    monkeypatch.setattr(api.sys, "platform", "darwin")
    monkeypatch.setattr(api.psutil, "net_connections", Mock(side_effect=api.psutil.AccessDenied))
    run = Mock(return_value=Mock(returncode=0, stdout="p111\nn127.0.0.1:8000\np222\nn10.0.0.1:8000\n"))
    monkeypatch.setattr(api.subprocess, "run", run)
    assert api._listener_pids(8000) == {111}
    assert "-sTCP:LISTEN" in run.call_args.args[0]
    assert run.call_args.args[0][0] == "/usr/sbin/lsof"


def test_identity_rejects_zombie_and_permission_denied(isolated, monkeypatch):
    process = Mock()
    process.status.return_value = api.psutil.STATUS_ZOMBIE
    monkeypatch.setattr(api.psutil, "Process", Mock(return_value=process))
    assert api._api_process_identity(222) is None
    process.status.side_effect = api.psutil.AccessDenied
    assert api._api_process_identity(222) is None


def test_atomic_write_failure_keeps_previous_record(isolated, monkeypatch):
    (isolated / "api.pid").write_text("111")
    monkeypatch.setattr(api.os, "replace", Mock(side_effect=PermissionError))
    with pytest.raises(PermissionError):
        api._write_pid_file(222)
    assert (isolated / "api.pid").read_text() == "111"
    assert not list(isolated.glob(".aiteam-pid-*"))


def test_cleanup_preserves_reused_live_pid(isolated, monkeypatch):
    proc = Mock(pid=111)
    proc.poll.return_value = 0
    monkeypatch.setattr(api, "_api_process", proc)
    (isolated / "api.pid").write_text("111")
    process = Mock()
    process.status.return_value = api.psutil.STATUS_RUNNING
    monkeypatch.setattr(api.psutil, "Process", Mock(return_value=process))
    api._cleanup_api()
    assert (isolated / "api.pid").read_text() == "111"


def test_explicit_remote_url_never_claims_local_service(isolated, monkeypatch):
    monkeypatch.setenv("AITEAM_API_URL", "https://example.invalid")
    reconcile = Mock()
    monkeypatch.setattr(api, "_reconcile_api_pid", reconcile)
    api._ensure_api_running()
    reconcile.assert_not_called()


def test_write_obeys_startup_lock(isolated):
    (isolated / "api.pid").write_text("111")
    (isolated / "startup.lock").write_text("222")
    with pytest.raises(OSError, match="lock is busy"):
        api._write_pid_file(333)
    assert (isolated / "api.pid").read_text() == "111"


@pytest.mark.parametrize("managed", [True, False])
def test_health_entry_only_reconciles_managed_listener(isolated, monkeypatch, managed):
    import os

    tools = {}
    registrar = Mock()
    registrar.tool.side_effect = lambda **kwargs: lambda fn: tools.setdefault(fn.__name__, fn)
    infra.register(registrar)
    (isolated / "uvicorn.py").write_text(
        "import http.server, json, sys\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers()\n"
        f"  self.wfile.write(json.dumps({{'version': {aiteam.__version__!r}, 'total': 0}}).encode())\n"
        " def log_message(self, *args): pass\n"
        "http.server.HTTPServer(('127.0.0.1', int(sys.argv[-1])), Handler).serve_forever()\n"
    )
    port = api._find_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--port", str(port)],
        cwd=isolated, env={**os.environ, "PYTHONPATH": str(isolated)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if api._is_api_healthy_on_port(port, timeout=0.1):
                break
            time.sleep(0.02)
        else:
            pytest.fail("Local HTTP fixture did not start")
        saved_port = port if managed else api._find_free_port()
        api._save_api_port(saved_port)
        (isolated / "api.pid").write_text("99999999")
        url = f"http://localhost:{port}"
        if not managed:
            monkeypatch.setenv("AITEAM_API_URL", url)
        monkeypatch.setattr(infra, "_usage_coverage_line", lambda: "no data")
        for _ in range(2):
            result = tools["os_health_check"]()
            assert result["status"] == "healthy"
            assert result["api_url"] == url
            assert (isolated / "api.pid").read_text() == (str(proc.pid) if managed else "99999999")
            assert api._get_api_port() == saved_port
        assert result["pid_reconciliation"]["status"] == ("verified" if managed else "not_managed")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.parametrize("lock_held", [False, True])
@pytest.mark.parametrize("change_during_check", [False, True])
def test_reconcile_rechecks_managed_port(isolated, monkeypatch, lock_held, change_during_check):
    (isolated / "api.pid").write_text("111")
    api._save_api_port(8000 if change_during_check else 8765)
    monkeypatch.setattr(api, "_listener_pids", Mock(return_value={222}))
    monkeypatch.setattr(api, "_api_process_identity", Mock(return_value=1.0))

    def healthy(*args, **kwargs):
        if change_during_check:
            api._save_api_port(8765)
        return True

    monkeypatch.setattr(api, "_is_api_healthy_on_port", healthy)
    assert api._reconcile_api_pid(8000, lock_held=lock_held) is None
    assert (isolated / "api.pid").read_text() == "111"
    assert api._get_api_port() == 8765


@pytest.mark.parametrize("alive", [True, False])
def test_missing_psutil_recovers_only_dead_lock_owner(isolated, monkeypatch, alive):
    monkeypatch.setattr(api, "psutil", None)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        if not alive:
            proc.terminate()
            proc.wait(timeout=5)
        lock = isolated / "startup.lock"
        lock.write_text(str(proc.pid))
        old = time.time() - api._STARTUP_LOCK_MAX_AGE - 5
        os.utime(lock, (old, old))
        fd = api._acquire_startup_lock()
        if alive:
            assert fd is None
            assert lock.read_text() == str(proc.pid)
        else:
            assert fd is not None
            api._release_startup_lock(fd)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ps fallback")
@pytest.mark.parametrize("is_api", [True, False])
def test_missing_psutil_verifies_real_recorded_process(isolated, monkeypatch, is_api):
    monkeypatch.setattr(api, "psutil", None)
    module = "uvicorn" if is_api else "other"
    (isolated / f"{module}.py").write_text(
        "import pathlib, time; pathlib.Path('ready').touch(); time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", module, "aiteam.api.app:create_app"],
        cwd=isolated, env={**os.environ, "PYTHONPATH": str(isolated)},
    )
    try:
        for _ in range(100):
            if (isolated / "ready").exists():
                break
            time.sleep(0.01)
        else:
            pytest.fail("Process fixture did not become ready")
        (isolated / "api.pid").write_text(str(proc.pid))
        assert api._read_pid_file() == (proc.pid if is_api else None), (
            subprocess.run(["/bin/ps", "-p", str(proc.pid), "-o", "uid=,stat=,lstart=,command="],
                           capture_output=True, text=True).stdout,
            api._api_process_identity(proc.pid), (isolated / "api.pid").stat().st_mtime,
        )
        assert proc.poll() is None
        assert not api._pid_is_aiteam_api(proc.pid)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_unknown_recorded_process_blocks_duplicate_start(isolated, monkeypatch):
    monkeypatch.setattr(api, "psutil", None)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (isolated / "api.pid").write_text(str(proc.pid))
        monkeypatch.setattr(api, "_api_process_identity", lambda pid: None)
        spawn = Mock()
        real_popen = subprocess.Popen

        def launch(args, **kwargs):
            if args[1:3] == ["-m", "uvicorn"]:
                return spawn(args, **kwargs)
            return real_popen(args, **kwargs)

        monkeypatch.setattr(api.subprocess, "Popen", launch)
        monkeypatch.setattr(api, "_is_port_open", lambda **kw: False)
        monkeypatch.setattr(api, "_is_api_healthy_on_port", lambda *a, **kw: True)
        monkeypatch.setattr(api.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(api, "_write_pid_file", Mock())
        monkeypatch.setattr(api, "_save_api_port", Mock())
        monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
        monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
        api._ensure_api_running_locked(aiteam.__version__)
        spawn.assert_not_called()
        assert (isolated / "api.pid").read_text() == str(proc.pid)
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="Requires a real unreaped POSIX child")
@pytest.mark.parametrize("missing_psutil", [True, False])
def test_zombie_pid_and_lock_do_not_block_start(isolated, monkeypatch, missing_psutil):
    if missing_psutil:
        monkeypatch.setattr(api, "psutil", None)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        for _ in range(100):
            status = subprocess.run(
                ["/bin/ps", "-p", str(proc.pid), "-o", "stat="],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            if status.startswith("Z"):
                break
            time.sleep(0.01)
        else:
            pytest.fail("Child did not reach zombie state")
        (isolated / "api.pid").write_text(str(proc.pid))
        lock = isolated / "startup.lock"
        lock.write_text(str(proc.pid))
        old = time.time() - api._STARTUP_LOCK_MAX_AGE - 5
        os.utime(lock, (old, old))
        fd = api._acquire_startup_lock()
        assert fd is not None
        try:
            monkeypatch.setattr(api, "_DEFAULT_PORT", api._find_free_port())
            api._save_api_port(api._DEFAULT_PORT)
            monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
            monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
            spawn = Mock(side_effect=OSError("isolated spawn boundary"))
            real_popen = subprocess.Popen

            def launch(args, **kwargs):
                if args[1:3] == ["-m", "uvicorn"]:
                    return spawn(args, **kwargs)
                return real_popen(args, **kwargs)

            monkeypatch.setattr(api.subprocess, "Popen", launch)
            api._ensure_api_running_locked(aiteam.__version__)
            spawn.assert_called_once()
        finally:
            api._release_startup_lock(fd)
    finally:
        proc.wait(timeout=5)


@pytest.mark.parametrize("occupant", ["old_api", "unknown", "free"])
def test_missing_psutil_and_pid_never_duplicate_occupied_service(isolated, monkeypatch, occupant):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if occupant == "old_api" else 404)
            self.end_headers()
            self.wfile.write(b'{"version":"0.0.0"}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if occupant == "free":
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    try:
        api._save_api_port(port)
        monkeypatch.setattr(api, "psutil", None)
        monkeypatch.setattr(api, "_DEFAULT_PORT", api._find_free_port())
        monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
        monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
        monkeypatch.setattr(api.time, "sleep", lambda seconds: None)
        kill = Mock()
        spawn = Mock(side_effect=OSError("isolated spawn boundary"))
        monkeypatch.setattr(api, "_kill_port_occupant", kill)
        monkeypatch.setattr(api.subprocess, "Popen", spawn)
        api._ensure_api_running()
        if occupant == "free":
            spawn.assert_called_once()
        else:
            spawn.assert_not_called()
        kill.assert_not_called()
        assert api._get_api_port() == port
        assert not (isolated / "api.pid").exists()
    finally:
        if occupant != "free":
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_missing_psutil_starts_and_reuses_free_loopback_api(isolated, monkeypatch):
    (isolated / "uvicorn.py").write_text(
        "import http.server, json, sys\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers()\n"
        f"  self.wfile.write(json.dumps({{'version': {aiteam.__version__!r}}}).encode())\n"
        " def log_message(self, *args): pass\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(isolated))
    monkeypatch.setattr(api, "psutil", None)
    port = api._find_free_port()
    monkeypatch.setattr(api, "_DEFAULT_PORT", port)
    api._save_api_port(port)
    monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
    monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
    try:
        api._ensure_api_running()
        proc = api._api_process
        assert proc is not None
        assert proc.poll() is None
        assert api._is_api_healthy_on_port(port)
        assert (isolated / "api.pid").read_text() == str(proc.pid)
        api._ensure_api_running()
        assert api._api_process is proc
        assert api._get_api_port() == port
        assert not (isolated / "startup.lock").exists()
    finally:
        if api._api_process is not None:
            api._api_process.terminate()
            api._api_process.wait(timeout=5)
