"""Lifecycle contracts independent of the shared diagnostics writer."""

from __future__ import annotations

import importlib.util
import signal
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi import FastAPI

SOURCE = Path(__file__).resolve().parents[2] / "src" / "aiteam"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    events = []
    writer = ModuleType("aiteam.diagnostics")
    writer.record_event = lambda event, **fields: events.append({"event": event, **fields})
    writer.process_snapshot = Mock(return_value={"pid": 12345, "ppid": 12300, "pgid": 12200})
    writer.flush_diagnostics = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, "aiteam.diagnostics", writer)
    import aiteam

    monkeypatch.setattr(aiteam, "diagnostics", writer, raising=False)
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_DIR", str(tmp_path))
    module = load_module("lifecycle_under_test", SOURCE / "api/lifecycle_diagnostics.py")
    monkeypatch.setitem(sys.modules, "aiteam.api.lifecycle_diagnostics", module)
    return SimpleNamespace(module=module, writer=writer, events=events)


@pytest.fixture
def signal_handlers(monkeypatch, lifecycle):
    handlers = {signal.SIGTERM: Mock(), signal.SIGINT: Mock()}
    original = handlers.copy()

    def install(signum, handler):
        previous = handlers[signum]
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(lifecycle.module.signal, "getsignal", handlers.__getitem__)
    monkeypatch.setattr(lifecycle.module.signal, "signal", install)
    return handlers, original


def test_signals_delegate_unchanged_and_restore(lifecycle, signal_handlers):
    handlers, original = signal_handlers
    frame = object()
    with lifecycle.module.observe_signals():
        handlers[signal.SIGTERM](signal.SIGTERM, frame)
        handlers[signal.SIGINT](signal.SIGINT, None)
        for signum, handler in original.items():
            handler.assert_called_once_with(signum, frame if signum == signal.SIGTERM else None)
    assert handlers == original
    assert lifecycle.events == [
        {"event": "api.signal.received", "signal": signum.name,
         "signal_number": int(signum), "sender_pid": "unknown",
         "startup_process": lifecycle.writer.process_snapshot.return_value,
         "signal_process": lifecycle.writer.process_snapshot.return_value}
        for signum in (signal.SIGTERM, signal.SIGINT)
    ]


@pytest.mark.parametrize("outcome", ["drained", "timeout", "error"])
def test_exit_drains_once_before_restoring_handlers(lifecycle, signal_handlers, outcome):
    handlers, original = signal_handlers
    handlers_at_flush = []

    def flush(*, timeout):
        handlers_at_flush.append(handlers.copy())
        if outcome == "error":
            raise OSError("private-canary")
        return outcome == "drained"

    lifecycle.writer.flush_diagnostics.side_effect = flush
    with lifecycle.module.observe_signals():
        lifecycle.module.record_lifecycle_event("api.startup.complete")
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        lifecycle.writer.flush_diagnostics.assert_not_called()
    lifecycle.writer.flush_diagnostics.assert_called_once_with(timeout=0.25)
    assert all(handlers_at_flush[0][signum] is not handler for signum, handler in original.items())
    original[signal.SIGTERM].assert_called_once_with(signal.SIGTERM, None)
    assert handlers == original


def test_flush_failure_does_not_mask_original_exception(lifecycle, signal_handlers):
    handlers, original = signal_handlers
    error = RuntimeError("original error")
    lifecycle.writer.flush_diagnostics.side_effect = OSError("private-canary")
    with pytest.raises(RuntimeError) as caught, lifecycle.module.observe_signals():
        raise error
    assert caught.value is error
    assert handlers == original
    lifecycle.writer.flush_diagnostics.assert_called_once_with(timeout=0.25)


def test_interrupt_during_flush_still_restores_handlers(lifecycle, signal_handlers):
    handlers, original = signal_handlers
    lifecycle.writer.flush_diagnostics.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt), lifecycle.module.observe_signals():
        pass
    assert handlers == original
    lifecycle.writer.flush_diagnostics.assert_called_once_with(timeout=0.25)


