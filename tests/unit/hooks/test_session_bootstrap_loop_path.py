"""Real SessionStart processes resolve existing /loop assets in isolated installs."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ._notice_fakes import ROOT, FakeApi, data_dir, hook_env, output


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("layout", [
    "source-global", "source-repo", "source-stale", "plugin-direct", "plugin-adjacent", "plugin-main-chain",
    "plugin-coexisting-global",
    "plugin-custom-config", "plugin-disabled", "plugin-stale", "registry-malformed", "missing",
])
def test_real_hook_references_only_existing_loop_prompt(tmp_path, layout):
    home = tmp_path / "home"
    home.mkdir()
    plugin = tmp_path / "plugin cache" / "ai-team-os" / "1.15.0"
    config = home / ".claude"
    script = home / ".claude" / "hooks" / "ai-team-os" / "session_bootstrap.py"
    expected = None
    extra = {}

    if layout == "source-global":
        expected = _write(config / "loop.md", "user's customized maintenance prompt")
    elif layout in ("source-repo", "source-stale"):
        source = tmp_path / "source repo"
        source.mkdir()
        (source / ".git").mkdir()
        _write(data_dir(home) / "install_path.txt", str(source))
        if layout == "source-repo":
            expected = _write(source / "plugin" / "loop.md", "source maintenance prompt")
    elif layout.startswith("plugin-"):
        prompt = _write(plugin / "loop.md", "plugin maintenance prompt")
        expected = prompt
        if layout in ("plugin-direct", "plugin-adjacent"):
            script = plugin / "hooks" / "session_bootstrap.py"
            if layout == "plugin-direct":
                extra["CLAUDE_PLUGIN_ROOT"] = str(plugin)
        else:
            if layout == "plugin-custom-config":
                config = tmp_path / "alternate config"
                extra["CLAUDE_CONFIG_DIR"] = str(config)
            _write(config / "plugins" / "installed_plugins.json", json.dumps({
                "plugins": {"ai-team-os@ai-team-os": [{"installPath": str(plugin)}]},
            }))
            if layout == "plugin-coexisting-global":
                _write(home / ".claude" / "loop.md", "user's customized maintenance prompt")
            if layout == "plugin-disabled":
                _write(config / "settings.json", json.dumps({
                    "enabledPlugins": {"ai-team-os@ai-team-os": False},
                }))
                expected = None
            elif layout == "plugin-stale":
                prompt.unlink()
                expected = None
    elif layout == "registry-malformed":
        _write(config / "plugins" / "installed_plugins.json", "{broken")
        _write(config / "settings.json", "[]")

    script.parent.mkdir(parents=True, exist_ok=True)
    for name in ("session_bootstrap.py", "user_notice.py", "hook_core.py"):
        shutil.copyfile(ROOT / "plugin" / "hooks" / name, script.parent / name)
    global_prompt = home / ".claude" / "loop.md"
    before = global_prompt.read_bytes() if global_prompt.exists() else None

    with FakeApi() as api:
        api.routes.update({
            ("GET", "/api/teams"): lambda _: (200, {"data": []}),
            ("GET", "/api/projects"): lambda _: (200, {"data": []}),
            ("GET", "/api/leader-briefings"): lambda _: (200, {"items": []}),
            ("POST", "/api/context/resolve"): lambda _: (200, {"project_id": "p-loop"}),
            ("GET", "/api/projects/p-loop/task-wall/digest"): lambda _: (200, {"text": "任务墙: 暂无"}),
            ("GET", "/api/memories"): lambda _: (200, {"data": []}),
        })
        proc = subprocess.run(
            [sys.executable, str(script)], text=True, capture_output=True,
            input=json.dumps({"session_id": "loop-path", "source": "startup", "cwd": str(home)}),
            cwd=home, env=hook_env(home, api.url, **extra), timeout=15,
        )
    assert proc.returncode == 0, proc.stderr
    context = output(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "以下是本会话的 OS 工作协议" in context
    assert "=== 自动唤醒 ===" in context
    if expected is None:
        assert "每轮做什么见" not in context
        assert "当前安装未找到 /loop 维护说明。" in context
    else:
        assert expected.is_file()
        assert f"每轮做什么见 {expected.resolve()}。" in context
        assert "当前安装未找到" not in context
    assert "~/.claude/loop.md" not in context
    assert (global_prompt.read_bytes() if global_prompt.exists() else None) == before
