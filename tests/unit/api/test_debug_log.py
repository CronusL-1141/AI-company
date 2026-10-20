"""create_app() 反复调用时 debug.log 只挂一个 handler（重复挂会每行多写、每个 handler 各轮转一次）."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import socket
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
import uvicorn

from aiteam.api.app import create_app
from aiteam.api.debug_log import setup_debug_log

_LOGGERS = ("aiteam", "uvicorn", "uvicorn.access", "uvicorn.error")


def _handlers_for(path: str) -> dict[str, list[logging.Handler]]:
    return {
        name: [
            handler for handler in logging.getLogger(name).handlers
            if isinstance(handler, RotatingFileHandler) and handler.baseFilename == path
        ]
        for name in _LOGGERS
    }


def test_create_app_twice_attaches_one_debug_log_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = os.path.abspath(tmp_path / ".claude" / "data" / "ai-team-os" / "debug.log")
    try:
        create_app()
        create_app()
        attached = _handlers_for(path)
        assert all(len(handlers) == 1 for handlers in attached.values()), attached
        assert len({id(handlers[0]) for handlers in attached.values()}) == 1

        logging.getLogger("aiteam.test_debug_log").info("dedupe-marker")
        lines = (tmp_path / ".claude" / "data" / "ai-team-os" / "debug.log").read_text().splitlines()
        assert sum("dedupe-marker" in line for line in lines) == 1
    finally:
        for name, handlers in _handlers_for(path).items():
            for handler in handlers:
                logging.getLogger(name).removeHandler(handler)
                handler.close()


@pytest.fixture
def isolated_debug_log(tmp_path, monkeypatch):
    """Keep the live-server tests away from real HOME and process-wide loggers."""
    saved = {
        name: (logger.handlers[:], logger.level, logger.propagate)
        for name in _LOGGERS
        for logger in [logging.getLogger(name)]
    }
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.setLevel(logging.INFO)
        logger.propagate = False
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = setup_debug_log()
    try:
        yield path
    finally:
        created = set()
        for name in _LOGGERS:
            logger = logging.getLogger(name)
            created.update(logger.handlers)
            logger.handlers, logger.level, logger.propagate = saved[name]
        for handler in created:
            handler.close()


def test_access_query_redaction_does_not_mutate_other_handlers(isolated_debug_log):
    path = isolated_debug_log
    logger = logging.getLogger("uvicorn.access")
    handler = _handlers_for(str(path))["uvicorn.access"][0]
    formatter = handler.formatter
    setup_debug_log()
    assert _handlers_for(str(path))["uvicorn.access"] == [handler]
    assert handler.formatter is formatter
    assert not handler.filters

    raw = io.StringIO()
    logger.addHandler(logging.StreamHandler(raw))  # Runs after the file handler.
    target = "/api/search?q=SYNTHETIC_PRIVATE_QUERY&unknown=SYNTHETIC_UNKNOWN"
    record = logger.makeRecord(
        logger.name, logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1234", "GET", target, "1.1", 200), None,
    )
    original = record.__dict__.copy()
    handler.format(record)
    assert record.__dict__ == original
    logger.handle(record)
    assert record.msg == original["msg"]
    assert record.args == original["args"]
    assert target in raw.getvalue()
    disk = path.read_text()
    assert '"GET /api/search HTTP/1.1" 200' in disk
    assert "?" not in disk
    assert "SYNTHETIC" not in disk


@pytest.mark.parametrize(
    ("message", "args", "expected"),
    [
        ('127.0.0.1:1234 - "GET /api/search?q=SYNTHETIC_PRIVATE HTTP/1.1" 404', (),
         '"GET /api/search HTTP/1.1" 404'),
        ("unexpected request /api/search?q=SYNTHETIC_PRIVATE", (), "[unrecognized uvicorn access record]"),
        ("broken %s %s", ("SYNTHETIC_PRIVATE",), "[unrecognized uvicorn access record]"),
    ],
    ids=["preformatted", "unknown-format", "invalid-format-arguments"],
)
def test_access_unexpected_formats_are_safe(isolated_debug_log, message, args, expected):
    logger = logging.getLogger("uvicorn.access")
    record = logger.makeRecord(logger.name, logging.INFO, __file__, 1, message, args, None)
    # Only the file sink owns this guarantee; unrelated capture/console handlers
    # still have their normal formatting behavior for malformed records.
    _handlers_for(str(isolated_debug_log))["uvicorn.access"][0].handle(record)
    disk = isolated_debug_log.read_text()
    assert expected in disk
    assert "SYNTHETIC_PRIVATE" not in disk


def test_nonaccess_logs_keep_their_message(isolated_debug_log):
    for name in ("aiteam.audit_test", "uvicorn.error"):
        logging.getLogger(name).warning("ordinary message %s", "/route?diagnostic=preserved")
    disk = isolated_debug_log.read_text()
    assert disk.count("ordinary message /route?diagnostic=preserved") == 2


@pytest.mark.parametrize("http_protocol", ["h11", "httptools"])
async def test_live_uvicorn_access_queries_are_redacted_on_disk(isolated_debug_log, http_protocol):
    async def app(scope, receive, send):
        assert scope["type"] == "http"
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, http=http_protocol,
        lifespan="off", access_log=True, log_config=None,
    )
    server = uvicorn.Server(config)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    task = asyncio.create_task(server.serve(sockets=[sock]))
    requests = [
        ("GET", "/api/search?q=SYNTHETIC_SEARCH&query=SYNTHETIC_TEXT&api_key=SYNTHETIC_KEY"
         "&lease_id=SYNTHETIC_LEASE&unknown=SYNTHETIC_UNKNOWN", "/api/search", b""),
        ("POST", "/api/search?token=SYNTHETIC_TOKEN", "/api/search", b'{"private":"SYNTHETIC_BODY"}'),
        ("GET", "/api/audit%3Fpath?q=SYNTHETIC%20ENCODED%2F&tag=one&tag=two", "/api/audit%3Fpath", b""),
        ("GET", "/api/search?q=&unknown=", "/api/search", b""),
        ("GET", "/api/search?", "/api/search", b""),
        ("GET", "/api/search", "/api/search", b""),
    ]
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail("uvicorn exited before starting")
                await asyncio.sleep(0.02)
        for method, target, _expected_path, body in requests:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write((
                    f"{method} {target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
                    f"Authorization: Bearer SYNTHETIC_HEADER\r\nContent-Length: {len(body)}\r\n\r\n"
                ).encode() + body)
                await writer.drain()
                async with asyncio.timeout(10):
                    response = await reader.read()
                assert response.startswith(b"HTTP/1.1 200 ")
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        finally:
            sock.close()
    assert task.done()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    # Read back the file only after all real HTTP requests and server shutdown.
    disk = isolated_debug_log.read_text()
    access = [line for line in disk.splitlines() if "uvicorn.access" in line]
    assert len(access) == len(requests)
    for line, (method, _target, expected_path, _body) in zip(access, requests, strict=True):
        assert f'"{method} {expected_path} HTTP/1.1" 200' in line
        assert "?" not in line
        assert "SYNTHETIC" not in line
