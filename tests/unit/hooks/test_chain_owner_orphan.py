"""Who owns a config folder's main chain, and when it is an orphan (docs/config-dir-compat-design.md §4.3, §7 T4).

The distributed session_bootstrap runs as a subprocess from a chain installed in
a configured Claude Code folder (CLAUDE_CONFIG_DIR, outside the default one),
registered in that folder's settings.json the way the installers write it now
(``${CLAUDE_CONFIG_DIR:-...}/hooks/ai-team-os/...``). HOME is a throwaway
directory and the API is unreachable: the orphan check reads files only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ._notice_fakes import PLUGIN_HOOKS, REFUSED_URL, data_dir, hook_env, output

PLUGIN = "ai-team-os@ai-team-os"
E06 = "插件已卸载或停用，但全局 hook 仍在运行"
MISSING = "找不到 OS 数据目录"
CHAIN_FILES = ("session_bootstrap.py", "user_notice.py", "hook_core.py", "hook_delivery.py", "uninstall_main_chain.py")


def _machine(tmp_path: Path, *, owner: str | None, plugin: str, old_marker: str | None = None,
             install_path: bool = False, data: bool = True) -> tuple[dict, Path]:
    """A config folder whose settings.json registers the main chain; returns (hook env, chain dir)."""
    home, config = tmp_path / "home", tmp_path / "profile"
    chain = config / "hooks" / "ai-team-os"
    chain.mkdir(parents=True)
    for name in CHAIN_FILES:
        shutil.copy2(PLUGIN_HOOKS / name, chain / name)
    command = f'"{sys.executable}" "${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}/hooks/ai-team-os/session_bootstrap.py"'
    settings: dict = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    if plugin != "absent":
        (config / "plugins").mkdir()
        (config / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {PLUGIN: [{"scope": "user"}]}}), encoding="utf-8")
        settings["enabledPlugins"] = {PLUGIN: plugin == "enabled"}
    (config / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    if owner is not None:
        (chain / ".chain-owner.json").write_text(json.dumps({"installed_by": owner, "baseline": "/b", "version": "1"}))
    env = hook_env(home, REFUSED_URL, CLAUDE_CONFIG_DIR=str(config))
    if old_marker is not None:  # the machine-wide marker plugins wrote before owner records
        (data_dir(home) / "main-chain.json").write_text(json.dumps({"installed_by": old_marker}))
    if install_path:  # what install.py recorded before owner records
        (data_dir(home) / "install_path.txt").write_text("/source/check-out", encoding="utf-8")
    if not data:
        shutil.rmtree(data_dir(home))
    return env, chain


def _session_start(env: dict, chain: Path) -> str:
    payload = {"session_id": "s1", "cwd": env["HOME"], "source": "startup"}
    result = subprocess.run([sys.executable, str(chain / "session_bootstrap.py")], input=json.dumps(payload),
                            text=True, capture_output=True, env=env, cwd=env["HOME"], timeout=30)
    assert result.returncode == 0, result.stderr
    return output(result.stdout).get("systemMessage", "")


@pytest.mark.parametrize(("owner", "plugin", "old_marker", "install_path", "orphan"), [
    pytest.param("plugin", "absent", None, False, True, id="plugin-owned-plugin-removed"),
    pytest.param("plugin", "disabled", None, False, True, id="plugin-owned-plugin-disabled"),
    pytest.param("plugin", "enabled", None, False, False, id="plugin-owned-plugin-enabled"),
    pytest.param("source", "absent", None, False, False, id="source-owned"),
    pytest.param(None, "absent", None, False, False, id="no-owner-record-no-evidence"),
    pytest.param("source", "absent", "plugin", False, False, id="owner-record-outranks-old-marker"),
    pytest.param("plugin", "absent", None, True, True, id="owner-record-outranks-install-path"),
    pytest.param(None, "absent", "plugin", False, True, id="before-owner-records-old-marker-says-plugin"),
    pytest.param(None, "absent", "plugin", True, False, id="before-owner-records-source-recorded"),
])
def test_only_a_plugin_owned_chain_without_its_plugin_is_an_orphan(tmp_path, owner, plugin, old_marker,
                                                                   install_path, orphan):
    env, chain = _machine(tmp_path, owner=owner, plugin=plugin, old_marker=old_marker, install_path=install_path)
    line = _session_start(env, chain)
    assert (E06 in line) is orphan, line
    assert MISSING not in line


@pytest.mark.parametrize(("owner", "plugin"), [
    pytest.param("plugin", "absent", id="plugin-owned-plugin-removed"),
    pytest.param("plugin", "enabled", id="plugin-owned-plugin-enabled"),
    pytest.param("source", "absent", id="source-owned"),
    pytest.param(None, "absent", id="no-owner-record"),
])
def test_without_a_data_directory_only_that_is_reported(tmp_path, owner, plugin):
    env, chain = _machine(tmp_path, owner=owner, plugin=plugin, data=False)
    line = _session_start(env, chain)
    assert MISSING in line and E06 not in line, line
    assert not data_dir(Path(env["HOME"])).exists(), "nothing is created"
