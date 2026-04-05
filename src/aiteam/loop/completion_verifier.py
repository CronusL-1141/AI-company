"""AI Team OS — Task completion verifier.

Checks whether a task is "truly" complete by verifying that the required
artifacts exist: at least one memo record, a summary-type memo, and that
the task status is marked completed.

Used by the PostToolUse completion protocol and the verify_completion MCP tool.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def verify_completion(task_id: str, repo: Any) -> dict[str, Any]:
    """Check whether a task is truly complete.

    Checks:
    1. Task exists and status == completed
    2. At least one memo record exists (task_memo_add was called)
    3. A summary-type memo exists (task_memo_add type=summary was called)

    Args:
        task_id: Task ID to verify
        repo: StorageRepository instance

    Returns:
        Verification result dict with passed bool and list of issues
    """
    issues: list[str] = []

    task = await repo.get_task(task_id)
    if task is None:
        return {
            "success": False,
            "task_id": task_id,
            "passed": False,
            "issues": [f"Task {task_id} not found"],
        }

    # Check task status
    from aiteam.types import TaskStatus
    if task.status != TaskStatus.COMPLETED:
        issues.append(
            f"Task status is '{task.status.value}', expected 'completed'. "
            "Call task_update to mark as completed."
        )

    # Check memo records (stored in task.config["memo"])
    config = task.config or {}
    memos: list[dict[str, Any]] = config.get("memo", [])

    if not memos:
        issues.append(
            "No memo records found. "
            "Call task_memo_add to record progress or decisions."
        )
    else:
        # Check for a summary-type memo
        summary_memos = [m for m in memos if m.get("type") == "summary"]
        if not summary_memos:
            issues.append(
                "No summary memo found. "
                "Call task_memo_add with type='summary' to document the final outcome."
            )

    passed = len(issues) == 0

    result: dict[str, Any] = {
        "success": True,
        "task_id": task_id,
        "task_title": task.title or task_id,
        "task_status": task.status.value if task.status else "unknown",
        "memo_count": len(memos),
        "passed": passed,
        "issues": issues,
    }

    if not passed:
        logger.info(
            "Completion verification FAILED for task %s: %d issue(s)",
            task_id,
            len(issues),
        )

    return result


def check_completion_signal(message: str) -> bool:
    """Detect whether a message contains a completion signal.

    Used by workflow_reminder PostToolUse to detect when an agent
    sends a completion report via SendMessage.

    Args:
        message: Message content to check

    Returns:
        True if the message appears to signal task completion
    """
    completion_keywords = [
        # English
        "completed", "done", "finished", "task complete", "implementation complete",
        # Chinese
        "完成", "已完成", "实现完成", "任务完成", "完毕",
    ]
    lower = message.lower()
    return any(kw.lower() in lower for kw in completion_keywords)
