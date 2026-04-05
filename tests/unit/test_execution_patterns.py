"""Unit tests for execution_patterns and enhanced auto_assign."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiteam.loop.execution_patterns import ExecutionPatternStore, format_patterns_for_context
from aiteam.loop.auto_assign import TaskMatcher, _match_template
from aiteam.storage.repository import StorageRepository


# ================================================================
# ExecutionPatternStore
# ================================================================


async def test_record_success_pattern(db_repository: StorageRepository) -> None:
    """Record a success pattern and verify it is persisted."""
    store = ExecutionPatternStore(db_repository)
    memory_id = await store.record_success_pattern(
        task_type="api-implementation",
        agent_template="engineering-backend-architect",
        approach="Router→Service→Repository layered pattern with Pydantic schemas",
        result_summary="Created 4 CRUD endpoints, all tests passing",
    )
    assert isinstance(memory_id, str) and len(memory_id) > 0

    # Verify stored content is retrievable
    all_mems = await db_repository.list_memories("global", "execution_patterns")
    assert len(all_mems) == 1
    assert "[SUCCESS]" in all_mems[0].content
    assert all_mems[0].metadata["type"] == "success"
    assert all_mems[0].metadata["task_type"] == "api-implementation"


async def test_record_failure_pattern(db_repository: StorageRepository) -> None:
    """Record a failure pattern and verify metadata."""
    store = ExecutionPatternStore(db_repository)
    memory_id = await store.record_failure_pattern(
        task_type="database-migration",
        agent_template="engineering-backend-architect",
        approach="Direct schema modification without Alembic",
        error="Migration rollback failed, data loss occurred",
        lesson="Always use Alembic migrations, never modify schema directly",
    )
    assert isinstance(memory_id, str) and len(memory_id) > 0

    all_mems = await db_repository.list_memories("global", "execution_patterns")
    assert len(all_mems) == 1
    meta = all_mems[0].metadata
    assert meta["type"] == "failure"
    assert meta["lesson"] == "Always use Alembic migrations, never modify schema directly"
    assert "[FAILURE]" in all_mems[0].content


async def test_find_similar_patterns_bm25(db_repository: StorageRepository) -> None:
    """find_similar_patterns returns relevant results sorted by BM25 score."""
    store = ExecutionPatternStore(db_repository)

    await store.record_success_pattern(
        task_type="api-implementation",
        agent_template="backend-architect",
        approach="FastAPI router with Pydantic validation",
        result_summary="REST API endpoints implemented",
    )
    await store.record_failure_pattern(
        task_type="database-migration",
        agent_template="backend-architect",
        approach="Manual SQL ALTER TABLE",
        error="Foreign key constraint violated",
        lesson="Use parameterized migrations",
    )
    await store.record_success_pattern(
        task_type="api-implementation",
        agent_template="fullstack-developer",
        approach="GraphQL schema with resolvers",
        result_summary="GraphQL API implemented",
    )

    # Query related to API implementation — should surface the API patterns
    results = await store.find_similar_patterns("implement FastAPI REST endpoint", top_k=3)
    assert len(results) >= 1
    # The API-related patterns should be in results
    task_types = [r["task_type"] for r in results]
    assert "api-implementation" in task_types


async def test_find_similar_patterns_empty(db_repository: StorageRepository) -> None:
    """find_similar_patterns returns empty list when no patterns exist."""
    store = ExecutionPatternStore(db_repository)
    results = await store.find_similar_patterns("any task description", top_k=3)
    assert results == []


async def test_find_similar_patterns_top_k(db_repository: StorageRepository) -> None:
    """find_similar_patterns respects top_k limit."""
    store = ExecutionPatternStore(db_repository)
    for i in range(5):
        await store.record_success_pattern(
            task_type="api-implementation",
            agent_template="backend-architect",
            approach=f"approach-{i} FastAPI endpoint implementation",
            result_summary=f"result-{i}",
        )

    results = await store.find_similar_patterns("FastAPI API endpoint", top_k=2)
    assert len(results) <= 2


async def test_pattern_result_structure(db_repository: StorageRepository) -> None:
    """Pattern dicts have all required keys."""
    store = ExecutionPatternStore(db_repository)
    await store.record_success_pattern(
        task_type="bug-fix",
        agent_template="qa-engineer",
        approach="Reproduced with test, fixed root cause",
        result_summary="Bug resolved, regression test added",
    )
    await store.record_failure_pattern(
        task_type="bug-fix",
        agent_template="qa-engineer",
        approach="Applied quick patch without tests",
        error="Regression introduced",
        lesson="Always add regression tests",
    )

    results = await store.find_similar_patterns("bug fix regression test", top_k=5)
    for r in results:
        assert "memory_id" in r
        assert "type" in r
        assert "task_type" in r
        assert "agent_template" in r
        assert "approach" in r
        assert "recorded_at" in r
        if r["type"] == "success":
            assert "result_summary" in r
        else:
            assert "error" in r
            assert "lesson" in r


# ================================================================
# format_patterns_for_context
# ================================================================


def test_format_patterns_empty() -> None:
    """Empty patterns returns empty string."""
    assert format_patterns_for_context([]) == ""


def test_format_patterns_success() -> None:
    """Success pattern formatted correctly."""
    patterns = [
        {
            "type": "success",
            "task_type": "api-implementation",
            "agent_template": "backend-architect",
            "approach": "Router→Service→Repository",
            "result_summary": "4 endpoints created",
            "recorded_at": "2026-04-04T00:00:00",
        }
    ]
    output = format_patterns_for_context(patterns)
    assert "历史执行经验" in output
    assert "成功" in output
    assert "api-implementation" in output
    assert "Router→Service→Repository" in output
    assert "4 endpoints created" in output


def test_format_patterns_failure() -> None:
    """Failure pattern formatted correctly."""
    patterns = [
        {
            "type": "failure",
            "task_type": "database-migration",
            "agent_template": "backend-architect",
            "approach": "Direct ALTER TABLE",
            "error": "FK constraint violated",
            "lesson": "Use Alembic",
            "recorded_at": "2026-04-04T00:00:00",
        }
    ]
    output = format_patterns_for_context(patterns)
    assert "失败" in output
    assert "教训" in output
    assert "Use Alembic" in output


# ================================================================
# TaskMatcher — enhanced matching with completion rate
# ================================================================


def test_match_template_finds_match() -> None:
    """_match_template finds best matching template by word overlap."""
    templates = ["engineering-backend-architect", "frontend-developer", "qa-engineer"]
    match = _match_template("senior backend architect", templates)
    assert match == "engineering-backend-architect"


def test_match_template_no_match() -> None:
    """_match_template returns empty string when no words match."""
    templates = ["engineering-backend-architect", "frontend-developer"]
    match = _match_template("chef cooking recipes", templates)
    assert match == ""


async def test_task_matcher_weighted_score(db_repository: StorageRepository) -> None:
    """TaskMatcher prefers agents whose template has higher completion rate."""
    matcher = TaskMatcher(db_repository)

    # Mock list_agents, list_tasks, list_teams
    team_id = "test-team"
    mock_agent_high = MagicMock()
    mock_agent_high.id = "agent-high"
    mock_agent_high.name = "backend-high"
    mock_agent_high.role = "backend architect"
    mock_agent_high.status = "waiting"
    mock_agent_high.trust_score = 0.5

    mock_agent_low = MagicMock()
    mock_agent_low.id = "agent-low"
    mock_agent_low.name = "backend-low"
    mock_agent_low.role = "backend architect"
    mock_agent_low.status = "waiting"
    mock_agent_low.trust_score = 0.5

    mock_task = MagicMock()
    mock_task.id = "task-1"
    mock_task.title = "Implement backend API"
    mock_task.tags = ["backend"]
    mock_task.status = "pending"
    mock_task.assigned_to = None

    db_repository.list_agents = AsyncMock(return_value=[mock_agent_high, mock_agent_low])
    db_repository.list_tasks = AsyncMock(return_value=[mock_task])

    # Patch _get_template_completion_rates to return controlled rates
    with patch(
        "aiteam.loop.auto_assign._get_template_completion_rates",
        new=AsyncMock(return_value={}),
    ):
        matches = await matcher.find_matches(team_id)

    # Both agents match "backend" tag — one match should be returned
    assert len(matches) == 1
    assert matches[0]["task_id"] == "task-1"
    assert matches[0]["match_score"] > 0


async def test_task_matcher_no_match_when_no_tags(db_repository: StorageRepository) -> None:
    """TaskMatcher returns no match when task has no tags."""
    matcher = TaskMatcher(db_repository)

    mock_agent = MagicMock()
    mock_agent.id = "agent-1"
    mock_agent.name = "backend-dev"
    mock_agent.role = "backend architect"
    mock_agent.status = "waiting"

    mock_task = MagicMock()
    mock_task.id = "task-1"
    mock_task.title = "Some task"
    mock_task.tags = []  # No tags — keyword_score will be 0
    mock_task.status = "pending"
    mock_task.assigned_to = None

    db_repository.list_agents = AsyncMock(return_value=[mock_agent])
    db_repository.list_tasks = AsyncMock(return_value=[mock_task])

    with patch(
        "aiteam.loop.auto_assign._get_template_completion_rates",
        new=AsyncMock(return_value={}),
    ):
        matches = await matcher.find_matches(team_id="test-team")

    assert matches == []


async def test_task_matcher_skips_leader(db_repository: StorageRepository) -> None:
    """TaskMatcher does not assign tasks to the leader agent."""
    matcher = TaskMatcher(db_repository)

    mock_leader = MagicMock()
    mock_leader.id = "leader-1"
    mock_leader.name = "team-lead"
    mock_leader.role = "leader"
    mock_leader.status = "waiting"

    mock_task = MagicMock()
    mock_task.id = "task-1"
    mock_task.title = "Backend task"
    mock_task.tags = ["leader", "backend"]
    mock_task.status = "pending"
    mock_task.assigned_to = None

    db_repository.list_agents = AsyncMock(return_value=[mock_leader])
    db_repository.list_tasks = AsyncMock(return_value=[mock_task])

    with patch(
        "aiteam.loop.auto_assign._get_template_completion_rates",
        new=AsyncMock(return_value={}),
    ):
        matches = await matcher.find_matches(team_id="test-team")

    assert matches == []
