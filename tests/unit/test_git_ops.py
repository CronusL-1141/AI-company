"""Unit tests for git_ops MCP tools and pipeline deploy suggestion.

Tests cover:
- git_auto_commit parameter validation (sensitive file blocking, no-changes guard)
- git_create_pr parameter validation (main-branch guard, same-branch guard)
- git_status_check on non-repo path
- pipeline deploy-stage completion _suggestion field
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.loop.pipeline import PipelineManager
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

# Import the module-level helpers from git_ops
from aiteam.mcp.tools.git_ops import (
    _check_gh_available,
    _check_git_available,
    _sanitize_branch_name,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo_fixture():
    """In-memory repo for direct PipelineManager tests."""
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    yield repo
    asyncio.get_event_loop().run_until_complete(close_db())


@pytest.fixture()
def app_client():
    """FastAPI TestClient with in-memory SQLite."""
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    manager = TeamManager(repository=repo, memory=memory)
    event_bus = EventBus(repo=repo)
    hook_translator = HookTranslator(repo=repo, event_bus=event_bus)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = manager
    deps._hook_translator = hook_translator

    app = create_app()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    app.router.lifespan_context = test_lifespan
    client = TestClient(app)
    yield client

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


@pytest.fixture()
def git_repo(tmp_path):
    """Create a temporary git repository with one initial commit."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        check=True, capture_output=True, cwd=str(tmp_path),
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        check=True, capture_output=True, cwd=str(tmp_path),
    )
    # Initial commit so HEAD exists
    readme = tmp_path / "README.md"
    readme.write_text("# test")
    subprocess.run(["git", "add", "README.md"], check=True, capture_output=True, cwd=str(tmp_path))
    subprocess.run(
        ["git", "commit", "-m", "init"],
        check=True, capture_output=True, cwd=str(tmp_path),
    )
    return tmp_path


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Helper / utility tests
# ---------------------------------------------------------------------------


def test_sanitize_branch_name_basic():
    assert _sanitize_branch_name("My Feature Branch!") == "my-feature-branch"


def test_sanitize_branch_name_special_chars():
    result = _sanitize_branch_name("fix: null-check (JIRA-123)")
    assert " " not in result
    assert "(" not in result
    assert ")" not in result


def test_sanitize_branch_name_collapses_hyphens():
    result = _sanitize_branch_name("a---b")
    assert "--" not in result


def test_check_git_available_when_present():
    """git should be available in the test environment."""
    import shutil
    if shutil.which("git"):
        assert _check_git_available() is None
    else:
        result = _check_git_available()
        assert result is not None
        assert result["success"] is False


def test_check_gh_available_returns_error_when_missing():
    """Simulate gh not installed."""
    with patch("shutil.which", return_value=None):
        result = _check_gh_available()
        assert result is not None
        assert result["success"] is False
        assert "gh" in result["error"].lower() or "GitHub" in result["error"]


# ---------------------------------------------------------------------------
# git_auto_commit — parameter validation (no real git operations needed)
# ---------------------------------------------------------------------------


def test_git_auto_commit_blocks_sensitive_files(git_repo):
    """Files matching sensitive patterns should be rejected before staging."""
    # Build a mock MCP and register tools
    registered = {}

    class MockMcp:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

    from aiteam.mcp.tools.git_ops import register
    register(MockMcp())

    commit_fn = registered["git_auto_commit"]

    result = commit_fn(
        message="should fail",
        files=[".env", "app/config.py"],
        working_dir=str(git_repo),
    )
    assert result["success"] is False
    assert ".env" in result["error"]


def test_git_auto_commit_blocks_key_files(git_repo):
    registered = {}

    class MockMcp:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

    from aiteam.mcp.tools.git_ops import register
    register(MockMcp())

    commit_fn = registered["git_auto_commit"]
    result = commit_fn(files=["server.key"], working_dir=str(git_repo))
    assert result["success"] is False
    assert "server.key" in result["error"]


