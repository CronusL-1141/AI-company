"""I26 (scripts/check_wake_push.py) catches the writes it promises to catch.

Each case copies the checker and the real files it has registrations for into a
scratch git repository, injects one change and runs the checker there, so the real
tree is never edited.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# The checker, the scrub module, the API module and every file with a registered string.
FILES = (
    "scripts/check_wake_push.py",
    "src/aiteam/session_env.py",
    "src/aiteam/api/app.py",
    "src/aiteam/mcp/_autostart.py",
    "src/aiteam/cli/commands/up_cmd.py",
    "install.py",
)


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "repo"
    for name in FILES:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, tree / name)
    subprocess.run(["git", "init", "-q", str(tree)], check=True, capture_output=True)
    return tree


def _check(tree: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "scripts/check_wake_push.py"], cwd=tree,
                          capture_output=True, text=True, timeout=60)


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_copied_tree_passes(tmp_path):
    result = _check(_tree(tmp_path))
    assert result.returncode == 0, result.stdout


def test_second_unlaunched_command_in_a_registered_scope_fails(tmp_path):
    """A registration covers one string: a second one beside it may not borrow it."""
    tree = _tree(tmp_path)
    _append(tree / "src/aiteam/mcp/_autostart.py",
            '\n_LEAK_CMD = ["python", "-m", "uvicorn", "aiteam.api.app:create_app"]\n')

    result = _check(tree)

    assert result.returncode == 1
    assert "命中 2 处" in result.stdout, result.stdout


def test_launch_without_scrubbed_env_fails(tmp_path):
    tree = _tree(tmp_path)
    _append(tree / "src/aiteam/mcp/_autostart.py",
            '\n\ndef _relaunch():\n'
            '    subprocess.Popen(["python", "-m", "uvicorn", "aiteam.api.app:create_app"])\n')

    result = _check(tree)

    assert result.returncode == 1
    assert "没有带 env=scrub_session_env" in result.stdout, result.stdout


@pytest.mark.parametrize("line", [
    "import psutil\n",
    "from aiteam.clock import utc_now\n",
    "from . import clock\n",
], ids=["third-party", "aiteam", "relative"])
def test_scrub_module_imports_the_standard_library_only(tmp_path, line):
    tree = _tree(tmp_path)
    _append(tree / "src/aiteam/session_env.py", line)

    result = _check(tree)

    assert result.returncode == 1
    assert "剔除模块只许导入标准库" in result.stdout, result.stdout
