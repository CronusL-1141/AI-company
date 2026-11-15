#!/usr/bin/env python3
"""I31: every API route says which face it serves (shared, session, local).

Multi-host sync routes a request by the face it serves: a shared-face write goes to
the center, a session-face write stays on the node it happened on, local requests
never leave the process. A route nobody classified cannot be routed, so the count of
unannotated routes is a ratchet passed in by ``scripts/check_invariants.sh``: above
it is red, below it asks for the ratchet to come down, equal is green. It stands at
0; the mechanism stays so that it can never creep back up.

One rule is not ratcheted: a DELETE on the shared face must not be ``offline_ok``.
A deletion does not commute with a concurrent edit, so it only ever runs on the
center, online.

The number of shared-face write routes marked ``offline_ok`` is a second ratchet
(``--offline-writes``). Those writes promise reproducible ids and an idempotent
replay, and a write route added to a module whose default is ``offline_ok`` makes
that promise silently; a changed count asks for the number to be set on purpose.

What is scanned:

* every route ``aiteam.api.routes.api_router`` really serves, walking each included
  router (FastAPI keeps them as lazy entries holding ``original_router``), so a
  router included outside ``ROUTERS`` is seen too; anything the walk cannot read
  counts as unannotated;
* the routes and mounts ``create_app`` adds itself, read from ``app.py``'s syntax:
  this checker may not import the API module (I26: importing it scrubs the
  importing process). A route function there needs the module default or its own
  mark; a mounted app must be passed through ``route_surface(...)``; a second
  ``include_router`` or any ``add_*route`` call counts as unannotated.

FastAPI's own documentation routes are local by definition (aiteam.sync.surface).
tests/unit/sync/test_surface.py walks the fully built app with the same rules.

Usage: python3 scripts/check_route_surfaces.py --max N --offline-writes M [--list]
Exit: 0 = both counts equal, 3 = a count below its ratchet (lower it), 1 = an
unannotated count over N, an offline-write count over M, a rule broken, or the scan
itself failed.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_MODULE = ROOT / "src" / "aiteam" / "api" / "app.py"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ROUTE_DECORATORS = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE",
                     "api_route": None, "websocket": "WS"}


def collect(app) -> list[tuple[str, str, object]]:
    """(methods, path, surface-or-None) for every route object of a built app."""
    return walk(app.routes)


def walk(routes) -> list[tuple[str, str, object]]:
    """Every route under ``routes``, descending into included routers."""
    from fastapi.routing import APIRoute, APIWebSocketRoute
    from starlette.routing import Mount, Route, WebSocketRoute

    from aiteam.sync.surface import resolve_surface

    entries = []
    for route in routes:
        if isinstance(route, (APIRoute, APIWebSocketRoute, Route, WebSocketRoute)):
            entries.append(_entry(route, resolve_surface(route.endpoint)))
        elif isinstance(route, Mount):
            entries.append(("MOUNT", route.path, resolve_surface(route.app)))
        elif hasattr(route, "original_router"):
            entries += walk(route.original_router.routes)
        else:  # unforeseen: unresolvable, so counted
            entries.append((type(route).__name__, getattr(route, "path", "?"), None))
    return entries


def _entry(route, surface) -> tuple[str, str, object]:
    methods = ",".join(sorted(getattr(route, "methods", None) or {"WS"}))
    return methods, route.path, surface


def collect_app_module(path: Path = APP_MODULE) -> list[tuple[str, str, object]]:
    """The routes and mounts ``create_app`` adds, from the syntax of app.py.

    Surface is the string ``"annotated"`` when resolvable, None otherwise.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    has_default = any(
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ROUTE_SURFACE" for t in node.targets)
        for node in tree.body
    )
    entries = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _OTHER_ROUTE_CALLS and not _is_the_api_router(node)):
            entries.append((node.func.attr, f"app.py:{node.lineno}", None))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            route = next((d for d in node.decorator_list if _app_route(d)), None)
            if route is None:
                continue
            marked = any(_named_call(d, "route_surface") for d in node.decorator_list)
            entries.append((_methods(route), _literal(route.args[0]) if route.args else "?",
                            "annotated" if (marked or has_default) else None))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "mount"
              and isinstance(node.func.value, ast.Name) and node.func.value.id == "app"):
            target = node.args[1] if len(node.args) > 1 else None
            wrapped = (isinstance(target, ast.Call) and _named_call(target.func, "route_surface"))
            entries.append(("MOUNT", _literal(node.args[0]) if node.args else "?", "annotated" if wrapped else None))
    return entries


