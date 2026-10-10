"""I27 (scripts/check_owner_title.py) keeps the internal owner title off src/ and plugin/.

Each case builds a scratch git repository holding a copy of the checker and a few
small files under src/, plugin/ and the out-of-scope directories, injects one
change and runs the checker there, so the real tree is never edited. Git runs with
a scratch HOME so a global ignore file cannot hide an injected file. The title is
built from code points here too, so this file carries neither it nor its escape text.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = "scripts/check_owner_title.py"
TITLE = "".join(map(chr, (0x7F14, 0x9020, 0x8005)))


def _escaped(upper: bool = False) -> str:
    """The title as \\uXXXX escape text, the way JSON dumps and JS bundles spell it."""
    text = "".join(f"\\u{ord(c):04x}" for c in TITLE)
    return text.upper().replace("\\U", "\\u") if upper else text


BASELINE = {
    "src/aiteam/sample.py": '"""Sample module."""\n\nGREETING = "用户"\n',
    "plugin/hooks/sample.py": "# sample hook\nprint('ok')\n",
    "plugin/config.json": '{"note": "\\u7528\\u6237"}\n',
    "tests/unit/test_sample.py": "def test_ok():\n    assert True\n",
    "scripts/tool.py": "print('tool')\n",
    "docs/notes.md": "# notes\n",
    ".gitignore": "__pycache__/\n*.pyc\n",
}


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), GIT_CONFIG_NOSYSTEM="1")
    return env


def _tree(tmp_path: Path) -> Path:
    tree = tmp_path / "repo"
    for name, text in BASELINE.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (tree / CHECKER).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / CHECKER, tree / CHECKER)
    env = _env(tmp_path)
    subprocess.run(["git", "init", "-q", str(tree)], check=True, capture_output=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=tree, check=True, capture_output=True, env=env)
    return tree


def _check(tree: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, CHECKER], cwd=tree, capture_output=True,
                          text=True, timeout=60, env=_env(tree.parent))


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(old + text, encoding="utf-8")


def test_clean_tree_passes(tmp_path):
    result = _check(_tree(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "src/ 1 个文件" in result.stdout and "plugin/ 2 个文件" in result.stdout, result.stdout


@pytest.mark.parametrize("path, text, where", [
    ("src/aiteam/sample.py", f"# 2026-10-10 {TITLE} 裁定\n", "src/aiteam/sample.py:4"),
    ("src/aiteam/new_module.py", f"x = 1\ny = '{TITLE}'\n", "src/aiteam/new_module.py:2"),
    ("plugin/hooks/sample.py", f"print('{TITLE}')\n", "plugin/hooks/sample.py:3"),
    ("plugin/dashboard-dist/assets/index.js", f'const a="x",b="{TITLE}";\n',
     "plugin/dashboard-dist/assets/index.js:1"),
    ("plugin/config.json", f'{{"who": "{_escaped()}"}}\n', "plugin/config.json:2"),
    ("src/aiteam/escaped.py", f'\n\nWHO = "{_escaped(upper=True)}"\n', "src/aiteam/escaped.py:3"),
], ids=["tracked-src", "untracked-src", "tracked-plugin", "one-line-bundle",
        "json-escape", "python-escape-upper-hex"])
def test_title_in_scope_fails_with_file_and_line(tmp_path, path, text, where):
    tree = _tree(tmp_path)
    _append(tree / path, text)

    result = _check(tree)

    assert result.returncode == 1, result.stdout
    assert f"{where}:" in result.stdout, result.stdout
    assert "用户" in result.stdout, result.stdout


def test_every_hit_is_reported(tmp_path):
    tree = _tree(tmp_path)
    _append(tree / "src/aiteam/sample.py", f"A = '{TITLE}'\nB = '{TITLE}'\n")
    _append(tree / "plugin/hooks/sample.py", f"# {TITLE}\n")

    result = _check(tree)

    assert result.returncode == 1
    for where in ("src/aiteam/sample.py:4:", "src/aiteam/sample.py:5:",
                  "plugin/hooks/sample.py:3:"):
        assert where in result.stdout, result.stdout
    assert "共 3 处" in result.stdout, result.stdout


@pytest.mark.parametrize("path", [
    "tests/unit/test_sample.py",
    "scripts/tool.py",
    "docs/notes.md",
    "CLAUDE.md",
    "src/aiteam/__pycache__/sample.cpython-312.pyc",
], ids=["tests", "scripts", "docs", "top-level", "ignored-under-src"])
def test_title_out_of_scope_passes(tmp_path, path):
    tree = _tree(tmp_path)
    _append(tree / path, f"# {TITLE}\n")

    result = _check(tree)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("gone", ["src", "plugin"])
def test_empty_scope_fails(tmp_path, gone):
    """No scanned file is no data, not zero hits: a scope that lost its files must not pass."""
    tree = _tree(tmp_path)
    shutil.rmtree(tree / gone)

    result = _check(tree)

    assert result.returncode == 1, result.stdout
    assert f"{gone}/ 下没有可扫描的文件" in result.stdout, result.stdout


def test_checker_and_this_file_carry_neither_spelling():
    """The checker would hit itself if scripts/ ever came into scope; so would this file."""
    spellings = (TITLE, _escaped(), _escaped(upper=True))
    for path in (ROOT / CHECKER, Path(__file__)):
        data = path.read_bytes()
        assert not [s for s in spellings if s.encode("utf-8") in data], path
