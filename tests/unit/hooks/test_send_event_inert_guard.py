"""send_event inert-tool guard — the cost control that lets the matcher stay "*".

send_event is registered at matcher "*" on PreToolUse/PostToolUse so the OS keeps
seeing every tool call (liveness, IDLE→BUSY self-heal, activity spans). The guard
inside the hook is what keeps that affordable: tool calls the OS provably ignores
never reach the API.

The two invariants worth a regression test:
  1. Tools the OS reacts to are never dropped (Read/Edit/Write/Bash are the
     hook_translator intent tools; Agent/Workflow drive team + workflow ingest).
  2. The drop is symmetric across PreToolUse/PostToolUse — dropping only the Post
     half would strand the Pre half's activity span in status="running" forever.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
HOOK = REPO_ROOT / "plugin" / "hooks" / "send_event.py"
DEAD_API = "http://127.0.0.1:9"  # discard port → POST fails fast, proving we tried


def _run(event: str, tool_name: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "AITEAM_API_URL": DEAD_API}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run(
        [sys.executable, str(HOOK), event],
        input=json.dumps({"session_id": "t", "tool_name": tool_name}),
        capture_output=True, text=True, timeout=20, env=env,
    )


def _forwarded(result: subprocess.CompletedProcess) -> bool:
    """The hook only prints '[aiteam-hook]' after attempting the HTTP POST."""
    return "[aiteam-hook]" in result.stderr


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "Bash", "Agent", "Workflow",
                                  "Grep", "mcp__ai-team-os__task_create"])
@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
def test_signal_tools_are_always_forwarded(event, tool):
    assert _forwarded(_run(event, tool)), f"{event}/{tool} was dropped"


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
def test_inert_tools_are_dropped_symmetrically(event):
    result = _run(event, "TodoWrite")
    assert result.returncode == 0
    assert not _forwarded(result), f"{event}/TodoWrite reached the API"


def test_non_tool_events_are_never_dropped():
    """Only tool events are filtered — lifecycle events always go through."""
    assert _forwarded(_run("SessionStart", "TodoWrite"))


def test_intent_tools_stay_out_of_the_inert_set():
    """Static guard: the OS's own intent tools must never be added to the denylist."""
    sys.path.insert(0, str(REPO_ROOT / "plugin" / "hooks"))
    try:
        import importlib

        module = importlib.import_module("send_event")
        importlib.reload(module)
        forbidden = {"Read", "Edit", "Write", "Bash", "Agent", "Workflow", "Task"}
        assert not (module._INERT_TOOLS & forbidden)
        assert not any(t.startswith("mcp__") for t in module._INERT_TOOLS)
    finally:
        sys.path.pop(0)
