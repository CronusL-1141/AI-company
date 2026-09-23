"""PUT /api/projects/{id} 必须校验新的 root_path。

项目根按最长前缀认领其下所有目录。改根曾不做任何校验：不存在的路径让项目再也匹配不到
会话；家目录或它的上级会把此后每个未注册目录都认领走（一个项目吞掉整台机器）；撞上
别的项目的根则在 UNIQUE 约束上炸成 500。这里每条拒绝都跨请求回读，确认库里的根没被改。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home" / "someone"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture()
def client(home: Path):
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = EventBus(repo=repo)
    deps._manager = TeamManager(repository=repo, memory=memory)

    app = create_app()

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan
    yield TestClient(app)

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None


def _create(client: TestClient, name: str, root: Path) -> str:
    resp = client.post("/api/projects", json={"name": name, "root_path": str(root)})
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def _root_of(client: TestClient, project_id: str) -> str:
    return client.get(f"/api/projects/{project_id}").json()["data"]["root_path"]


@pytest.fixture()
def project(client: TestClient, home: Path) -> tuple[str, Path]:
    root = home / "work" / "alpha"
    root.mkdir(parents=True)
    return _create(client, "alpha", root), root


def _rejected(client: TestClient, project_id: str, root_path: str, status: int = 400) -> str:
    resp = client.put(f"/api/projects/{project_id}", json={"root_path": root_path})
    assert resp.status_code == status, resp.text
    return resp.json().get("detail", "")


def test_valid_directory_is_persisted(client, project, home):
    project_id, _ = project
    new_root = home / "work" / "beta"
    new_root.mkdir()

    resp = client.put(f"/api/projects/{project_id}", json={"root_path": f"{new_root}/"})

    assert resp.status_code == 200, resp.text
    assert _root_of(client, project_id) == str(new_root)


def test_missing_directory_is_rejected(client, project, home):
    project_id, root = project
    _rejected(client, project_id, str(home / "does-not-exist"))
    assert _root_of(client, project_id) == str(root)


def test_regular_file_is_rejected(client, project, home):
    project_id, root = project
    file_path = home / "notes.txt"
    file_path.write_text("x")
    _rejected(client, project_id, str(file_path))
    assert _root_of(client, project_id) == str(root)


def test_relative_path_is_rejected(client, project):
    project_id, root = project
    _rejected(client, project_id, "work/alpha")
    assert _root_of(client, project_id) == str(root)


@pytest.mark.parametrize("which", ["home", "parent", "fs-root"])
def test_home_and_its_ancestors_are_rejected(client, project, home, which):
    project_id, root = project
    target = {"home": home, "parent": home.parent, "fs-root": Path("/")}[which]
    assert "家目录" in _rejected(client, project_id, str(target))
    assert _root_of(client, project_id) == str(root)


def test_another_projects_root_is_a_conflict(client, project, home):
    project_id, root = project
    other_root = home / "work" / "gamma"
    other_root.mkdir()
    _create(client, "gamma", other_root)

    _rejected(client, project_id, str(other_root), status=409)
    assert _root_of(client, project_id) == str(root)


def test_keeping_its_own_root_is_allowed(client, project):
    project_id, root = project
    resp = client.put(f"/api/projects/{project_id}", json={"root_path": str(root)})
    assert resp.status_code == 200, resp.text


def test_other_fields_skip_root_validation(client, project):
    project_id, root = project
    resp = client.put(f"/api/projects/{project_id}", json={"description": "只改描述"})
    assert resp.status_code == 200, resp.text
    assert _root_of(client, project_id) == str(root)
