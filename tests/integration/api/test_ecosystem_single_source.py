"""Integration tests for v1.6.0 backend single source of truth (SST) refactor.

Tests cover:
1. stage_status uses shallow_summary fallback when no deep_review exists
2. relevance_score present in list (detail=false) response
3. facet_counts includes topics dimension
4. facet_counts topics are sorted descending by count
5. facet_counts topics span full project (not truncated by query limit)
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from aiteam.api.app import create_app
from aiteam.api.deps import get_repository, get_scoped_repository
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository
from aiteam.types import EcosystemRepoProfile

PROJECT_ID = "proj-sst-test-001"


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://", project_scope=PROJECT_ID)
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def client(repo: StorageRepository) -> AsyncClient:
    app = create_app()
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_scoped_repository] = lambda: repo
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _upsert_repo(
    repo: StorageRepository,
    repo_full_name: str,
    stars: int = 5000,
    shallow_summary: str = "",
    topics: list[str] | None = None,
    relevance_score: int = 7,
) -> EcosystemRepoProfile:
    profile = EcosystemRepoProfile(
        repo_full_name=repo_full_name,
        name=repo_full_name.split("/")[-1],
        owner=repo_full_name.split("/")[0],
        stars=stars,
        project_id=PROJECT_ID,
        shallow_summary=shallow_summary,
        topics=topics or [],
        relevance_score=relevance_score,
    )
    await repo.upsert_ecosystem_profile(profile, project_id=PROJECT_ID)
    fetched = await repo.get_ecosystem_profile(repo_full_name, project_id=PROJECT_ID)
    assert fetched is not None
    return fetched


# ──────────────────────────────────────────────────────────────
# Test 1: stage_status shallow_summary fallback
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stage_status_uses_shallow_summary_fallback(client: AsyncClient, repo: StorageRepository) -> None:
    """Repo with shallow_summary but 0 deep_reviews must return stage_status='shallow_done'."""
    await _upsert_repo(repo, "owner/has-summary", shallow_summary="This repo does cool things.")

    resp = await client.get(
        "/api/ecosystem/profiles",
        params={"keyword": "has-summary"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    profiles = resp.json()["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["stage_status"] == "shallow_done"


@pytest.mark.asyncio
async def test_stage_status_queued_when_no_summary_no_review(client: AsyncClient, repo: StorageRepository) -> None:
    """Repo with no shallow_summary and no deep_reviews must return stage_status='queued'."""
    await _upsert_repo(repo, "owner/no-summary", shallow_summary="")

    resp = await client.get(
        "/api/ecosystem/profiles",
        params={"keyword": "no-summary"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    profiles = resp.json()["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["stage_status"] == "queued"


# ──────────────────────────────────────────────────────────────
# Test 2: relevance_score in list (detail=false) response
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_relevance_score_not_exposed(client: AsyncClient, repo: StorageRepository) -> None:
    """v1.6.1 弃用锁：relevance_score（硬编码启发式）不再透出到列表响应。"""
    await _upsert_repo(repo, "owner/scored-repo", relevance_score=8)

    resp = await client.get(
        "/api/ecosystem/profiles",
        params={"keyword": "scored-repo"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    profiles = resp.json()["profiles"]
    assert len(profiles) == 1
    assert "relevance_score" not in profiles[0]


@pytest.mark.asyncio
async def test_relevance_category_not_exposed(client: AsyncClient, repo: StorageRepository) -> None:
    """v1.6.1 弃用锁：relevance_category 同样不再透出（与 relevance_score 同批弃用）。"""
    profile = EcosystemRepoProfile(
        repo_full_name="owner/zero-score",
        name="zero-score",
        owner="owner",
        stars=5000,
        project_id=PROJECT_ID,
        relevance_score=0,
    )
    await repo.upsert_ecosystem_profile(profile, project_id=PROJECT_ID)

    resp = await client.get(
        "/api/ecosystem/profiles",
        params={"keyword": "zero-score"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    profiles = resp.json()["profiles"]
    assert len(profiles) == 1
    assert "relevance_category" not in profiles[0]


# ──────────────────────────────────────────────────────────────
# Test 3: facet_counts includes topics dimension
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_facet_counts_topics_present(client: AsyncClient, repo: StorageRepository) -> None:
    """facet_counts=true must include a 'topics' key in the response."""
    await _upsert_repo(repo, "owner/topic-repo-a", topics=["mcp", "agent"])

    resp = await client.get(
        "/api/ecosystem/profiles",
        params={"facet_counts": "true"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "facet_counts" in data
    assert "topics" in data["facet_counts"]


# ──────────────────────────────────────────────────────────────
# Test 4: facet_counts topics sorted descending by count
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_facet_counts_topics_descending(client: AsyncClient, repo: StorageRepository) -> None:
    """topics dict must be sorted by count desc — first entry has highest count."""
    # "mcp" appears in 2 repos, "agent" in 1
    await _upsert_repo(repo, "owner/t-repo-1", topics=["mcp", "agent"])
    await _upsert_repo(repo, "owner/t-repo-2", topics=["mcp"])

    resp = await client.get(
        "/api/ecosystem/profiles",
        params={"facet_counts": "true"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    topics = resp.json()["facet_counts"]["topics"]
    assert isinstance(topics, dict)
    assert len(topics) >= 2

    items = list(topics.items())
    assert items[0][0] == "mcp"
    assert items[0][1] == 2
    # "agent" should be after "mcp"
    topic_names = [k for k, _ in items]
    assert topic_names.index("mcp") < topic_names.index("agent")


# ──────────────────────────────────────────────────────────────
# Test 5: facet_counts topics spans full project, not limited by query limit
# ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_facet_counts_topics_full_project(client: AsyncClient, repo: StorageRepository) -> None:
    """topics facet must count repos beyond the query limit parameter."""
    # Create 5 repos all tagged "rare-topic", but only return limit=2 in search
    for i in range(5):
        await _upsert_repo(repo, f"owner/full-proj-repo-{i}", topics=["rare-topic"], stars=5000 - i * 10)

    resp = await client.get(
        "/api/ecosystem/profiles",
        params={"facet_counts": "true", "limit": "2"},
        headers={"X-Project-Id": PROJECT_ID},
    )
    assert resp.status_code == 200
    data = resp.json()
    # The profiles list is paginated (limit=2)
    assert len(data["profiles"]) == 2
    # But topics facet should count all 5 repos, not just the 2 returned
    topics = data["facet_counts"]["topics"]
    assert "rare-topic" in topics
    assert topics["rare-topic"] == 5
