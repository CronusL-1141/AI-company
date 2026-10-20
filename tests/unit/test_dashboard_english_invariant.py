"""Exercise I29's executable checker against regressions in scratch sources."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def scratch(tmp_path: Path) -> Path:
    for relative in ("scripts/check_dashboard_english.py", "dashboard/src/i18n/en.ts"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return tmp_path


def run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "scripts/check_dashboard_english.py")],
        capture_output=True, text=True, timeout=10,
    )


def test_current_copy_passes(scratch: Path):
    result = run(scratch)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "无例外" in result.stdout


@pytest.mark.parametrize("dash", ["\u2014", r"\u2014", r"\u{2014}"], ids=["raw", "escape", "braced"])
def test_reintroduced_punctuation_fails(scratch: Path, dash: str):
    source = scratch / "dashboard/src/i18n/en.ts"
    with source.open("a", encoding="utf-8") as stream:
        stream.write(f"\nconst regression = 'Do this {dash} then that';\n")
    result = run(scratch)
    assert result.returncode == 1
    assert "en.ts:" in result.stdout
    assert "regression" in result.stdout


def test_missing_data_marker_has_no_exception(scratch: Path):
    # The empty-coverage marker once kept an em dash by exact-line exemption.
    source = scratch / "dashboard/src/i18n/en.ts"
    text = source.read_text(encoding="utf-8")
    marker = "Page evidence coverage: - (no saved executions)"
    assert marker in text
    source.write_text(text.replace(marker, "Page evidence coverage: \u2014 (no saved executions)"), encoding="utf-8")
    result = run(scratch)
    assert result.returncode == 1
    assert "pageCoverageEmpty" in result.stdout


@pytest.mark.parametrize("missing", [False, True], ids=["empty", "missing"])
def test_unavailable_source_fails(scratch: Path, missing: bool):
    source = scratch / "dashboard/src/i18n/en.ts"
    if missing:
        source.unlink()
    else:
        source.write_text("")
    assert run(scratch).returncode == 1
