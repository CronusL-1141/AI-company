"""Correlate MCP transport evidence without inspecting HTTP payloads."""

from __future__ import annotations

import re
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aiteam.diagnostics import record_event

_HEADER = b"x-aiteam-request-id"


class RequestDiagnosticsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_id = next((value for key, value in scope.get("headers", []) if key.lower() == _HEADER), b"")
        if not re.fullmatch(rb"[0-9a-f]{32}", raw_id):
            await self.app(scope, receive, send)
            return
        context = {"request_id": raw_id.decode("ascii"), "method": scope.get("method"), "path": scope.get("path")}
        started = time.monotonic()
        record_event("http.server.received", **context)

        async def traced_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != _HEADER]
                message["headers"] = [*headers, (_HEADER, raw_id)]
                record_event("http.server.response", **context, status=message["status"],
                             elapsed_ms=round((time.monotonic() - started) * 1000, 3))
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
        except BaseException as error:
            record_event("http.server.failed", **context, error_type=type(error).__name__,
                         elapsed_ms=round((time.monotonic() - started) * 1000, 3))
            raise
