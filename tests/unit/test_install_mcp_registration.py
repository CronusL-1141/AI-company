"""AI Team OS - installer / CC plugin mutual detection tests.

The plugin and the source installer each provide the same MCP server. Installing
the plugin first and then running install.py used to register a second copy that
every session loaded on top of the plugin's one, because register_global_mcp only
ever asked whether ~/.claude.json already had an entry.

Home is faked by monkeypatching pathlib.Path.home so nothing touches the real
~/.claude, and subprocess.run is stubbed so the real `claude` CLI is never
invoked (it would write to the real user config regardless of the fake home).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def install_mod():
    return _load(REPO_ROOT / "install.py", "install_mcp_reg_under_test")


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture()
def no_claude_cli(monkeypatch):
    """Stub the `claude mcp add-json` call and record whether it ran.

    Returns a non-zero result so registration falls through to the direct
    ~/.claude.json write, which is the path the fake home can observe.
    """
    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no cli")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


@pytest.fixture()
def project_root(tmp_path):
    """A throwaway project dir - the fallback branch writes .mcp.json into it."""
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _write_settings(home: Path, enabled: dict | None) -> None:
    payload: dict = {}
    if enabled is not None:
        payload["enabledPlugins"] = enabled
    (home / ".claude" / "settings.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_installed_plugins(home: Path, keys: list[str]) -> None:
    plugins_dir = home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "plugins": {key: [{"scope": "user", "version": "1.0.0"}] for key in keys},
    }
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _global_mcp_names(home: Path) -> set[str]:
    path = home / ".claude.json"
    if not path.exists():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}))


# ---------------------------------------------------------------------------
# plugin_install_state
# ---------------------------------------------------------------------------

class TestPluginInstallState:
    def test_absent_when_nothing_installed(self, install_mod, fake_home):
        assert install_mod.plugin_install_state() == install_mod.PLUGIN_ABSENT

    def test_enabled_requires_explicit_true(self, install_mod, fake_home):
        _write_settings(fake_home, {"ai-team-os@ai-team-os": True})
        assert install_mod.plugin_install_state() == install_mod.PLUGIN_ENABLED

    def test_any_marketplace_suffix_matches(self, install_mod, fake_home):
        _write_settings(fake_home, {"ai-team-os@some-other-marketplace": True})
        assert install_mod.plugin_install_state() == install_mod.PLUGIN_ENABLED

    def test_other_plugins_do_not_match(self, install_mod, fake_home):
        _write_settings(fake_home, {"equity-research@whatever": True})
        _write_installed_plugins(fake_home, ["equity-research@whatever"])
        assert install_mod.plugin_install_state() == install_mod.PLUGIN_ABSENT

    def test_installed_but_switched_off_is_not_enabled(self, install_mod, fake_home):
        """Installed + explicit false still needs the global registration."""
        _write_settings(fake_home, {"ai-team-os@ai-team-os": False})
        _write_installed_plugins(fake_home, ["ai-team-os@ai-team-os"])
        assert install_mod.plugin_install_state() == install_mod.PLUGIN_PRESENT_UNKNOWN

    def test_installed_without_settings_entry_is_unknown(self, install_mod, fake_home):
        _write_installed_plugins(fake_home, ["ai-team-os@ai-team-os"])
        assert install_mod.plugin_install_state() == install_mod.PLUGIN_PRESENT_UNKNOWN

    def test_unreadable_files_read_as_absent(self, install_mod, fake_home):
        """Corrupt config must not silently skip registration."""
        (fake_home / ".claude" / "settings.json").write_text("{ not json", encoding="utf-8")
        plugins_dir = fake_home / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        (plugins_dir / "installed_plugins.json").write_text("[]", encoding="utf-8")
        assert install_mod.plugin_install_state() == install_mod.PLUGIN_ABSENT


# ---------------------------------------------------------------------------
# register_global_mcp
# ---------------------------------------------------------------------------

class TestRegisterGlobalMcp:
    def test_registers_when_plugin_absent(
        self, install_mod, fake_home, no_claude_cli, project_root
    ):
        install_mod.register_global_mcp(project_root)
        assert "ai-team-os" in _global_mcp_names(fake_home)
        assert no_claude_cli, "the CLI path should have been attempted"

    def test_skips_when_plugin_enabled(
        self, install_mod, fake_home, no_claude_cli, project_root, capsys
    ):
        _write_settings(fake_home, {"ai-team-os@ai-team-os": True})
        _write_installed_plugins(fake_home, ["ai-team-os@ai-team-os"])

        install_mod.register_global_mcp(project_root)

        assert _global_mcp_names(fake_home) == set()
        assert no_claude_cli == [], "nothing should be registered at all"
        assert not (project_root / ".mcp.json").exists(), "no fallback write either"
        assert "[SKIP]" in capsys.readouterr().out

    def test_force_registers_despite_enabled_plugin(
        self, install_mod, fake_home, no_claude_cli, project_root
    ):
        _write_settings(fake_home, {"ai-team-os@ai-team-os": True})
        _write_installed_plugins(fake_home, ["ai-team-os@ai-team-os"])

        install_mod.register_global_mcp(project_root, force=True)

        assert "ai-team-os" in _global_mcp_names(fake_home)

    def test_installed_but_unknown_warns_and_registers(
        self, install_mod, fake_home, no_claude_cli, project_root, capsys
    ):
        """A duplicate server is recoverable; no server at all is not."""
        _write_installed_plugins(fake_home, ["ai-team-os@ai-team-os"])

        install_mod.register_global_mcp(project_root)

        assert "ai-team-os" in _global_mcp_names(fake_home)
        assert "[WARN]" in capsys.readouterr().out

    def test_existing_unrelated_servers_are_kept(
        self, install_mod, fake_home, no_claude_cli, project_root
    ):
        (fake_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
        )

        install_mod.register_global_mcp(project_root)

        assert _global_mcp_names(fake_home) == {"other", "ai-team-os"}


# ---------------------------------------------------------------------------
# verify_installation - the mutual-exclusion line
# ---------------------------------------------------------------------------

class TestVerifyReportsMcpSource:
    def test_both_sources_present_warns(
        self, install_mod, fake_home, project_root, capsys
    ):
        _write_settings(fake_home, {"ai-team-os@ai-team-os": True})
        (fake_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"ai-team-os": {"command": "x"}}}), encoding="utf-8"
        )

        install_mod.verify_installation(project_root)

        out = capsys.readouterr().out
        assert "[WARN] MCP server registered once" in out

    def test_plugin_only_is_ok_and_global_not_required(
        self, install_mod, fake_home, project_root, capsys
    ):
        """Plugin enabled and no global entry is a correct install, not a failure."""
        _write_settings(fake_home, {"ai-team-os@ai-team-os": True})

        install_mod.verify_installation(project_root)

        out = capsys.readouterr().out
        assert "[OK] MCP server registered once" in out
        assert "[FAIL] Global MCP in ~/.claude.json" not in out
        assert "[WARN] Global MCP in ~/.claude.json" in out

    def test_global_only_is_ok(self, install_mod, fake_home, project_root, capsys):
        (fake_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"ai-team-os": {"command": "x"}}}), encoding="utf-8"
        )

        install_mod.verify_installation(project_root)

        out = capsys.readouterr().out
        assert "[OK] MCP server registered once" in out
        assert "[OK] Global MCP in ~/.claude.json" in out

    def test_neither_source_fails(self, install_mod, fake_home, project_root, capsys):
        install_mod.verify_installation(project_root)

        out = capsys.readouterr().out
        assert "[FAIL] Global MCP in ~/.claude.json" in out
