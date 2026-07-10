"""Unit tests for wake_manager --bare mode optimization (_build_cmd, _cleanup_prompt_file)."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from aiteam.api.wake_manager import _build_cmd, _cleanup_prompt_file

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**kwargs) -> dict:
    return kwargs


# ---------------------------------------------------------------------------
# A. Default bare_mode=True
# ---------------------------------------------------------------------------

def test_bare_mode_default_adds_flags():
    """bare_mode defaults to True — cmd must contain --bare and --exclude flag."""
    cmd, prompt_file = _build_cmd("hello", "10", "Read,Write", _cfg())
    assert "--bare" in cmd
    assert "--exclude-dynamic-system-prompt-sections" in cmd
    assert prompt_file is None


def test_bare_mode_true_explicit():
    """bare_mode=True explicitly — same as default."""
    cmd, _ = _build_cmd("hi", "5", "Read", _cfg(bare_mode=True))
    assert "--bare" in cmd
    assert "--exclude-dynamic-system-prompt-sections" in cmd


# ---------------------------------------------------------------------------
# B. bare_mode=False escape hatch
# ---------------------------------------------------------------------------

def test_bare_mode_false_omits_flags():
    """bare_mode=False — no --bare or --exclude flags in cmd."""
    cmd, prompt_file = _build_cmd("hello", "10", "Read", _cfg(bare_mode=False))
    assert "--bare" not in cmd
    assert "--exclude-dynamic-system-prompt-sections" not in cmd
    assert prompt_file is None


def test_bare_mode_false_preserves_allowed_tools():
    """bare_mode=False — --allowedTools and --max-turns still present."""
    cmd, _ = _build_cmd("prompt", "7", "Read,Bash", _cfg(bare_mode=False))
    assert "--allowedTools" in cmd
    assert "Read,Bash" in cmd
    assert "--max-turns" in cmd
    assert "7" in cmd


# ---------------------------------------------------------------------------
# C. --mcp-config injection when .mcp.json exists
# ---------------------------------------------------------------------------

def test_bare_mode_injects_mcp_config_when_file_exists(tmp_path):
    """--mcp-config is added when .mcp.json is found at cwd."""
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text('{"mcpServers":{}}')

    cmd, _ = _build_cmd("hi", "5", "Read", _cfg(bare_mode=True, cwd=str(tmp_path)))
    assert "--mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == str(mcp_file)


def test_bare_mode_no_mcp_config_when_file_missing(tmp_path):
    """--mcp-config is NOT added when no .mcp.json exists in cwd."""
    cmd, _ = _build_cmd("hi", "5", "Read", _cfg(bare_mode=True, cwd=str(tmp_path)))
    assert "--mcp-config" not in cmd


def test_bare_mode_explicit_mcp_config_path(tmp_path):
    """Explicit mcp_config in cfg overrides auto-discovery."""
    explicit_path = str(tmp_path / "custom.mcp.json")
    cmd, _ = _build_cmd("hi", "5", "Read", _cfg(bare_mode=True, mcp_config=explicit_path))
    assert "--mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == explicit_path


def test_bare_mode_false_no_mcp_config(tmp_path):
    """When bare_mode=False, --mcp-config is never added even if .mcp.json exists."""
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text('{}')
    cmd, _ = _build_cmd("hi", "5", "Read", _cfg(bare_mode=False, cwd=str(tmp_path)))
    assert "--mcp-config" not in cmd


# ---------------------------------------------------------------------------
# D. Cmd structure integrity
# ---------------------------------------------------------------------------

def test_cmd_starts_with_claude_p():
    """cmd always starts with ['claude', '-p', <prompt_or_ref>]."""
    cmd, _ = _build_cmd("test prompt", "10", "Read", _cfg())
    assert cmd[0] == "claude"
    assert cmd[1] == "-p"
    assert cmd[2] == "test prompt"


def test_allowed_tools_preserved_in_bare_mode():
    """--allowedTools value is passed through unchanged in bare mode."""
    tools = "Read,Glob,mcp__ai-team-os__task_memo_add"
    cmd, _ = _build_cmd("hi", "3", tools, _cfg(bare_mode=True))
    assert "--allowedTools" in cmd
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == tools


def test_max_turns_preserved_in_bare_mode():
    """--max-turns value is passed through unchanged in bare mode."""
    cmd, _ = _build_cmd("hi", "15", "Read", _cfg(bare_mode=True))
    assert "--max-turns" in cmd
    idx = cmd.index("--max-turns")
    assert cmd[idx + 1] == "15"


# ---------------------------------------------------------------------------
# E. Long prompt → temp file
# ---------------------------------------------------------------------------

def test_long_prompt_uses_temp_file():
    """Prompts > 4000 chars are written to a temp file, cmd references @path."""
    long_prompt = "x" * 4001
    cmd, prompt_file = _build_cmd(long_prompt, "10", "Read", _cfg())
    assert prompt_file is not None
    assert cmd[2].startswith("@")
    assert cmd[2] == f"@{prompt_file}"
    # Temp file must exist and contain the prompt
    assert Path(prompt_file).exists()
    assert Path(prompt_file).read_text(encoding="utf-8") == long_prompt
    # Cleanup
    Path(prompt_file).unlink(missing_ok=True)


def test_short_prompt_inline():
    """Prompts <= 4000 chars are passed inline, no temp file."""
    short_prompt = "a" * 4000
    cmd, prompt_file = _build_cmd(short_prompt, "10", "Read", _cfg())
    assert prompt_file is None
    assert cmd[2] == short_prompt


def test_long_prompt_boundary():
    """Exactly 4001 chars triggers temp file; 4000 chars does not."""
    cmd_4000, pf_4000 = _build_cmd("z" * 4000, "10", "Read", _cfg())
    cmd_4001, pf_4001 = _build_cmd("z" * 4001, "10", "Read", _cfg())
    assert pf_4000 is None
    assert pf_4001 is not None
    if pf_4001:
        Path(pf_4001).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# F. _cleanup_prompt_file
# ---------------------------------------------------------------------------

def test_cleanup_removes_file():
    """_cleanup_prompt_file deletes the file."""
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    tf.write("test")
    tf.close()
    assert Path(tf.name).exists()
    _cleanup_prompt_file(tf.name)
    assert not Path(tf.name).exists()


def test_cleanup_none_is_noop():
    """_cleanup_prompt_file with None does not raise."""
    _cleanup_prompt_file(None)  # Should not raise


def test_cleanup_missing_file_is_noop():
    """_cleanup_prompt_file with a non-existent path does not raise."""
    _cleanup_prompt_file("/tmp/does_not_exist_xyz_12345.txt")


def test_cleanup_called_after_subprocess_error(tmp_path):
    """When subprocess fails to start, temp file is cleaned up."""
    import asyncio
    from unittest.mock import AsyncMock

    from aiteam.api.wake_manager import WakeAgentManager
    from aiteam.types import WakeSession

    long_prompt_cfg = {
        "agent_name": "test-agent",
        "bare_mode": True,
        # long prompt_template triggers temp file
        "prompt_template": "x" * 4001,
    }

    task = MagicMock()
    task.id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    task.action_config = long_prompt_cfg

    session = WakeSession(
        scheduled_task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        agent_name="test-agent",
    )
    repo = AsyncMock()
    repo.get_consecutive_failures = AsyncMock(return_value=0)
    repo.has_actionable_tasks = AsyncMock(return_value=(True, "1 task"))
    repo.list_projects = AsyncMock(return_value=[])
    repo.create_wake_session = AsyncMock(return_value=session)
    repo.update_wake_session = AsyncMock(return_value=session)

    manager = WakeAgentManager(repo=repo, event_bus=MagicMock())

    created_prompt_files: list[str] = []

    original_build_cmd = __import__("aiteam.api.wake_manager", fromlist=["_build_cmd"])._build_cmd

    def tracking_build_cmd(prompt, max_turns, allowed_tools_str, cfg):
        cmd, pf = original_build_cmd(prompt, max_turns, allowed_tools_str, cfg)
        if pf:
            created_prompt_files.append(pf)
        return cmd, pf

    async def run():
        with patch("aiteam.api.wake_manager._build_cmd", side_effect=tracking_build_cmd):
            with patch(
                "aiteam.api.wake_manager.asyncio.create_subprocess_exec",
                side_effect=OSError("claude not found"),
            ):
                result = await manager.try_wake(task)
        return result

    result = asyncio.run(run())
    assert result == "error_start"
    # All temp files must have been cleaned up
    for pf in created_prompt_files:
        assert not Path(pf).exists(), f"Temp file not cleaned up: {pf}"