def test_git_auto_commit_no_changes(git_repo):
    """When working tree is clean, commit should fail gracefully."""
    registered = {}

    class MockMcp:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

    from aiteam.mcp.tools.git_ops import register
    register(MockMcp())

    commit_fn = registered["git_auto_commit"]
    # No changes in clean repo — git add -u stages nothing
    result = commit_fn(message="empty commit", working_dir=str(git_repo))
    assert result["success"] is False
    assert "暂存" in result["error"] or "变更" in result["error"]


def test_git_auto_commit_success(git_repo):
    """Commit a real file change in a temp repo."""
    registered = {}

    class MockMcp:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

    from aiteam.mcp.tools.git_ops import register
    register(MockMcp())

    commit_fn = registered["git_auto_commit"]

    # Create and track a new file
    new_file = git_repo / "main.py"
    new_file.write_text("print('hello')")
    subprocess.run(["git", "add", "main.py"], check=True, capture_output=True, cwd=str(git_repo))
    # Now make a change to the tracked file
    new_file.write_text("print('world')")

    result = commit_fn(message="test: update main.py", working_dir=str(git_repo))
    assert result["success"] is True
    assert result["data"]["file_count"] >= 1
    assert result["data"]["commit_hash"]


def test_git_auto_commit_not_a_repo(tmp_path):
    """Non-git directory returns a clear error."""
    registered = {}

    class MockMcp:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

    from aiteam.mcp.tools.git_ops import register
    register(MockMcp())

    commit_fn = registered["git_auto_commit"]
    result = commit_fn(working_dir=str(tmp_path))
    assert result["success"] is False
    assert "git 仓库" in result["error"]


# ---------------------------------------------------------------------------
# git_create_pr — parameter validation
# ---------------------------------------------------------------------------


def _make_pr_fn():
    registered = {}

    class MockMcp:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

    from aiteam.mcp.tools.git_ops import register
    register(MockMcp())
    return registered["git_create_pr"]


def test_git_create_pr_rejects_main_branch(git_repo):
    """PR from main/master should be rejected."""
    pr_fn = _make_pr_fn()
    # Ensure we're on main
    subprocess.run(
        ["git", "checkout", "-b", "main"] if
        subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                       capture_output=True, text=True, cwd=str(git_repo)).stdout.strip() != "main"
        else ["git", "checkout", "main"],
        capture_output=True, cwd=str(git_repo)
    )
    # Force current branch to main by renaming
    subprocess.run(["git", "branch", "-m", "main"], capture_output=True, cwd=str(git_repo))

    result = pr_fn(title="should fail", working_dir=str(git_repo))
    assert result["success"] is False
    assert "主分支" in result["error"]


def test_git_create_pr_rejects_same_branch(git_repo):
    """PR where head == base should be rejected."""
    pr_fn = _make_pr_fn()

    # Get current branch name
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=str(git_repo),
    )
    current = proc.stdout.strip()

    # Create a non-main/master branch to bypass that check
    feature = "feature/test"
    subprocess.run(["git", "checkout", "-b", feature], capture_output=True, cwd=str(git_repo))

    result = pr_fn(title="same branch", base_branch=feature, working_dir=str(git_repo))
    assert result["success"] is False
    assert "相同" in result["error"]


def test_git_create_pr_no_remote(git_repo):
    """Repository with no remote should fail gracefully."""
    pr_fn = _make_pr_fn()

    subprocess.run(["git", "checkout", "-b", "feature/no-remote"], capture_output=True, cwd=str(git_repo))

    result = pr_fn(working_dir=str(git_repo))
    assert result["success"] is False
    assert "remote" in result["error"].lower() or "remote" in result.get("hint", "").lower()


