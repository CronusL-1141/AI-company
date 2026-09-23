"""README rule-count claims are pinned to the served rule tables.

scripts/check_readme_numbers.sh item 7: every "N rules" / "N 条规则" claim in the
bilingual README equals the number of rules GET /api/system/rules serves. The
READMEs said "48+" against a measured 38 with nothing checking it.

Runs the real script against a scratch copy of the files it reads, so a drifted
README can be planted without touching the repo.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aiteam.api.routes.system import _ADVISORY_RULES, _AUTOMATED_RULES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RULES = len(_AUTOMATED_RULES) + len(_ADVISORY_RULES)


@pytest.fixture()
def scratch_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in (
        "scripts/check_readme_numbers.sh",
        "scripts/check_invariants.sh",
        "src/aiteam/__init__.py",
        "dashboard/src/App.tsx",
        "plugin/.claude-plugin/plugin.json",
        "README.md",
        "README.zh-CN.md",
    ):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, root / rel)
    # The script imports the package from ./src and greps the tool modules.
    shutil.rmtree(root / "src")
    (root / "src").symlink_to(REPO_ROOT / "src", target_is_directory=True)
    return root


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(root / "scripts" / "check_readme_numbers.sh")],
        capture_output=True, text=True, cwd=root, timeout=120,
    )


def _plant(root: Path, name: str, line: str) -> None:
    path = root / name
    path.write_text(path.read_text(encoding="utf-8") + f"\n{line}\n", encoding="utf-8")


def test_real_readmes_match_the_rule_tables(scratch_repo):
    proc = _run(scratch_repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"规则 {RULES}" in proc.stdout


@pytest.mark.parametrize(
    ("name", "line"),
    [
        pytest.param("README.md", f"- Rule system: {RULES + 10}+ rules", id="en-inflated-plus"),
        pytest.param("README.md", f"| Rules | 4-layer defense ({RULES - 1} rules) |", id="en-stale"),
        pytest.param("README.md", f"- Rule system: {RULES}+ rules", id="en-plus-on-exact"),
        pytest.param("README.zh-CN.md", f"- 规则体系：{RULES + 10}+ 条规则", id="zh-inflated-plus"),
        pytest.param("README.zh-CN.md", f"| 规则 | 四层防线（{RULES - 1} 条规则）|", id="zh-stale"),
    ],
)
def test_drifted_rule_count_fails(scratch_repo, name, line):
    _plant(scratch_repo, name, line)
    proc = _run(scratch_repo)
    assert proc.returncode == 1, proc.stdout
    assert "规则数声明" in proc.stdout


@pytest.mark.parametrize(
    ("name", "line"),
    [
        pytest.param("README.md", "- Bootstrap compression (23 to 5 core rules)", id="en-top5"),
        pytest.param("README.zh-CN.md", "- 启动简报压缩（23 → 5 条核心规则）", id="zh-top5"),
    ],
)
def test_top5_mentions_are_not_rule_count_claims(scratch_repo, name, line):
    _plant(scratch_repo, name, line)
    proc = _run(scratch_repo)
    assert proc.returncode == 0, proc.stdout