def test_temporarily_missing_flush_method_keeps_handler_restoration(lifecycle, signal_handlers):
    handlers, original = signal_handlers
    del lifecycle.writer.flush_diagnostics
    with lifecycle.module.observe_signals():
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert handlers == original


def test_repeated_sigint_preserves_uvicorn_force_exit(lifecycle, signal_handlers):
    handlers, _ = signal_handlers
    server = uvicorn.Server(uvicorn.Config(FastAPI()))
    handlers.update({signum: server.handle_exit for signum in handlers})
    with lifecycle.module.observe_signals():
        handlers[signal.SIGINT](signal.SIGINT, None)
        assert server.should_exit and not server.force_exit
        handlers[signal.SIGINT](signal.SIGINT, None)
        assert server.force_exit
    assert handlers[signal.SIGINT] == server.handle_exit
    assert len(lifecycle.events) == 2


def test_writer_failure_does_not_change_signal_behavior(lifecycle, signal_handlers):
    handlers, original = signal_handlers
    lifecycle.writer.record_event = Mock(side_effect=OSError("private-canary"))
    with lifecycle.module.observe_signals():
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    original[signal.SIGTERM].assert_called_once_with(signal.SIGTERM, None)
    assert handlers == original


def test_signal_keeps_startup_ancestry_after_reparenting(lifecycle, signal_handlers):
    handlers, _ = signal_handlers
    startup = lifecycle.writer.process_snapshot.return_value.copy()
    with lifecycle.module.observe_signals():
        lifecycle.writer.process_snapshot.return_value = {"pid": 12345, "ppid": 1, "pgid": 12000}
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert lifecycle.events[-1]["startup_process"] == startup
    assert lifecycle.events[-1]["signal_process"] == {"pid": 12345, "ppid": 1, "pgid": 12000}
    assert lifecycle.events[-1]["sender_pid"] == "unknown"


def test_snapshot_failure_does_not_prevent_signal_delivery(lifecycle, signal_handlers):
    handlers, original = signal_handlers
    lifecycle.writer.process_snapshot.side_effect = OSError("private-canary")
    with lifecycle.module.observe_signals():
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    original[signal.SIGTERM].assert_called_once_with(signal.SIGTERM, None)
    assert lifecycle.events[-1]["signal_process"] == {"snapshot_error_type": "OSError"}
    assert "private-canary" not in repr(lifecycle.events)


def test_handler_exception_propagates_and_restores(lifecycle, signal_handlers):
    handlers, original = signal_handlers
    original[signal.SIGINT].side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt), lifecycle.module.observe_signals():
        handlers[signal.SIGINT](signal.SIGINT, None)
    assert handlers == original


def test_does_not_overwrite_new_owner_on_exit(lifecycle, signal_handlers):
    handlers, original = signal_handlers
    replacement = Mock()
    with lifecycle.module.observe_signals():
        handlers[signal.SIGTERM] = replacement
    assert handlers[signal.SIGTERM] is replacement
    assert handlers[signal.SIGINT] is original[signal.SIGINT]


def test_default_and_ignored_handlers_are_not_replaced(lifecycle, signal_handlers):
    handlers, _ = signal_handlers
    handlers.update({signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.SIG_IGN})
    with lifecycle.module.observe_signals():
        assert handlers == {signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.SIG_IGN}


def test_partial_signal_install_failure_preserves_existing_handlers(
    lifecycle, signal_handlers, monkeypatch,
):
    handlers, original = signal_handlers
    install = lifecycle.module.signal.signal

    def fail_interrupt(signum, handler):
        if signum == signal.SIGINT:
            raise ValueError("private-canary")
        return install(signum, handler)

    monkeypatch.setattr(lifecycle.module.signal, "signal", fail_interrupt)
    with lifecycle.module.observe_signals():
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert handlers[signal.SIGINT] is original[signal.SIGINT]
    assert handlers == original
    assert lifecycle.events[0] == {
        "event": "api.signal.observer_failed", "signal": "SIGINT", "phase": "install",
        "exception_type": "ValueError",
    }


