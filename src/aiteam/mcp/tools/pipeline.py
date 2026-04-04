"""Pipeline (workflow stage) MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register all pipeline-related MCP tools."""

    @mcp.tool()
    def pipeline_create(
        task_id: str,
        pipeline_type: str,
        skip_stages: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a stage pipeline for a task, auto-generating chained subtasks.

        Pipeline types enforce a standard workflow per task type:
          feature:   Research → Design → Implement → Review → Test → Deploy
          bugfix:    Reproduce → Diagnose → Fix → Review → Test
          research:  Survey → Analyze → Report → Review
          refactor:  Analysis → Plan → Implement → Review → Test
          quick-fix: Implement → Test (shortcut)
          spike:     Research → Report (shortcut)
          hotfix:    Fix → Test (shortcut)

        Args:
            task_id: Task ID to attach the pipeline to
            pipeline_type: Pipeline type (feature/bugfix/research/refactor/quick-fix/spike/hotfix)
            skip_stages: Stage names to skip (optional, e.g. ["deploy"] to skip deployment)

        Returns:
            Pipeline overview with stages, subtask IDs, and recommended Agent template for first stage
        """
        payload: dict[str, Any] = {"pipeline_type": pipeline_type}
        if skip_stages:
            payload["skip_stages"] = skip_stages
        return _api_call("POST", f"/api/tasks/{task_id}/pipeline", payload)

    @mcp.tool()
    def pipeline_advance(
        task_id: str,
        result_summary: str = "",
    ) -> dict[str, Any]:
        """Advance the pipeline to the next stage (marks current stage as completed).

        Call this when the current stage's work is done.
        Returns the next stage info and recommended Agent template.
        When all stages are done, returns pipeline_completed=True.

        Args:
            task_id: Task ID with an active pipeline
            result_summary: Brief summary of what was accomplished in the completed stage

        Returns:
            Next stage info, Agent template recommendation, and progress
        """
        payload: dict[str, Any] = {}
        if result_summary:
            payload["result_summary"] = result_summary
        return _api_call("POST", f"/api/tasks/{task_id}/pipeline/advance", payload)

    @mcp.tool()
    def pipeline_status(task_id: str) -> dict[str, Any]:
        """Get pipeline progress overview for a task.

        Shows all stages with their status, progress percentage,
        current stage, and recommended Agent template.

        Args:
            task_id: Task ID with a pipeline

        Returns:
            Full pipeline status including all stages, stats, and progress
        """
        return _api_call("GET", f"/api/tasks/{task_id}/pipeline")
