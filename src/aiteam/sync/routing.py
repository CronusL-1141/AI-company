"""Find the route a request will reach, and the face it serves, before it runs.

The edge decides per request whether a write goes to the center; the center checks
that an operation it is asked to carry out names a shared-face write. Both match
the request against the application's own routes, in registration order, the way
the router will (included routers are walked like I31 walks them).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from starlette.routing import Match

from aiteam.sync.surface import resolve_surface
from aiteam.types import RouteSurface, SyncFace

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class RouteMatch:
    """The route a request reaches: its path template, path parameters and face."""

    template: str
    params: dict[str, Any]
    surface: RouteSurface

    @property
    def shared_write(self) -> bool:
        return self.surface.face is SyncFace.SHARED


def _flatten(routes: Iterable[Any]) -> list[Any]:
    from fastapi.routing import APIRoute

    flat: list[Any] = []
    for route in routes:
        if isinstance(route, APIRoute):
            flat.append(route)
        elif hasattr(route, "original_router"):
            flat += _flatten(route.original_router.routes)
    return flat


class RouteTable:
    """The HTTP routes of one application, matched as its router matches them."""

    def __init__(self, routes: Iterable[Any]) -> None:
        self._routes = _flatten(routes)

    def match(self, method: str, path: str) -> RouteMatch | None:
        scope = {"type": "http", "method": method, "path": path, "root_path": ""}
        for route in self._routes:
            matched, child = route.matches(scope)
            if matched is Match.FULL:
                surface = resolve_surface(route.endpoint)
                if surface is None:
                    return None
                return RouteMatch(route.path, dict(child.get("path_params", {})), surface)
        return None


def writes(match: RouteMatch, method: str, body: bytes) -> bool:
    """Whether this request to a shared route is a write (``RouteSurface.write_flag``)."""
    if method not in WRITE_METHODS or not match.shared_write:
        return False
    flag = match.surface.write_flag
    if not flag:
        return True
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return True  # unreadable: let the center's validation answer it
    return isinstance(payload, dict) and payload.get(flag) is True


def refused(status: int, body: bytes) -> bool:
    """Whether a route turned the write down: an error status, or a refusal it answers
    with 200 and ``"success": false`` (quota limits, a replacement of a gone entry)."""
    if status >= 400:
        return True
    try:
        payload = json.loads(body or b"null")
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("success") is False


async def call_app(app: Any, method: str, path: str, *, query: str = "", headers: dict[str, str] | None = None,
                   body: bytes = b"") -> tuple[int, dict[str, str], bytes]:
    """Run one request through ``app`` in this process (all its middleware) and return
    (status, headers, body). Context variables set by the caller reach the route."""
    import httpx

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://aiteam.sync") as client:
        response = await client.request(
            method, path + (f"?{query}" if query else ""), headers=headers or {}, content=body, timeout=None)
    return response.status_code, {k.lower(): v for k, v in response.headers.items()}, response.content
