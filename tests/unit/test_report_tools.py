"""Unit tests for report_save / report_list / report_read MCP tools."""

from __future__ import annotations

import hashlib
import os
import pathlib
from datetime import date
from unittest.mock import patch

import aiteam.mcp.tools.reports as rpt

# Import the mcp instance to call registered tool functions
from aiteam.mcp.server import mcp as _mcp

# The tool functions are nested inside register(), but the module-level
# helpers (_get_reports_dir, _ensure_reports_dir, _parse_report_filename)
# are importable directly. For calling tool functions, we use the helpers
# at module level and invoke the logic through the module's internal funcs.

# Since tools are registered as nested functions via @mcp.tool(), we need
# a way to call them in tests. We'll invoke them through asyncio since
# FastMCP tools are async-compatible, but the original test called them
# synchronously via srv.report_save(...). The simplest fix: re-create
# thin wrappers that call the module-level helpers directly.

def _report_save(**kwargs):
    """Test helper: call report_save logic directly."""
    import asyncio
    return asyncio.get_event_loop().run_until_complete(
        _mcp.call_tool("report_save", kwargs)
    )


# Actually, the cleanest approach is to test the internal helpers directly
# and use the registered tools via the mcp instance. But since the original
# tests call srv.report_save() synchronously and check return dicts,
# let's use a simpler pattern: import the register function, call it on a
# mock mcp to capture the tool functions.

class _ToolCapture:
    """Minimal mock that captures functions passed to @mcp.tool()."""
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator

_capture = _ToolCapture()
rpt.register(_capture)

