"""Fail-open lifecycle logging without taking ownership of server signals."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any

from aiteam import diagnostics

SignalHandler = Callable[[int, FrameType | None], Any]


def record_lifecycle_event(event: str, **fields: Any) -> None:
    """Keep application and signal behavior intact even if the sink fails."""
    try:
        diagnostics.record_event(event, **fields)
    except Exception:
        pass


def capture_process_snapshot() -> dict[str, Any]:
    """Capture ancestry once without allowing inspection failures to escape."""
    try:
        return dict(diagnostics.process_snapshot())
    except Exception as exc:
        return {"snapshot_error_type": type(exc).__name__}


def _observe_handler(handler: SignalHandler, startup_process: dict[str, Any]) -> SignalHandler:
    def observed(signum: int, frame: FrameType | None) -> Any:
        record_lifecycle_event(
            "api.signal.received", signal=signal.Signals(signum).name,
            signal_number=signum, sender_pid="unknown",
            startup_process=startup_process, signal_process=capture_process_snapshot(),
        )
        return handler(signum, frame)

    return observed


@contextmanager
def observe_signals(startup_process: dict[str, Any] | None = None) -> Iterator[None]:
    """Delegate to installed handlers; never replace defaults or ignored signals.

    Uvicorn installs its handlers before entering the application lifespan.
    Restore them before its own signal restoration and replay on server exit.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    if startup_process is None:
        startup_process = capture_process_snapshot()
    installed: dict[signal.Signals, tuple[SignalHandler, SignalHandler]] = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                previous = signal.getsignal(signum)
                if not callable(previous):
                    continue
                observer = _observe_handler(previous, startup_process)
                signal.signal(signum, observer)
                installed[signum] = (observer, previous)
            except (OSError, ValueError, RuntimeError) as exc:
                record_lifecycle_event(
                    "api.signal.observer_failed", signal=signum.name,
                    phase="install", exception_type=type(exc).__name__,
                )
        yield
    finally:
        try:
            # Uvicorn may re-raise SIGTERM immediately after handler restoration.
            diagnostics.flush_diagnostics(timeout=0.25)
        except Exception:
            pass
        finally:
            for signum, (observer, previous) in installed.items():
                try:
                    # A later owner must not be overwritten during our teardown.
                    if signal.getsignal(signum) is observer:
                        signal.signal(signum, previous)
                except (OSError, ValueError, RuntimeError) as exc:
                    record_lifecycle_event(
                        "api.signal.observer_failed", signal=signum.name,
                        phase="restore", exception_type=type(exc).__name__,
                    )
