"""create_app() 反复调用时 debug.log 只挂一个 handler（重复挂会每行多写、每个 handler 各轮转一次）."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from aiteam.api.app import create_app

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
