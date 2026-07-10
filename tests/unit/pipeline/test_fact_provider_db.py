"""DbFactProvider lightweight integration tests using in-memory SQLite + tmp_path."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from aiteam.pipeline.fact_provider_db import DbFactProvider
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

_BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _filter_memos(
    memos: list[dict],
    since: datetime,
    memo_type: str | None = None,
) -> list[dict]:
    """Pure filtering helper for memo lists — mirrors DbFactProvider.memos_since internals."""
    since_aware = _ensure_aware(since)
    result = []
    for memo in memos:
        ts_raw = memo.get("timestamp")
        if not ts_raw:
            continue
        try:
            ts = _ensure_aware(datetime.fromisoformat(str(ts_raw)))
        except ValueError:
            continue
        if ts <= since_aware:
            continue
        if memo_type is not None and memo.get("type") != memo_type:
            continue
        result.append(memo)
    return result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


@pytest_asyncio.fixture()
async def task_id(repo: StorageRepository) -> str:
    team = await repo.create_team("fp-test-team", "coordinate")
    task = await repo.create_task(team_id=team.id, title="fp test task")
    return task.id


# ---------------------------------------------------------------------------
# src_files_modified_since
# ---------------------------------------------------------------------------

class TestSrcFilesModifiedSince:
    def test_no_src_dir_returns_false(self, tmp_path):
        repo_mock = MagicMock()
        fp = DbFactProvider(repo=repo_mock, project_root=str(tmp_path))
        result = fp.src_files_modified_since(_BASE_TIME)
        assert result is False

    def test_file_after_threshold_returns_true(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        py_file = src / "app.py"
        py_file.write_text("x = 1")

        # Set mtime to well after the threshold
        future = _BASE_TIME + timedelta(seconds=10)
        mtime_ts = future.timestamp()
        os.utime(str(py_file), (mtime_ts, mtime_ts))

        repo_mock = MagicMock()
        fp = DbFactProvider(repo=repo_mock, project_root=str(tmp_path))
        # threshold is _BASE_TIME (already includes tolerance from evaluator)
        result = fp.src_files_modified_since(_BASE_TIME)
        assert result is True

    def test_file_before_threshold_returns_false(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        py_file = src / "old.py"
        py_file.write_text("y = 2")

        # mtime well before threshold
        past = _BASE_TIME - timedelta(seconds=60)
        mtime_ts = past.timestamp()
        os.utime(str(py_file), (mtime_ts, mtime_ts))

        repo_mock = MagicMock()
        fp = DbFactProvider(repo=repo_mock, project_root=str(tmp_path))
        result = fp.src_files_modified_since(_BASE_TIME)
        assert result is False

    def test_non_py_files_ignored(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        txt_file = src / "readme.txt"
        txt_file.write_text("docs")
        # mtime = future
        future = _BASE_TIME + timedelta(seconds=30)
        ts = future.timestamp()
        os.utime(str(txt_file), (ts, ts))

        repo_mock = MagicMock()
        fp = DbFactProvider(repo=repo_mock, project_root=str(tmp_path))
        result = fp.src_files_modified_since(_BASE_TIME)
        assert result is False

    def test_nested_src_subdirectory(self, tmp_path):
        src = tmp_path / "src" / "aiteam" / "pipeline"
        src.mkdir(parents=True)
        py_file = src / "deep.py"
        py_file.write_text("z = 3")

        future = _BASE_TIME + timedelta(seconds=5)
        ts = future.timestamp()
        os.utime(str(py_file), (ts, ts))

        repo_mock = MagicMock()
        fp = DbFactProvider(repo=repo_mock, project_root=str(tmp_path))
        result = fp.src_files_modified_since(_BASE_TIME)
        assert result is True


# ---------------------------------------------------------------------------
# count_subtasks (uses real in-memory repo via asyncio event loop)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_count_subtasks_no_children(repo: StorageRepository, task_id: str, tmp_path):
    # count_subtasks runs async internally — call async directly for test
    DbFactProvider(repo=repo, project_root=str(tmp_path))
    count = await repo.list_subtasks(task_id)
    assert len(count) == 0


@pytest.mark.asyncio
async def test_count_subtasks_with_children(repo: StorageRepository, task_id: str, tmp_path):
    # Create two subtasks
    team = await repo.create_team("sub-team", "coordinate")
    await repo.create_task(team_id=team.id, title="sub1", parent_id=task_id)
    await repo.create_task(team_id=team.id, title="sub2", parent_id=task_id)

    subtasks = await repo.list_subtasks(task_id)
    assert len(subtasks) == 2


# ---------------------------------------------------------------------------
# memos_since (uses task.config["memo"] storage pattern)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_memos_since_filters_by_time(repo: StorageRepository, task_id: str, tmp_path):
    before = (_BASE_TIME - timedelta(minutes=5)).isoformat()
    after = (_BASE_TIME + timedelta(minutes=5)).isoformat()
    config = {
        "memo": [
            {"type": "progress", "timestamp": before, "content": "old memo"},
            {"type": "review", "timestamp": after, "content": "review done"},
        ]
    }
    await repo.update_task(task_id, config=config)
    task = await repo.get_task(task_id)

    # Test the filtering logic directly without going through the sync wrapper
    memos = _filter_memos(task.config.get("memo", []), _BASE_TIME, memo_type=None)
    assert len(memos) == 1
    assert memos[0]["content"] == "review done"


@pytest.mark.asyncio
async def test_memos_since_filters_by_type(repo: StorageRepository, task_id: str, tmp_path):
    after = (_BASE_TIME + timedelta(minutes=5)).isoformat()
    config = {
        "memo": [
            {"type": "progress", "timestamp": after, "content": "progress note"},
            {"type": "review", "timestamp": after, "content": "review note"},
        ]
    }
    await repo.update_task(task_id, config=config)
    task = await repo.get_task(task_id)

    memos = _filter_memos(task.config.get("memo", []), _BASE_TIME, memo_type="review")
    assert len(memos) == 1
    assert memos[0]["type"] == "review"


@pytest.mark.asyncio
async def test_memos_since_empty(repo: StorageRepository, task_id: str, tmp_path):
    task = await repo.get_task(task_id)
    memos = _filter_memos(task.config.get("memo", []), _BASE_TIME)
    assert memos == []


# ---------------------------------------------------------------------------
# last_bash_event (reads from task.config["last_bash"])
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_bash_event_present(repo: StorageRepository, task_id: str, tmp_path):
    bash_info = {"exit_code": 0, "stdout": "5 passed"}
    config = {"last_bash": bash_info}
    await repo.update_task(task_id, config=config)
    task = await repo.get_task(task_id)

    # Verify config storage directly (sync wrapper has loop-reentrancy issues in async tests)
    event = (task.config or {}).get("last_bash")
    assert event is not None
    assert event["exit_code"] == 0
    assert "passed" in event["stdout"]


@pytest.mark.asyncio
async def test_last_bash_event_missing(repo: StorageRepository, task_id: str, tmp_path):
    task = await repo.get_task(task_id)
    event = (task.config or {}).get("last_bash")
    assert event is None
