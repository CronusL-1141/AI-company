"""Runtime PID reconciliation with real loopback HTTP and isolated files."""

import os
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import psutil
import pytest

import aiteam
from aiteam.mcp import _autostart as api
from aiteam.mcp import _base
from aiteam.mcp.tools import infra

# Loopback stand-in for the API: answers every GET with the current version
# (FAKE_API_VERSION overrides it; FAKE_API_IGNORE_SIGTERM makes it ignore SIGTERM).
# It listens on the port after --port or --api-port (uvicorn / aiteam up flags).
_SERVE_ON_PORT_FLAG = (
    "import http.server, json, os, signal, sys\n"
    "if os.environ.get('FAKE_API_IGNORE_SIGTERM'):\n"
    " signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    f"version = os.environ.get('FAKE_API_VERSION', {aiteam.__version__!r})\n"
    "class Handler(http.server.BaseHTTPRequestHandler):\n"
    " def do_GET(self):\n"
    "  self.send_response(200); self.end_headers()\n"
    "  self.wfile.write(json.dumps({'version': version, 'total': 0}).encode())\n"
    " def log_message(self, *args): pass\n"
    "flag = '--port' if '--port' in sys.argv else '--api-port'\n"
    "port = int(sys.argv[sys.argv.index(flag) + 1])\n"
    "http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()\n"
)


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


