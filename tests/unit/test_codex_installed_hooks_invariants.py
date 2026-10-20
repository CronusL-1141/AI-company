"""I28 in the total invariant check: warn on drift, never fail the commit gate on it.

User ruling 2026-10-16 (task 00a77472): before a commit the working tree is always
ahead of the installed Codex copies, so drift and installed-only rows only warn
here; the standalone checker keeps exit code 1 (covered by
test_check_codex_installed_hooks.py) for the release lifecycle step and /os-doctor.

The I28 section of the real check_invariants.sh is cut out and run with the real
helpers and verdict, so its relation to the exit code is tested without depending
on every other invariant being green. One run of the whole script then proves the
section is wired into it. Neither mocks the reconciler.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "plugin/harness/codex/hooks"
INVARIANTS = ROOT / "scripts/check_invariants.sh"


def _i28_harness() -> str:
    """check_invariants.sh reduced to its helpers, the I28 section and the verdict."""
    text = INVARIANTS.read_text(encoding="utf-8")
    helpers_start = text.index("FAIL=0\n")
    helpers_end = text.index("\n", text.index("ok()   {")) + 1
    block = text[text.index("# ── I28:"):text.index("# ── I29:")]
    verdict = text[text.index('echo\nif [ "$FAIL" -eq 1 ]'):]
    return "set -u\n" + text[helpers_start:helpers_end] + block + verdict


def _codex_home(tmp_path: Path, state: str) -> Path:
    home = tmp_path / "codex-home"
    installed = home / "hooks/ai-team-os-observer"
    if state != "absent":
        installed.mkdir(parents=True)
        shutil.copyfile(ADAPTER / "send_event_codex.py", installed / "send_event_codex.py")
        if state == "drift":
            with (installed / "send_event_codex.py").open("ab") as stream:
                stream.write(b"\n# injected installation drift\n")
    return home


def _env(tmp_path: Path, home: Path, *, crash: bool = False) -> dict:
    # check_invariants.sh invokes python3; pin it to this test interpreter even
    # on macOS, whose /usr/bin/python3 may be too old for the repository.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    python3 = bindir / "python3"
    if crash:
        python3.write_text(
            "#!/bin/sh\n"
            'case "$1" in *check_codex_installed_hooks.py)\n'
            '  echo "Traceback (most recent call last):" >&2; exit 1;;\n'
            "esac\n"
            f'exec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        python3.chmod(0o755)
    else:
        python3.symlink_to(sys.executable)
    return dict(os.environ, CODEX_HOME=str(home), AITEAM_DB_PATH=str(tmp_path / "no-such.db"),
                PATH=str(bindir) + os.pathsep + os.environ["PATH"])


def _snapshot(tmp_path: Path) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in tmp_path.rglob("*")
            if p.is_file() and "bin" not in p.relative_to(tmp_path).parts}


@pytest.mark.parametrize("state", ["absent", "same", "drift", "crash"])
def test_i28_section_warns_on_drift_and_fails_only_when_the_check_breaks(tmp_path: Path, state: str) -> None:
    home = _codex_home(tmp_path, "same" if state == "crash" else state)
    env = _env(tmp_path, home, crash=state == "crash")
    harness = tmp_path / "i28.sh"
    harness.write_text(_i28_harness(), encoding="utf-8")
    before = _snapshot(tmp_path)
    result = subprocess.run(["bash", str(harness)], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    output = result.stdout + result.stderr
    assert result.returncode == (1 if state == "crash" else 0), output
    if state == "absent":
        assert "⚠️  [I28]" in output and "跳过字节对账" in output
        assert "✅ [I28]" not in output and "❌ [I28]" not in output
    elif state == "same":
        assert "✅ [I28]" in output and "不代表安装完整" in output
    elif state == "drift":
        assert "⚠️  [I28]" in output and "❌ [I28]" not in output
        assert "漂移 1" in output and "send_event_codex.py" in output, "the table is printed"
        assert "python3 scripts/check_codex_installed_hooks.py 严格复核" in output
        assert "结论: ✅" in output
    else:
        assert "❌ [I28]" in output and "异常退出（rc=1）" in output
        assert "结论: ❌" in output
    # The reconciliation is read-only: the fake installation is neither fixed nor completed.
    assert _snapshot(tmp_path) == before


def test_total_check_reports_drift_as_a_warning_not_a_failure(tmp_path: Path) -> None:
    home = _codex_home(tmp_path, "drift")
    env = _env(tmp_path, home)
    before = _snapshot(tmp_path)
    result = subprocess.run(["bash", str(INVARIANTS)], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=240)
    output = result.stdout + result.stderr
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if "[I28]" in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith(("✅ [", "⚠️  [", "❌ [")))
    section = "\n".join(lines[start:end])
    assert lines[start].startswith("⚠️  [I28]"), section
    assert "漂移 1" in section and "send_event_codex.py" in section
    # Other invariants may be red on a working tree; I28 must not be the reason.
    others = [line for line in lines if line.startswith("❌ [") and not line.startswith("❌ [I28]")]
    assert (result.returncode != 0) == bool(others), output
    assert _snapshot(tmp_path) == before
