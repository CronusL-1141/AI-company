"""Unit tests for Agent trust scoring."""

from __future__ import annotations

import pytest

from aiteam.loop.trust_scoring import get_agent_trust_scores, update_trust_score
from aiteam.storage.repository import StorageRepository


async def test_default_trust_score(db_repository: StorageRepository) -> None:
    """New agents start with trust_score=0.5."""
    team = await db_repository.create_team("t1", "coordinate")
    agent = await db_repository.create_agent(team.id, "bot", "developer")
    assert agent.trust_score == pytest.approx(0.5)


async def test_trust_score_success(db_repository: StorageRepository) -> None:
    """Task success increases trust by 0.05."""
    team = await db_repository.create_team("t1", "coordinate")
    agent = await db_repository.create_agent(team.id, "bot", "developer")

    new_score = await update_trust_score(db_repository, agent.id, "success")
    assert new_score == pytest.approx(0.55)

    refreshed = await db_repository.get_agent(agent.id)
    assert refreshed.trust_score == pytest.approx(0.55)


async def test_trust_score_failure(db_repository: StorageRepository) -> None:
    """Task failure decreases trust by 0.10."""
    team = await db_repository.create_team("t1", "coordinate")
    agent = await db_repository.create_agent(team.id, "bot", "developer")

    new_score = await update_trust_score(db_repository, agent.id, "failure")
    assert new_score == pytest.approx(0.40)


async def test_trust_score_timeout(db_repository: StorageRepository) -> None:
    """Task timeout decreases trust by 0.05."""
    team = await db_repository.create_team("t1", "coordinate")
    agent = await db_repository.create_agent(team.id, "bot", "developer")

    new_score = await update_trust_score(db_repository, agent.id, "timeout")
    assert new_score == pytest.approx(0.45)


async def test_trust_score_cap_at_one(db_repository: StorageRepository) -> None:
    """Trust score cannot exceed 1.0."""
    team = await db_repository.create_team("t1", "coordinate")
    agent = await db_repository.create_agent(team.id, "bot", "developer")
    # Start at 0.5, apply 10 successes (+0.05 each = +0.50), should cap at 1.0
    agent_id = agent.id
    for _ in range(10):
        score = await update_trust_score(db_repository, agent_id, "success")
    assert score == pytest.approx(1.0)


async def test_trust_score_floor_at_zero(db_repository: StorageRepository) -> None:
    """Trust score cannot go below 0.0."""
    team = await db_repository.create_team("t1", "coordinate")
    agent = await db_repository.create_agent(team.id, "bot", "developer")
    # Start at 0.5, apply 10 failures (-0.10 each = -1.0), should floor at 0.0
    agent_id = agent.id
    for _ in range(10):
        score = await update_trust_score(db_repository, agent_id, "failure")
    assert score == pytest.approx(0.0)


async def test_trust_score_agent_not_found(db_repository: StorageRepository) -> None:
    """Returns -1.0 when agent_id does not exist."""
    result = await update_trust_score(db_repository, "nonexistent-id", "success")
    assert result == pytest.approx(-1.0)


async def test_get_agent_trust_scores_sorted(db_repository: StorageRepository) -> None:
    """get_agent_trust_scores returns agents sorted by trust_score descending."""
    team = await db_repository.create_team("t1", "coordinate")
    a1 = await db_repository.create_agent(team.id, "agent-a", "dev")
    a2 = await db_repository.create_agent(team.id, "agent-b", "dev")
    await db_repository.create_agent(team.id, "agent-c", "dev")

    await update_trust_score(db_repository, a1.id, "success")   # 0.55
    await update_trust_score(db_repository, a2.id, "failure")   # 0.40
    # agent-c stays at 0.50

    scores = await get_agent_trust_scores(db_repository)
    trust_values = [r["trust_score"] for r in scores]
    assert trust_values == sorted(trust_values, reverse=True)

    names = [r["agent_name"] for r in scores]
    assert names[0] == "agent-a"   # highest: 0.55
    assert names[-1] == "agent-b"  # lowest: 0.40


async def test_get_agent_trust_scores_includes_team_name(db_repository: StorageRepository) -> None:
    """Each trust score record includes team_name."""
    team = await db_repository.create_team("my-team", "coordinate")
    await db_repository.create_agent(team.id, "bot", "dev")

    scores = await get_agent_trust_scores(db_repository)
    assert len(scores) == 1
    assert scores[0]["team_name"] == "my-team"
    assert "trust_score" in scores[0]
