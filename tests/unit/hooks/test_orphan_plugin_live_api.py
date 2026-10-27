"""A surviving API cannot make a removed plugin's global hooks usable.

Run the distributed scripts from a throwaway installed chain, with isolated
HOME/config and a real temporary HTTP listener. No host settings or shared
service are changed; the HTTP fake validates production pending schemas.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ._notice_fakes import PLUGIN_HOOKS, FakeApi, data_dir, hook_env, local_records, output, owed_starts

PLUGIN = "ai-team-os@ai-team-os"
E06 = "插件已卸载或停用，但全局 hook 仍在运行"


@pytest.fixture()
def installed(tmp_path):
    home = tmp_path / "home"
    config = home / ".claude"
    chain = config / "hooks" / "ai-team-os"
    chain.mkdir(parents=True)
    for name in ("session_bootstrap.py", "channel_unread.py", "user_notice.py", "hook_core.py",
                 "hook_delivery.py", "uninstall_main_chain.py"):
        shutil.copy2(PLUGIN_HOOKS / name, chain / name)
    _settings(config, chain)
    # The plugin's self-heal records that it installed the chain (auto_install._mark_plugin_chain);
    # without that marker nothing is judged an orphan (task b7d973fd).
    data_dir(home).mkdir(parents=True, exist_ok=True)
    (data_dir(home) / "main-chain.json").write_text(json.dumps({"installed_by": "plugin"}), encoding="utf-8")
    return home, config, chain


def _settings(config: Path, chain: Path, *, disabled: bool = False, registered: bool = True):
    settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
        "command": f'"{sys.executable}" "{chain}/session_bootstrap.py"'}]}]}} if registered else {}
    if disabled:
        settings["enabledPlugins"] = {PLUGIN: False}
    (config / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _plugin(config: Path):
    path = config / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "plugins": {PLUGIN: [{"scope": "user"}]}}), encoding="utf-8")


def _run(installed, api_url: str, *, source: str = "startup", session: str = "s1", prompt: bool = False):
    home, config, chain = installed
    env = hook_env(home, api_url, CLAUDE_CONFIG_DIR=str(config))
    payload = {"session_id": session, "cwd": str(home), "source": source, "prompt": "hello"}
    script = "channel_unread.py" if prompt else "session_bootstrap.py"
    result = subprocess.run(
        [sys.executable, str(chain / script), *(["leader-cc"] if prompt else [])],
        input=json.dumps(payload), text=True, capture_output=True, env=env, cwd=home, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result


def _routes(api: FakeApi, home: Path):
    api.routes.update({
        ("GET", "/api/teams"): lambda _: (200, {"data": []}),
        ("GET", "/api/projects"): lambda _: (200, {"data": [{"id": "project-1", "root_path": str(home)}]}),
        ("GET", "/api/leader-briefings"): lambda _: (200, {"items": [], "total": 0}),
        ("POST", "/api/context/resolve"): lambda _: (200, {"project_id": ""}),
        ("GET", "/api/projects/project-1/task-wall/digest"): lambda _: (200, {"text": "TASK-WALL-CONTENT"}),
        ("GET", "/api/memories"): lambda _: (200, {"data": []}),
        ("GET", "/api/hooks/compact-checkpoint"): lambda _: (200, {"found": False}),
    })


def _assert_orphan(result):
    doc = output(result.stdout)
    assert E06 in doc.get("systemMessage", ""), result.stdout
    context = doc["hookSpecificOutput"]["additionalContext"]
    assert "uninstall_main_chain.py" in context
    for absent in ("Leader简报", "task_memo_add", "report_save", "project_create", "服务未启动", "LEDGER-MCP-NOTE"):
        assert absent not in result.stdout


@pytest.mark.parametrize("state", ["removed", "disabled"])
@pytest.mark.parametrize("source", ["startup", "resume", "fork", "clear", "compact"])
def test_orphan_is_detected_before_online_api_and_does_not_owe_a_briefing(installed, state, source):
    home, config, chain = installed
    if state == "disabled":
        _plugin(config)
        _settings(config, chain, disabled=True)
    with FakeApi() as api:
        _routes(api, home)
        api.pending = [{"language": "zh", "model_text": "LEDGER-MCP-NOTE"}]
        _assert_orphan(_run(installed, api.url, source=source))
        assert api.requests == [], "detect installation absence before probing API or fetching notices"
        assert owed_starts(home) == [], "removed tools cannot have a briefing owed to them"
        first_prompt = _run(installed, api.url, prompt=True)
        if source == "startup":
            assert first_prompt.stdout == "", "reliable startup already showed E06"
        else:
            _assert_orphan(first_prompt)  # clear/resume/fork/compact lines may not be displayed by CC
        assert _run(installed, api.url, prompt=True).stdout == ""
        assert api.requests == [], "neither owed nor ordinary prompts fetch orphan ledger notes"
        assert not any(r.get("catalog_id") == "api_down" for r in local_records(home))


@pytest.mark.parametrize("source", ["startup", "compact"])
def test_offline_orphan_does_not_mark_start_owed(installed, source):
    _assert_orphan(_run(installed, "http://127.0.0.1:9", source=source))
    assert owed_starts(installed[0]) == []


def test_a_previously_owed_start_is_cancelled_if_plugin_is_removed(installed):
    home, config, _chain = installed
    _plugin(config)
    first = _run(installed, "http://127.0.0.1:9")
    assert "服务正在启动" in output(first.stdout)["systemMessage"]
    assert len(owed_starts(home)) == 1
    (config / "plugins" / "installed_plugins.json").unlink()
    with FakeApi() as api:
        _routes(api, home)
        _assert_orphan(_run(installed, api.url, prompt=True))
        assert owed_starts(home) == [] and api.requests == []
        assert _run(installed, api.url, prompt=True).stdout == ""


@pytest.mark.parametrize("owner", ["enabled-plugin", "source-install", "source-marker", "unregistered-chain",
                                   "no-marker"])
def test_legitimate_owners_keep_their_online_briefing(installed, owner):
    """No marker is no evidence: an old source install without install_path.txt is not an orphan
    (the default used to be "plugin", which reported a moved source install, task b7d973fd)."""
    home, config, chain = installed
    if owner == "no-marker":
        (data_dir(home) / "main-chain.json").unlink()
    elif owner == "enabled-plugin":
        _plugin(config)
    elif owner == "source-install":
        data_dir(home).mkdir(parents=True, exist_ok=True)
        (data_dir(home) / "install_path.txt").write_text("/source/check-out", encoding="utf-8")
    elif owner == "source-marker":
        data_dir(home).mkdir(parents=True, exist_ok=True)
        (data_dir(home) / "main-chain.json").write_text(json.dumps({"installed_by": "source"}))
    else:
        _settings(config, chain, registered=False)
    with FakeApi() as api:
        _routes(api, home)
        result = _run(installed, api.url)
        assert "Session启动 — Leader简报" in result.stdout
        assert "TASK-WALL-CONTENT" in result.stdout
        assert E06 not in result.stdout
        assert "/api/teams" in api.paths() and "/api/notices/pending" in api.paths()


def test_custom_claude_config_directory_is_used_for_orphan_detection(installed, tmp_path):
    home, _config, chain = installed
    config = tmp_path / "separate-claude-config"
    config.mkdir()
    _settings(config, chain, disabled=True)
    _plugin(config)
    # Default config is an enabled plugin, so only CLAUDE_CONFIG_DIR can establish the orphan.
    _plugin(home / ".claude")
    with FakeApi() as api:
        _assert_orphan(_run((home, config, chain), api.url))
        assert api.requests == []


def test_in_progress_install_keeps_e02_priority(installed):
    home, _config, _chain = installed
    data_dir(home).mkdir(parents=True, exist_ok=True)
    (data_dir(home) / "install-state.json").write_text(json.dumps({
        "phase": "installing", "plugin_version": "1.15.2", "attempt": 2, "started_at": time.time(),
    }))
    result = _run(installed, "http://127.0.0.1:9")
    assert "正在安装依赖（第 2 次）" in output(result.stdout)["systemMessage"]
    assert E06 not in result.stdout


def test_uninstall_preview_recognizes_delivery_support_but_warns_about_foreign_files(installed):
    home, config, chain = installed
    (chain / "foreign.py").write_text("# not ours\n")
    before = (config / "settings.json").read_bytes()
    result = subprocess.run([sys.executable, str(chain / "uninstall_main_chain.py")],
        text=True, capture_output=True, env=hook_env(home), cwd=home, timeout=30)
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    files = {item["name"]: item for item in preview["files"]}
    assert files["hook_delivery.py"]["unrecognized"] is False
    assert files["foreign.py"]["unrecognized"] is True
    assert "hook_delivery.py" not in "\n".join(preview["warnings"])
    assert (config / "settings.json").read_bytes() == before


def test_a_stale_disabled_key_does_not_outvote_an_enabled_installed_plugin(installed):
    """Another marketplace's disabled key left in settings; this marketplace's copy is installed
    and enabled. The chain has an owner: briefing and ledger notes stay, no E06."""
    home, config, chain = installed
    _plugin(config)
    settings = json.loads((config / "settings.json").read_text(encoding="utf-8"))
    settings["enabledPlugins"] = {"ai-team-os@old-market": False, PLUGIN: True}
    (config / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    with FakeApi() as api:
        _routes(api, home)
        api.pending = [{"language": "zh", "model_text": "LEDGER-MCP-NOTE"}]
        start = _run(installed, api.url)
        assert "Session启动 — Leader简报" in start.stdout and E06 not in start.stdout
        first_prompt = _run(installed, api.url, prompt=True)
        assert "LEDGER-MCP-NOTE" in first_prompt.stdout and E06 not in first_prompt.stdout
    assert not any(r.get("catalog_id") == "orphan_main_chain" for r in local_records(home))


@pytest.mark.parametrize("enabled", [
    {"ai-team-os@old-market": True, PLUGIN: False},
    {PLUGIN: False},
], ids=["only-installed-copy-disabled", "single-key-disabled"])
def test_the_installed_copy_disabled_is_still_an_orphan(installed, enabled):
    home, config, chain = installed
    _plugin(config)  # only PLUGIN is installed; an enabled key for an absent copy owns nothing
    settings = json.loads((config / "settings.json").read_text(encoding="utf-8"))
    settings["enabledPlugins"] = enabled
    (config / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    _assert_orphan(_run(installed, "http://127.0.0.1:9"))
