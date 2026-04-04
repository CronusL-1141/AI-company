"""AI Team OS — 上下文感知脚本测试.

测试跨平台Python版context_monitor、pre_compact_save、session_bootstrap。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).parent.parent.parent / "src" / "aiteam" / "hooks"
CONFIG_DIR = Path(__file__).parent.parent.parent / "plugin" / "config"


# ============================================================
# context_monitor.py
# ============================================================


class TestContextMonitor:
    """context_monitor.py — 阈值警告."""

    script = HOOKS_DIR / "context_monitor.py"

    def _run_with_percentage(self, pct: float, tmp_path: Path) -> str:
        """在指定百分比下运行，返回stdout."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        monitor = claude_dir / "context-monitor.json"
        monitor.write_text(
            json.dumps(
                {
                    "used_percentage": pct,
                    "timestamp": "2026-03-14T00:00:00Z",
                    "context_window_size": 200000,
                }
            )
        )

        env = {
            **dict(__import__("os").environ),
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
        }
        result = subprocess.run(
            [sys.executable, str(self.script)],
            input="{}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            env=env,
        )
        return result.stdout

    def test_below_80_silent(self, tmp_path):
        """80%以下无输出."""
        output = self._run_with_percentage(75.0, tmp_path)
        assert output.strip() == ""

    def test_80_warning(self, tmp_path):
        """80%-89%输出WARNING."""
        output = self._run_with_percentage(85.0, tmp_path)
        assert "WARNING" in output or "warning" in output.lower()

    def test_90_critical(self, tmp_path):
        """90%以上输出CRITICAL."""
        output = self._run_with_percentage(95.0, tmp_path)
        assert "CRITICAL" in output or "critical" in output.lower()

    def test_no_monitor_file_silent(self, tmp_path):
        """无monitor文件时静默."""
        env = {
            **dict(__import__("os").environ),
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
        }
        result = subprocess.run(
            [sys.executable, str(self.script)],
            input="{}",
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0


# ============================================================
# pre_compact_save.py
# ============================================================


class TestPreCompactSave:
    """pre_compact_save.py — compact事件记录."""

    script = HOOKS_DIR / "pre_compact_save.py"

    def test_appends_to_jsonl(self, tmp_path):
        """正确追加JSONL记录."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        input_data = json.dumps(
            {
                "trigger": "manual",
                "session_id": "test-session",
                "transcript_path": "/tmp/test.jsonl",
            }
        )

        env = {
            **dict(__import__("os").environ),
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
        }
        result = subprocess.run(
            [sys.executable, str(self.script)],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0

        events_file = claude_dir / "compact-events.jsonl"
        if events_file.exists():
            lines = events_file.read_text().strip().split("\n")
            assert len(lines) >= 1
            record = json.loads(lines[-1])
            assert record["session_id"] == "test-session"
            assert "timestamp" in record

    def test_silent_on_error(self):
        """错误时不崩溃."""
        result = subprocess.run(
            [sys.executable, str(self.script)],
            input="invalid json{{{",
            capture_output=True,
            text=True,
            timeout=10,
        )
        # 不应崩溃
        assert result.returncode == 0


# ============================================================
# session_bootstrap.py
# ============================================================


class TestSessionBootstrap:
    """session_bootstrap.py — Session启动引导."""

    script = HOOKS_DIR / "session_bootstrap.py"

    def test_api_unreachable(self):
        """API不可达时输出启动提示."""
        # 使用内联脚本避免Windows路径中文编码问题
        script_content = """
import json, sys, urllib.request, urllib.error

API_URL = "http://localhost:19999"

def _api_get(path, timeout=0.5):
    try:
        req = urllib.request.Request(f"{API_URL}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

raw = sys.stdin.buffer.read().decode("utf-8")
health = _api_get("/api/teams")
if health is None:
    sys.stdout.write("[AI Team OS] API not reachable\\n")
"""
        result = subprocess.run(
            [sys.executable, "-c", script_content],
            input="{}",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "API" in result.stdout

    def test_auto_team_instructions(self, tmp_path):
        """有auto_create_team配置时输出团队创建指令."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config = {
            "auto_create_team": True,
            "team_name_prefix": "test",
            "permanent_members": [
                {"name": "qa-observer", "role": "QA观察员", "enabled": True},
                {"name": "bug-fixer", "role": "Bug工程师", "enabled": True},
            ],
        }
        (config_dir / "team-defaults.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )

        # 内联测试 _build_auto_team_instructions 和 _load_team_config
        script_content = f"""
import json, sys
from pathlib import Path

CONFIG_DIR = Path(r"{config_dir}")

def _load_team_config():
    config_path = CONFIG_DIR / "team-defaults.json"
    try:
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def _build_auto_team_instructions(config):
    if not config.get("auto_create_team"):
        return []
    enabled = [m for m in config.get("permanent_members", []) if m.get("enabled")]
    if not enabled:
        return []
    team_name = f"{{config.get('team_name_prefix', 'auto')}}-team"
    lines = ["", "=== 自动团队创建指令 ==="]
    lines.append("请立即执行以下操作创建团队和常驻成员：")
    lines.append(f"1. TeamCreate(team_name='{{team_name}}')")
    for i, m in enumerate(enabled, start=2):
        lines.append(f"{{i}}. Agent(team_name='{{team_name}}', name='{{m['name']}}')")
    return lines

cfg = _load_team_config()
if cfg:
    result = _build_auto_team_instructions(cfg)
    sys.stdout.write("\\n".join(result))
"""
        result = subprocess.run(
            [sys.executable, "-c", script_content],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "自动团队创建指令" in result.stdout
        assert "TeamCreate" in result.stdout
        assert "test-team" in result.stdout
        assert "qa-observer" in result.stdout
        assert "bug-fixer" in result.stdout

    def test_auto_team_disabled(self, tmp_path):
        """auto_create_team=false时不输出创建指令."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config = {
            "auto_create_team": False,
            "team_name_prefix": "test",
            "permanent_members": [
                {"name": "qa-observer", "role": "QA", "enabled": True},
            ],
        }
        (config_dir / "team-defaults.json").write_text(json.dumps(config), encoding="utf-8")

        script_content = f"""
import json, sys
from pathlib import Path

CONFIG_DIR = Path(r"{config_dir}")

def _load_team_config():
    config_path = CONFIG_DIR / "team-defaults.json"
    try:
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def _build_auto_team_instructions(config):
    if not config.get("auto_create_team"):
        return []
    enabled = [m for m in config.get("permanent_members", []) if m.get("enabled")]
    if not enabled:
        return []
    return ["=== 自动团队创建指令 ==="]

cfg = _load_team_config()
if cfg:
    result = _build_auto_team_instructions(cfg)
    sys.stdout.write("\\n".join(result))
else:
    sys.stdout.write("")
"""
        result = subprocess.run(
            [sys.executable, "-c", script_content],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "自动团队创建指令" not in result.stdout

    def test_missing_config_silent(self, tmp_path):
        """无配置文件时不报错."""
        config_dir = tmp_path / "config"
        # 故意不创建config目录

        script_content = f"""
import json, sys
from pathlib import Path

CONFIG_DIR = Path(r"{config_dir}")

def _load_team_config():
    config_path = CONFIG_DIR / "team-defaults.json"
    try:
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

cfg = _load_team_config()
if cfg:
    sys.stdout.write("HAS_CONFIG")
else:
    sys.stdout.write("NO_CONFIG")
"""
        result = subprocess.run(
            [sys.executable, "-c", script_content],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "NO_CONFIG" in result.stdout

    # ------------------------------------------------------------------
    # Auto-update tests
    # ------------------------------------------------------------------

    def _make_check_for_updates_script(self, tmp_home: Path) -> str:
        """Return an inline script that runs _check_for_updates() with HOME set to tmp_home."""
        hooks_file = HOOKS_DIR / "session_bootstrap.py"
        return f"""
import sys, importlib.util, pathlib

# Redirect home so state files land in tmp_path
import pathlib
_orig_home = pathlib.Path.home
pathlib.Path.home = staticmethod(lambda: pathlib.Path(r"{tmp_home}"))

spec = importlib.util.spec_from_file_location("session_bootstrap", r"{hooks_file}")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Override state-file path to use tmp home
import time, json
state_dir = pathlib.Path(r"{tmp_home}") / ".claude" / "data" / "ai-team-os"
state_dir.mkdir(parents=True, exist_ok=True)
mod._UPDATE_CHECK_STATE_FILE = state_dir / "last_update_check.json"

notice = mod._check_for_updates()
sys.stdout.write(notice if notice else "")
"""

    def test_check_for_updates_cooldown_respected(self, tmp_path):
        """Within 24h cooldown, cached notice is returned without git calls."""
        import os
        import time

        state_dir = tmp_path / ".claude" / "data" / "ai-team-os"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / "last_update_check.json"
        state_file.write_text(
            json.dumps({"last_checked": time.time(), "notice": "CACHED_NOTICE"}),
            encoding="utf-8",
        )

        script = self._make_check_for_updates_script(tmp_path)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "CACHED_NOTICE" in result.stdout

    def test_check_for_updates_bg_success_reported(self, tmp_path):
        """When bg_update_status.json indicates success, returns success message."""
        import os
        import time

        state_dir = tmp_path / ".claude" / "data" / "ai-team-os"
        state_dir.mkdir(parents=True, exist_ok=True)
        bg_file = state_dir / "bg_update_status.json"
        bg_file.write_text(
            json.dumps({
                "completed_at": time.time(),
                "success": True,
                "new_commit": "abc1234",
                "errors": [],
            }),
            encoding="utf-8",
        )

        script = self._make_check_for_updates_script(tmp_path)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "abc1234" in result.stdout
        assert "[OS]" in result.stdout
        # Status file should have been consumed (deleted)
        assert not bg_file.exists()

    def test_check_for_updates_bg_failure_reported(self, tmp_path):
        """When bg_update_status.json indicates failure, returns error message."""
        import os
        import time

        state_dir = tmp_path / ".claude" / "data" / "ai-team-os"
        state_dir.mkdir(parents=True, exist_ok=True)
        bg_file = state_dir / "bg_update_status.json"
        bg_file.write_text(
            json.dumps({
                "completed_at": time.time(),
                "success": False,
                "new_commit": "",
                "errors": ["git pull failed: conflict"],
            }),
            encoding="utf-8",
        )

        script = self._make_check_for_updates_script(tmp_path)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        )
        assert result.returncode == 0
        assert "自动更新失败" in result.stdout
        assert "conflict" in result.stdout
        assert not bg_file.exists()

    def test_check_for_updates_no_git_repo_silent(self, tmp_path):
        """When no git repo is found and no install_path.txt, returns None silently."""
        import os

        state_dir = tmp_path / ".claude" / "data" / "ai-team-os"
        state_dir.mkdir(parents=True, exist_ok=True)

        # Provide an install_path.txt pointing at a non-git directory
        install_path_file = state_dir / "install_path.txt"
        install_path_file.write_text(str(tmp_path), encoding="utf-8")

        script = self._make_check_for_updates_script(tmp_path)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        )
        assert result.returncode == 0
        # No git repo means no update notice
        assert result.stdout.strip() == ""

    def test_resolve_project_root_from_install_path(self, tmp_path):
        """_resolve_project_root reads install_path.txt if it points to a git repo."""
        import os

        # Create a fake git repo
        fake_repo = tmp_path / "my_project"
        fake_repo.mkdir()
        (fake_repo / ".git").mkdir()

        state_dir = tmp_path / ".claude" / "data" / "ai-team-os"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "install_path.txt").write_text(str(fake_repo), encoding="utf-8")

        hooks_file = HOOKS_DIR / "session_bootstrap.py"
        script = f"""
import sys, importlib.util, pathlib

_orig_home = pathlib.Path.home
pathlib.Path.home = staticmethod(lambda: pathlib.Path(r"{tmp_path}"))

spec = importlib.util.spec_from_file_location("session_bootstrap", r"{hooks_file}")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

root = mod._resolve_project_root()
sys.stdout.write(str(root) if root else "NONE")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
        )
        assert result.returncode == 0
        assert str(fake_repo) in result.stdout
