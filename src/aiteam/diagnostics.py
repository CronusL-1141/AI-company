"""Bounded, best-effort local diagnostics without bodies or credentials."""

from __future__ import annotations

import atexit
import json
import math
import os
import queue
import re
import socket
import stat
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from aiteam.clock import utc_now

_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 2
_lock = threading.RLock()
_handler: _PrivateSink | None = None
_handler_key: tuple[int, str] | None = None
_queue_lock = threading.RLock()
_space_available = threading.Condition(_queue_lock)
_queue: queue.SimpleQueue[Any] | None = None
_writer: threading.Thread | None = None
_writer_pid: int | None = None
_pending = 0
_dropped = 0
_QUEUE_SIZE = 512
_initializing = False
_SENSITIVE = frozenset({
    "authorization", "proxyauthorization", "cookie", "setcookie", "password",
    "token", "secret", "apikey", "accesstoken", "body", "requestbody",
    "responsebody", "headers", "cmdline", "command", "args", "env", "environ",
})
_PROXY_ENV_KEYS = (
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
)


class _PrivateSink:
    """Unbuffered FD owned only by the writer, outside logging.shutdown()."""

    def __init__(self, path: Path, max_bytes: int, backup_count: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.fd: int | None = None
        self._open()

    def _open(self) -> None:
        flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND
                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(self.path, flags, 0o600)
        try:
            self._verify(fd)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    @staticmethod
    def _verify(fd: int) -> os.stat_result:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (
            info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1
        )):
            raise PermissionError("Diagnostic file is not private")
        return info

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)

    def write(self, line: str) -> None:
        if self.fd is None:
            self._open()
        raw = (line + "\n").encode("utf-8")
        info = self._verify(self.fd)
        if info.st_size and info.st_size + len(raw) > self.max_bytes:
            self.close()
            for number in range(self.backup_count - 1, 0, -1):
                try:
                    os.replace(f"{self.path}.{number}", f"{self.path}.{number + 1}")
                except FileNotFoundError:
                    pass
            try:
                os.replace(self.path, f"{self.path}.1")
            except FileNotFoundError:
                pass
            self._open()
        while raw:
            written = os.write(self.fd, raw)
            if written == 0:
                raise OSError("Diagnostic write made no progress")
            raw = raw[written:]