@pytest.mark.parametrize("cause", ["access_denied", "ps_unavailable"])
def test_unknown_recorded_process_blocks_duplicate_start(isolated, monkeypatch, cause):
    """A live recorded PID that cannot be inspected keeps the runtime as it is."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (isolated / "api.pid").write_text(str(proc.pid))
        if cause == "access_denied":
            real_process = api.psutil.Process

            def inspect(pid):
                if pid == proc.pid:
                    raise api.psutil.AccessDenied(pid)
                return real_process(pid)

            monkeypatch.setattr(api.psutil, "Process", inspect)
        else:
            if os.name != "posix":
                pytest.skip("POSIX ps fallback")
            monkeypatch.setattr(api, "psutil", None)
            real_run = subprocess.run

            def run(args, **kwargs):
                if args[0] == "/bin/ps":
                    raise OSError("ps unavailable")
                return real_run(args, **kwargs)

            monkeypatch.setattr(api.subprocess, "run", run)
        assert api._classify_api_process(proc.pid) == (api._UNKNOWN, None)
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


@pytest.mark.parametrize("missing_psutil", [False, True])
def test_reused_recorded_pid_is_cleared_and_api_starts(isolated, monkeypatch, missing_psutil):
    """The ledger names a live process that is no API: start one and overwrite the record."""
    if missing_psutil and os.name != "posix":
        pytest.skip("POSIX ps fallback")
    if "aiteam" in sys.executable:
        pytest.skip("The ps fallback treats commands naming aiteam as uncertain")
    (isolated / "uvicorn.py").write_text(_SERVE_ON_PORT_FLAG)
    monkeypatch.setenv("PYTHONPATH", str(isolated))
    if missing_psutil:
        monkeypatch.setattr(api, "psutil", None)
    port = api._find_free_port()
    monkeypatch.setattr(api, "_DEFAULT_PORT", port)
    api._save_api_port(port)
    monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
    monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (isolated / "api.pid").write_text(str(sleeper.pid))
        assert api._classify_api_process(sleeper.pid) == (api._NOT_OURS, None)
        api._ensure_api_running()
        proc = api._api_process
        assert proc is not None
        assert proc.poll() is None
        assert api._is_api_healthy_on_port(port)
        assert (isolated / "api.pid").read_text() == str(proc.pid)
        assert sleeper.poll() is None
    finally:
        if api._api_process is not None:
            api._api_process.terminate()
            api._api_process.wait(timeout=5)
        sleeper.terminate()
        sleeper.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="Needs POSIX permissions")
def test_unremovable_stale_record_blocks_start(isolated, monkeypatch):
    """A foreign record we cannot delete could not be replaced after a spawn either."""
    ledger = isolated / "ledger"
    ledger.mkdir()
    pid_file = ledger / "api.pid"
    monkeypatch.setattr(api, "_PID_FILE", str(pid_file))
    monkeypatch.setattr(api, "_DEFAULT_PORT", api._find_free_port())
    api._save_api_port(api._DEFAULT_PORT)
    monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
    monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pid_file.write_text(str(sleeper.pid))
        ledger.chmod(0o555)
        spawn = Mock(side_effect=OSError("isolated spawn boundary"))
        real_popen = subprocess.Popen
        monkeypatch.setattr(api.subprocess, "Popen", lambda args, **kw: (
            spawn(args, **kw) if "--factory" in args else real_popen(args, **kw)))
        api._ensure_api_running_locked(aiteam.__version__)
        spawn.assert_not_called()
        assert pid_file.read_text() == str(sleeper.pid)
    finally:
        ledger.chmod(0o755)
        sleeper.terminate()
        sleeper.wait(timeout=5)


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


@pytest.mark.parametrize(("args", "expected"), [
    (["python", "-m", "uvicorn", "aiteam.api.app:create_app", "--port", "8000"], True),
    (["python", "-m", "uvicorn", "--reload", "aiteam.api.app:create_app"], True),
    (["python", "-m", "uvicorn", "aiteam.api.app:create_app", "--workers", "2"], True),
    (["uvicorn", "aiteam.api.app:create_app", "--factory"], True),
    (["/usr/bin/python3", "/usr/local/bin/uvicorn", "--workers", "2", "aiteam.api.app:create_app"], True),
    (["uvicorn.exe", "aiteam.api.app:create_app"], True),
    (["/usr/bin/python3", "/usr/local/bin/aiteam", "up", "--api-port", "8000"], True),
    (["/usr/bin/python3", "/usr/local/bin/aiteam", "up", "--reload"], True),
    (["aiteam", "up"], True),
    (["python.exe", "aiteam.exe", "up"], True),
    (["python", "aiteam-script.py", "up"], True),
    (["python", "-m", "aiteam.cli.app", "up"], True),
    (["python", "-m", "uvicorn", "other.app:create_app"], False),
    (["python", "-m", "unrelated_server", "aiteam.api.app:create_app"], False),
    (["/usr/bin/python3", "/usr/local/bin/aiteam", "status"], False),
    (["/usr/bin/python3", "/usr/local/bin/aiteam"], False),
    (["python", "-m", "aiteam.mcp.entry"], False),
    (["python", "-c", "from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=5)",
      "--multiprocessing-fork"], False),
    (["vim", "aiteam.api.app:create_app"], False),
    (["python", "-c", "import time; time.sleep(30)"], False),
    (["python"], False),
    ([], False),
], ids=lambda value: (" ".join(value)[:60] or "empty") if isinstance(value, list) else None)
def test_api_command_recognizes_product_launch_forms(args, expected):
    assert api._is_api_command(args) is expected


@pytest.mark.parametrize("missing_psutil", [False, True])
def test_classify_real_processes(isolated, monkeypatch, missing_psutil):
    """Our API form is ours with its birth time; a live or exited other process is not ours."""
    if missing_psutil:
        if os.name != "posix":
            pytest.skip("POSIX ps fallback")
        monkeypatch.setattr(api, "psutil", None)
    if "aiteam" in sys.executable:
        pytest.skip("The ps fallback treats commands naming aiteam as uncertain")
    (isolated / "uvicorn.py").write_text(
        "import pathlib, time; pathlib.Path('ready').touch(); time.sleep(30)\n"
    )
    ours = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--reload"],
        cwd=isolated, env={**os.environ, "PYTHONPATH": str(isolated)},
    )
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait(timeout=10)
    try:
        for _ in range(500):
            if (isolated / "ready").exists():
                break
            time.sleep(0.01)
        else:
            pytest.fail("Process fixture did not become ready")
        verdict, created = api._classify_api_process(ours.pid)
        assert verdict == api._OURS
        assert abs(created - psutil.Process(ours.pid).create_time()) < 2
        assert api._classify_api_process(other.pid) == (api._NOT_OURS, None)
        assert api._classify_api_process(exited.pid) == (api._NOT_OURS, None)
    finally:
        for proc in (ours, other):
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.parametrize(("case", "expected"), [
    ("other_owner", "not_ours"),
    ("zombie", "not_ours"),
    ("vanished", "not_ours"),
    ("access_denied", "unknown"),
    ("empty_command", "unknown"),
])
def test_classify_with_psutil(monkeypatch, case, expected):
    process = Mock()
    process.status.return_value = api.psutil.STATUS_RUNNING
    process.uids.return_value = Mock(real=os.getuid() if hasattr(os, "getuid") else 0)
    process.cmdline.return_value = ["python", "-c", "pass"]
    if case == "other_owner":
        if not hasattr(os, "getuid"):
            pytest.skip("POSIX owner check")
        process.uids.return_value = Mock(real=os.getuid() + 1)
        process.cmdline.return_value = ["python", "-m", "uvicorn", "aiteam.api.app:create_app"]
    elif case == "zombie":
        process.status.return_value = api.psutil.STATUS_ZOMBIE
    elif case == "vanished":
        process.cmdline.side_effect = api.psutil.NoSuchProcess(222)
    elif case == "access_denied":
        process.cmdline.side_effect = api.psutil.AccessDenied(222)
    elif case == "empty_command":
        process.cmdline.return_value = []
    monkeypatch.setattr(api.psutil, "Process", Mock(return_value=process))
    assert api._classify_api_process(222) == (expected, None)
    assert api._api_process_identity(222) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX ps fallback")
@pytest.mark.parametrize(("case", "expected"), [
    ("other_owner", "not_ours"),
    ("zombie", "not_ours"),
    ("foreign_command", "not_ours"),
    ("spaced_interpreter", "unknown"),
    ("unbalanced_quote", "unknown"),
    ("truncated", "unknown"),
    ("ps_failed", "unknown"),
])
def test_classify_with_ps_fallback(monkeypatch, case, expected):
    monkeypatch.setattr(api, "psutil", None)
    monkeypatch.setattr(api, "_process_exists", lambda pid: True)
    uid, stat, command = os.getuid(), "S", "/usr/bin/python3 -c import time; time.sleep(30)"
    if case == "other_owner":
        uid, command = os.getuid() + 1, "/usr/bin/python3 -m uvicorn aiteam.api.app:create_app"
    elif case == "zombie":
        stat = "Z"
    elif case == "spaced_interpreter":
        # argv[0] "/opt/my env/python" is indistinguishable from two arguments.
        command = "/opt/my env/python -m uvicorn aiteam.api.app:create_app"
    elif case == "unbalanced_quote":
        command = "/opt/it's/python -m uvicorn aiteam.api.app:create_app"
    line = f"{uid} {stat} Mon Sep 28 10:00:00 2026 {command}\n"
    if case == "truncated":
        line = f"{uid} {stat}\n"
    run = Mock(return_value=Mock(stdout=line))
    if case == "ps_failed":
        run.side_effect = subprocess.CalledProcessError(1, "/bin/ps")
    monkeypatch.setattr(api.subprocess, "run", run)
    assert api._classify_api_process(222) == (expected, None)


def test_classify_ps_fallback_reads_our_birth_time(monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX ps fallback")
    monkeypatch.setattr(api, "psutil", None)
    monkeypatch.setattr(api, "_process_exists", lambda pid: True)
    line = f"{os.getuid()} S Mon Sep 28 10:00:00 2026 /usr/bin/python3 /usr/local/bin/aiteam up\n"
    monkeypatch.setattr(api.subprocess, "run", Mock(return_value=Mock(stdout=line)))
    verdict, created = api._classify_api_process(222)
    assert verdict == api._OURS
    assert created == time.mktime(time.strptime("Mon Sep 28 10:00:00 2026", "%a %b %d %H:%M:%S %Y"))


def test_reconcile_rejects_listener_outside_api_family(isolated, monkeypatch):
    (isolated / "api.pid").write_text("111")
    monkeypatch.setattr(api, "_listener_pids", Mock(return_value={222, 333}))
    monkeypatch.setattr(api, "_api_process_identity", lambda pid: 1.0 if pid == 222 else None)
    monkeypatch.setattr(api, "_is_api_healthy_on_port", lambda *a, **kw: True)
    parents = {333: 999}
    monkeypatch.setattr(api.psutil, "Process", lambda pid: Mock(ppid=Mock(return_value=parents[pid])))
    assert api._reconcile_api_pid(8000) is None
    assert (isolated / "api.pid").read_text() == "111"
    parents[333] = 222
    assert api._reconcile_api_pid(8000) == 222
    assert (isolated / "api.pid").read_text() == "222"


# A minimal ASGI stand-in for aiteam.api.app so real uvicorn can run it alone or
# with --reload / --workers (children then hold the listening socket as well).
# Its lifespan shutdown takes FAKE_API_SHUTDOWN_SECONDS, like the API's lease
# release under a held lock, and then leaves a released-<pid> marker.
_FAKE_API_APP = (
    "import asyncio, json, os, pathlib\n"
    f"VERSION = {aiteam.__version__!r}\n"
    "SHUTDOWN_SECONDS = float(os.environ.get('FAKE_API_SHUTDOWN_SECONDS', '0'))\n"
    "def create_app():\n"
    "    async def app(scope, receive, send):\n"
    "        if scope['type'] == 'lifespan':\n"
    "            while True:\n"
    "                message = await receive()\n"
    "                if message['type'] == 'lifespan.startup':\n"
    "                    await send({'type': 'lifespan.startup.complete'})\n"
    "                elif message['type'] == 'lifespan.shutdown':\n"
    "                    await asyncio.sleep(SHUTDOWN_SECONDS)\n"
    "                    pathlib.Path(f'released-{os.getpid()}').touch()\n"
    "                    await send({'type': 'lifespan.shutdown.complete'})\n"
    "                    return\n"
    "        await send({'type': 'http.response.start', 'status': 200,\n"
    "                    'headers': [(b'content-type', b'application/json')]})\n"
    "        await send({'type': 'http.response.body',\n"
    "                    'body': json.dumps({'version': VERSION, 'total': 0}).encode()})\n"
    "    return app\n"
)
_LISTENERS = {"aiteam_up": 1, "single": 1, "reload": 2, "workers": 3}


def _start_api_form(root, form: str, shutdown_seconds: float = 0) -> tuple[subprocess.Popen, int]:
    """Start a real process tree in *form* and wait until every listener is up."""
    port = api._find_free_port()
    env = {**os.environ, "PYTHONPATH": str(root), "FAKE_API_SHUTDOWN_SECONDS": str(shutdown_seconds)}
    if form == "aiteam_up":
        (root / "aiteam").write_text(_SERVE_ON_PORT_FLAG)
        args = [sys.executable, str(root / "aiteam"), "up", "--api-port", str(port)]
    else:
        package = root / "aiteam" / "api"
        package.mkdir(parents=True)
        (root / "aiteam" / "__init__.py").write_text("")
        (package / "__init__.py").write_text("")
        (package / "app.py").write_text(_FAKE_API_APP)
        (root / "watch").mkdir()
        extra = {"single": [], "reload": ["--reload", "--reload-dir", str(root / "watch")],
                 "workers": ["--workers", "2"]}[form]
        args = [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
                "--host", "127.0.0.1", "--port", str(port), *extra]
    proc = subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(300):
        if (api._is_api_healthy_on_port(port, timeout=0.2)
                and len(api._listener_pids(port)) == _LISTENERS[form]):
            return proc, port
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    proc.kill()
    proc.wait(timeout=5)
    pytest.fail(f"{form} fixture did not start ({len(api._listener_pids(port))} listeners)")


def _stop_api_form(proc: subprocess.Popen, family: list) -> None:
    """Stop the root and any listener captured earlier; psutil refuses reused PIDs."""
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    for member in family:
        try:
            member.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(family, timeout=10)
    for member in alive:
        try:
            member.kill()
        except psutil.NoSuchProcess:
            pass


def _api_tree(proc: subprocess.Popen) -> list:
    """Handles for the root and its multiprocessing workers, the root first."""
    root = psutil.Process(proc.pid)
    return [root, *(child for child in root.children()
                    if "--multiprocessing-fork" in child.cmdline())]


def _released(root, family: list) -> set[int]:
    """PIDs whose lifespan shutdown ran to the end (the lease release stand-in)."""
    return {member.pid for member in family if (root / f"released-{member.pid}").exists()}


def _port_freed(port: int) -> bool:
    for _ in range(100):
        if not api._listener_pids(port):
            return True
        time.sleep(0.1)
    return False


def _capture_events(monkeypatch) -> list[dict]:
    events: list[dict] = []
    real = api._record_event
    monkeypatch.setattr(api, "_record_event", lambda event, **fields: (
        events.append({"event": event, **fields}), real(event, **fields)))
    return events


@pytest.mark.skipif(os.name != "posix", reason="POSIX process trees")
@pytest.mark.parametrize("form", ["aiteam_up", "reload", "workers"])
def test_health_check_verifies_product_launch_forms(isolated, monkeypatch, form):
    """aiteam up and a multi-process uvicorn are verified and adopted as the API."""
    proc, port = _start_api_form(isolated, form)
    family = _api_tree(proc)
    try:
        tools = {}
        registrar = Mock()
        registrar.tool.side_effect = lambda **kwargs: lambda fn: tools.setdefault(fn.__name__, fn)
        infra.register(registrar)
        monkeypatch.setattr(infra, "_usage_coverage_line", lambda: "no data")
        api._save_api_port(port)
        (isolated / "api.pid").write_text("99999999")
        result = tools["os_health_check"]()
        assert result["status"] == "healthy"
        assert result["pid_reconciliation"] == {"status": "verified", "pid": proc.pid}
        assert (isolated / "api.pid").read_text() == str(proc.pid)
        # os_restart_api reads its old_pid through the same record.
        assert api._read_pid_file() == proc.pid
    finally:
        _stop_api_form(proc, family)


# Lifespan shutdowns that run past the old SIGTERM-to-SIGKILL gap (none on the
# upgrade path, 2s on the stuck path) and stay inside the exit grace.
_SLOW_SHUTDOWN_SECONDS = 3.0


@pytest.mark.skipif(os.name != "posix", reason="POSIX process trees")
@pytest.mark.parametrize("form", ["single", "reload", "workers"])
def test_port_occupant_is_terminated_gently_with_its_workers(isolated, monkeypatch, form):
    """The replaced API finishes its exit (lease release); the whole family is gone."""
    proc, port = _start_api_form(isolated, form, shutdown_seconds=_SLOW_SHUTDOWN_SECONDS)
    family = _api_tree(proc)
    monkeypatch.setattr(api, "_TERMINATE_GRACE_SECONDS", 60.0)
    try:
        api._kill_port_occupant(port)
        # uvicorn replays the SIGTERM it handled once its shutdown is complete.
        assert proc.wait(timeout=5) in (0, -signal.SIGTERM)
        assert _port_freed(port)
        # Workers run the lifespan in multi-process forms, the root in a single one.
        serving = family[1:] or family[:1]
        assert _released(isolated, serving) == {member.pid for member in serving}
        assert not any(member.is_running() for member in family)
    finally:
        _stop_api_form(proc, family)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process trees")
@pytest.mark.parametrize("form", ["single", "workers"])
def test_stuck_api_is_terminated_gently_with_its_workers(isolated, monkeypatch, form):
    """15s without health: the recorded API gets SIGTERM and the exit grace, workers included."""
    proc, port = _start_api_form(isolated, form, shutdown_seconds=_SLOW_SHUTDOWN_SECONDS)
    family = _api_tree(proc)
    real_sleep = time.sleep
    try:
        api._save_api_port(port)
        monkeypatch.setattr(api, "_DEFAULT_PORT", port)
        monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
        monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
        (isolated / "api.pid").write_text(str(proc.pid))
        assert api._read_pid_file() == proc.pid
        monkeypatch.setattr(api, "_is_api_healthy_on_port", lambda *a, **kw: False)
        # Scaled waits: the 15 health polls and the old 2s gap pass at once; the
        # exit grace keeps 30s so a contended runner does not escalate.
        monkeypatch.setattr(api.time, "sleep", lambda seconds: real_sleep(min(seconds, 0.05)))
        monkeypatch.setattr(api, "_TERMINATE_GRACE_SECONDS", 60.0)
        spawn = Mock(side_effect=OSError("isolated spawn boundary"))
        real_popen = subprocess.Popen
        monkeypatch.setattr(api.subprocess, "Popen", lambda args, **kw: (
            spawn(args, **kw) if "--factory" in args else real_popen(args, **kw)))
        api._ensure_api_running_locked(aiteam.__version__)
        assert proc.wait(timeout=5) in (0, -signal.SIGTERM)
        assert _port_freed(port)
        serving = family[1:] or family[:1]
        assert _released(isolated, serving) == {member.pid for member in serving}
        assert not (isolated / "api.pid").exists()
        spawn.assert_called_once()
    finally:
        _stop_api_form(proc, family)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process trees")
def test_termination_escalates_when_sigterm_is_ignored(isolated, monkeypatch):
    (isolated / "uvicorn.py").write_text(_SERVE_ON_PORT_FLAG)
    port = api._find_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--port", str(port)],
        cwd=isolated, env={**os.environ, "PYTHONPATH": str(isolated), "FAKE_API_IGNORE_SIGTERM": "1"},
    )
    try:
        for _ in range(500):
            if api._is_api_healthy_on_port(port, timeout=0.1):
                break
            time.sleep(0.02)
        else:
            pytest.fail("Local HTTP fixture did not start")
        monkeypatch.setattr(api, "_TERMINATE_GRACE_SECONDS", 0.5)
        events = _capture_events(monkeypatch)
        api._kill_port_occupant(port)
        assert proc.wait(timeout=5) == -signal.SIGKILL
        assert [(event["signal"], event["reason"]) for event in events
                if event["event"] == "api.termination.requested"] == [
            ("SIGTERM", "stale_port_occupant"), ("SIGKILL", "stale_port_occupant_escalation"),
        ]
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process trees")
def test_escalation_stops_the_workers_of_a_hung_main(isolated, monkeypatch):
    """A main that cannot stop its workers is killed; its workers, pinned as its
    children even when the listener lookup saw only the main, get their own
    SIGTERM and finish their exit (lease release) before anything is killed."""
    proc, port = _start_api_form(isolated, "workers", shutdown_seconds=0.5)
    family = _api_tree(proc)
    listeners = api._listener_pids
    real_sleep = time.sleep
    try:
        os.kill(proc.pid, signal.SIGSTOP)  # hung: SIGTERM stays pending
        api._save_api_port(port)
        monkeypatch.setattr(api, "_DEFAULT_PORT", port)
        monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
        monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
        (isolated / "api.pid").write_text(str(proc.pid))
        monkeypatch.setattr(api, "_is_api_healthy_on_port", lambda *a, **kw: False)
        monkeypatch.setattr(api, "_listener_pids", lambda port: {proc.pid})
        monkeypatch.setattr(api.time, "sleep", lambda seconds: real_sleep(min(seconds, 0.05)))
        monkeypatch.setattr(api, "_TERMINATE_GRACE_SECONDS", 1.0)
        monkeypatch.setattr(api, "_WORKER_GRACE_SECONDS", 60.0)
        spawn = Mock(side_effect=OSError("isolated spawn boundary"))
        real_popen = subprocess.Popen
        monkeypatch.setattr(api.subprocess, "Popen", lambda args, **kw: (
            spawn(args, **kw) if "--factory" in args else real_popen(args, **kw)))
        events = _capture_events(monkeypatch)
        api._ensure_api_running_locked(aiteam.__version__)
        assert proc.wait(timeout=5) == -signal.SIGKILL
        for _ in range(100):
            if not listeners(port):
                break
            real_sleep(0.05)
        assert listeners(port) == set()
        workers = family[1:]
        assert _released(isolated, workers) == {member.pid for member in workers}
        signals = [(event["target_pid"], event["signal"]) for event in events
                   if event["event"] == "api.termination.requested"]
        assert signals == [(proc.pid, "SIGTERM"), (proc.pid, "SIGKILL"),
                           *((member.pid, "SIGTERM") for member in workers)]
    finally:
        _stop_api_form(proc, family)


def test_signals_go_through_pinned_handles_only(monkeypatch):
    """A handle whose PID was reused refuses to signal (psutil checks the birth
    time); the family termination never falls back to a bare PID."""
    reused = Mock(pid=4242)
    reused.terminate.side_effect = api.psutil.NoSuchProcess(4242, msg="PID has been reused")
    reused.kill.side_effect = api.psutil.NoSuchProcess(4242, msg="PID has been reused")
    reused.is_running.return_value = True
    reused.status.return_value = api.psutil.STATUS_RUNNING

    def bare_kill(pid, signum):
        pytest.fail(f"signalled bare PID {pid}")

    monkeypatch.setattr(api.os, "kill", bare_kill)
    monkeypatch.setattr(api, "_TERMINATE_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)
    api._terminate_api_family([reused, reused], reason="stale_port_occupant", port=1)
    reused.terminate.assert_called_once_with()
    assert reused.kill.call_count == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX process trees")
@pytest.mark.parametrize("form", ["workers", "reload"])
def test_orphaned_workers_are_reported_not_killed(isolated, monkeypatch, form):
    proc, port = _start_api_form(isolated, form)
    family = _api_tree(proc)
    try:
        proc.kill()
        proc.wait(timeout=5)
        workers = family[1:]
        for _ in range(100):
            if api._listener_pids(port) == {member.pid for member in workers}:
                break
            time.sleep(0.05)
        else:
            pytest.fail("Workers did not outlive their main process")
        events = _capture_events(monkeypatch)
        api._kill_port_occupant(port)
        assert events[-1]["event"] == "api.termination.skipped"
        assert events[-1]["reason"] == "orphaned_workers"
        assert all(member.is_running() for member in workers)
    finally:
        _stop_api_form(proc, family)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process trees")
def test_concurrently_replaced_api_is_not_killed(isolated, monkeypatch):
    """Session B saw a stale API; session A replaced it before B's kill: B keeps A's API."""
    (isolated / "uvicorn.py").write_text(_SERVE_ON_PORT_FLAG)
    port = api._find_free_port()
    monkeypatch.setattr(api, "_DEFAULT_PORT", port)
    monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
    monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
    api._save_api_port(port)
    command = [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--port", str(port)]
    env = {**os.environ, "PYTHONPATH": str(isolated)}
    real_popen = subprocess.Popen
    started: list[subprocess.Popen] = []

    def serve(version: str) -> subprocess.Popen:
        proc = real_popen(command, cwd=isolated, env={**env, "FAKE_API_VERSION": version})
        started.append(proc)
        for _ in range(500):
            if api._get_running_api_version_on_port(port, timeout=0.1) == version:
                return proc
            time.sleep(0.02)
        pytest.fail("Local HTTP fixture did not start")

    stale = serve("0.0.0")
    real_record = api._record_event

    def record(event, **fields):
        real_record(event, **fields)
        if event == "api.autostart.restart_requested":
            # Session A replaces the stale API and records its own.
            stale.terminate()
            stale.wait(timeout=5)
            fresh = serve(aiteam.__version__)
            (isolated / "api.pid").write_text(str(fresh.pid))

    monkeypatch.setattr(api, "_record_event", record)
    spawn = Mock(side_effect=OSError("isolated spawn boundary"))
    monkeypatch.setattr(api.subprocess, "Popen", lambda args, **kw: (
        spawn(args, **kw) if "--factory" in args else real_popen(args, **kw)))
    try:
        api._ensure_api_running()
        fresh = started[-1]
        assert fresh is not stale
        assert fresh.poll() is None
        assert (isolated / "api.pid").read_text() == str(fresh.pid)
        spawn.assert_not_called()
    finally:
        for proc in started:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process trees")
def test_default_port_api_already_current_is_adopted_not_duplicated(isolated, monkeypatch):
    """Step 5 finds a healthy API step 2 did not adopt (a concurrent replacement, or a
    transient probe failure). It answers with the current version: adopt it, spawn nothing."""
    (isolated / "uvicorn.py").write_text(_SERVE_ON_PORT_FLAG)
    port = api._find_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--port", str(port)],
        cwd=isolated, env={**os.environ, "PYTHONPATH": str(isolated)},
    )
    try:
        for _ in range(500):
            if api._is_api_healthy_on_port(port, timeout=0.1):
                break
            time.sleep(0.02)
        else:
            pytest.fail("Local HTTP fixture did not start")
        api._save_api_port(api._find_free_port())
        monkeypatch.setattr(api, "_DEFAULT_PORT", port)
        monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
        monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
        spawn = Mock(side_effect=OSError("isolated spawn boundary"))
        real_popen = subprocess.Popen
        monkeypatch.setattr(api.subprocess, "Popen", lambda args, **kw: (
            spawn(args, **kw) if "--factory" in args else real_popen(args, **kw)))
        api._ensure_api_running_locked(aiteam.__version__)
        spawn.assert_not_called()
        assert proc.poll() is None
        assert api._get_api_port() == port
        assert (isolated / "api.pid").read_text() == str(proc.pid)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process trees")
def test_recorded_pid_serving_the_api_port_is_kept(isolated, monkeypatch):
    """An API form the allow-list misses (python -u) still serves the port: keep its record."""
    (isolated / "uvicorn.py").write_text(_SERVE_ON_PORT_FLAG)
    port = api._find_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "uvicorn", "aiteam.api.app:create_app", "--port", str(port)],
        cwd=isolated, env={**os.environ, "PYTHONPATH": str(isolated)},
    )
    try:
        for _ in range(500):
            if api._is_api_healthy_on_port(port, timeout=0.1):
                break
            time.sleep(0.02)
        else:
            pytest.fail("Local HTTP fixture did not start")
        assert api._classify_api_process(proc.pid) == (api._NOT_OURS, None)
        api._save_api_port(port)
        monkeypatch.setattr(api, "_DEFAULT_PORT", port)
        monkeypatch.setattr(api, "_DEBUG_LOG_DIR", str(isolated))
        monkeypatch.setattr(api, "_API_STDERR_LOG", str(isolated / "stderr.log"))
        (isolated / "api.pid").write_text(str(proc.pid))
        spawn = Mock(side_effect=OSError("isolated spawn boundary"))
        real_popen = subprocess.Popen
        monkeypatch.setattr(api.subprocess, "Popen", lambda args, **kw: (
            spawn(args, **kw) if "--factory" in args else real_popen(args, **kw)))
        api._ensure_api_running_locked(aiteam.__version__)
        spawn.assert_not_called()
        assert (isolated / "api.pid").read_text() == str(proc.pid)
        assert proc.poll() is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process trees")
def test_kill_targets_listener_not_connected_client(isolated, monkeypatch):
    """A client connected to the port (listed by fuser and lsof -ti too) is left alone."""
    (isolated / "uvicorn.py").write_text(_SERVE_ON_PORT_FLAG)
    port = api._find_free_port()
    # Started first so its PID usually sorts before the server's, as the old
    # first-line pick needed; the listener lookup does not depend on order.
    client = subprocess.Popen([sys.executable, "-c", (
        "import pathlib, socket, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "while not (root / 'go').exists(): time.sleep(0.01)\n"
        "held = socket.create_connection(('127.0.0.1', int(sys.argv[2])))\n"
        "(root / 'connected').touch()\n"
        "time.sleep(30)\n"), str(isolated), str(port)])
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--port", str(port)],
        cwd=isolated, env={**os.environ, "PYTHONPATH": str(isolated)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(500):
            if api._is_api_healthy_on_port(port, timeout=0.1):
                break
            time.sleep(0.02)
        else:
            pytest.fail("Local HTTP fixture did not start")
        (isolated / "go").touch()
        for _ in range(500):
            if (isolated / "connected").exists():
                break
            time.sleep(0.01)
        else:
            pytest.fail("Client did not connect")
        api._kill_port_occupant(port)
        assert server.wait(timeout=5) == -signal.SIGTERM
        assert client.poll() is None
    finally:
        for proc in (server, client):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
