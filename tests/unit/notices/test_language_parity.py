"""The hook-side language resolver matches the API one (docs/user-notice-design.md §4.5).

Local lines (blocks, "the API is down") are rendered by the hook without the
API, so it resolves the language itself from the same files. Both run here
against one isolated HOME for every combination of inputs.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
from pathlib import Path

import pytest

from aiteam.api import language as api_language

ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "src" / "aiteam" / "hooks" / "user_notice.py"

DASHBOARD = ["zh", "en", "follow", None]
CC_SETTING = ["local", "project", "user", None]
HOSTS = ["cc", "codex"]
SYSTEM = ["zh_CN.UTF-8", "en_US.UTF-8"]
CASES = list(itertools.product(DASHBOARD, CC_SETTING, HOSTS, SYSTEM))
IDS = [f"dash-{d}-cc-{c}-{h}-{s[:2]}" for d, c, h, s in CASES]


def _hook():
    spec = importlib.util.spec_from_file_location("user_notice_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("dashboard", "cc_setting", "host", "system"), CASES, ids=IDS)
def test_same_answer_for_the_same_inputs(isolated_home, tmp_path, monkeypatch, dashboard, cc_setting, host, system):
    from aiteam.api.routes import settings

    data_dir = isolated_home / ".claude" / "data" / "ai-team-os"
    data_dir.mkdir(parents=True)
    config = data_dir / "wake_config.json"
    monkeypatch.setattr(settings, "_CONFIG_PATH", config)
    if dashboard is not None:
        config.write_text(json.dumps({"language_mode": dashboard}))
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    # The CC setting disagrees with the system language, so a wrong source shows.
    wanted = "Chinese" if system.startswith("en") else "English"
    targets = {
        "local": project / ".claude" / "settings.local.json",
        "project": project / ".claude" / "settings.json",
        "user": isolated_home / ".claude" / "settings.json",
    }
    if cc_setting is not None:
        targets[cc_setting].parent.mkdir(parents=True, exist_ok=True)
        targets[cc_setting].write_text(json.dumps({"language": wanted}))
    monkeypatch.setenv("LANG", system)
    monkeypatch.setattr(api_language.sys, "platform", "linux")
    hook = _hook()
    monkeypatch.setattr(hook.sys, "platform", "linux")

    expected = api_language._resolve_language(str(project), host, None)["effective"]
    assert hook.resolve_language_local(host, str(project)) == expected