# Now _capture.tools has all the tool functions as plain callables
_report_save = _capture.tools["report_save"]
_report_list = _capture.tools["report_list"]
_report_read = _capture.tools["report_read"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_reports_dir(tmp_path: pathlib.Path):
    """Return a context manager that redirects _get_reports_dir() to tmp_path."""
    return patch.object(rpt, "_get_reports_dir", return_value=tmp_path)


def _project_reports_path(base: pathlib.Path, project_dir: str) -> pathlib.Path:
    """Compute the expected project-scoped reports path under *base*."""
    normalized = str(pathlib.Path(project_dir).resolve())
    project_id = hashlib.md5(normalized.encode()).hexdigest()[:12]
    return base / ".claude" / "data" / "ai-team-os" / "projects" / project_id / "reports"


# ---------------------------------------------------------------------------
# report_save
# ---------------------------------------------------------------------------

class TestReportSave:
    def test_creates_file(self, tmp_path):
        with _patch_reports_dir(tmp_path):
            result = _report_save(
                author="rd-scanner",
                topic="ai-products-march",
                content="# Report\nSome findings.",
            )
        assert result["success"] is True
        assert result["author"] == "rd-scanner"
        assert result["topic"] == "ai-products-march"
        today = date.today().isoformat()
        assert result["date"] == today
        expected_filename = f"rd-scanner_ai-products-march_{today}.md"
        assert result["filename"] == expected_filename
        assert (tmp_path / expected_filename).exists()

    def test_file_contains_frontmatter_and_content(self, tmp_path):
        with _patch_reports_dir(tmp_path):
            _report_save(
                author="analyst",
                topic="market-survey",
                content="Body text here.",
                report_type="analysis",
            )
        today = date.today().isoformat()
        text = (tmp_path / f"analyst_market-survey_{today}.md").read_text(encoding="utf-8")
        assert "author: analyst" in text
        assert "topic: market-survey" in text
        assert "type: analysis" in text
        assert "Body text here." in text

    def test_overwrite_existing(self, tmp_path):
        with _patch_reports_dir(tmp_path):
            _report_save(author="a", topic="t", content="v1")
            _report_save(author="a", topic="t", content="v2")
        today = date.today().isoformat()
        text = (tmp_path / f"a_t_{today}.md").read_text(encoding="utf-8")
        assert "v2" in text
        assert "v1" not in text

    def test_creates_directory_if_missing(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        with _patch_reports_dir(nested):
            result = _report_save(author="x", topic="y", content="z")
        assert result["success"] is True
        assert nested.exists()

    def test_default_report_type_is_research(self, tmp_path):
        with _patch_reports_dir(tmp_path):
            result = _report_save(author="a", topic="b", content="c")
        assert result["report_type"] == "research"
        today = date.today().isoformat()
        text = (tmp_path / f"a_b_{today}.md").read_text(encoding="utf-8")
        assert "type: research" in text


# ---------------------------------------------------------------------------
# report_list
# ---------------------------------------------------------------------------

class TestReportList:
    def _make_reports(self, reports_dir: pathlib.Path, specs: list[tuple[str, str, str]]):
        """Create dummy report files: (author, topic, date_str)."""
        reports_dir.mkdir(parents=True, exist_ok=True)
        for author, topic, date_str in specs:
            filename = f"{author}_{topic}_{date_str}.md"
            (reports_dir / filename).write_text(f"---\nauthor: {author}\n---\n", encoding="utf-8")

    def test_empty_directory(self, tmp_path):
        with _patch_reports_dir(tmp_path):
            result = _report_list()
        assert result["success"] is True
        assert result["reports"] == []
        assert result["total"] == 0

    def test_lists_all_reports(self, tmp_path):
        self._make_reports(tmp_path, [
            ("agent-a", "topic-x", "2026-03-22"),
            ("agent-b", "topic-y", "2026-03-21"),
        ])
        with _patch_reports_dir(tmp_path):
            result = _report_list()
        assert result["success"] is True
        assert result["total"] == 2

    def test_filter_by_author(self, tmp_path):
        self._make_reports(tmp_path, [
            ("agent-a", "topic-x", "2026-03-22"),
            ("agent-b", "topic-y", "2026-03-21"),
        ])
        with _patch_reports_dir(tmp_path):
            result = _report_list(author="agent-a")
        assert result["total"] == 1
        assert result["reports"][0]["author"] == "agent-a"

    def test_filter_by_topic_keyword(self, tmp_path):
        self._make_reports(tmp_path, [
            ("a", "ai-products-march", "2026-03-22"),
            ("b", "backend-refactor", "2026-03-20"),
        ])
        with _patch_reports_dir(tmp_path):
            result = _report_list(topic="ai-products")
        assert result["total"] == 1
        assert result["reports"][0]["topic"] == "ai-products-march"

    def test_limit(self, tmp_path):
        self._make_reports(tmp_path, [("a", f"topic{i}", "2026-03-22") for i in range(5)])
        with _patch_reports_dir(tmp_path):
            result = _report_list(limit=3)
        assert result["total"] == 3

    def test_sorted_newest_first(self, tmp_path):
        self._make_reports(tmp_path, [
            ("a", "t", "2026-03-20"),
            ("a", "t2", "2026-03-22"),
            ("a", "t3", "2026-03-21"),
        ])
        with _patch_reports_dir(tmp_path):
            result = _report_list()
        dates = [r["date"] for r in result["reports"]]
        assert dates == sorted(dates, reverse=True)

    def test_skips_files_with_invalid_names(self, tmp_path):
        (tmp_path / "badly-named-file.md").write_text("x", encoding="utf-8")
        self._make_reports(tmp_path, [("a", "valid", "2026-03-22")])
        with _patch_reports_dir(tmp_path):
            result = _report_list()
        assert result["total"] == 1


# ---------------------------------------------------------------------------
# report_read
# ---------------------------------------------------------------------------

class TestReportRead:
    def test_reads_existing_report(self, tmp_path):
        with _patch_reports_dir(tmp_path):
            _report_save(author="rd", topic="survey", content="Details here.")
            today = date.today().isoformat()
            filename = f"rd_survey_{today}.md"
            result = _report_read(filename)
        assert result["success"] is True
        assert result["filename"] == filename
        assert "Details here." in result["content"]
        assert result["author"] == "rd"
        assert result["topic"] == "survey"
        assert result["date"] == today

    def test_not_found(self, tmp_path):
        with _patch_reports_dir(tmp_path):
            result = _report_read("nonexistent_file_2026-03-22.md")
        assert result["success"] is False
        assert "Report not found" in result["error"]

    def test_returns_path(self, tmp_path):
        with _patch_reports_dir(tmp_path):
            _report_save(author="x", topic="y", content="content")
            today = date.today().isoformat()
            filename = f"x_y_{today}.md"
            result = _report_read(filename)
        assert result["success"] is True
        assert str(tmp_path) in result["path"]


# ---------------------------------------------------------------------------
# Project-scoped path routing
# ---------------------------------------------------------------------------

class TestProjectScopedPath:
    """Verify _get_reports_dir() returns project-scoped path when CLAUDE_PROJECT_DIR is set."""

    def test_uses_project_scoped_path_when_env_set(self, tmp_path, monkeypatch):
        fake_project_dir = str(tmp_path / "myproject")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", fake_project_dir)

        result_path = rpt._get_reports_dir()

        normalized = str(pathlib.Path(fake_project_dir).resolve())
        project_id = hashlib.md5(normalized.encode()).hexdigest()[:12]
        assert f"projects/{project_id}/reports" in result_path.as_posix()

    def test_uses_global_path_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        result_path = rpt._get_reports_dir()
        assert result_path == rpt._REPORTS_GLOBAL

    def test_report_save_writes_to_project_dir(self, tmp_path, monkeypatch):
        fake_project_dir = str(tmp_path / "proj")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", fake_project_dir)

        normalized = str(pathlib.Path(fake_project_dir).resolve())
        project_id = hashlib.md5(normalized.encode()).hexdigest()[:12]
        scoped_dir = tmp_path / ".claude" / "data" / "ai-team-os" / "projects" / project_id / "reports"

        with patch.object(rpt, "_get_reports_dir", return_value=scoped_dir):
            result = _report_save(author="ag", topic="scope-test", content="scoped content")

        assert result["success"] is True
        today = date.today().isoformat()
        assert (scoped_dir / f"ag_scope-test_{today}.md").exists()

    def test_different_projects_get_different_dirs(self, tmp_path, monkeypatch):
        dir_a = str(tmp_path / "project-alpha")
        dir_b = str(tmp_path / "project-beta")

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", dir_a)
        path_a = rpt._get_reports_dir()

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", dir_b)
        path_b = rpt._get_reports_dir()

        assert path_a != path_b