def safe_url(url: str) -> str:
    """Remove userinfo, queries and fragments before persisting a destination."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        authority = f"{host}:{parsed.port}" if parsed.port is not None else host
        return urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path[:512], "", ""))
    except (ValueError, TypeError):
        return "<invalid-url>"


def _clean(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:1024]
    if isinstance(value, dict):
        result = {}
        for key, child in list(value.items())[:48]:
            if not isinstance(key, str):
                continue
            normalized = re.sub(r"[^a-z]", "", key.lower())
            if normalized in _SENSITIVE:
                continue
            result[key[:80]] = (
                safe_url(child) if key.endswith("url") and isinstance(child, str) else _clean(child, depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_clean(child, depth + 1) for child in value[:32]]
    return None


def process_snapshot(pid: int | None = None) -> dict[str, Any]:
    """Read identity without command arguments or environment contents."""
    target = os.getpid() if pid is None else pid
    result: dict[str, Any] = {"pid": target}
    try:
        import psutil

        process = psutil.Process(target)
        with process.oneshot():
            result.update(ppid=process.ppid(), name=process.name(), start_time=process.create_time())
        if hasattr(os, "getpgid"):
            result["pgid"] = os.getpgid(target)
    except Exception as error:
        result["snapshot_error_type"] = type(error).__name__
    return result


def _write_record(directory: Path, payload: dict[str, Any]) -> None:
    global _handler, _handler_key
    key = (os.getpid(), str(directory))
    line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    with _lock:
        if key != _handler_key:
            if _handler is not None:
                _handler.close()
            _handler = None
            _handler_key = None
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or (os.name == "posix" and (
                info.st_uid != os.getuid() or info.st_mode & 0o077
            )):
                raise PermissionError("Diagnostic directory is not private")
            _handler = _PrivateSink(
                directory / f"runtime-{key[0]}.jsonl", max_bytes=_MAX_BYTES,
                backup_count=_BACKUP_COUNT,
            )
            _handler_key = key
        _handler.write(line)


def _consume(records: queue.SimpleQueue[Any]) -> None:
    global _pending
    while True:
        item = records.get()
        marker = isinstance(item, threading.Event)
        try:
            if not marker:
                _write_record(*item)
        except Exception:
            pass
        finally:
            with _space_available:
                _pending -= 1
                _space_available.notify_all()
            if marker:
                item.set()


def _get_queue() -> queue.SimpleQueue[Any] | None:
    global _queue, _writer, _writer_pid, _pending, _initializing
    with _queue_lock:
        if _initializing:
            return None
        _initializing = True
        try:
            if _writer is None or not _writer.is_alive() or _writer_pid != os.getpid():
                # SimpleQueue.put is reentrant, including from a Python signal callback.
                # Admission is bounded separately so producers never wait for disk IO.
                _queue = queue.SimpleQueue()
                _writer_pid = os.getpid()
                _pending = 0
                _writer = threading.Thread(target=_consume, args=(_queue,), name="aiteam-diagnostics", daemon=True)
                _writer.start()
            return _queue
        finally:
            _initializing = False


def record_event(event: str, **fields: Any) -> None:
    """Enqueue without waiting for disk IO; a full queue drops diagnostics only."""
    global _dropped, _pending
    if os.environ.get("AITEAM_DIAGNOSTICS_ENABLED", "1") == "0":
        return
    try:
        directory = Path(os.environ.get("AITEAM_DIAGNOSTICS_DIR") or (
            Path.home() / ".claude" / "data" / "ai-team-os" / "diagnostics"
        ))
        pid = os.getpid()
        payload = _clean(fields)
        payload.update(timestamp=utc_now().isoformat(), event=event[:100], pid=pid, ppid=os.getppid())
        if hasattr(os, "getpgrp"):
            payload["pgid"] = os.getpgrp()
        session = (os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")
                   or os.environ.get("CLAUDE_SESSION_ID") or "")
        if re.fullmatch(r"[0-9a-fA-F-]{36}", session):
            payload["session_id"] = session
        records = _get_queue()
        with _queue_lock:
            if records is None or _pending >= _QUEUE_SIZE:
                _dropped += 1
                return
            if _dropped:
                payload["dropped_before"] = _dropped
                _dropped = 0
            _pending += 1
            records.put((directory, payload))
    except Exception:
        pass


def flush_diagnostics(timeout: float = 0.25) -> bool:
    """Bounded drain for shutdown/tests only; not a guarantee of sink success."""
    global _pending
    records = _queue
    if records is None or _writer_pid != os.getpid():
        return True
    marker = threading.Event()
    deadline = time.monotonic() + max(0, timeout)
    try:
        with _space_available:
            while _pending >= _QUEUE_SIZE:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _space_available.wait(timeout=remaining)
            _pending += 1
            records.put(marker)
        return marker.wait(max(0, deadline - time.monotonic()))
    except ValueError:
        return False


def _after_fork() -> None:
    global _lock, _queue_lock, _handler, _handler_key, _queue, _writer, _writer_pid
    global _space_available, _pending, _initializing, _dropped
    _lock = threading.RLock()
    _queue_lock = threading.RLock()
    _space_available = threading.Condition(_queue_lock)
    if _handler is not None:
        _handler.close()
    _handler = None
    _handler_key = None
    _queue = None
    _writer = None
    _writer_pid = None
    _pending = 0
    _initializing = False
    _dropped = 0


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
atexit.register(flush_diagnostics)


def proxy_snapshot(url: str) -> dict[str, Any]:
    """Configuration evidence only; never claim it proves the connected route."""
    try:
        proxies = urllib.request.getproxies()
        target = urllib.parse.urlsplit(url)
        env_present = {key: key in os.environ for key in _PROXY_ENV_KEYS}
        observed = {}
        for scheme in ("http", "https", "all"):
            value = proxies.get(scheme)
            if isinstance(value, str):
                parsed = urllib.parse.urlsplit(value if "://" in value else f"http://{value}")
                observed[scheme] = {"scheme": parsed.scheme, "host": parsed.hostname, "port": parsed.port}
        return {
            "evidence_kind": "configuration_snapshot_not_route_proof",
            "proxy_env_present": env_present,
            "proxies": observed,
            "target_bypass": urllib.request.proxy_bypass(target.netloc),
            "loopback_bypass": {host: urllib.request.proxy_bypass(host) for host in ("localhost", "127.0.0.1", "::1")},
        }
    except Exception as error:
        return {"snapshot_error_type": type(error).__name__}


def response_snapshot(response: Any) -> dict[str, Any]:
    """Capture a small response allowlist and the live socket peer if available."""
    result: dict[str, Any] = {"peer": None, "response_headers": {}}
    try:
        status = getattr(response, "status", None)
        if not isinstance(status, int):
            status = getattr(response, "code", None)
        if isinstance(status, int):
            result["status"] = status
        headers = getattr(response, "headers", None)
        if headers is not None:
            for key in ("server", "via", "content-type"):
                value = headers.get(key)
                if isinstance(value, str):
                    result["response_headers"][key] = value[:256]
        final_url = response.geturl()
        if isinstance(final_url, str):
            result["response_url"] = safe_url(final_url)
        for path in (("fp", "raw", "_sock"), ("fp", "fp", "raw", "_sock")):
            current = response
            for name in path:
                current = getattr(current, name, None)
            if isinstance(current, socket.socket):
                try:
                    peer = current.getpeername()
                    if isinstance(peer, tuple) and len(peer) >= 2:
                        result["peer"] = {"host": peer[0], "port": peer[1]}
                        break
                except OSError:
                    continue
    except Exception as error:
        result["snapshot_error_type"] = type(error).__name__
    return result
