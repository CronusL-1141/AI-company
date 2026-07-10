"""Unit tests for real GitHub topics extraction via _fetch_repo_topics and _parse_gh_repo."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import aiteam.mcp.tools.ecosystem as eco

# ---------------------------------------------------------------------------
# _fetch_repo_topics tests
# ---------------------------------------------------------------------------


class TestFetchRepoTopics:
    def _make_result(self, returncode: int, stdout: str) -> MagicMock:
        r = MagicMock()
        r.returncode = returncode
        r.stdout = stdout
        return r

    def test_returns_topics_on_success(self):
        """_fetch_repo_topics returns list of topic strings from gh api output."""
        fake = self._make_result(0, "workflow-automation\nmcp\nn8n\n")
        with patch.object(eco.subprocess, "run", return_value=fake):
            topics = eco._fetch_repo_topics("n8n-io/n8n")
        assert topics == ["workflow-automation", "mcp", "n8n"]

    def test_returns_empty_on_nonzero_returncode(self):
        """_fetch_repo_topics returns [] when gh api call fails."""
        fake = self._make_result(1, "")
        with patch.object(eco.subprocess, "run", return_value=fake):
            topics = eco._fetch_repo_topics("some/nonexistent-repo")
        assert topics == []

    def test_returns_empty_on_empty_stdout(self):
        """_fetch_repo_topics returns [] when gh api returns empty output (no topics)."""
        fake = self._make_result(0, "")
        with patch.object(eco.subprocess, "run", return_value=fake):
            topics = eco._fetch_repo_topics("bare/repo")
        assert topics == []

    def test_returns_empty_on_timeout(self):
        """_fetch_repo_topics returns [] on subprocess timeout."""
        with patch.object(
            eco.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=10),
        ):
            topics = eco._fetch_repo_topics("slow/repo")
        assert topics == []

    def test_returns_empty_on_file_not_found(self):
        """_fetch_repo_topics returns [] when gh is not installed."""
        with patch.object(eco.subprocess, "run", side_effect=FileNotFoundError):
            topics = eco._fetch_repo_topics("any/repo")
        assert topics == []

    def test_strips_whitespace_from_topics(self):
        """_fetch_repo_topics strips leading/trailing whitespace from each topic."""
        fake = self._make_result(0, "  agent  \n  llm  \n")
        with patch.object(eco.subprocess, "run", return_value=fake):
            topics = eco._fetch_repo_topics("test/repo")
        assert topics == ["agent", "llm"]


# ---------------------------------------------------------------------------
# _parse_gh_repo real topics integration
# ---------------------------------------------------------------------------


class TestParseGhRepoRealTopics:
    _BASE_ITEM = {
        "fullName": "n8n-io/n8n",
        "name": "n8n",
        "owner": {"login": "n8n-io"},
        "description": "Workflow automation tool",
        "stargazersCount": 50000,
        "language": "TypeScript",
        "homepage": None,
        "pushedAt": "2026-05-01T00:00:00Z",
    }

    def test_uses_real_topics_when_available(self):
        """_parse_gh_repo uses real GitHub topics from _fetch_repo_topics over hint_topics."""
        real_topics = ["workflow-automation", "mcp", "n8n", "automation"]
        with patch.object(eco, "_fetch_repo_topics", return_value=real_topics):
            parsed = eco._parse_gh_repo(self._BASE_ITEM, min_stars=1000, hint_topics=["mcp"])
        assert parsed is not None
        assert parsed["topics"] == real_topics

    def test_falls_back_to_hint_topics_when_real_empty(self):
        """_parse_gh_repo falls back to hint_topics when _fetch_repo_topics returns []."""
        with patch.object(eco, "_fetch_repo_topics", return_value=[]):
            parsed = eco._parse_gh_repo(
                self._BASE_ITEM, min_stars=1000, hint_topics=["mcp", "claude"]
            )
        assert parsed is not None
        assert parsed["topics"] == ["mcp", "claude"]

    def test_falls_back_to_empty_when_both_missing(self):
        """_parse_gh_repo returns empty topics when both real and hint are unavailable."""
        with patch.object(eco, "_fetch_repo_topics", return_value=[]):
            parsed = eco._parse_gh_repo(
                self._BASE_ITEM, min_stars=1000, hint_topics=None
            )
        assert parsed is not None
        assert parsed["topics"] == []

    def test_real_topics_used_for_classification(self):
        """_parse_gh_repo uses real topics for category classification."""
        real_topics = ["mcp-server", "model-context-protocol"]
        with patch.object(eco, "_fetch_repo_topics", return_value=real_topics):
            parsed = eco._parse_gh_repo(
                self._BASE_ITEM, min_stars=1000, hint_topics=["unrelated"]
            )
        assert parsed is not None
        # mcp-server keyword in real topics should drive category to mcp-server
        assert parsed["relevance_category"] == "mcp-server"

    def test_hint_topics_used_for_classification_on_fallback(self):
        """When real topics unavailable, hint_topics still drive classification."""
        with patch.object(eco, "_fetch_repo_topics", return_value=[]):
            parsed = eco._parse_gh_repo(
                self._BASE_ITEM, min_stars=1000, hint_topics=["mcp-server"]
            )
        assert parsed is not None
        assert parsed["relevance_category"] == "mcp-server"

    def test_returns_none_for_excluded_repo(self):
        """_parse_gh_repo returns None for repos in exclusion list regardless of topics."""
        item = dict(self._BASE_ITEM)
        item["fullName"] = "CronusL-1141/AI-company"
        with patch.object(eco, "_fetch_repo_topics", return_value=["something"]):
            parsed = eco._parse_gh_repo(item, min_stars=1000, hint_topics=[])
        assert parsed is None

    def test_returns_none_below_min_stars(self):
        """_parse_gh_repo returns None for repos below min_stars threshold."""
        item = dict(self._BASE_ITEM)
        item["stargazersCount"] = 500
        with patch.object(eco, "_fetch_repo_topics", return_value=["mcp"]):
            parsed = eco._parse_gh_repo(item, min_stars=1000, hint_topics=[])
        assert parsed is None
