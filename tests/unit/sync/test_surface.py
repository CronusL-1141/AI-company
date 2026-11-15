"""Route sync faces: the annotation itself, and the I31 scan over the real app."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI

from aiteam.sync.surface import resolve_surface, route_surface
from aiteam.types import RouteSurface, SyncFace

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def i31():
    spec = importlib.util.spec_from_file_location("check_route_surfaces", ROOT / "scripts" / "check_route_surfaces.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module_with(name: str, default: RouteSurface | None) -> types.ModuleType:
    module = types.ModuleType(name)
    if default is not None:
        module.ROUTE_SURFACE = default
    return module


def test_an_endpoint_takes_its_own_mark_then_its_module_default(monkeypatch) -> None:
    module = _module_with("fake_routes_default", RouteSurface.shared(offline_ok=True))
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def plain():
        return None

    def marked():
        return None

    plain.__module__ = marked.__module__ = module.__name__
    route_surface(RouteSurface.local())(marked)
    assert resolve_surface(plain) == RouteSurface(SyncFace.SHARED, True)
    assert resolve_surface(marked) == RouteSurface(SyncFace.LOCAL, True)
    plain.__module__ = "nowhere_annotated"
    assert resolve_surface(plain) is None
    with pytest.raises(TypeError):
        route_surface("shared")


def test_every_route_of_the_real_app_is_annotated(i31) -> None:
    from aiteam.api.app import create_app
    from aiteam.api.routes import api_router

    entries = i31.collect(create_app())  # the built app, every included router walked
    unannotated, violations, faces = i31.check(entries)
    assert unannotated == [] and violations == []
    assert faces["shared"] and faces["session"] and faces["local"]
    assert faces["shared_offline_ok"] == 35  # the I31_OFFLINE_WRITES ratchet in check_invariants.sh
    script_view = i31.walk(api_router.routes)  # what the I31 script sees without building the app
    assert {(m, p) for m, p, _ in script_view} <= {(m, p) for m, p, _ in entries}
    deletes = [(path, surface) for methods, path, surface in entries if "DELETE" in methods.split(",")]
    assert ("/api/projects/{project_id}", RouteSurface.shared(offline_ok=False)) in deletes
    assert ("/api/reports/{report_id}", RouteSurface.shared(offline_ok=False)) in deletes


def test_the_scan_counts_an_unannotated_route_and_refuses_an_offline_shared_delete(i31, monkeypatch) -> None:
    module = _module_with("fake_routes_bare", None)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    router = APIRouter()

    async def bare():
        return {}

    async def remove():
        return {}

    bare.__module__ = remove.__module__ = module.__name__
    router.get("/bare")(bare)
    router.delete("/thing")(route_surface(RouteSurface.shared(offline_ok=True))(remove))
    # Included two levels down, the way a router slipped in beside ROUTERS would be.
    outer = APIRouter()
    outer.include_router(router)
    app = FastAPI()
    app.include_router(outer)
    unannotated, violations, _ = i31.check(i31.collect(app))
    assert unannotated == ["GET /bare"]
    assert violations == ["DELETE /thing: 共享面删除只许在线（offline_ok 必须为 False）"]


def test_the_static_scan_sees_what_create_app_adds(i31, tmp_path) -> None:
    entries = i31.collect_app_module()
    assert {(methods, path) for methods, path, _ in entries} >= {
        ("GET", "/api/mcp/http-readiness"), ("MOUNT", "/mcp/"), ("MOUNT", "/assets"), ("GET", "/{path:path}")}
    assert all(surface == "annotated" for _, _, surface in entries)
    bare = tmp_path / "app.py"
    bare.write_text(
        "def create_app():\n"
        "    @app.get('/x')\n"
        "    async def x():\n"
        "        return 1\n"
        "    app.mount('/static', StaticFiles(directory='.'))\n"
        "    app.include_router(api_router)\n"
        "    app.include_router(extra_router)\n"
        "    app.add_api_route('/y', y)\n",
        encoding="utf-8",
    )
    assert sorted(i31.collect_app_module(bare)) == sorted([
        ("GET", "/x", None), ("MOUNT", "/static", None),
        ("include_router", "app.py:7", None), ("add_api_route", "app.py:8", None)])
