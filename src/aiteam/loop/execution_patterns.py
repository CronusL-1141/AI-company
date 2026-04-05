"""Execution pattern memory — record and retrieve success/failure patterns for agent learning.

Stores structured execution patterns in MemoryStore under category='execution_pattern'.
Uses BM25 retrieval to surface relevant historical experiences for new tasks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from aiteam.memory.retriever import bm25_search, keyword_search
from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)

_SCOPE = "global"
_SCOPE_ID = "execution_patterns"
_CATEGORY = "execution_pattern"


class ExecutionPatternStore:
    """Records and retrieves agent execution patterns for cross-task learning."""

    def __init__(self, repo: StorageRepository) -> None:
        self._repo = repo

    async def record_success_pattern(
        self,
        task_type: str,
        agent_template: str,
        approach: str,
        result_summary: str,
    ) -> str:
        """Record a successful execution pattern.

        Args:
            task_type: Category of task (e.g. "api-implementation", "bug-fix").
            agent_template: Agent template name that executed the task.
            approach: Description of the approach taken.
            result_summary: Summary of what was achieved.

        Returns:
            ID of the created memory record.
        """
        content = (
            f"[SUCCESS] task_type={task_type} template={agent_template}\n"
            f"approach: {approach}\n"
            f"result: {result_summary}"
        )
        metadata: dict[str, Any] = {
            "category": _CATEGORY,
            "type": "success",
            "task_type": task_type,
            "agent_template": agent_template,
            "approach": approach,
            "result_summary": result_summary,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        memory = await self._repo.create_memory(
            scope=_SCOPE,
            scope_id=_SCOPE_ID,
            content=content,
            metadata=metadata,
        )
        memory_id = memory.id if hasattr(memory, "id") else str(memory)
        logger.info("ExecutionPatternStore: recorded success pattern for task_type='%s'", task_type)
        return memory_id

    async def record_failure_pattern(
        self,
        task_type: str,
        agent_template: str,
        approach: str,
        error: str,
        lesson: str,
    ) -> str:
        """Record a failure pattern with the lesson learned.

        Args:
            task_type: Category of task.
            agent_template: Agent template name that executed the task.
            approach: Description of the approach that failed.
            error: Error or failure description.
            lesson: Lesson learned / what to avoid next time.

        Returns:
            ID of the created memory record.
        """
        content = (
            f"[FAILURE] task_type={task_type} template={agent_template}\n"
            f"approach: {approach}\n"
            f"error: {error}\n"
            f"lesson: {lesson}"
        )
        metadata: dict[str, Any] = {
            "category": _CATEGORY,
            "type": "failure",
            "task_type": task_type,
            "agent_template": agent_template,
            "approach": approach,
            "error": error,
            "lesson": lesson,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        memory = await self._repo.create_memory(
            scope=_SCOPE,
            scope_id=_SCOPE_ID,
            content=content,
            metadata=metadata,
        )
        memory_id = memory.id if hasattr(memory, "id") else str(memory)
        logger.info("ExecutionPatternStore: recorded failure pattern for task_type='%s'", task_type)
        return memory_id

    async def find_similar_patterns(
        self,
        task_description: str,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """Find historically similar execution patterns using BM25 retrieval.

        Args:
            task_description: Description of the current task to match against.
            top_k: Maximum number of patterns to return.

        Returns:
            List of pattern dicts with keys: type, task_type, agent_template,
            approach, result_summary/lesson, recorded_at.
        """
        all_memories = await self._repo.list_memories(_SCOPE, _SCOPE_ID)
        # Filter to execution_pattern category only
        pattern_memories = [
            m for m in all_memories
            if (m.metadata or {}).get("category") == _CATEGORY
        ]
        if not pattern_memories:
            return []

        # BM25 search with fallback to keyword search
        ranked = bm25_search(pattern_memories, task_description)
        if not ranked:
            ranked = keyword_search(pattern_memories, task_description)

        results: list[dict[str, Any]] = []
        for mem in ranked[:top_k]:
            meta = mem.metadata or {}
            entry: dict[str, Any] = {
                "memory_id": mem.id,
                "type": meta.get("type", "unknown"),
                "task_type": meta.get("task_type", ""),
                "agent_template": meta.get("agent_template", ""),
                "approach": meta.get("approach", ""),
                "recorded_at": meta.get("recorded_at", ""),
            }
            if meta.get("type") == "success":
                entry["result_summary"] = meta.get("result_summary", "")
            else:
                entry["error"] = meta.get("error", "")
                entry["lesson"] = meta.get("lesson", "")
            results.append(entry)

        return results


def format_patterns_for_context(patterns: list[dict[str, Any]]) -> str:
    """Format retrieved patterns as an injectable context block.

    Args:
        patterns: List of pattern dicts from find_similar_patterns.

    Returns:
        Formatted string for injection into agent context, or empty string if no patterns.
    """
    if not patterns:
        return ""

    lines = ["## 历史执行经验"]
    for i, p in enumerate(patterns, 1):
        status = "成功" if p.get("type") == "success" else "失败"
        lines.append(f"\n[{i}] [{status}] 任务类型: {p.get('task_type', '未知')}")
        lines.append(f"    模板: {p.get('agent_template', '未知')}")
        lines.append(f"    方法: {p.get('approach', '')}")
        if p.get("type") == "success":
            lines.append(f"    结果: {p.get('result_summary', '')}")
        else:
            lines.append(f"    错误: {p.get('error', '')}")
            lines.append(f"    教训: {p.get('lesson', '')}")
    lines.append("")
    return "\n".join(lines)
