"""AI Team OS — Dashboard静态文件挂载测试.

验证FastAPI正确serve Dashboard静态文件，且不拦截API路由。
"""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from aiteam.api.app import create_app


@asynccontextmanager
async def _noop_lifespan(app):
    yield


def _make_client(app):
    """创建测试客户端，跳过lifespan初始化."""
    app.router.lifespan_context = _noop_lifespan
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def fake_dist(tmp_path):
    """创建模拟的dashboard/dist目录结构."""
    dist = tmp_path / "dashboard" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<html><head><title>AI Team OS Dashboard</title></head>"
        "<body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    assets = dist / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log('app');", encoding="utf-8")
    (assets / "index-abc123.css").write_text("body{margin:0}", encoding="utf-8")
    return tmp_path


def test_root_serves_html(fake_dist):
    """dist存在时，/ 返回Dashboard HTML."""
    with patch.object(Path, "resolve", lambda self: fake_dist / "src" / "aiteam" / "api" / "app.py"):
        app = create_app()

    client = _make_client(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "AI Team OS Dashboard" in resp.text


def test_assets_served(fake_dist):
    """dist存在时，/assets/ 下的文件可访问."""
    with patch.object(Path, "resolve", lambda self: fake_dist / "src" / "aiteam" / "api" / "app.py"):
        app = create_app()

    client = _make_client(app)
    resp = client.get("/assets/index-abc123.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_api_routes_not_intercepted(fake_dist):
    """API路由不被静态文件拦截，/api/teams返回JSON."""
    with patch.object(Path, "resolve", lambda self: fake_dist / "src" / "aiteam" / "api" / "app.py"):
        app = create_app()

    client = _make_client(app)
    resp = client.get("/api/teams")
    # API应该响应（可能500因为没有初始化deps，但不应该返回HTML）
    assert resp.headers.get("content-type", "").startswith("application/json")


def test_app_works_without_dist():
    """dist不存在时，应用正常启动，API正常工作."""
    with patch.object(
        Path, "resolve",
        lambda self: Path(tempfile.mkdtemp()) / "src" / "aiteam" / "api" / "app.py",
    ):
        app = create_app()

    client = _make_client(app)
    # API应可访问（可能500因为没有deps，但应用本身不崩溃）
    resp = client.get("/api/teams")
    assert resp.headers.get("content-type", "").startswith("application/json")
