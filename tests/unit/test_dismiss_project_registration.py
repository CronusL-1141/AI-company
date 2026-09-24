"""Unit tests for dismiss_project_registration MCP tool."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from aiteam.mcp.tools import project as project_tools


@pytest.fixture(autouse=True)
def api_calls(monkeypatch):
    """Never reach a real OS API (port 8000 on a developer machine): answer 404 unless told."""
    calls: list[tuple[str, str]] = []
    answer = {"value": {"success": False, "error": "HTTP 404: Not Found"}}

    def fake(method, path, data=None, **_kwargs):
        calls.append((method, path))
        return answer["value"]

    monkeypatch.setattr(project_tools, "_api_call", fake)
    return calls, answer


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


class TestDismissAlsoClosesTheNotice:
    """D1: saying no used to leave the folder's notice open on the Dashboard and in /os-doctor."""

    def test_the_ledger_notice_is_dismissed(self, tmp_path, api_calls):
        calls, answer = api_calls
        work = tmp_path / "work"
        work.mkdir()
        key = f"unregistered_dir:{work.resolve().as_posix()}"
        answer["value"] = {"key": key, "status": "dismissed"}
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _make_dismiss_tool()(cwd=str(work))
        assert result["notice"] == "dismissed"
        from urllib.parse import quote

        assert calls == [("POST", f"/api/notices/{quote(key, safe=':')}/dismiss")]

    def test_no_open_notice_is_fine(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _make_dismiss_tool()(cwd=str(tmp_path / "w"))["notice"] == "none"

    @pytest.mark.parametrize(("session", "host"), [("sess-1", "cc"), ("", "codex")])
    def test_api_unreachable_queues_a_local_record(self, tmp_path, api_calls, monkeypatch, session, host):
        _calls, answer = api_calls
        answer["value"] = {"success": False, "error": "无法连接到 AI Team OS API", "_error_category": "api_unavailable"}
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
        work = tmp_path / "work"
        work.mkdir()
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _make_dismiss_tool()(cwd=str(work))
        assert result["notice"] == "queued"
        records = tmp_path / ".claude" / "data" / "ai-team-os" / f"notice-local.{host}.jsonl"
        (record,) = [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines()]
        assert record["kind"] == "notice_dismiss"
        assert record["key"] == f"unregistered_dir:{work.resolve().as_posix()}"
        assert len(record["uuid"]) == 32 and record["at"].endswith("Z")
