"""ecosystem_scan + ecosystem_search MCP 工具单元测试（mock gh + API）。"""

from __future__ import annotations

import json
from unittest.mock import patch

import aiteam.mcp.tools.ecosystem as eco

# ---------------------------------------------------------------------------
# Tool capture helper
# ---------------------------------------------------------------------------


class _ToolCapture:
    """最简 mock：捕获 @mcp.tool() 注册的函数。"""

    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


_capture = _ToolCapture()
eco.register(_capture)

_ecosystem_scan = _capture.tools["ecosystem_scan"]
_ecosystem_search = _capture.tools["ecosystem_search"]


# ---------------------------------------------------------------------------
# Sample gh search response
# ---------------------------------------------------------------------------

_SAMPLE_GH_ITEMS = [
    {
        "fullName": "anthropics/claude-code",
        "name": "claude-code",
        "owner": {"login": "anthropics"},
        "description": "Claude Code is an AI coding assistant",
        "stargazersCount": 45000,
        "primaryLanguage": {"name": "TypeScript"},
        "repositoryTopics": [{"name": "claude"}, {"name": "ai"}, {"name": "claude-code"}],
        "homepageUrl": "https://claude.ai/code",
        "pushedAt": "2026-05-01T10:00:00Z",
    },
    {
        "fullName": "modelcontextprotocol/servers",
        "name": "servers",
        "owner": {"login": "modelcontextprotocol"},
        "description": "MCP servers reference implementations",
        "stargazersCount": 22000,
        "primaryLanguage": {"name": "Python"},
        "repositoryTopics": [{"name": "mcp"}, {"name": "mcp-server"}],
        "homepageUrl": None,
        "pushedAt": "2026-04-28T08:00:00Z",
    },
    {
        "fullName": "low-stars/tiny-agent",
        "name": "tiny-agent",
        "owner": {"login": "low-stars"},
        "description": "Small agent lib",
        "stargazersCount": 2000,  # below 5000 threshold
        "primaryLanguage": {"name": "Python"},
        "repositoryTopics": [{"name": "agent"}],
        "homepageUrl": None,
        "pushedAt": "2026-03-01T00:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# ecosystem_scan dry_run tests (3 cases)
# ---------------------------------------------------------------------------


class TestEcosystemScanDryRun:
    def _patch_scan(self, items=None, org_items=None):
        """Helper: patch both _run_gh_search and subprocess.run for org search."""

        _items = items if items is not None else _SAMPLE_GH_ITEMS
        _org = org_items if org_items is not None else []

        class _FakeResult:
            returncode = 0
            stdout = json.dumps(_org)

        def _fake_gh_search(keyword, min_stars, topics=None, timeout=30):
            return _items

        return (
            patch.object(eco, "_run_gh_search", side_effect=_fake_gh_search),
            patch.object(eco.subprocess, "run", return_value=_FakeResult()),
        )

    def test_dry_run_returns_dry_run_flag(self):
        """dry_run=True 时返回 dry_run 标记，不写库。"""
        p1, p2 = self._patch_scan()
        with p1, p2:
            result = _ecosystem_scan(min_stars=5000, dry_run=True)
        assert result["dry_run"] is True

    def test_dry_run_filters_low_stars(self):
        """dry_run 结果只包含 stars >= min_stars 的仓库。"""
        p1, p2 = self._patch_scan()
        with p1, p2:
            result = _ecosystem_scan(min_stars=5000, dry_run=True)
        sample_names = [r["repo"] for r in result.get("sample_repos", [])]
        assert "low-stars/tiny-agent" not in sample_names

    def test_dry_run_does_not_call_api(self):
        """dry_run 模式下不调用 _api_call 写入数据库。"""
        p1, p2 = self._patch_scan()
        with p1, p2:
            with patch.object(eco, "_api_call") as mock_api:
                _ecosystem_scan(min_stars=5000, dry_run=True)
        mock_api.assert_not_called()

    def test_dry_run_reports_per_query_stats(self):
        """dry_run 返回 per_query_stats 字段。"""
        p1, p2 = self._patch_scan()
        with p1, p2:
            result = _ecosystem_scan(min_stars=5000, dry_run=True)
        assert "per_query_stats" in result
        assert isinstance(result["per_query_stats"], dict)

    def test_dry_run_excludes_known_repos(self):
        """已知排除仓库（CronusL-1141/AI-company）不出现在 dry_run 结果中。"""
        items_with_excluded = list(_SAMPLE_GH_ITEMS) + [
            {
                "fullName": "CronusL-1141/AI-company",
                "name": "AI-company",
                "owner": {"login": "CronusL-1141"},
                "description": "My own repo",
                "stargazersCount": 100000,
                "primaryLanguage": {"name": "Python"},
                "repositoryTopics": [],
                "homepageUrl": None,
                "pushedAt": "2026-05-01T00:00:00Z",
            }
        ]
        p1, p2 = self._patch_scan(items=items_with_excluded)
        with p1, p2:
            result = _ecosystem_scan(min_stars=5000, dry_run=True)
        sample_names = [r["repo"] for r in result.get("sample_repos", [])]
        assert "CronusL-1141/AI-company" not in sample_names


# ---------------------------------------------------------------------------
# ecosystem_scan (non-dry_run) API write test (3 cases)
# ---------------------------------------------------------------------------


class TestEcosystemScanWrite:
    def _mock_api_call(self, method, path, body=None):
        if method == "POST" and "/api/ecosystem/profiles" in path:
            return {"id": "test-id", "created": True}
        return None

    def _patch_scan(self, items=None):

        _items = items if items is not None else _SAMPLE_GH_ITEMS

        class _FakeResult:
            returncode = 0
            stdout = "[]"

        def _fake_gh_search(keyword, min_stars, topics=None, timeout=30):
            return _items

        return (
            patch.object(eco, "_run_gh_search", side_effect=_fake_gh_search),
            patch.object(eco.subprocess, "run", return_value=_FakeResult()),
        )

    def test_scan_calls_api_for_each_valid_repo(self):
        """scan 正常模式调用 _api_call 写入每个有效仓库。"""
        p1, p2 = self._patch_scan()
        with p1, p2:
            with patch.object(eco, "_api_call", side_effect=self._mock_api_call) as mock_api:
                _ecosystem_scan(min_stars=5000, dry_run=False)
        assert mock_api.call_count >= 2

    def test_scan_returns_count_stats(self):
        """scan 返回包含 scanned / new_profiles / updated_profiles / skipped 字段。"""
        p1, p2 = self._patch_scan()
        with p1, p2:
            with patch.object(eco, "_api_call", side_effect=self._mock_api_call):
                result = _ecosystem_scan(min_stars=5000, dry_run=False)
        assert "scanned" in result
        assert "new_profiles" in result
        assert "updated_profiles" in result
        assert "skipped" in result

    def test_scan_returns_category_distribution(self):
        """scan 结果包含 category_distribution 字段。"""
        p1, p2 = self._patch_scan()
        with p1, p2:
            with patch.object(eco, "_api_call", side_effect=self._mock_api_call):
                result = _ecosystem_scan(min_stars=5000, dry_run=False)
        assert "category_distribution" in result
        assert isinstance(result["category_distribution"], dict)


# ---------------------------------------------------------------------------
# ecosystem_search tests (3 cases)
# ---------------------------------------------------------------------------


_SEARCH_RESPONSE = {
    "profiles": [
        {
            "id": "abc123",
            "repo_full_name": "anthropics/claude-code",
            "name": "claude-code",
            "owner": "anthropics",
            "stars": 45000,
            "relevance_category": "skill-system",
            "needs_deep_review": False,
        }
    ],
    "total": 1,
}


class TestEcosystemSearch:
    def test_search_passes_keyword_to_api(self):
        """ecosystem_search 将 keyword 参数传给 API。"""
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _ecosystem_search(keyword="claude")
        call_url = mock_api.call_args[0][1]
        assert "keyword=claude" in call_url

    def test_search_returns_profiles_list(self):
        """ecosystem_search 返回 profiles 列表。"""
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE):
            result = _ecosystem_search(keyword="claude")
        assert "profiles" in result
        assert result["total"] == 1

    def test_search_api_failure_returns_empty(self):
        """API 返回 None 时 ecosystem_search 返回空列表。"""
        with patch.object(eco, "_api_call", return_value=None):
            result = _ecosystem_search(keyword="claude")
        assert result["profiles"] == []
        assert result["total"] == 0

    def test_search_passes_category_param(self):
        """ecosystem_search 将 category 参数传给 API。"""
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _ecosystem_search(category="mcp-server")
        call_url = mock_api.call_args[0][1]
        assert "category=mcp-server" in call_url

    def test_search_passes_min_stars_param(self):
        """ecosystem_search 将 min_stars 参数传给 API。"""
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _ecosystem_search(min_stars=10000)
        call_url = mock_api.call_args[0][1]
        assert "min_stars=10000" in call_url
