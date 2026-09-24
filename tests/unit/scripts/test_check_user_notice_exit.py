"""I22: user-visible hook lines leave through user_notice.py only.

Runs the real check against the real tree, then against copies of it with one
violation planted each, so every rule is shown to go red (the reverse check).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_user_notice_exit.py"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), str(root)], capture_output=True, text=True, timeout=60)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """The hook directories and installer of this checkout, copied."""
    copy = tmp_path / "repo"
    for relative in ("plugin/hooks", "src/aiteam/hooks"):
        shutil.copytree(ROOT / relative, copy / relative, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "install.py", copy / "install.py")
    return copy


def test_current_tree_is_clean():
    result = _run(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("[OK] I22:")


def test_invariants_script_runs_i22():
    text = (ROOT / "scripts" / "check_invariants.sh").read_text(encoding="utf-8")
    assert "# ── I22:" in text
    assert "scripts/check_user_notice_exit.py" in text


@pytest.mark.parametrize(
    ("directory", "body"),
    [
        pytest.param("plugin/hooks", 'print(json.dumps({"systemMessage": "hi"}))\n', id="plugin-systemMessage"),
        pytest.param("src/aiteam/hooks", 'LINE = "请重启 Claude Code"\n', id="src-restart-wording"),
        pytest.param("plugin/hooks", 'HINT = "Tell Claude \\"restart\\""\n', id="plugin-tell-claude"),
        pytest.param("plugin/hooks", 'HINT = "run /os-doctor"\n', id="plugin-command-name"),
    ],
)
def test_a_hook_that_bypasses_emit_goes_red(tree: Path, directory: str, body: str):
    probe = tree / directory / "probe_bypass.py"
    probe.write_text("import json\n" + body, encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "probe_bypass.py" in result.stdout


def test_the_exit_module_itself_may_say_it(tree: Path):
    """user_notice.py is the one place these literals belong."""
    result = _run(tree)
    assert result.returncode == 0, result.stdout
    assert "systemMessage" in (tree / "plugin/hooks/user_notice.py").read_text(encoding="utf-8")


def test_source_install_must_copy_the_notice_module(tree: Path):
    installer = tree / "install.py"
    text = installer.read_text(encoding="utf-8")
    assert '    "user_notice.py",' in text
    installer.write_text(text.replace('    "user_notice.py",', "", 1), encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "user_notice.py" in result.stdout and "HOOK_SUPPORT_MODULES" in result.stdout


def test_source_install_really_copies_it(tmp_path, monkeypatch):
    """The declared support module lands next to the hooks that load it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("install_for_i22", ROOT / "install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    installer.copy_hook_scripts(ROOT)
    runtime = home / ".claude" / "hooks" / "ai-team-os"
    assert (runtime / "user_notice.py").read_bytes() == (ROOT / "plugin/hooks/user_notice.py").read_bytes()
    for name in installer.HOOK_SCRIPTS:
        assert (runtime / name).exists()
