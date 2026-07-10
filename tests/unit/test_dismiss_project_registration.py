"""Unit tests for dismiss_project_registration MCP tool."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch


def _make_dismiss_tool():
    """Build a standalone callable that exercises dismiss_project_registration logic.

    We call the inner function directly by invoking register() on a mock mcp
    and capturing the decorated function.
    """
    from aiteam.mcp.tools.project import register

    captured = {}

    class MockMcp:
        def tool(self, **kwargs):
            def decorator(fn):
                captured[fn.__name__] = fn
                return fn
            return decorator

    register(MockMcp())
    return captured["dismiss_project_registration"]


class TestDismissProjectRegistration:
    """Tests for the dismiss_project_registration MCP tool."""

    def test_dismiss_creates_file_and_adds_cwd(self, tmp_path):
        """Calling dismiss writes the cwd to dismissed_projects.json."""
        dismiss_fn = _make_dismiss_tool()

        _ = tmp_path / "dismissed_projects.json"
        with patch("pathlib.Path.home", return_value=tmp_path):
            # tmp_path acts as ~, so the file will be at
            # tmp_path/.claude/data/ai-team-os/dismissed_projects.json
            test_cwd = str(tmp_path / "my-project")
            result = dismiss_fn(cwd=test_cwd)

        assert result["success"] is True
        assert result["dismissed_count"] == 1

        # Find the file that was actually written
        written_file = tmp_path / ".claude" / "data" / "ai-team-os" / "dismissed_projects.json"
        assert written_file.exists()
        data = json.loads(written_file.read_text(encoding="utf-8"))
        assert len(data["dismissed"]) == 1
        stored = data["dismissed"][0]
        # Should be normalized: lowercase, forward slashes
        assert "\\" not in stored
        assert stored == stored.lower()

    def test_dismiss_idempotent(self, tmp_path):
        """Calling dismiss twice for the same cwd only adds one entry."""
        dismiss_fn = _make_dismiss_tool()

        with patch("pathlib.Path.home", return_value=tmp_path):
            test_cwd = str(tmp_path / "my-project")
            dismiss_fn(cwd=test_cwd)
            result2 = dismiss_fn(cwd=test_cwd)

        assert result2["dismissed_count"] == 1

    def test_dismiss_appends_to_existing(self, tmp_path):
        """dismiss_project_registration appends to existing list without overwriting."""
        dismiss_fn = _make_dismiss_tool()

        # Pre-create a dismissed_projects.json with one entry
        dismissed_dir = tmp_path / ".claude" / "data" / "ai-team-os"
        dismissed_dir.mkdir(parents=True)
        existing_path = "/c/users/tuf/other-project"
        (dismissed_dir / "dismissed_projects.json").write_text(
            json.dumps({"dismissed": [existing_path]}), encoding="utf-8"
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            test_cwd = str(tmp_path / "new-project")
            result = dismiss_fn(cwd=test_cwd)

        assert result["dismissed_count"] == 2
        written_file = dismissed_dir / "dismissed_projects.json"
        data = json.loads(written_file.read_text(encoding="utf-8"))
        assert existing_path in data["dismissed"]

    def test_dismiss_empty_cwd_uses_getcwd(self, tmp_path):
        """Calling dismiss with empty cwd defaults to os.getcwd()."""
        dismiss_fn = _make_dismiss_tool()

        fake_cwd = str(tmp_path / "auto-cwd")

        with patch("pathlib.Path.home", return_value=tmp_path):
            with patch("os.getcwd", return_value=fake_cwd):
                result = dismiss_fn(cwd="")

        assert result["success"] is True
        assert result["dismissed_count"] >= 1
        cwd_norm = str(Path(fake_cwd).resolve()).replace("\\", "/").lower()
        assert result["cwd"] == cwd_norm

    def test_dismiss_result_contains_normalized_cwd(self, tmp_path):
        """Result cwd field is normalized (lowercase, forward slashes)."""
        dismiss_fn = _make_dismiss_tool()

        with patch("pathlib.Path.home", return_value=tmp_path):
            # Pass a path with backslashes to verify normalization
            test_cwd = str(tmp_path / "My Project").replace("/", "\\")
            result = dismiss_fn(cwd=test_cwd)

        assert result["success"] is True
        assert "\\" not in result["cwd"]
        assert result["cwd"] == result["cwd"].lower()
