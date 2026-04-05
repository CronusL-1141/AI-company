"""Unit tests for MCP error recovery strategy mapping.

Covers:
- _error_recovery module helpers (get_http_recovery, get_connection_recovery, get_business_recovery)
- _api_call in _base.py attaches _recovery and _error_category on HTTP errors
- _api_call attaches recovery on URLError / generic exceptions
- Existing successful responses are unchanged
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from aiteam.mcp._error_recovery import (
    BUSINESS_ERROR_MAP,
    ERROR_RECOVERY_MAP,
    get_business_recovery,
    get_connection_recovery,
    get_http_recovery,
)
from aiteam.mcp._base import _api_call


# ============================================================
# get_http_recovery
# ============================================================


class TestGetHttpRecovery:
    def test_404_returns_resource_not_found(self):
        info = get_http_recovery(404)
        assert info["category"] == "resource_not_found"
        assert "_recovery" not in info  # field name is "recovery" not "_recovery"
        assert "recovery" in info

    def test_409_returns_conflict(self):
        info = get_http_recovery(409)
        assert info["category"] == "conflict"

    def test_422_returns_validation_error(self):
        info = get_http_recovery(422)
        assert info["category"] == "validation_error"

    def test_500_returns_server_error(self):
        info = get_http_recovery(500)
        assert info["category"] == "server_error"
        assert "os_health_check" in info["recovery"]

    def test_unknown_5xx_falls_back_to_500(self):
        info = get_http_recovery(503)
        # 503 is explicitly mapped
        assert info["category"] == "service_unavailable"

    def test_unmapped_5xx_falls_back_to_server_error(self):
        # 599 has no entry, should fall back to 500 category
        info = get_http_recovery(599)
        assert info["category"] == "server_error"

    def test_unmapped_4xx_returns_empty(self):
        # 418 I'm a Teapot — not in map, not 5xx
        info = get_http_recovery(418)
        assert info == {}


# ============================================================
# get_connection_recovery
# ============================================================


class TestGetConnectionRecovery:
    def test_connection_refused(self):
        info = get_connection_recovery("Connection refused")
        assert info["category"] == "api_unavailable"

    def test_timeout(self):
        info = get_connection_recovery("timed out waiting for server")
        assert info["category"] == "timeout"

    def test_generic_connect_string(self):
        info = get_connection_recovery("Failed to connect to host")
        assert info["category"] == "api_unavailable"

    def test_unknown_error_falls_back(self):
        info = get_connection_recovery("some weird ssl error")
        assert info["category"] == "unknown"


# ============================================================
# get_business_recovery
# ============================================================


class TestGetBusinessRecovery:
    def test_not_found_english(self):
        assert get_business_recovery("Task not found in database") == "resource_not_found"

    def test_not_found_chinese(self):
        assert get_business_recovery("该资源不存在") == "resource_not_found"

    def test_conflict_already_exists(self):
        assert get_business_recovery("Team already exists") == "conflict"

    def test_state_conflict_ended(self):
        assert get_business_recovery("会议已结束，无法操作") == "state_conflict"

    def test_permission(self):
        assert get_business_recovery("permission denied for this action") == "permission_denied"

    def test_no_match_returns_empty(self):
        assert get_business_recovery("everything is fine") == ""

    def test_case_insensitive(self):
        assert get_business_recovery("NOT FOUND") == "resource_not_found"


# ============================================================
# _api_call — HTTP errors attach recovery fields
# ============================================================


def _make_http_error(code: int, reason: str = "Error", body: bytes = b"") -> urllib.error.HTTPError:
    resp = MagicMock()
    resp.read.return_value = body
    return urllib.error.HTTPError(url="http://localhost:8000/test", code=code,
                                  msg=reason, hdrs={}, fp=BytesIO(body))


class TestApiCallHttpErrors:
    @patch("urllib.request.urlopen")
    def test_404_has_recovery_fields(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(404, "Not Found", b'{"detail":"not found"}')
        result = _api_call("GET", "/api/nonexistent")
        assert result["success"] is False
        assert result["_error_category"] == "resource_not_found"
        assert len(result["_recovery"]) > 0

    @patch("urllib.request.urlopen")
    def test_409_has_conflict_category(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(409, "Conflict", b'{"detail":"already exists"}')
        result = _api_call("POST", "/api/teams")
        assert result["success"] is False
        # business keyword "already exists" upgrades category
        assert result["_error_category"] == "conflict"

    @patch("urllib.request.urlopen")
    def test_422_has_validation_category(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(422, "Unprocessable Entity")
        result = _api_call("POST", "/api/tasks")
        assert result["_error_category"] == "validation_error"

    @patch("urllib.request.urlopen")
    def test_500_has_server_error_and_health_check_hint(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(500, "Internal Server Error")
        result = _api_call("GET", "/api/teams")
        assert result["_error_category"] == "server_error"
        assert "os_health_check" in result["_recovery"]

    @patch("urllib.request.urlopen")
    def test_error_preserves_existing_fields(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(404, "Not Found", b'{}')
        result = _api_call("GET", "/api/tasks/bad-id")
        # Original fields still present
        assert "error" in result
        assert "detail" in result
        assert result["success"] is False

    @patch("urllib.request.urlopen")
    def test_business_keyword_in_body_overrides_category(self, mock_urlopen):
        # 404 base category is resource_not_found, body also says "not found" → stays resource_not_found
        mock_urlopen.side_effect = _make_http_error(404, "Not Found", b'{"detail":"task not found"}')
        result = _api_call("GET", "/api/tasks/123")
        assert result["_error_category"] == "resource_not_found"

    @patch("urllib.request.urlopen")
    def test_409_with_already_exists_body(self, mock_urlopen):
        mock_urlopen.side_effect = _make_http_error(409, "Conflict", b'{"detail":"Team already exists"}')
        result = _api_call("POST", "/api/teams")
        # business "already exists" → conflict
        assert result["_error_category"] == "conflict"


# ============================================================
# _api_call — connection/generic errors attach recovery fields
# ============================================================


class TestApiCallConnectionErrors:
    @patch("urllib.request.urlopen")
    def test_url_error_connection_refused(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        result = _api_call("GET", "/api/teams")
        assert result["success"] is False
        assert result["_error_category"] == "api_unavailable"
        assert len(result["_recovery"]) > 0
        # Original hint still present
        assert "hint" in result

    @patch("urllib.request.urlopen")
    def test_url_error_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("timed out")
        result = _api_call("GET", "/api/teams")
        assert result["success"] is False
        assert result["_error_category"] == "timeout"

    @patch("urllib.request.urlopen")
    def test_generic_exception(self, mock_urlopen):
        mock_urlopen.side_effect = RuntimeError("unexpected ssl failure")
        result = _api_call("GET", "/api/teams")
        assert result["success"] is False
        assert "_error_category" in result
        assert "_recovery" in result


# ============================================================
# _api_call — successful responses are unchanged
# ============================================================


class TestApiCallSuccessUnchanged:
    @patch("urllib.request.urlopen")
    def test_success_has_no_recovery_fields(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps({"success": True, "data": []}).encode()
        mock_urlopen.return_value = mock_resp

        result = _api_call("GET", "/api/teams")
        assert result["success"] is True
        assert "_recovery" not in result
        assert "_error_category" not in result
