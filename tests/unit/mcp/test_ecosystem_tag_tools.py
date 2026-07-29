"""Tests for the 5 Stage D ecosystem-tag MCP tools (mocked _api_call)."""

from __future__ import annotations

from unittest.mock import patch

import aiteam.mcp.tools.ecosystem as eco
from aiteam.services.ecosystem_tagger import DEFAULT_LLM_AGENT_TEMPLATE


class _ToolCapture:
    """Capture @mcp.tool() registered functions."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        def decorator(fn):  # type: ignore[no-untyped-def]
            self.tools[fn.__name__] = fn
            return fn

        return decorator


_capture = _ToolCapture()
eco.register(_capture)

_tag_list = _capture.tools["ecosystem_tag_list"]
_tag_apply_batch = _capture.tools["ecosystem_tag_apply_batch"]
_tag_dispatch_llm = _capture.tools["ecosystem_tag_dispatch_llm"]
_tag_apply_llm_result = _capture.tools["ecosystem_tag_apply_llm_result"]
_repo_tags = _capture.tools["ecosystem_repo_tags"]


# ============================================================
# ecosystem_tag_list
# ============================================================


class TestTagList:
    def test_returns_tag_list_from_api(self) -> None:
        fake = {
            "tags": [
                {
                    "id": "1",
                    "name": "memory_system",
                    "category": "capability",
                    "aliases": [],
                    "description": "",
                }
            ],
            "total": 1,
        }
        with patch.object(eco, "_api_call", return_value=fake) as mock_api:
            result = _tag_list()
        assert result["total"] == 1
        url = mock_api.call_args[0][1]
        assert "/api/ecosystem/tags" in url

    def test_passes_category_filter(self) -> None:
        with patch.object(eco, "_api_call", return_value={"tags": [], "total": 0}) as mock_api:
            _tag_list(category="tech_stack")
        url = mock_api.call_args[0][1]
        assert "category=tech_stack" in url

    def test_api_failure_returns_empty(self) -> None:
        with patch.object(eco, "_api_call", return_value=None):
            result = _tag_list()
        assert result == {"tags": [], "total": 0}


# ============================================================
# ecosystem_tag_apply_batch
# ============================================================


class TestTagApplyBatch:
    def test_passes_repo_ids_as_payload(self) -> None:
        fake = {
            "repos_processed": 2,
            "layer1_applied": 1,
            "layer2_applied": 1,
            "layer3_applied": 0,
            "repos_needing_llm": 0,
            "repos_failed": 0,
            "by_repo": [],
        }
        with patch.object(eco, "_api_call", return_value=fake) as mock_api:
            result = _tag_apply_batch(repo_ids=["a", "b"], agent_id="tagger-x")
        method, path = mock_api.call_args[0][0], mock_api.call_args[0][1]
        body = mock_api.call_args[0][2]
        assert method == "POST"
        assert path == "/api/ecosystem/tags/apply"
        assert body["repo_ids"] == ["a", "b"]
        assert body["agent_id"] == "tagger-x"
        assert result["repos_processed"] == 2

    def test_default_filters_empty(self) -> None:
        with patch.object(eco, "_api_call", return_value={"repos_processed": 0}) as mock_api:
            _tag_apply_batch()
        body = mock_api.call_args[0][2]
        assert body["repo_ids"] == []
        assert body["repo_full_names"] == []

    def test_api_failure_propagates_error(self) -> None:
        with patch.object(
            eco, "_api_call", return_value={"success": False, "error": "x"}
        ):
            result = _tag_apply_batch()
        assert result["success"] is False


# ============================================================
# ecosystem_tag_dispatch_llm
# ============================================================


class TestTagDispatchLLM:
    def test_caps_concurrency_via_payload(self) -> None:
        fake_plan = {
            "agent_template": "general-purpose",
            "max_concurrency": 20,
            "total_requested": 25,
            "dispatched": 20,
            "skipped_due_to_limit": 5,
            "dispatch": [],
            "instructions": "...",
        }
        with patch.object(eco, "_api_call", return_value=fake_plan) as mock_api:
            result = _tag_dispatch_llm(
                repo_ids=[f"id-{i}" for i in range(25)], max_concurrency=20
            )
        body = mock_api.call_args[0][2]
        assert body["max_concurrency"] == 20
        assert result["skipped_due_to_limit"] == 5

    def test_default_template_is_spawnable_and_no_team_name(self) -> None:
        """Regression: the tool used to default to the non-existent 'researcher'
        template and to forward the deprecated team_name."""
        with patch.object(eco, "_api_call", return_value={"dispatch": []}) as mock_api:
            _tag_dispatch_llm(repo_ids=["a"])
        body = mock_api.call_args[0][2]
        assert body["agent_template"] == DEFAULT_LLM_AGENT_TEMPLATE
        assert "team_name" not in body

    def test_path_is_dispatch_plan(self) -> None:
        with patch.object(eco, "_api_call", return_value={"dispatch": []}) as mock_api:
            _tag_dispatch_llm(repo_ids=["a"])
        path = mock_api.call_args[0][1]
        assert path == "/api/ecosystem/tags/llm/dispatch_plan"


# ============================================================
# ecosystem_tag_apply_llm_result
# ============================================================


class TestTagApplyLLMResult:
    def test_passes_tags_to_api(self) -> None:
        fake = {
            "repo_id": "r1",
            "layer3_tags": ["memory_system"],
            "skipped_unknown": [],
            "total_applied": 1,
        }
        with patch.object(eco, "_api_call", return_value=fake) as mock_api:
            result = _tag_apply_llm_result(
                repo_id="r1",
                tags=[{"name": "memory_system", "confidence": 0.85}],
                agent_id="sub-1",
            )
        body = mock_api.call_args[0][2]
        assert body["repo_id"] == "r1"
        assert body["agent_id"] == "sub-1"
        assert body["tags"][0]["name"] == "memory_system"
        assert result["total_applied"] == 1

    def test_omits_agent_id_when_empty(self) -> None:
        with patch.object(
            eco, "_api_call", return_value={"layer3_tags": []}
        ) as mock_api:
            _tag_apply_llm_result(repo_id="r1", tags=[])
        body = mock_api.call_args[0][2]
        assert "agent_id" not in body

    def test_path_is_llm_result(self) -> None:
        with patch.object(
            eco, "_api_call", return_value={"layer3_tags": []}
        ) as mock_api:
            _tag_apply_llm_result(repo_id="r1", tags=[])
        path = mock_api.call_args[0][1]
        assert path == "/api/ecosystem/tags/llm/result"


# ============================================================
# ecosystem_repo_tags
# ============================================================


class TestRepoTags:
    def test_calls_repo_tags_endpoint(self) -> None:
        fake = {
            "repo_id": "r1",
            "tags": [
                {
                    "tag_name": "python",
                    "tag_category": "tech_stack",
                    "confidence": 0.95,
                    "source": "github_topic",
                    "agent_id": None,
                    "repo_tag_id": "rt1",
                    "tag_id": "t1",
                    "repo_id": "r1",
                    "created_at": None,
                }
            ],
            "total": 1,
        }
        with patch.object(eco, "_api_call", return_value=fake) as mock_api:
            result = _repo_tags(repo_id="r1")
        path = mock_api.call_args[0][1]
        assert path == "/api/ecosystem/repos/r1/tags"
        assert result["total"] == 1

    def test_api_failure_returns_empty(self) -> None:
        with patch.object(eco, "_api_call", return_value=None):
            result = _repo_tags(repo_id="r1")
        assert result == {"repo_id": "r1", "tags": [], "total": 0}
