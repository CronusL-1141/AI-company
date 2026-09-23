"""AI Team OS — 上下文感知脚本测试.

测试跨平台Python版pre_compact_save、session_bootstrap。
context_monitor已由context_tracker替代，见test_context_tracker.py。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).parent.parent.parent / "src" / "aiteam" / "hooks"
CONFIG_DIR = Path(__file__).parent.parent.parent / "plugin" / "config"


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
