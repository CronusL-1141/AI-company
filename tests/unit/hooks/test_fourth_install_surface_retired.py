"""The fourth hook registration surface stays retired.

``src/aiteam/hooks/install.py`` + the ``aiteam hooks`` CLI wrote a *project-level*
``.claude/settings.local.json`` registering only ``send_event.py`` across a stale
7-event / old-matcher surface. Running alongside the global chain
(``install.py`` -> ``~/.claude/settings.json``, 11 events / 17 entries) it made
every hook event fire ``send_event`` twice, and it clobbered the whole ``hooks``
key of whatever settings.local.json it found.

Batch 5 pinned the registration surface to a three-way lockstep
(install.py <-> hooks.json <-> README, machine-checked as I8). This file keeps
the retired fourth surface from creeping back.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_hooks_install_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("aiteam.hooks.install")


def test_hooks_install_file_is_gone():
    assert not (_REPO_ROOT / "src" / "aiteam" / "hooks" / "install.py").exists()


def test_hooks_cli_command_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("aiteam.cli.commands.hooks_cmd")


def test_cli_no_longer_exposes_hooks_group():
    """`aiteam hooks ...` must not resolve to a command group any more."""
    result = subprocess.run(
        [sys.executable, "-m", "aiteam.cli.app", "--help"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_REPO_ROOT / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert "hooks" not in result.stdout


def test_no_source_references_the_retired_surface():
    """Nothing in the shipped tree may still point users at the old CLI."""
    haystacks = [
        _REPO_ROOT / "src",
        _REPO_ROOT / "plugin" / "commands",
        _REPO_ROOT / "plugin" / "hooks",
        _REPO_ROOT / "scripts",
    ]
    needles = ("aiteam.hooks.install", "aiteam hooks install", "hooks install .")
    offenders: list[str] = []
    for root in haystacks:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md", ".json", ".sh"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in needles:
                if needle in text:
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}: {needle}")
    assert offenders == [], offenders
