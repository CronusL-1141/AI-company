"""Development restart entry-point checks, isolated from the shared service."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aiteam.mcp import _autostart
from aiteam.mcp.tools import infra


class Capture:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


capture = Capture()
infra.register(capture)
restart = capture.tools["os_restart_api"]
ROOT = Path(__file__).resolve().parents[2]


def test_real_import_dry_run_does_not_touch_service():
    with (
        patch.object(_autostart, "_get_api_port", return_value=8765),
        patch.object(infra, "_restart_local_post") as post,
        patch.object(infra, "_restart_spawn_on_port") as spawn,
        patch.object(_autostart, "_write_pid_file") as write,
    ):
        result = restart(source_root=str(ROOT), dry_run=True)
    assert result["success"], result
    assert result["source_file"] == str(ROOT / "src/aiteam/api/app.py")
    assert result["port"] == 8765
    assert result["command"][0] == result["interpreter"]
    post.assert_not_called()
    spawn.assert_not_called()
    write.assert_not_called()


def test_invalid_source_fails_before_shutdown(tmp_path):
    with patch.object(infra, "_restart_local_post") as post:
        result = restart(source_root=str(tmp_path))
    assert result["error"] == "preflight_failed"
    post.assert_not_called()


def test_import_failure_preserves_service():
    import subprocess

    with (
        patch.object(infra.subprocess, "run", side_effect=subprocess.TimeoutExpired("check", 20)),
        patch.object(infra, "_restart_local_post") as post,
    ):
        result = restart(source_root=str(ROOT))
    assert result["error"] == "preflight_failed"
    post.assert_not_called()


def test_preflight_rejects_import_from_other_checkout():
    completed = MagicMock(stdout='{"source_file": "/tmp/other/src/aiteam/api/app.py"}')
    with (
        patch.object(infra.subprocess, "run", return_value=completed),
        patch.object(infra, "_restart_local_post") as post,
    ):
        result = restart(source_root=str(ROOT))
    assert result["error"] == "preflight_failed"
    post.assert_not_called()


@pytest.mark.parametrize("pid,status", [(123, "verified"), (None, "unverified")])
def test_health_check_reports_actual_reconciliation(pid, status):
    with (
        patch.object(infra, "_api_call", return_value={"success": True, "total": 2}),
        patch.object(infra, "_usage_coverage_line", return_value="no data"),
        patch.object(infra, "API_URL", "http://localhost:8765"),
        patch.object(_autostart, "_get_api_port", return_value=8765),
        patch.object(_autostart, "_reconcile_api_pid", return_value=pid) as reconcile,
    ):
        result = capture.tools["os_health_check"]()
    assert result["pid_reconciliation"] == {"status": status, "pid": pid}
    reconcile.assert_called_once_with(8765)


def test_remote_health_does_not_reconcile_local_pid():
    with (
        patch.object(infra, "_api_call", return_value={"success": True}),
        patch.object(infra, "_usage_coverage_line", return_value="no data"),
        patch.object(infra, "API_URL", "https://example.test:8000"),
        patch.object(_autostart, "_reconcile_api_pid") as reconcile,
    ):
        result = capture.tools["os_health_check"]()
    assert result["pid_reconciliation"]["status"] == "not_local"
    reconcile.assert_not_called()


def test_spawn_bookkeeping_failure_reports_started_child(tmp_path):
    auto = MagicMock(_api_process=None)
    auto._write_pid_file.side_effect = OSError("read-only")
    proc = MagicMock(pid=333)
    with (
        patch.object(_autostart, "_API_STDERR_LOG", str(tmp_path / "stderr.log")),
        patch.object(infra.subprocess, "Popen", return_value=proc),
        patch.object(infra.atexit, "unregister"),
        patch.object(infra.atexit, "register"),
    ):
        result = infra._restart_spawn_on_port(auto, 8765)
    assert result["error"] == "bookkeeping_failed"
    assert result["new_pid"] == 333
    assert auto._api_process is proc


@pytest.mark.parametrize("response_pid,expected", [(222, 222), (True, 111), (0, 111),
                                                   (-1, 111), ("222", 111)])
def test_shutdown_pid_validation_ignores_stale_record(response_pid, expected):
    with (
        patch.object(_autostart, "_get_api_port", return_value=8765),
        patch.object(_autostart, "_read_pid_file", return_value=111),
        patch.object(_autostart, "_is_port_open", return_value=False),
        patch.object(infra, "_restart_local_get", return_value={"version": "1"}),
        patch.object(infra, "_restart_local_post", return_value={"success": True, "pid": response_pid}),
        patch.object(infra, "_restart_pid_alive", return_value=False) as alive,
        patch.object(infra, "_restart_spawn_on_port", return_value={"success": True, "new_pid": 333}),
    ):
        result = restart(force=True)
    assert result["old_pid"] == expected
    alive.assert_called_once_with(expected)


def test_explicit_source_switch_and_restore_use_same_flow():
    for root in ("/tmp/development-repo", "/tmp/original-repo"):
        with (
            patch.object(infra, "_restart_preflight", return_value={
                "success": True, "source_root": root, "source_file": root + "/src/aiteam/api/app.py",
            }),
            patch.object(_autostart, "_read_pid_file", return_value=None),
            patch.object(_autostart, "_is_port_open", return_value=False),
            patch.object(infra, "_restart_local_get", side_effect=[None, {"version": "1"}]),
            patch.object(infra, "_restart_spawn_on_port", return_value={"success": True, "new_pid": 333}) as spawn,
        ):
            result = restart(source_root=root)
        assert result["source_root"] == root
        assert spawn.call_args.kwargs == {"source_root": root}


def test_spawn_preserves_environment_and_updates_bookkeeping(tmp_path, monkeypatch):
    monkeypatch.setenv("AITEAM_DB_PATH", "/tmp/existing-db")
    monkeypatch.setenv("AITEAM_TEST_SENTINEL", "keep")
    previous = MagicMock()
    previous.poll.return_value = 0
    auto = MagicMock(_api_process=previous)
    proc = MagicMock(pid=333)
    with (
        patch.object(_autostart, "_API_STDERR_LOG", str(tmp_path / "stderr.log")),
        patch.object(infra.subprocess, "Popen", return_value=proc) as spawn,
        patch.object(infra.atexit, "unregister") as unregister,
        patch.object(infra.atexit, "register") as register,
    ):
        result = infra._restart_spawn_on_port(auto, 8765, source_root=str(ROOT))
    assert result["success"]
    env = spawn.call_args.kwargs["env"]
    assert env["AITEAM_DB_PATH"] == "/tmp/existing-db"
    assert env["AITEAM_TEST_SENTINEL"] == "keep"
    assert env["PYTHONPATH"] == str(ROOT / "src")
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    auto._write_pid_file.assert_called_once_with(333)
    auto._save_api_port.assert_called_once_with(8765)
    assert auto._api_process is proc
    previous.poll.assert_called_once()
    unregister.assert_called_once_with(auto._cleanup_api)
    register.assert_called_once_with(auto._cleanup_api)


def test_first_restart_after_adoption_registers_exit_cleanup(tmp_path):
    auto = MagicMock(_api_process=None)
    proc = MagicMock(pid=333)
    with (
        patch.object(_autostart, "_API_STDERR_LOG", str(tmp_path / "stderr.log")),
        patch.object(infra.subprocess, "Popen", return_value=proc),
        patch.object(infra.atexit, "unregister") as unregister,
        patch.object(infra.atexit, "register") as register,
    ):
        result = infra._restart_spawn_on_port(auto, 8765)
    assert result["success"]
    assert auto._api_process is proc
    unregister.assert_called_once_with(auto._cleanup_api)
    register.assert_called_once_with(auto._cleanup_api)


@pytest.mark.parametrize("path_kind", ["relative", "absolute", "home"])
def test_source_switch_child_keeps_existing_database(tmp_path, monkeypatch, path_kind):
    import sqlite3
    import sys

    caller = tmp_path / "caller"
    candidate = tmp_path / "candidate"
    caller.mkdir()
    candidate.mkdir()
    (candidate / "src").symlink_to(ROOT / "src", target_is_directory=True)
    monkeypatch.chdir(caller)
    db_path = caller / "state" / "service.db"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE probe (value TEXT)")
        connection.execute("INSERT INTO probe VALUES ('original')")
    override = {
        "relative": "state/service.db",
        "absolute": str(db_path),
        "home": "~/state/service.db",
    }[path_kind]
    monkeypatch.setenv("HOME", str(caller))
    monkeypatch.setenv("AITEAM_DB_PATH", override)
    child_code = (
        "import sqlite3; from aiteam.storage.connection import DEFAULT_DB_URL; "
        "path = DEFAULT_DB_URL.removeprefix('sqlite+aiosqlite:///'); "
        "connection = sqlite3.connect(path); "
        "connection.execute('CREATE TABLE IF NOT EXISTS probe (value TEXT)'); "
        "connection.execute(\"INSERT INTO probe VALUES ('child')\"); "
        "connection.commit(); connection.close()"
    )
    monkeypatch.setattr(infra, "_restart_command", lambda port: [sys.executable, "-c", child_code])
    auto = MagicMock(_api_process=None)
    with (
        patch.object(_autostart, "_API_STDERR_LOG", str(tmp_path / "stderr.log")),
        patch.object(infra.atexit, "unregister"),
        patch.object(infra.atexit, "register"),
    ):
        result = infra._restart_spawn_on_port(auto, 8765, source_root=str(candidate))
        assert result["success"], result
        proc = auto._api_process
        try:
            assert proc.wait(timeout=10) == 0, (tmp_path / "stderr.log").read_text()
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT value FROM probe ORDER BY rowid").fetchall() == [
            ("original",), ("child",),
        ]
    assert not (candidate / "state").exists()


@pytest.mark.parametrize("override", [None, ""])
def test_source_switch_does_not_invent_database_override(tmp_path, monkeypatch, override):
    if override is None:
        monkeypatch.delenv("AITEAM_DB_PATH", raising=False)
    else:
        monkeypatch.setenv("AITEAM_DB_PATH", override)
    auto = MagicMock(_api_process=None)
    with (
        patch.object(_autostart, "_API_STDERR_LOG", str(tmp_path / "stderr.log")),
        patch.object(infra.subprocess, "Popen", return_value=MagicMock(pid=333)) as spawn,
        patch.object(infra.atexit, "unregister"),
        patch.object(infra.atexit, "register"),
    ):
        result = infra._restart_spawn_on_port(auto, 8765, source_root=str(ROOT))
    assert result["success"]
    assert spawn.call_args.kwargs["env"].get("AITEAM_DB_PATH") == override
