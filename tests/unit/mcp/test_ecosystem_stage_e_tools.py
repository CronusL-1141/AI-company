"""Stage E MCP 工具单元测试：ecosystem_search 升级 + ecosystem_repo_get + ecosystem_search_by_capability。"""

from __future__ import annotations

from unittest.mock import patch

import aiteam.mcp.tools.ecosystem as eco

# ---------------------------------------------------------------------------
# Tool registration capture
# ---------------------------------------------------------------------------


class _ToolCapture:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


_capture = _ToolCapture()
eco.register(_capture)

_search = _capture.tools["ecosystem_search"]
_repo_get = _capture.tools["ecosystem_repo_get"]
_search_by_capability = _capture.tools["ecosystem_search_by_capability"]


_SEARCH_RESPONSE = {
    "profiles": [
        {
            "id": "abc",
            "repo_full_name": "anthropics/claude-code",
            "stars": 45000,
            "relevance_category": "skill-system",
            "needs_deep_review": False,
        }
    ],
    "total": 1,
    "limit": 30,
    "offset": 0,
}


_FULL_RESPONSE = {
    "profile": {"id": "abc", "repo_full_name": "anthropics/claude-code"},
    "tags": [],
    "deep_reviews": [],
    "relations_from": [],
    "relations_to": [],
    "scan_run": None,
}


# ---------------------------------------------------------------------------
# ecosystem_search Stage E params (6 cases)
# ---------------------------------------------------------------------------


class TestEcosystemSearchStageE:
    def test_search_passes_language_param(self):
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _search(language="Python")
        url = mock_api.call_args[0][1]
        assert "language=Python" in url

    def test_search_passes_pushed_after_param(self):
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _search(pushed_after="2026-01-01T00:00:00")
        url = mock_api.call_args[0][1]
        assert "pushed_after=2026-01-01" in url

    def test_search_passes_is_archived_param(self):
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _search(is_archived=True)
        url = mock_api.call_args[0][1]
        assert "is_archived=True" in url

    def test_search_passes_tags_and_match_mode(self):
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _search(tags=["memory_system", "vector_db"], tag_match_mode="any")
        url = mock_api.call_args[0][1]
        assert "tags=memory_system,vector_db" in url
        assert "tag_match_mode=any" in url

    def test_search_passes_sort_recency(self):
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _search(sort="recency")
        url = mock_api.call_args[0][1]
        assert "sort=recency" in url

    def test_search_default_sort_omitted(self):
        """sort=stars 是默认值，不发送参数（节省 url）。"""
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _search()
        url = mock_api.call_args[0][1]
        assert "sort=" not in url

    def test_search_passes_offset_param(self):
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _search(offset=20)
        url = mock_api.call_args[0][1]
        assert "offset=20" in url

    def test_search_facet_counts_flag(self):
        with patch.object(eco, "_api_call", return_value=_SEARCH_RESPONSE) as mock_api:
            _search(facet_counts=True)
        url = mock_api.call_args[0][1]
        assert "facet_counts=True" in url

    def test_search_api_failure_returns_envelope(self):
        with patch.object(eco, "_api_call", return_value=None):
            result = _search(keyword="x")
        assert result == {"profiles": [], "total": 0, "limit": 30, "offset": 0}


# ---------------------------------------------------------------------------
# ecosystem_repo_get (4 cases)
# ---------------------------------------------------------------------------


class TestEcosystemRepoGet:
    def test_repo_get_requires_identifier(self):
        result = _repo_get()
        assert "error" in result

    def test_repo_get_uses_repo_full_name_in_path(self):
        with patch.object(eco, "_api_call", return_value=_FULL_RESPONSE) as mock_api:
            _repo_get(repo_full_name="anthropics/claude-code")
        url = mock_api.call_args[0][1]
        assert "/api/ecosystem/profiles/anthropics/claude-code/full" in url

    def test_repo_get_uses_repo_id_when_full_name_missing(self):
        with patch.object(eco, "_api_call", return_value=_FULL_RESPONSE) as mock_api:
            _repo_get(repo_id="some-uuid")
        url = mock_api.call_args[0][1]
        assert "/api/ecosystem/profiles/some-uuid/full" in url

    def test_repo_get_returns_not_found_envelope(self):
        with patch.object(eco, "_api_call", return_value=None):
            result = _repo_get(repo_full_name="ghost/none")
        assert result["error"] == "not_found"
        assert result["repo_full_name"] == "ghost/none"


# ---------------------------------------------------------------------------
# ecosystem_search_by_capability (4 cases)
# ---------------------------------------------------------------------------


_BY_CAP_RESPONSE = {
    "profiles": [],
    "total": 0,
    "matched_tags": ["memory_system"],
    "match_mode": "all",
    "limit": 30,
    "offset": 0,
}


class TestEcosystemSearchByCapability:
    def test_no_tags_short_circuits(self):
        """空 tags 不发请求。"""
        with patch.object(eco, "_api_call") as mock_api:
            result = _search_by_capability(tags=None)
        mock_api.assert_not_called()
        assert result["profiles"] == []
        assert result["matched_tags"] == []

    def test_passes_tags_list(self):
        with patch.object(eco, "_api_call", return_value=_BY_CAP_RESPONSE) as mock_api:
            _search_by_capability(tags=["memory_system", "vector_db"])
        url = mock_api.call_args[0][1]
        assert "tags=memory_system,vector_db" in url
        assert "match_mode=all" in url
        assert "/api/ecosystem/search/by_capability" in url

    def test_match_mode_any(self):
        with patch.object(eco, "_api_call", return_value=_BY_CAP_RESPONSE) as mock_api:
            _search_by_capability(tags=["x"], match_mode="any")
        url = mock_api.call_args[0][1]
        assert "match_mode=any" in url

    def test_passes_min_stars_and_sort(self):
        with patch.object(eco, "_api_call", return_value=_BY_CAP_RESPONSE) as mock_api:
            _search_by_capability(tags=["x"], min_stars=10000, sort="recency")
        url = mock_api.call_args[0][1]
        assert "min_stars=10000" in url
        assert "sort=recency" in url

    def test_api_failure_returns_envelope(self):
        with patch.object(eco, "_api_call", return_value=None):
            result = _search_by_capability(tags=["x"])
        assert result["profiles"] == []
        assert result["matched_tags"] == ["x"]
