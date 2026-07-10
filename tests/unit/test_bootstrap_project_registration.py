"""Unit tests for project registration check in session_bootstrap."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers to import bootstrap without triggering module-level side-effects
# ---------------------------------------------------------------------------

def _import_bootstrap():
    """Import session_bootstrap with API calls patched out."""
    with patch("urllib.request.urlopen"):
        import importlib

        import aiteam.hooks.session_bootstrap as mod
        importlib.reload(mod)
        return mod


# ---------------------------------------------------------------------------
# Tests for _check_project_registration
# ---------------------------------------------------------------------------

class TestCheckProjectRegistration:
    """Tests for _check_project_registration helper."""

    def _make_module(self):
        with patch("urllib.request.urlopen"):
            import importlib

            import aiteam.hooks.session_bootstrap as mod
            importlib.reload(mod)
            return mod

    def test_registered_project_via_context_resolve(self, tmp_path):
        """API returns project_id -> is_registered=True."""
        mod = self._make_module()

        fake_response_body = json.dumps({
            "project_id": "abc123",
            "project": {"id": "abc123", "name": "Test Project"},
        }).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            is_registered, is_dismissed, proj = mod._check_project_registration(
                "http://localhost:8000", str(tmp_path)
            )

        assert is_registered is True
        assert is_dismissed is False
        assert proj.get("id") == "abc123"

    def test_unregistered_project_returns_empty_project_id(self, tmp_path):
        """API returns empty project_id -> is_registered=False."""
        mod = self._make_module()

        fake_response_body = json.dumps({"project_id": "", "project": None}).encode("utf-8")

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            is_registered, is_dismissed, proj = mod._check_project_registration(
                "http://localhost:8000", str(tmp_path)
            )

        assert is_registered is False
        assert proj == {}

    def test_dismissed_cwd_returns_is_dismissed_true(self, tmp_path, monkeypatch):
        """If cwd is in dismissed list, is_dismissed=True regardless of API."""
        # Isolate from the real ~/.claude data dir: fake home BEFORE module
        # reload so _DISMISSED_PROJECTS_FILE is baked with tmp_path.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        mod = self._make_module()

        cwd = str(tmp_path)
        cwd_norm = str(Path(cwd).resolve()).replace("\\", "/").lower()

        # Write a dismissed_projects.json containing this cwd (under fake home)
        dismissed_file = tmp_path / ".claude" / "data" / "ai-team-os" / "dismissed_projects.json"
        dismissed_file.parent.mkdir(parents=True, exist_ok=True)
        dismissed_file.write_text(json.dumps({"dismissed": [cwd_norm]}), encoding="utf-8")

        fake_response_body = json.dumps({"project_id": "", "project": None}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            is_registered, is_dismissed, proj = mod._check_project_registration(
                "http://localhost:8000", cwd
            )

        assert is_dismissed is True

    def test_api_unreachable_returns_not_registered(self, tmp_path):
        """If API is unreachable, gracefully returns is_registered=False."""
        mod = self._make_module()

        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            is_registered, is_dismissed, proj = mod._check_project_registration(
                "http://localhost:9999", str(tmp_path)
            )

        assert is_registered is False
        assert proj == {}


# ---------------------------------------------------------------------------
# Tests for _build_briefing injection behavior
# ---------------------------------------------------------------------------

class TestBuildBriefingProjectRegistrationSection:
    """Tests that _build_briefing injects or omits the registration prompt."""

    def _make_module(self):
        with patch("urllib.request.urlopen"):
            import importlib

            import aiteam.hooks.session_bootstrap as mod
            importlib.reload(mod)
            return mod

    def _patch_build_briefing_deps(self, mod, *, is_registered, is_dismissed):
        """Helper to patch all side-effectful calls in _build_briefing."""
        return [
            patch.object(mod, "_check_teams_dir_cleanup", return_value=None),
            patch.object(mod, "_check_for_updates", return_value=None),
            patch.object(mod, "_check_project_registration",
                         return_value=(is_registered, is_dismissed, {})),
            patch.object(mod, "_api_get", return_value=None),
            patch.object(mod, "_load_team_config", return_value=None),
            patch("os.getcwd", return_value="/tmp/test-project"),
            patch("os.path.isdir", return_value=False),
        ]

    def test_unregistered_not_dismissed_shows_prompt(self):
        """Unregistered + not dismissed -> registration prompt appears in briefing."""
        mod = self._make_module()

        patches = self._patch_build_briefing_deps(mod, is_registered=False, is_dismissed=False)
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)

            # Also patch ThreadPoolExecutor to avoid real HTTP calls
            mock_future = MagicMock()
            mock_future.result.return_value = None
            mock_pool = MagicMock()
            mock_pool.__enter__ = lambda s: s
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.submit.return_value = mock_future
            stack.enter_context(
                patch("aiteam.hooks.session_bootstrap.ThreadPoolExecutor", return_value=mock_pool)
            )

            briefing = mod._build_briefing()

        assert "未注册到 AI Team OS 项目系统" in briefing
        assert "dismiss_project_registration" in briefing
        assert "project_create" in briefing

    def test_unregistered_dismissed_no_prompt(self):
        """Unregistered + dismissed -> no registration prompt in briefing."""
        mod = self._make_module()

        patches = self._patch_build_briefing_deps(mod, is_registered=False, is_dismissed=True)
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)

            mock_future = MagicMock()
            mock_future.result.return_value = None
            mock_pool = MagicMock()
            mock_pool.__enter__ = lambda s: s
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.submit.return_value = mock_future
            stack.enter_context(
                patch("aiteam.hooks.session_bootstrap.ThreadPoolExecutor", return_value=mock_pool)
            )

            briefing = mod._build_briefing()

        assert "未注册到 AI Team OS 项目系统" not in briefing

    def test_registered_project_no_registration_prompt(self):
        """Registered project -> no registration prompt in briefing."""
        mod = self._make_module()

        patches = self._patch_build_briefing_deps(mod, is_registered=True, is_dismissed=False)
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)

            mock_future = MagicMock()
            mock_future.result.return_value = None
            mock_pool = MagicMock()
            mock_pool.__enter__ = lambda s: s
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.submit.return_value = mock_future
            stack.enter_context(
                patch("aiteam.hooks.session_bootstrap.ThreadPoolExecutor", return_value=mock_pool)
            )

            briefing = mod._build_briefing()

        assert "未注册到 AI Team OS 项目系统" not in briefing