def test_worker_thread_does_not_install_signals(lifecycle, signal_handlers):
    handlers, original = signal_handlers
    observed = []

    def run():
        with lifecycle.module.observe_signals():
            observed.append(handlers == original)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert observed == [True]


@pytest.fixture
def application(lifecycle, monkeypatch):
    module = load_module("application_under_test", SOURCE / "api/app.py")
    calls = []

    async def initialize():
        calls.append("initialize")

    async def cleanup():
        calls.append("cleanup")

    monkeypatch.setattr(module, "init_dependencies", initialize)
    monkeypatch.setattr(module, "cleanup_dependencies", cleanup)
    monkeypatch.setattr(module, "_get_mcp_http_app", lambda: None)
    return module, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("with_mcp", [False, True])
async def test_lifespan_boundaries_include_mcp_exit(lifecycle, application, monkeypatch, with_mcp):
    module, calls = application
    if with_mcp:
        @asynccontextmanager
        async def mcp_lifespan(app):
            calls.append("mcp_enter")
            yield
            assert lifecycle.events[-1]["event"] == "api.shutdown.begin"
            calls.append("mcp_exit")

        monkeypatch.setattr(module, "_get_mcp_http_app", lambda: SimpleNamespace(lifespan=mcp_lifespan))
    async with module.lifespan(FastAPI()):
        calls.append("request")
        assert lifecycle.events[-1]["event"] == "api.startup.complete"
    assert calls == (["mcp_enter"] if with_mcp else []) + ["initialize", "request", "cleanup"] + (
        ["mcp_exit"] if with_mcp else []
    )
    assert [event["event"] for event in lifecycle.events] == [
        "api.startup.begin", "api.startup.complete", "api.shutdown.begin", "api.shutdown.complete",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["startup", "shutdown", "running"])
async def test_lifespan_failure_preserves_exception_without_message(
    lifecycle, application, monkeypatch, phase,
):
    module, _ = application
    error = OSError(5, "private-canary")

    async def fail():
        raise error

    if phase != "running":
        monkeypatch.setattr(module, "init_dependencies" if phase == "startup" else "cleanup_dependencies", fail)
    with pytest.raises(OSError) as caught:
        async with module.lifespan(FastAPI()):
            if phase == "running":
                raise error
    assert caught.value is error
    assert lifecycle.events[-1] == {
        "event": "api.lifecycle.failed", "phase": phase, "exception_type": "OSError",
        "startup_process": lifecycle.writer.process_snapshot.return_value,
    }
    assert "private-canary" not in repr(lifecycle.events)


@pytest.mark.asyncio
async def test_writer_failure_keeps_lifespan_order(lifecycle, application):
    module, calls = application
    lifecycle.writer.record_event = Mock(side_effect=OSError("private-canary"))
    async with module.lifespan(FastAPI()):
        calls.append("request")
    assert calls == ["initialize", "request", "cleanup"]


@pytest.mark.asyncio
async def test_shutdown_keeps_startup_ancestry(lifecycle, application):
    module, _ = application
    startup = lifecycle.writer.process_snapshot.return_value.copy()
    async with module.lifespan(FastAPI()):
        lifecycle.writer.process_snapshot.return_value = {"pid": 12345, "ppid": 1, "pgid": 12000}
    assert all(event["startup_process"] == startup for event in lifecycle.events)


