"""/os-watcher's inline shell writes the switch where the guard reads it (docs/config-dir-compat-design.md §6 batch 4).

The command runs a one-line shell script from plugin/commands/os-watcher.md; the
Stop guard (turn_end_guard.py) reads ``arm-hint.off`` in the OS data directory.
Both are run here as Claude Code runs them, in a throwaway HOME.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMAND = ROOT / "plugin" / "commands" / "os-watcher.md"
GUARD = ROOT / "plugin" / "hooks" / "turn_end_guard.py"


def _script() -> str:
    match = re.search(r"^!`(.*)`$", COMMAND.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "os-watcher.md lost its inline shell line"
    return match.group(1)


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in ("CLAUDE_CONFIG_DIR", "AITEAM_DATA_DIR", "AITEAM_DB_PATH")}
    return {**env, "HOME": str(home), "PYTHONPATH": str(ROOT / "src"), **extra}


def _toggle(home: Path, argument: str) -> str:
    result = subprocess.run(["bash", "-c", _script()], capture_output=True, text=True, timeout=30,
                            env=_env(home, ARGUMENTS=argument))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _guard_flag(home: Path) -> str:
    code = ("import importlib.util, sys; spec = importlib.util.spec_from_file_location('g', sys.argv[1]); "
            "g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g); print(g._arm_hint_off_flag())")
    result = subprocess.run([sys.executable, "-c", code, str(GUARD)], capture_output=True, text=True,
                            timeout=30, env=_env(home))
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_switch_lands_where_the_guard_reads_it(tmp_path):
    home = tmp_path / "home"
    (home / ".aiteam").mkdir(parents=True)

    assert "已静默" in _toggle(home, "off")
    flag = home / ".aiteam" / "arm-hint.off"
    assert flag.exists() and _guard_flag(home) == str(flag)
    assert "已静默" in _toggle(home, "status")
    assert "已开启" in _toggle(home, "on") and not flag.exists()
    assert not (home / ".claude").exists()


def test_without_a_data_directory_nothing_is_created(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert "aiteam paths --locate" in _toggle(home, "off")
    assert list(home.iterdir()) == []
