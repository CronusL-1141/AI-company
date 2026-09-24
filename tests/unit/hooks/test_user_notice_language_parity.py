"""Hook-local language resolution equals the API's (aiteam.api.language).

Lines the hook renders without the API (blocks, "service down") must come out
in the same language the API would pick for the same machine state. Also pins
the composition production uses: the hook sends its system language as
``fallback_language`` and the API resolves with it.
"""

from __future__ import annotations

import itertools
import json
import plistlib
import sys
from pathlib import Path

import pytest

from aiteam.api import language as api_language
from aiteam.api.routes import settings as settings_route

DASHBOARD = ("zh", "en", "follow", None)
CC_SETTING = ("local", "project", "user", None)
HOSTS = ("cc", "codex")
SYSTEM = ("zh_CN.UTF-8", "en_US.UTF-8")


@pytest.fixture()
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".claude").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    module = sys.modules["user_notice"]
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    for key in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(module, "STATE_DIR_OVERRIDE", str(data))
    monkeypatch.setattr(settings_route, "_CONFIG_PATH", data / "wake_config.json")
    return module, home, project, data


def _arrange(home: Path, project: Path, data: Path, dashboard, cc_setting, cc_value: str) -> None:
    if dashboard is not None:
        (data / "wake_config.json").write_text(json.dumps({"language_mode": dashboard}), encoding="utf-8")
    files = {
        "local": project / ".claude" / "settings.local.json",
        "project": project / ".claude" / "settings.json",
        "user": home / ".claude" / "settings.json",
    }
    if cc_setting is not None:
        files[cc_setting].write_text(json.dumps({"language": cc_value}), encoding="utf-8")


CASES = [
    pytest.param(d, c, h, s, id=f"dash-{d}-cc-{c}-{h}-{s[:2]}")
    for d, c, h, s in itertools.product(DASHBOARD, CC_SETTING, HOSTS, SYSTEM)
]


@pytest.mark.parametrize(("dashboard", "cc_setting", "host", "system"), CASES)
def test_same_answer_as_the_api(world, monkeypatch, dashboard, cc_setting, host, system):
    module, home, project, data = world
    # The CC setting disagrees with the system language, so the order is visible.
    cc_value = "English" if system.startswith("zh") else "中文"
    _arrange(home, project, data, dashboard, cc_setting, cc_value)
    monkeypatch.setenv("LANG", system)
    monkeypatch.setattr(api_language.sys, "platform", "linux")
    monkeypatch.setattr(module.sys, "platform", "linux")
    local = module.resolve_language_local(host, str(project))
    assert local == api_language._resolve_language(str(project), host, None)["effective"]
    # Production: the hook sends its system language, the API resolves with it.
    sent = module.system_language()
    assert local == api_language._resolve_language(str(project), host, sent)["effective"]


@pytest.mark.skipif(sys.platform != "darwin", reason="AppleLanguages is read on macOS only")
@pytest.mark.parametrize("apple", [["zh-Hans-CN", "en"], ["en-US", "zh-Hans"]], ids=["zh-first", "en-first"])
def test_macos_preference_list_wins_over_locale_variables(world, monkeypatch, apple):
    module, home, project, _ = world
    preferences = home / "Library" / "Preferences" / ".GlobalPreferences.plist"
    preferences.parent.mkdir(parents=True)
    preferences.write_bytes(plistlib.dumps({"AppleLanguages": apple}))
    monkeypatch.setenv("LANG", "en_US.UTF-8" if apple[0].startswith("zh") else "zh_CN.UTF-8")
    expected = "zh" if apple[0].startswith("zh") else "en"
    for host in HOSTS:
        assert module.resolve_language_local(host, str(project)) == expected
        assert api_language._resolve_language(str(project), host, None)["effective"] == expected


def test_claude_config_dir_is_honoured(world, monkeypatch, tmp_path):
    module, _home, project, _ = world
    config = tmp_path / "custom-config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"language": "中文"}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setattr(module.sys, "platform", "linux")
    assert module.resolve_language_local("cc", str(project)) == "zh"
    assert module.resolve_language_local("codex", str(project)) == "en", "Codex never reads CC settings"