def test_request_observer_is_outside_business_middleware(application, monkeypatch):
    from fastapi.middleware.cors import CORSMiddleware

    from aiteam.api import debug_log
    from aiteam.api.request_diagnostics import RequestDiagnosticsMiddleware

    monkeypatch.setattr(debug_log, "setup_debug_log", Mock())
    module, _ = application
    app = module.create_app()
    assert app.user_middleware[0].cls is RequestDiagnosticsMiddleware
    assert app.user_middleware[1].cls is CORSMiddleware


@pytest.fixture
def autostart(lifecycle, monkeypatch, tmp_path):
    module = load_module("autostart_under_test", SOURCE / "mcp/_autostart.py")
    for name, filename in {
        "_DEBUG_LOG_DIR": "logs", "_DEBUG_LOG_FILE": "debug.log", "_API_STDERR_LOG": "stderr.log",
        "_PID_FILE": "api.pid", "_PORT_FILE": "port.txt", "_STARTUP_LOCK_FILE": "startup.lock",
    }.items():
        monkeypatch.setattr(module, name, str(tmp_path / filename))
    monkeypatch.setattr(module, "_debug_log", Mock())
    monkeypatch.setattr(module, "_get_api_port", lambda: 43123)
    monkeypatch.setattr(module, "_is_port_open", Mock(return_value=False))
    monkeypatch.setattr(module, "_is_api_healthy_on_port", Mock(return_value=False))
    monkeypatch.setattr(module, "_read_pid_file", Mock(return_value=None))
    monkeypatch.setattr(module, "_reconcile_api_pid", Mock())
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=Mock()))
    monkeypatch.setattr(module.atexit, "register", Mock())
    monkeypatch.delenv("AITEAM_API_URL", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Process operations must be explicitly replaced by the fixture")

    monkeypatch.setattr(module.os, "kill", forbidden)
    for name in ("Popen", "call", "check_output"):
        monkeypatch.setattr(module.subprocess, name, forbidden)
    return module


@pytest.mark.parametrize("outcome", ["ready", "child_exited", "health_timeout"])
def test_spawn_outcomes_are_recorded(lifecycle, autostart, monkeypatch, outcome):
    process = Mock(pid=12345, returncode=7 if outcome == "child_exited" else None)
    process.poll.return_value = process.returncode
    monkeypatch.setattr(autostart.subprocess, "Popen", Mock(return_value=process))
    autostart._is_api_healthy_on_port.return_value = outcome == "ready"
    autostart._ensure_api_running_locked("test-version")
    assert [event["event"] for event in lifecycle.events[:2]] == [
        "api.autostart.spawn_attempt", "api.autostart.spawned",
    ]
    result = lifecycle.events[-1]
    assert result["event"] == ("api.autostart.ready" if outcome == "ready" else "api.autostart.failed")
    assert result["target_pid"] == 12345
    if outcome != "ready":
        assert result["reason"] == outcome
    if outcome == "child_exited":
        assert result["returncode"] == 7


def test_spawn_failure_does_not_log_exception_message(lifecycle, autostart, monkeypatch):
    monkeypatch.setattr(autostart.subprocess, "Popen", Mock(side_effect=OSError(13, "private-canary")))
    autostart._ensure_api_running_locked("test-version")
    assert lifecycle.events[-1] == {
        "event": "api.autostart.failed", "port": autostart._DEFAULT_PORT,
        "reason": "spawn_error", "exception_type": "PermissionError",
    }
    assert "private-canary" not in repr(lifecycle.events)


def test_spawn_state_write_failure_remains_visible_to_caller(lifecycle, autostart, monkeypatch):
    process = Mock(pid=12345)
    error = PermissionError("private-canary")
    monkeypatch.setattr(autostart.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(autostart, "_write_pid_file", Mock(side_effect=error))
    with pytest.raises(PermissionError) as caught:
        autostart._ensure_api_running_locked("test-version")
    assert caught.value is error
    assert lifecycle.events[-1] == {
        "event": "api.autostart.failed", "target_pid": 12345, "port": autostart._DEFAULT_PORT,
        "reason": "runtime_state_write_error", "exception_type": "PermissionError",
    }
    assert "private-canary" not in repr(lifecycle.events)
    process.terminate.assert_not_called()


def test_internal_signals_are_logged_before_delivery(lifecycle, autostart, monkeypatch):
    autostart._read_pid_file.return_value = 12345
    monkeypatch.setattr(autostart, "_pid_is_aiteam_api", Mock(return_value=True))
    monkeypatch.setattr(autostart.subprocess, "Popen", Mock(side_effect=OSError("stop fixture")))
    sent = []

    def send(pid, signum):
        event = lifecycle.events[-1]
        assert event["event"] == "api.termination.requested"
        assert event["target_pid"] == pid
        assert event["signal_number"] == int(signum)
        sent.append(signum)

    monkeypatch.setattr(autostart.os, "kill", send)
    autostart._ensure_api_running_locked("test-version")
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    termination = [event for event in lifecycle.events if event["event"] == "api.termination.requested"]
    assert [event["reason"] for event in termination] == [
        "existing_pid_health_timeout", "existing_pid_health_timeout_escalation",
    ]
    assert all("pid" not in event and "ppid" not in event for event in termination)


@pytest.mark.parametrize("platform", ["darwin", "win32"])
@pytest.mark.parametrize("verified", [False, True])
def test_port_termination_keeps_identity_guard(lifecycle, autostart, monkeypatch, platform, verified):
    monkeypatch.setattr(autostart, "sys", SimpleNamespace(platform=platform))
    output = "TCP 127.0.0.1:43123 0.0.0.0:0 LISTENING 12345" if platform == "win32" else "12345"
    monkeypatch.setattr(autostart.subprocess, "check_output", Mock(return_value=output))
    monkeypatch.setattr(autostart, "_pid_is_aiteam_api", Mock(return_value=verified))
    kill = Mock()
    taskkill = Mock(return_value=0)
    monkeypatch.setattr(autostart.os, "kill", kill)
    monkeypatch.setattr(autostart.subprocess, "call", taskkill)
    autostart._kill_port_occupant(43123)
    event = lifecycle.events[-1]
    assert event["target_pid"] == 12345
    assert event["event"] == ("api.termination.requested" if verified else "api.termination.skipped")
    if not verified:
        kill.assert_not_called()
        taskkill.assert_not_called()
    elif platform == "win32":
        assert event["signal"] == "TerminateProcess"
        taskkill.assert_called_once()
    else:
        assert event["signal"] == "SIGKILL"
        kill.assert_called_once_with(12345, 9)


def test_cleanup_does_not_terminate_shared_api(lifecycle, autostart):
    process = Mock(pid=12345)
    process.poll.return_value = None
    autostart._api_process = process
    autostart._cleanup_api()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()
    assert lifecycle.events[-1] == {
        "event": "api.autostart.retained", "target_pid": 12345, "reason": "mcp_exit_shared_api_retained",
    }


def test_manual_url_is_not_logged(lifecycle, autostart, monkeypatch):
    monkeypatch.setenv("AITEAM_API_URL", "https://private-canary:secret@example.test/?token=value")
    autostart._ensure_api_running()
    assert lifecycle.events == [
        {"event": "api.autostart.begin"},
        {"event": "api.autostart.skipped", "reason": "manual_api_url"},
    ]
    autostart._is_api_healthy_on_port.assert_not_called()


def test_writer_failure_does_not_prevent_autostart(lifecycle, autostart, monkeypatch):
    lifecycle.writer.record_event = Mock(side_effect=OSError("private-canary"))
    process = Mock(pid=12345)
    monkeypatch.setattr(autostart.subprocess, "Popen", Mock(return_value=process))
    autostart._is_api_healthy_on_port.return_value = True
    autostart._ensure_api_running_locked("test-version")
    assert autostart._api_process is process
    assert Path(autostart._PID_FILE).read_text() == "12345"
