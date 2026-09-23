"""Leader briefings must be filterable by project and tag.

``leader_briefings`` already carried ``project_id`` (auto-stamped by
``briefing_add``), but nothing downstream exposed it: the ``briefing_list`` MCP
tool had no project parameter, and there was no tag dimension at all — so a
Leader could not ask "what decisions are pending for *this* project / *this*
topic" without eyeballing the whole queue.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import COLUMNS_TO_ENSURE, close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture()
def client():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = EventBus(repo=repo)
    deps._manager = TeamManager(repository=repo, memory=memory)

    app = create_app()

    from contextlib import asynccontextmanager

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


def _add(client, **kwargs) -> dict:
    body = {"title": "t", "description": "", "urgency": "medium"}
    body.update(kwargs)
    resp = client.post("/api/leader-briefings", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_tags_column_is_in_migration_list():
    """ORM field additions must ship with their migration entry."""
    pairs = {(t, c) for t, c, _ in COLUMNS_TO_ENSURE}
    assert ("leader_briefings", "tags") in pairs


def test_create_and_read_back_tags(client):
    created = _add(client, title="带标签", tags=["release", "governance"])
    assert created["tags"] == ["release", "governance"]

    items = client.get("/api/leader-briefings").json()["items"]
    assert items[0]["tags"] == ["release", "governance"]


def test_tags_default_to_empty_list(client):
    created = _add(client, title="无标签")
    assert created["tags"] == []


def test_list_response_carries_project_id_and_tags(client):
    _add(client, title="有归属", project_id="proj-a", tags=["ci"])
    item = client.get("/api/leader-briefings").json()["items"][0]
    assert item["project_id"] == "proj-a"
    assert item["tags"] == ["ci"]


def test_filter_by_tag(client):
    _add(client, title="发布相关", tags=["release"])
    _add(client, title="治理相关", tags=["governance"])
    _add(client, title="两者", tags=["release", "governance"])

    titles = [
        i["title"] for i in client.get("/api/leader-briefings?tag=release").json()["items"]
    ]
    assert sorted(titles) == ["两者", "发布相关"]


def test_filter_by_project_and_tag_together(client):
    _add(client, title="A-release", project_id="proj-a", tags=["release"])
    _add(client, title="A-other", project_id="proj-a", tags=["misc"])
    _add(client, title="B-release", project_id="proj-b", tags=["release"])

    items = client.get(
        "/api/leader-briefings?project_id=proj-a&tag=release"
    ).json()["items"]
    assert [i["title"] for i in items] == ["A-release"]


def test_tag_filter_does_not_match_substrings(client):
    """A tag filter matches whole tags, not JSON text fragments."""
    _add(client, title="精确", tags=["release"])
    _add(client, title="不该命中", tags=["release-candidate"])

    titles = [
        i["title"] for i in client.get("/api/leader-briefings?tag=release").json()["items"]
    ]
    assert titles == ["精确"]


# ---------------------------------------------------------------------------
# 项目头下的默认口径：本项目 + 无归属。MCP 客户端每个请求都带项目头，旧实现把
# 通用项目过滤套在简报上，无归属条目（钩子与后台检查至今仍在产出）全部被藏：
# 真库 pending 307 条，briefing_list 只返回 1 条。
# ---------------------------------------------------------------------------


def _seed_three_way(client) -> None:
    _add(client, title="A 的", project_id="proj-a")
    _add(client, title="B 的", project_id="proj-b")
    _add(client, title="无归属")


def _titles(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return sorted(i["title"] for i in resp.json()["items"])


def test_scoped_default_includes_unstamped_items(client):
    _seed_three_way(client)
    resp = client.get("/api/leader-briefings", headers={"X-Project-Id": "proj-a"})
    assert _titles(resp) == ["A 的", "无归属"]


def test_scoped_explicit_project_lists_only_stamped_items(client):
    _seed_three_way(client)
    resp = client.get(
        "/api/leader-briefings?project_id=proj-a", headers={"X-Project-Id": "proj-a"}
    )
    assert _titles(resp) == ["A 的"]


def test_explicit_project_is_not_masked_by_session_scope(client):
    _seed_three_way(client)
    resp = client.get(
        "/api/leader-briefings?project_id=proj-b", headers={"X-Project-Id": "proj-a"}
    )
    assert _titles(resp) == ["B 的"]


def test_unscoped_default_lists_everything(client):
    _seed_three_way(client)
    assert _titles(client.get("/api/leader-briefings")) == ["A 的", "B 的", "无归属"]


def test_scoped_default_respects_status_and_tag(client):
    _add(client, title="无归属-release", tags=["release"])
    _add(client, title="无归属-misc", tags=["misc"])
    done = _add(client, title="无归属-已决", tags=["release"])
    client.put(f"/api/leader-briefings/{done['id']}/resolve", json={"resolution": "ok"})

    resp = client.get("/api/leader-briefings?tag=release", headers={"X-Project-Id": "proj-a"})
    assert _titles(resp) == ["无归属-release"]
