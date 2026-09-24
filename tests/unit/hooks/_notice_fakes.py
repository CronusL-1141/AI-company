"""Shared fixtures for the user-notice hook tests: a fake OS API and a hook runner.

The fake API validates every ``POST /api/notices/pending`` body with the
production request model (``aiteam.types.PendingRequest``) and answers with the
production response model, so a hook that sends a field the real route would
reject fails here too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pydantic import ValidationError

from aiteam.types import PendingRequest, PendingResponse

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_HOOKS = ROOT / "plugin" / "hooks"
REFUSED_URL = "http://127.0.0.1:9"


class FakeApi:
    """A tiny OS API on a free port.

    ``routes`` maps ``(method, path-without-query)`` to a callable taking the
    parsed JSON body (or None) and returning ``(status, document)``. The pending
    route is built in: ``pending`` is a list of PendingResponse keyword dicts,
    served in order (the last one repeats); ``pending_status`` overrides the
    status code.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        self.pending: list[dict] = [{}]
        self.pending_status = 200
        self.routes: dict[tuple[str, str], Callable[[object], tuple[int, object]]] = {}
        self._served = 0
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _reply(self, status: int, document: object) -> None:
                body = b"" if document is None else json.dumps(document).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else None
                path = self.path.split("?", 1)[0]
                api.requests.append((method, self.path, body))
                if (method, path) == ("POST", "/api/notices/pending"):
                    try:
                        PendingRequest.model_validate(body)
                    except ValidationError as exc:
                        self._reply(422, {"detail": json.loads(exc.json())})
                        return
                    if api.pending_status != 200:
                        self._reply(api.pending_status, {"detail": "scripted"})
                        return
                    scripted = api.pending[min(api._served, len(api.pending) - 1)]
                    api._served += 1
                    self._reply(200, PendingResponse(**scripted).model_dump(mode="json"))
                    return
                handler = api.routes.get((method, path))
                if handler is None:
                    self._reply(404, {"detail": "Not Found"})
                    return
                status, document = handler(body)
                self._reply(status, document)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )

    def __enter__(self) -> FakeApi:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def pending_bodies(self) -> list[dict]:
        return [body for method, path, body in self.requests
                if method == "POST" and path.startswith("/api/notices/pending")]

    def paths(self) -> list[str]:
        return [path.split("?", 1)[0] for _method, path, _body in self.requests]


def hook_env(home: Path, api_url: str = REFUSED_URL, *, language: str = "zh_CN.UTF-8",
             entrypoint: str | None = "cli", **extra: str) -> dict:
    """Environment for a hook subprocess: isolated HOME, pinned language, chosen API."""
    env = {key: value for key, value in os.environ.items()
           if key not in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_ENTRYPOINT",
                          "LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "AITEAM_API_URL")}
    env.update(HOME=str(home), AITEAM_API_URL=api_url, LC_ALL=language, PYTHONDONTWRITEBYTECODE="1")
    if entrypoint is not None:
        env["CLAUDE_CODE_ENTRYPOINT"] = entrypoint
    env.update(extra)
    return env


def run_hook(script: str, payload: dict, env: dict, *args: str, cwd: Path | None = None,
             timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PLUGIN_HOOKS / script), *args],
        input=json.dumps(payload), text=True, capture_output=True, env=env,
        cwd=str(cwd) if cwd else None, timeout=timeout,
    )


def data_dir(home: Path) -> Path:
    return home / ".claude" / "data" / "ai-team-os"


def local_records(home: Path, host: str = "cc") -> list[dict]:
    path = data_dir(home) / f"notice-local.{host}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def output(stdout: str) -> dict:
    """The one JSON document a hook printed ({} when it printed nothing)."""
    return json.loads(stdout) if stdout.strip() else {}