_OTHER_ROUTE_CALLS = frozenset({"include_router", "add_api_route", "add_route", "add_websocket_route",
                                 "add_api_websocket_route", "host"})


def _is_the_api_router(call: ast.Call) -> bool:
    return (call.func.attr == "include_router" and len(call.args) == 1  # type: ignore[union-attr]
            and isinstance(call.args[0], ast.Name) and call.args[0].id == "api_router")


def _app_route(decorator: ast.expr) -> bool:
    return (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr in _ROUTE_DECORATORS
            and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "app")


def _named_call(node: ast.expr, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name


def _literal(node: ast.expr) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else "?"


def _methods(decorator: ast.Call) -> str:
    fixed = _ROUTE_DECORATORS[decorator.func.attr]  # type: ignore[union-attr]
    if fixed:
        return fixed
    for keyword in decorator.keywords:
        if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple)):
            return ",".join(sorted(_literal(e) for e in keyword.value.elts))
    return "GET"


def check(entries) -> tuple[list[str], list[str], Counter]:
    """(unannotated routes, rule violations, counts by face)."""
    from aiteam.types import RouteSurface, SyncFace

    unannotated, violations, faces = [], [], Counter()
    for methods, path, surface in entries:
        if surface is None:
            unannotated.append(f"{methods} {path}")
            continue
        if not isinstance(surface, RouteSurface):
            faces["create_app"] += 1
            continue
        faces[surface.face.value] += 1
        written = set(methods.split(",")) & WRITE_METHODS
        if surface.face is SyncFace.SHARED and surface.offline_ok:
            faces["shared_offline_ok"] += bool(written)
            if "DELETE" in written:
                violations.append(f"{methods} {path}: 共享面删除只许在线（offline_ok 必须为 False）")
    return unannotated, violations, faces


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, required=True, help="ratchet: allowed unannotated routes")
    parser.add_argument("--offline-writes", type=int, required=True,
                        help="ratchet: shared-face write routes marked offline_ok")
    parser.add_argument("--list", action="store_true", help="print every unannotated route")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="i31-") as workdir:
        # Importing the routes resolves a database path: keep it off the real one.
        os.environ["AITEAM_DB_PATH"] = str(Path(workdir) / "aiteam.db")
        sys.path.insert(0, str(ROOT / "src"))
        from aiteam.api.routes import api_router

        entries = walk(api_router.routes) + collect_app_module()
    unannotated, violations, faces = check(entries)
    summary = (f"{len(entries)} 个路由对象：shared {faces['shared']}（其中可离线写 "
               f"{faces['shared_offline_ok']}）、session {faces['session']}、local {faces['local']}、"
               f"create_app 自加 {faces['create_app']}，未标注 {len(unannotated)}")
    if violations:
        print("\n".join(violations))
        return 1
    if len(unannotated) > args.max:
        print(f"未标注的路由 {len(unannotated)} 个，超过棘轮 {args.max}：")
        print("\n".join(unannotated))
        return 1
    offline = faces["shared_offline_ok"]
    if offline > args.offline_writes:
        print(f"{summary}；可离线的共享面写路由 {offline} 个，多于登记的 {args.offline_writes}：新路由若确实"
              "满足「ID 可重现、重放幂等」，把 check_invariants.sh 的 I31_OFFLINE_WRITES 改到此数，"
              "否则标 offline_ok=False")
        return 1
    if args.list and unannotated:
        print("\n".join(unannotated))
    if len(unannotated) < args.max:
        print(f"{summary}；低于棘轮 {args.max}，把 check_invariants.sh 的 I31_MAX 降到 {len(unannotated)}")
        return 3
    if offline < args.offline_writes:
        print(f"{summary}；可离线写少于登记的 {args.offline_writes}，把 I31_OFFLINE_WRITES 降到 {offline}")
        return 3
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
