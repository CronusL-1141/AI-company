"""Unit tests for git_ops MCP tools.

Tests cover:
- git_auto_commit parameter validation (sensitive file blocking, no-changes guard)
- git_create_pr parameter validation (main-branch guard, same-branch guard)
- git_status_check on non-repo path
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

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
        def tool(self, *args, **kwargs):
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
        def tool(self, *args, **kwargs):
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
        def tool(self, *args, **kwargs):
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
        def tool(self, *args, **kwargs):
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
        def tool(self, *args, **kwargs):
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
        def tool(self, *args, **kwargs):
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
        def tool(self, *args, **kwargs):
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
        def tool(self, *args, **kwargs):
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
