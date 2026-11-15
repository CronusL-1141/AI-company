"""Which face every API route serves (shared, session, local), and offline rules.

A route module declares its default as a module constant ``ROUTE_SURFACE``; a route
that differs carries its own with ``@route_surface(...)``. Mounted sub-applications
carry theirs as an attribute set the same way. A route with neither is unannotated,
which the I31 machine check refuses: a sync layer cannot route a request it cannot
classify, and a new route nobody classified would quietly take its module's default
the day it is added to an unannotated module.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, TypeVar

from aiteam.types import RouteSurface

MODULE_DEFAULT = "ROUTE_SURFACE"
_ATTRIBUTE = "__route_surface__"
# FastAPI's own documentation routes describe this process's code.
_FRAMEWORK_MODULES = frozenset({"fastapi.applications"})

T = TypeVar("T")


def route_surface(surface: RouteSurface) -> Callable[[T], T]:
    """Mark an endpoint (or a mounted app) with the face it serves; returns it unchanged."""
    if not isinstance(surface, RouteSurface):
        raise TypeError(f"route_surface needs a RouteSurface, got {surface!r}")

    def mark(target: T) -> T:
        setattr(target, _ATTRIBUTE, surface)
        return target

    return mark


def resolve_surface(target: Any) -> RouteSurface | None:
    """The surface of an endpoint function or a mounted app, or None when unannotated."""
    own = getattr(target, _ATTRIBUTE, None)
    if isinstance(own, RouteSurface):
        return own
    module_name = getattr(target, "__module__", None)
    if module_name in _FRAMEWORK_MODULES:
        return RouteSurface.local()
    default = getattr(sys.modules.get(module_name or ""), MODULE_DEFAULT, None)
    return default if isinstance(default, RouteSurface) else None
