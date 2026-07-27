"""Tests for the v1.6.0 P0.3 ecosystem MCP tool wrappers.

Covers:
- ecosystem_quick_setup
- ecosystem_index_update
- ecosystem_index_diff_latest

(``ecosystem_data_source_create`` / ``ecosystem_scan_profile_update`` were
deprecated no-op stubs after the v1.6.1 single-source ruling and have since been
dropped from the tool surface — settings is the only scan-config source now.)

All tests mock _api_call so no live backend is required.
"""

from __future__ import annotations

from unittest.mock import patch

import aiteam.mcp.tools.ecosystem as eco


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

_quick_setup = _capture.tools["ecosystem_quick_setup"]
_index_update = _capture.tools["ecosystem_index_update"]
_index_diff_latest = _capture.tools["ecosystem_index_diff_latest"]


# ============================================================
# ecosystem_quick_setup
# ============================================================


class TestQuickSetup:
    def test_happy_path_multi_source_default_profile(self) -> None:
        """Multiple sources + use_defaults=True should POST to /quick_setup with all fields."""
        fake = {
            "success": True,
            "data_source_ids": ["ds-1", "ds-2"],
            "scan_profile_id": "sp-1",
            "scan_profile_version": 1,
            "next_step": "Call POST /api/ecosystem/index_update?dry_run=true to preview changes",
        }
        with patch.object(eco, "_api_call", return_value=fake) as mock_api:
            result = _quick_setup(
                sources=["github", "huggingface"],
                queries=["mcp-server", "claude-agent"],
            )

        # Verify request shape
        assert mock_api.call_args[0][0] == "POST"
        assert mock_api.call_args[0][1] == "/api/ecosystem/quick_setup"
        body = mock_api.call_args[0][2]
        assert body["sources"] == ["github", "huggingface"]
        assert body["queries"] == ["mcp-server", "claude-agent"]
        assert body["use_defaults"] is True
        assert "custom_profile" not in body  # only when explicitly passed

        # Verify response shape
        assert result["success"] is True
        assert result["data_sources_created"] == 2
        assert result["data_source_ids"] == ["ds-1", "ds-2"]
        assert result["scan_profile_id"] == "sp-1"
        assert result["scan_profile_version"] == 1
        assert result["profile_created"] is True
        assert result["next_action"] == "call ecosystem_index_update(dry_run=True)"

    def test_default_sources_is_github_when_empty(self) -> None:
        """Empty/None sources defaults to ['github']."""
        fake = {
            "success": True,
            "data_source_ids": ["ds-1"],
            "scan_profile_id": "sp-1",
            "scan_profile_version": 1,
        }
        with patch.object(eco, "_api_call", return_value=fake) as mock_api:
            _quick_setup()
        body = mock_api.call_args[0][2]
        assert body["sources"] == ["github"]
        assert body["queries"] == []

    def test_passes_custom_profile_when_provided(self) -> None:
        """custom_profile dict should appear in body when use_defaults=False."""
        custom = {"min_popularity_floor": {"github": 100}}
        fake = {
            "success": True,
            "data_source_ids": ["ds-1"],
            "scan_profile_id": "sp-1",
            "scan_profile_version": 1,
        }
        with patch.object(eco, "_api_call", return_value=fake) as mock_api:
            _quick_setup(
                sources=["github"],
                use_defaults=False,
                custom_profile=custom,
            )
        body = mock_api.call_args[0][2]
        assert body["use_defaults"] is False
        assert body["custom_profile"] == custom

    def test_api_unavailable_returns_error_dict(self) -> None:
        with patch.object(eco, "_api_call", return_value=None):
            result = _quick_setup(sources=["github"])
        assert result == {"success": False, "error": "api_unavailable"}

    def test_api_returns_failure_passes_through(self) -> None:
        failure = {"success": False, "error": "HTTP 422", "detail": "Invalid kind"}
        with patch.object(eco, "_api_call", return_value=failure):
            result = _quick_setup(sources=["unknown-kind"])
        assert result["success"] is False
        assert "422" in result["error"]


# ============================================================
# ecosystem_index_update
# ============================================================


class TestIndexUpdate:
    def test_dry_run_true_is_default(self) -> None:
        """Calling without args should send dry_run=True."""
        fake = {
            "success": True,
            "dry_run": True,
            "missing_setup": [],
            "data_sources": 1,
            "scan_profile_version": 1,
        }
        with patch.object(eco, "_api_call", return_value=fake) as mock_api:
            result = _index_update()

        assert mock_api.call_args[0][0] == "POST"
        assert mock_api.call_args[0][1] == "/api/ecosystem/index_update"
        body = mock_api.call_args[0][2]
        assert body == {"dry_run": True}
        assert result["success"] is True

    def test_dry_run_false_sent_when_explicit(self) -> None:
        fake = {"success": True, "dry_run": False, "missing_setup": []}
        with patch.object(eco, "_api_call", return_value=fake) as mock_api:
            _index_update(dry_run=False)
        body = mock_api.call_args[0][2]
        assert body == {"dry_run": False}

    def test_missing_setup_returned_as_is(self) -> None:
        """Backend returns success=False with missing_setup list — wrapper passes through."""
        fake = {
            "success": False,
            "dry_run": True,
            "missing_setup": ["data_source", "scan_profile"],
            "message": "Setup incomplete. Call POST /api/ecosystem/quick_setup first ...",
        }
        with patch.object(eco, "_api_call", return_value=fake):
            result = _index_update(dry_run=True)
        assert result["missing_setup"] == ["data_source", "scan_profile"]
        assert result["success"] is False

    def test_api_unavailable_returns_error_dict(self) -> None:
        with patch.object(eco, "_api_call", return_value=None):
            result = _index_update()
        assert result == {"success": False, "error": "api_unavailable"}


