"""I30's scanner (scripts/check_data_paths.py) sees the spellings people actually write.

The ratchet only means something if the scan catches every way of naming the
Claude Code config folder, the Codex home or the OS data directory outside the
path section; a spelling it misses is a false green.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("check_data_paths_under_test", ROOT / "scripts" / "check_data_paths.py")
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)

CAUGHT = [
    'CLAUDE_HOME = "~/.claude"',
    'base = home / ".claude" / "data" / "ai-team-os"',
    'Path("~/.claude").expanduser()',
    'os.environ["HOME"] + "/.claude/hooks/ai-team-os"',
    "Path.home().joinpath('.claude', 'settings.json')",
    'f"{home}/.claude/hooks"',
    'settings = Path.home() / ".claude" / "settings.json"',
    'Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")',
]
LEFT = [
    'candidate = project / ".claude" / "agents"',  # a project's own .claude folder, not the config folder
    'local = ".claude/settings.local.json"',
    'name = "claude"',
]


@pytest.mark.parametrize("line", CAUGHT, ids=[f"caught-{index}" for index in range(len(CAUGHT))])
def test_a_spelling_of_the_config_folder_is_caught(line):
    assert scanner.PY_PATTERN.search(line), line


@pytest.mark.parametrize("line", LEFT, ids=[f"left-{index}" for index in range(len(LEFT))])
def test_a_project_folder_or_a_word_is_left_alone(line):
    assert not scanner.PY_PATTERN.search(line), line
