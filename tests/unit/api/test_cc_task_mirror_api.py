"""服务端这半边的镜像契约：幂等 + CC 依赖 id 的正确落位。

CC 可能对同一个任务重复报完成（重跑、会话恢复），所以建任务这一端必须按
``cc_task_id`` 幂等——否则墙上会长出重复行。而 CC 的 ``blockedBy`` 装的是
**CC 自己的任务 id**，直接塞进 OS 的 ``depends_on`` 会让阻塞判定去查一个根本
不存在的行，因此只有已镜像过的上游才转成 depends_on，其余原样存证。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest_asyncio.fixture()
async def repo():
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def client(repo):
    from fastapi.testclient import TestClient

    from aiteam.api import deps
    from aiteam.api.app import create_app

    app = create_app()
    app.dependency_overrides[deps.get_repository] = lambda: repo
    app.dependency_overrides[deps.get_scoped_repository] = lambda: repo
    with TestClient(app) as c:
        yield c


@pytest_asyncio.fixture()
async def project(repo):
    return await repo.create_project(name="p", root_path="/tmp/p")


def _mirror(client, project_id, **body):
    payload = {
        "title": "t",
        "description": "d",
        "tags": ["cc-task"],
        "status": "completed",
        **body,
    }
    return client.post(f"/api/projects/{project_id}/tasks", json=payload)


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_same_cc_task_never_creates_a_second_row(self, client, project, repo):
        first = _mirror(client, project.id, cc_task_id="20").json()
        second = _mirror(client, project.id, cc_task_id="20", title="改过的标题").json()

        assert second["data"]["id"] == first["data"]["id"]
        assert second["data"]["title"] == "t"  # 已存在的行不被覆写
        assert len(await repo.list_tasks_by_project(project.id)) == 1

    @pytest.mark.asyncio
    async def test_different_cc_tasks_still_create_separate_rows(self, client, project, repo):
        _mirror(client, project.id, cc_task_id="20")
        _mirror(client, project.id, cc_task_id="21")
        assert len(await repo.list_tasks_by_project(project.id)) == 2

    @pytest.mark.asyncio
    async def test_plain_tasks_without_a_cc_id_are_unaffected(self, client, project, repo):
        _mirror(client, project.id)
        _mirror(client, project.id)
        assert len(await repo.list_tasks_by_project(project.id)) == 2


class TestCompletionLedgerFields:
    @pytest.mark.asyncio
    async def test_owner_and_completion_are_recorded(self, client, project):
        data = _mirror(client, project.id, cc_task_id="20", assigned_to="alice").json()["data"]
        assert data["assigned_to"] == "alice"
        assert data["status"] == "completed"
        assert data["completed_at"] is not None
        assert data["cc_task_id"] == "20"

    @pytest.mark.asyncio
    async def test_default_creation_is_still_a_pending_task(self, client, project):
        body = {"title": "普通任务", "description": ""}
        data = client.post(f"/api/projects/{project.id}/tasks", json=body).json()["data"]
        assert data["status"] == "pending"
        assert data["completed_at"] is None
        assert data["cc_task_id"] is None


class TestDependencyResolution:
    @pytest.mark.asyncio
    async def test_mirrored_upstream_becomes_a_real_depends_on(self, client, project):
        upstream = _mirror(client, project.id, cc_task_id="19").json()["data"]
        downstream = _mirror(
            client, project.id, cc_task_id="20", cc_blocked_by=["19"]
        ).json()["data"]

        assert downstream["depends_on"] == [upstream["id"]]
        assert downstream["config"]["cc_blocked_by_unresolved"] == []

    @pytest.mark.asyncio
    async def test_unmirrored_upstream_is_kept_as_provenance_not_a_fake_id(
        self, client, project
    ):
        """CC 的 id 绝不能冒充 OS 任务 id 混进 depends_on。"""
        data = _mirror(
            client, project.id, cc_task_id="20", cc_blocked_by=["19", "18"]
        ).json()["data"]

        assert data["depends_on"] == []
        assert data["config"]["cc_blocked_by"] == ["19", "18"]
        assert data["config"]["cc_blocked_by_unresolved"] == ["19", "18"]

    @pytest.mark.asyncio
    async def test_partially_mirrored_chain_splits_correctly(self, client, project):
        upstream = _mirror(client, project.id, cc_task_id="19").json()["data"]
        data = _mirror(
            client, project.id, cc_task_id="20", cc_blocked_by=["19", "18"]
        ).json()["data"]

        assert data["depends_on"] == [upstream["id"]]
        assert data["config"]["cc_blocked_by_unresolved"] == ["18"]
