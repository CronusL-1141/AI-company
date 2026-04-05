"""Unit tests for completion verifier."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from aiteam.loop.completion_verifier import check_completion_signal, verify_completion
from aiteam.types import TaskStatus


def _make_task(status=TaskStatus.COMPLETED, memos=None):
    task = MagicMock()
    task.title = "Test Task"
    task.status = status
    task.config = {"memo": memos or []}
    return task


@pytest.mark.asyncio
async def test_verify_completion_task_not_found():
    repo = AsyncMock()
    repo.get_task.return_value = None

    result = await verify_completion("missing-id", repo)
    assert result["success"] is False
    assert result["passed"] is False
    assert "not found" in result["issues"][0]


@pytest.mark.asyncio
async def test_verify_completion_passes():
    """Completed task with memos and summary → passed."""
    memos = [
        {"type": "progress", "content": "did work", "author": "dev"},
        {"type": "summary", "content": "all done", "author": "dev"},
    ]
    repo = AsyncMock()
    repo.get_task.return_value = _make_task(status=TaskStatus.COMPLETED, memos=memos)

    result = await verify_completion("task-1", repo)
    assert result["passed"] is True
    assert result["issues"] == []
    assert result["memo_count"] == 2


@pytest.mark.asyncio
async def test_verify_completion_fails_no_memo():
    """Completed but no memo → fails."""
    repo = AsyncMock()
    repo.get_task.return_value = _make_task(status=TaskStatus.COMPLETED, memos=[])

    result = await verify_completion("task-2", repo)
    assert result["passed"] is False
    assert any("memo" in i.lower() for i in result["issues"])


@pytest.mark.asyncio
async def test_verify_completion_fails_no_summary():
    """Has memo but no summary-type → fails."""
    memos = [{"type": "progress", "content": "started", "author": "dev"}]
    repo = AsyncMock()
    repo.get_task.return_value = _make_task(status=TaskStatus.COMPLETED, memos=memos)

    result = await verify_completion("task-3", repo)
    assert result["passed"] is False
    assert any("summary" in i.lower() for i in result["issues"])


@pytest.mark.asyncio
async def test_verify_completion_fails_wrong_status():
    """Task not completed → reports status issue."""
    memos = [
        {"type": "progress", "content": "started", "author": "dev"},
        {"type": "summary", "content": "done", "author": "dev"},
    ]
    repo = AsyncMock()
    repo.get_task.return_value = _make_task(status=TaskStatus.RUNNING, memos=memos)

    result = await verify_completion("task-4", repo)
    assert result["passed"] is False
    assert any("status" in i.lower() or "completed" in i.lower() for i in result["issues"])


# ---- check_completion_signal ----

def test_completion_signal_english():
    assert check_completion_signal("Task completed successfully") is True
    assert check_completion_signal("Implementation done!") is True
    assert check_completion_signal("All finished.") is True


def test_completion_signal_chinese():
    assert check_completion_signal("任务已完成") is True
    assert check_completion_signal("实现完成，请审查") is True


def test_completion_signal_negative():
    assert check_completion_signal("Still working on it") is False
    assert check_completion_signal("Blocked by dependency") is False
    assert check_completion_signal("In progress") is False