# ============================================================
# ecosystem_index_diff_latest
# ============================================================


class TestIndexDiffLatest:
    def test_404_returns_p04_stub_signal(self) -> None:
        """HTTP 404 (endpoint not yet built) must be mapped to a stable stub message."""
        failure = {
            "success": False,
            "error": "HTTP 404: Not Found",
            "detail": "endpoint not found",
        }
        with patch.object(eco, "_api_call", return_value=failure):
            result = _index_diff_latest()
        assert result["success"] is False
        assert result["error"] == "P0.4 will implement"

    def test_happy_path_passes_through(self) -> None:
        """When P0.4 ships, the wrapper should pass the diff payload through."""
        fake = {
            "success": True,
            "index_diff": {
                "id": "id-1",
                "project_id": "p-1",
                "summary": {"added": 5, "removed": 1, "updated": 3},
            },
        }
        with patch.object(eco, "_api_call", return_value=fake) as mock_api:
            result = _index_diff_latest()
        assert mock_api.call_args[0][0] == "GET"
        assert mock_api.call_args[0][1] == "/api/ecosystem/index_diffs/latest"
        assert result["success"] is True
        assert result["index_diff"]["summary"]["added"] == 5

    def test_api_unavailable_returns_error_dict(self) -> None:
        with patch.object(eco, "_api_call", return_value=None):
            result = _index_diff_latest()
        assert result == {"success": False, "error": "api_unavailable"}

    def test_other_failure_passes_through(self) -> None:
        """500-style errors must NOT be confused with the 404 P0.4 stub signal."""
        failure = {
            "success": False,
            "error": "HTTP 500: Internal Server Error",
            "detail": "boom",
        }
        with patch.object(eco, "_api_call", return_value=failure):
            result = _index_diff_latest()
        assert result["success"] is False
        assert result["error"] == "HTTP 500: Internal Server Error"


# ============================================================
# Project header propagation (smoke check across the P0.3 tools)
# ============================================================


class TestProjectHeaderPropagation:
    """Every P0.3 tool must pass extra_headers (X-Project-Id) to _api_call."""

    def _assert_called_with_headers(self, mock_api) -> None:
        kwargs = mock_api.call_args.kwargs
        assert "extra_headers" in kwargs

    def test_quick_setup_passes_headers(self) -> None:
        with patch.object(eco, "_api_call", return_value={"success": True, "data_source_ids": []}) as mock_api:
            _quick_setup(sources=["github"])
        self._assert_called_with_headers(mock_api)

    def test_index_update_passes_headers(self) -> None:
        with patch.object(eco, "_api_call", return_value={"success": True}) as mock_api:
            _index_update()
        self._assert_called_with_headers(mock_api)

    def test_index_diff_latest_passes_headers(self) -> None:
        with patch.object(eco, "_api_call", return_value={"success": True}) as mock_api:
            _index_diff_latest()
        self._assert_called_with_headers(mock_api)


# ============================================================
# ecosystem_repo_manual_status (merged: pin / unpin / no_value / clear)
# ============================================================


_manual_status = _capture.tools["ecosystem_repo_manual_status"]


class TestRepoManualStatus:
    """One tool replaces four wrappers that posted identical payloads."""

    def _call(self, **kwargs):  # type: ignore[no-untyped-def]
        with patch.object(
            eco, "_api_call", return_value={"success": True}
        ) as mock_api:
            result = _manual_status(repo_id="repo-1", **kwargs)
        return result, mock_api

    def test_pin_posts_pinned_status(self) -> None:
        _, mock_api = self._call(status="pinned", reason="core dep")
        args = mock_api.call_args.args
        assert args[0] == "POST"
        assert args[1] == "/api/ecosystem/repos/repo-1/manual_status"
        assert args[2]["status"] == "pinned"
        assert args[2]["reason"] == "core dep"

    def test_no_value_posts_no_value_status(self) -> None:
        _, mock_api = self._call(status="no_value", reason="dead project")
        assert mock_api.call_args.args[2]["status"] == "no_value"

    def test_empty_status_clears_override(self) -> None:
        """Both former 'unpin' and 'clear_manual_status' collapse into this."""
        _, mock_api = self._call()
        assert mock_api.call_args.args[2]["status"] is None

    def test_status_is_normalized(self) -> None:
        _, mock_api = self._call(status="  PINNED ")
        assert mock_api.call_args.args[2]["status"] == "pinned"

    def test_unknown_status_rejected_without_api_call(self) -> None:
        with patch.object(eco, "_api_call") as mock_api:
            result = _manual_status(repo_id="repo-1", status="bogus")
        assert result["success"] is False
        assert result["error"] == "invalid_status"
        mock_api.assert_not_called()

    def test_passes_project_headers(self) -> None:
        with patch.object(
            eco, "_api_call", return_value={"success": True}
        ) as mock_api:
            _manual_status(repo_id="repo-1", project_id="proj-9")
        assert "extra_headers" in mock_api.call_args.kwargs

    def test_api_unavailable_returns_error_dict(self) -> None:
        with patch.object(eco, "_api_call", return_value=None):
            result = _manual_status(repo_id="repo-1", status="pinned")
        assert result["success"] is False
        assert result["error"] == "api_unavailable"