def test_git_create_pr_gh_not_installed(git_repo):
    """When gh CLI is missing, return a clear error."""
    pr_fn = _make_pr_fn()

    subprocess.run(["git", "checkout", "-b", "feature/no-gh"], capture_output=True, cwd=str(git_repo))

    with patch("shutil.which", side_effect=lambda x: "/usr/bin/git" if x == "git" else None):
        result = pr_fn(working_dir=str(git_repo))
    assert result["success"] is False
    assert "gh" in result["error"].lower() or "GitHub" in result["error"]


# ---------------------------------------------------------------------------
# git_status_check
# ---------------------------------------------------------------------------


def test_git_status_check_not_a_repo(tmp_path):
    registered = {}

    class MockMcp:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

    from aiteam.mcp.tools.git_ops import register
    register(MockMcp())

    status_fn = registered["git_status_check"]
    result = status_fn(working_dir=str(tmp_path))
    assert result["success"] is False


def test_git_status_check_clean_repo(git_repo):
    registered = {}

    class MockMcp:
        def tool(self):
            def decorator(fn):
                registered[fn.__name__] = fn
                return fn
            return decorator

    from aiteam.mcp.tools.git_ops import register
    register(MockMcp())

    status_fn = registered["git_status_check"]
    result = status_fn(working_dir=str(git_repo))
    assert result["success"] is True
    assert result["data"]["is_clean"] is True
    assert result["data"]["staged_count"] == 0
    assert result["data"]["unstaged_count"] == 0


# ---------------------------------------------------------------------------
# Pipeline deploy suggestion tests
# ---------------------------------------------------------------------------


def test_pipeline_deploy_completion_includes_suggestion(repo_fixture):
    """When a feature pipeline's deploy stage completes, _suggestion is present."""
    repo = repo_fixture
    team = _run(repo.create_team("suggestion-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Deploy suggestion test"))

    mgr = PipelineManager(repo)
    # feature pipeline: research → design → implement → review → test → deploy
    _run(mgr.create_pipeline(task.id, "feature"))

    # Advance through all stages until deploy completes
    for _ in range(6):
        result = _run(mgr.advance_stage(task.id))

    assert result["success"] is True
    assert result["data"].get("pipeline_completed") is True
    assert "_suggestion" in result["data"]
    suggestion = result["data"]["_suggestion"]
    assert "git_auto_commit" in suggestion or "git" in suggestion.lower()
    assert "git_create_pr" in suggestion or "PR" in suggestion


def test_pipeline_non_deploy_completion_no_suggestion(repo_fixture):
    """Spike pipeline (no deploy stage) completes without _suggestion."""
    repo = repo_fixture
    team = _run(repo.create_team("no-suggestion-team", "coordinate"))
    task = _run(repo.create_task(team.id, "Spike no suggestion"))

    mgr = PipelineManager(repo)
    _run(mgr.create_pipeline(task.id, "spike"))  # research → report

    _run(mgr.advance_stage(task.id))  # research done
    result = _run(mgr.advance_stage(task.id))  # report done → pipeline_completed

    assert result["success"] is True
    assert result["data"].get("pipeline_completed") is True
    assert "_suggestion" not in result["data"]


def test_pipeline_deploy_suggestion_via_api(app_client):
    """End-to-end API test: feature pipeline deploy completion returns _suggestion."""
    resp = app_client.post("/api/teams", json={"name": "e2e-git-team", "mode": "coordinate"})
    team_id = resp.json()["data"]["id"]

    resp = app_client.post(
        f"/api/teams/{team_id}/tasks/run",
        json={"title": "E2E Git Suggestion", "description": "test"},
    )
    task_id = resp.json()["data"]["id"]

    app_client.post(f"/api/tasks/{task_id}/pipeline", json={"pipeline_type": "feature"})

    # Advance through all 6 stages
    last_resp = None
    for _ in range(6):
        last_resp = app_client.post(f"/api/tasks/{task_id}/pipeline/advance", json={})

    data = last_resp.json()
    assert data["success"] is True
    assert data["data"].get("pipeline_completed") is True
    assert "_suggestion" in data["data"]
