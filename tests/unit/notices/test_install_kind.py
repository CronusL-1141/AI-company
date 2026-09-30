"""Installation kind is decided API-side from files (docs/user-notice-design.md E09)."""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi import FastAPI

from aiteam.services.notices import install_kind

from .conftest import write_json


def _plugins(home, key="ai-team-os@ai-team-os", version="1.14.0", install_path="/cache/ai-team-os/1.14.0"):
    return write_json(home / ".claude/plugins/installed_plugins.json", {
        "version": 2, "plugins": {key: [{"scope": "user", "version": version, "installPath": install_path}]},
    })


def _settings(home, enabled):
    return write_json(home / ".claude/settings.json", {"enabledPlugins": enabled})


def _source(home, path="/src/AI-company"):
    target = home / ".claude/data/ai-team-os/install_path.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(path + "\n")


async def test_codex_is_always_codex(isolated_home):
    _plugins(isolated_home)
    assert (await install_kind.detect("codex")).kind == "codex"


@pytest.mark.parametrize("enabled", [None, {"ai-team-os@ai-team-os": True}, {"other@x": False}],
                         ids=["no-entry", "enabled", "other-plugin-off"])
async def test_installed_plugin_not_switched_off_is_plugin(isolated_home, enabled, monkeypatch):
    # The main-chain copy that serves plugin users has no CLAUDE_PLUGIN_ROOT.
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    _plugins(isolated_home)
    _source(isolated_home)
    if enabled is not None:
        _settings(isolated_home, enabled)
    result = await install_kind.detect("cc")
    assert result.kind == "cc-plugin" and result.variant == "cc_plugin"
    assert result.plugin_version == "1.14.0" and result.baseline == "/cache/ai-team-os/1.14.0"


async def test_switched_off_plugin_falls_back_to_source(isolated_home):
    _plugins(isolated_home)
    _settings(isolated_home, {"ai-team-os@ai-team-os": False})
    _source(isolated_home, "/src/tree")
    result = await install_kind.detect("cc")
    assert result.kind == "cc-source" and result.baseline == "/src/tree"


async def test_nothing_known_is_unknown(isolated_home):
    assert (await install_kind.detect("cc")).kind == "unknown"
    (isolated_home / ".claude/plugins").mkdir(parents=True)
    (isolated_home / ".claude/plugins/installed_plugins.json").write_text("{broken")
    assert (await install_kind.detect("cc")).variant == "unknown"


async def test_other_plugins_do_not_count(isolated_home):
    _plugins(isolated_home, key="equity-research@x")
    _source(isolated_home)
    assert (await install_kind.detect("cc")).kind == "cc-source"


async def test_claude_config_dir_is_honoured(isolated_home, tmp_path, monkeypatch):
    config = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    write_json(config / "plugins/installed_plugins.json", {"plugins": {"ai-team-os@m": [{"version": "2"}]}})
    assert (await install_kind.detect("cc")).kind == "cc-plugin"


async def test_release_endpoint_ignores_the_hook_guess_for_cc(isolated_home, tmp_path, monkeypatch):
    """Old hook copies send installation=cc-source; a plugin user still gets the plugin command."""
    from aiteam.api import release_updates
    from aiteam.api.routes import health

    _plugins(isolated_home)
    checker = release_updates.ReleaseChecker(
        tmp_path / "rel.json",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            "tag_name": "v99.0.0", "draft": False, "prerelease": False})),
    )
    monkeypatch.setattr(health, "release_checker", checker)
    app = FastAPI()
    app.include_router(health.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        cc = (await client.get("/api/releases/latest?host=cc&installation=cc-source&language=en")).json()
        assert "claude plugin marketplace update ai-team-os" in cc["additional_context"]
        codex = (await client.get("/api/releases/latest?host=codex&installation=cc-plugin")).json()
        assert "codex_adapter.py upgrade" in codex["notice"]
    assert os.environ.get("CLAUDE_PLUGIN_ROOT") is None
