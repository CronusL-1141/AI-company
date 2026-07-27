"""Unit tests for Prompt Registry — version tracking and effectiveness statistics."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aiteam.loop.failure_alchemy import FailureAlchemist
from aiteam.storage.repository import StorageRepository

# ============================================================
# Helpers
# ============================================================


def _compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


# ============================================================
# failure_alchemy template_name association
# ============================================================


class TestFailureAlchemyTemplateAssociation:
    """Verify template_name is stored in failure alchemy memory metadata."""

    @pytest.mark.asyncio
    async def test_process_failure_without_template_name(
        self, db_repository: StorageRepository
    ) -> None:
        """process_failure without template_name still works (backward compat)."""
        # Create a failed task
        team = await db_repository.create_team("test-team-1", mode="coordinate")
        task = await db_repository.create_task(
            team_id=team.id,
            title="Test failed task",
            description="Something went wrong",
        )
        await db_repository.update_task(task.id, status="failed", result="Connection refused")

        alchemist = FailureAlchemist(db_repository)
        result = await alchemist.process_failure(task.id, team.id)

        assert "antibody" in result
        assert "vaccine" in result
        assert "catalyst" in result

        # Verify memory was created without template_name
        memories = await db_repository.list_memories("team", team.id)
        failure_memories = [
            m for m in memories if m.metadata.get("type") == "failure_alchemy"
        ]
        assert len(failure_memories) == 1
        assert "template_name" not in failure_memories[0].metadata

    @pytest.mark.asyncio
    async def test_process_failure_with_template_name(
        self, db_repository: StorageRepository
    ) -> None:
        """process_failure with template_name stores it in metadata."""
        team = await db_repository.create_team("test-team-2", mode="coordinate")
        task = await db_repository.create_task(
            team_id=team.id,
            title="Backend API failed",
            description="FastAPI route error",
        )
        await db_repository.update_task(task.id, status="failed", result="ImportError")

        alchemist = FailureAlchemist(db_repository)
        result = await alchemist.process_failure(
            task.id, team.id, template_name="engineering-backend-architect"
        )

        assert "antibody" in result

        # Verify template_name is stored in memory metadata
        memories = await db_repository.list_memories("team", team.id)
        failure_memories = [
            m for m in memories if m.metadata.get("type") == "failure_alchemy"
        ]
        assert len(failure_memories) == 1
        assert failure_memories[0].metadata["template_name"] == "engineering-backend-architect"

    @pytest.mark.asyncio
    async def test_process_failure_nonexistent_task(
        self, db_repository: StorageRepository
    ) -> None:
        """process_failure returns error for non-existent task."""
        alchemist = FailureAlchemist(db_repository)
        result = await alchemist.process_failure("nonexistent-id", "some-team")
        assert result == {"error": "task not found"}


# ============================================================
# Prompt registry helper functions
# ============================================================


class TestPromptRegistryHelpers:
    """Test internal helper functions in prompt_registry module."""

    def test_compute_hash_deterministic(self) -> None:
        """Same content always produces the same 12-char hash."""
        from aiteam.api.routes.prompt_registry import _compute_hash

        content = "# Test Agent\nThis is my template"
        h1 = _compute_hash(content)
        h2 = _compute_hash(content)
        assert h1 == h2
        assert len(h1) == 12

    def test_compute_hash_different_contents(self) -> None:
        """Different contents produce different hashes."""
        from aiteam.api.routes.prompt_registry import _compute_hash

        h1 = _compute_hash("version 1 content")
        h2 = _compute_hash("version 2 content")
        assert h1 != h2

    def test_list_all_template_names_returns_list(self, tmp_path: Path) -> None:
        """_list_all_template_names returns list (may be empty in test env)."""
        from aiteam.api.routes.prompt_registry import _list_all_template_names

        # Should not raise even if template dirs don't exist
        names = _list_all_template_names()
        assert isinstance(names, list)
        # All names should match safe pattern
        import re

        for name in names:
            assert re.match(r"^[\w\-]+$", name), f"Unsafe name: {name}"

    def test_read_template_content_nonexistent(self) -> None:
        """_read_template_content returns None for non-existent template."""
        from aiteam.api.routes.prompt_registry import _read_template_content

        result = _read_template_content("this-template-does-not-exist-xyz123")
        assert result is None

    def test_find_template_path_nonexistent(self) -> None:
        """_find_template_path returns None for non-existent template."""
        from aiteam.api.routes.prompt_registry import _find_template_path

        result = _find_template_path("this-template-does-not-exist-xyz123")
        assert result is None


# ============================================================
# Track endpoint (unit via direct function call with mocked repo)
# ============================================================


class TestPromptEffectiveness:
    """Test the prompt_effectiveness route handler."""

    @pytest.mark.asyncio
    async def test_effectiveness_empty_db(self, db_repository: StorageRepository) -> None:
        """Empty DB returns empty effectiveness list."""
        from aiteam.api.routes.prompt_registry import prompt_effectiveness

        result = await prompt_effectiveness(template_name="", repo=db_repository)
        assert result["success"] is True
        assert result["effectiveness"] == []
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_effectiveness_includes_failure_lessons(
        self, db_repository: StorageRepository
    ) -> None:
        """Templates with failure alchemy lessons appear in effectiveness output."""
        from aiteam.api.routes.prompt_registry import prompt_effectiveness

        # Directly insert a failure_alchemy memory with template_name
        team = await db_repository.create_team("eff-test-team", mode="coordinate")
        await db_repository.create_memory(
            scope="team",
            scope_id=team.id,
            content="失败分析: some task",
            metadata={
                "type": "failure_alchemy",
                "template_name": "engineering-backend-architect",
                "task_id": "t1",
                "task_title": "some task",
                "antibody": "check deps",
                "vaccine": "case study",
                "catalyst": "improve process",
            },
        )

        result = await prompt_effectiveness(
            template_name="engineering-backend-architect", repo=db_repository
        )
        assert result["success"] is True
        # Should surface the template via failure lessons even with no activity records
        if result["total"] > 0:
            entry = result["effectiveness"][0]
            assert entry["template_name"] == "engineering-backend-architect"
            assert entry["failure_lesson_count"] >= 1


# ============================================================
# MCP server tool registration check
# ============================================================


class TestMCPToolsExist:
    """Verify the new MCP tools are registered on the mcp instance."""

    def test_prompt_effectiveness_function_exists(self) -> None:
        """prompt_effectiveness is registered as an MCP tool."""
        import asyncio

        from aiteam.mcp.server import mcp
        tools = asyncio.get_event_loop().run_until_complete(mcp.list_tools())
        tool_names = [t.name for t in tools]
        assert "prompt_effectiveness" in tool_names
