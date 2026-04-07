"""Unit tests for report_save / report_list / report_read MCP tools (database-backed)."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import aiteam.mcp.tools.reports as rpt


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

_report_save = _capture.tools["report_save"]
_report_list = _capture.tools["report_list"]
_report_read = _capture.tools["report_read"]


def _mock_api_call(method, path, body=None):
    """Mock _api_call for testing — simulates API responses."""
    today = date.today().isoformat()
    if method == "POST" and path == "/api/reports":
        return {
            "id": "test-report-001",
            "filename": f"{body['author']}_{body['topic']}_{today}.md",
            "author": body["author"],
            "topic": body["topic"],
            "report_type": body.get("report_type", "research"),
            "date": today,
            "project_id": "test-project",
        }
    if method == "GET" and path.startswith("/api/reports?"):
        return [
            {"id": "r1", "filename": "a_t_2026-03-22.md", "author": "a", "topic": "t", "date": "2026-03-22", "report_type": "research"},
            {"id": "r2", "filename": "b_t2_2026-03-21.md", "author": "b", "topic": "t2", "date": "2026-03-21", "report_type": "design"},
        ]
    if method == "GET" and path.startswith("/api/reports/"):
        report_id = path.split("/")[-1]
        return {
            "id": report_id,
            "filename": f"rd_survey_{today}.md",
            "content": "# Report\nDetails here.",
            "author": "rd",
            "topic": "survey",
            "date": today,
            "report_type": "research",
        }
    return None


class TestReportSave:
    def test_creates_report(self):
        with patch.object(rpt, "_api_call", side_effect=_mock_api_call):
            result = _report_save(
                author="rd-scanner",
                topic="ai-products-march",
                content="# Report\nSome findings.",
            )
        assert result["success"] is True
        assert result["author"] == "rd-scanner"
        assert result["topic"] == "ai-products-march"
        assert result["id"] == "test-report-001"

    def test_default_report_type_is_research(self):
        with patch.object(rpt, "_api_call", side_effect=_mock_api_call):
            result = _report_save(author="a", topic="b", content="c")
        assert result["report_type"] == "research"

    def test_returns_date(self):
        with patch.object(rpt, "_api_call", side_effect=_mock_api_call):
            result = _report_save(author="a", topic="b", content="c")
        today = date.today().isoformat()
        assert result["date"] == today

    def test_api_failure_returns_error(self):
        with patch.object(rpt, "_api_call", return_value=None):
            result = _report_save(author="a", topic="b", content="c")
        assert result["success"] is False


class TestReportList:
    def test_lists_reports(self):
        with patch.object(rpt, "_api_call", side_effect=_mock_api_call):
            result = _report_list()
        assert result["success"] is True
        assert result["total"] == 2

    def test_reports_have_metadata(self):
        with patch.object(rpt, "_api_call", side_effect=_mock_api_call):
            result = _report_list()
        r = result["reports"][0]
        assert "id" in r
        assert "author" in r
        assert "topic" in r
        assert "date" in r
        assert "report_type" in r

    def test_api_failure_returns_error(self):
        with patch.object(rpt, "_api_call", return_value=None):
            result = _report_list()
        assert result["success"] is False


class TestReportRead:
    def test_reads_report(self):
        with patch.object(rpt, "_api_call", side_effect=_mock_api_call):
            result = _report_read("test-report-001")
        assert result["success"] is True
        assert "Details here." in result["content"]
        assert result["author"] == "rd"

    def test_api_failure_returns_error(self):
        with patch.object(rpt, "_api_call", return_value=None):
            result = _report_read("nonexistent")
        assert result["success"] is False
