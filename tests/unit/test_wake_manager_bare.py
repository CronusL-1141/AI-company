"""Unit tests for wake_manager command building (_build_cmd, _cleanup_prompt_file).

The session is spawned with its full environment by default. --bare is an explicit
opt-in and only survives when the subprocess env carries a credential bare mode can
read (ANTHROPIC_API_KEY or a third-party provider flag): bare never reads an OAuth /
keychain login, so a subscription user's run would exit "Not logged in".
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aiteam.api.wake_manager import _bare_auth_available, _build_cmd, _cleanup_prompt_file

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Subprocess envs: an OAuth-only login (nothing bare can read) vs an API key user.
OAUTH_ENV: dict[str, str] = {"PATH": "/usr/bin", "HOME": "/home/u"}
API_KEY_ENV: dict[str, str] = {**OAUTH_ENV, "ANTHROPIC_API_KEY": "sk-ant-test"}


def _cfg(**kwargs) -> dict:
    return kwargs


# ---------------------------------------------------------------------------
# A. Default: full environment, no --bare
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", [OAUTH_ENV, API_KEY_ENV], ids=["oauth", "api_key"])
def test_default_is_not_bare(env):
    """No bare_mode in cfg -> no --bare, whatever login the user has."""
    cmd, prompt_file = _build_cmd("hello", "10", _cfg(), env=env)
    assert "--bare" not in cmd
    assert "--exclude-dynamic-system-prompt-sections" not in cmd
    assert prompt_file is None


def test_default_uses_os_environ_when_env_omitted(monkeypatch):
    """env=None reads os.environ; the default stays non-bare either way."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cmd, _ = _build_cmd("hello", "10", _cfg())
    assert "--bare" not in cmd


# ---------------------------------------------------------------------------
# B. bare_mode=True opt-in, gated on a credential bare mode can read
# ---------------------------------------------------------------------------

def test_bare_opt_in_with_api_key_adds_flags():
    cmd, _ = _build_cmd("hi", "5", _cfg(bare_mode=True), env=API_KEY_ENV)
    assert "--bare" in cmd
    assert "--exclude-dynamic-system-prompt-sections" in cmd


def test_bare_opt_in_without_readable_credential_is_dropped(caplog):
    """OAuth-only env: bare would exit "Not logged in", so the opt-in is dropped."""
    with caplog.at_level("WARNING", logger="aiteam.api.wake_manager"):
        cmd, _ = _build_cmd("hi", "5", _cfg(bare_mode=True), env=OAUTH_ENV)
    assert "--bare" not in cmd
    assert "--exclude-dynamic-system-prompt-sections" not in cmd
    assert any("bare_mode requested" in r.getMessage() for r in caplog.records)


def test_bare_opt_in_blank_api_key_is_dropped():
    cmd, _ = _build_cmd(
        "hi", "5", _cfg(bare_mode=True), env={**OAUTH_ENV, "ANTHROPIC_API_KEY": "  "}
    )
    assert "--bare" not in cmd


@pytest.mark.parametrize(
    "flag",
    ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"],
)
def test_bare_opt_in_with_third_party_provider_kept(flag):
    cmd, _ = _build_cmd("hi", "5", _cfg(bare_mode=True), env={**OAUTH_ENV, flag: "1"})
    assert "--bare" in cmd


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (OAUTH_ENV, False),
        (API_KEY_ENV, True),
        ({"ANTHROPIC_API_KEY": ""}, False),
        ({"CLAUDE_CODE_USE_BEDROCK": "true"}, True),
        ({"CLAUDE_CODE_USE_VERTEX": "0"}, False),
    ],
    ids=["oauth", "api_key", "blank_key", "bedrock_true", "vertex_zero"],
)
def test_bare_auth_available(env, expected):
    assert _bare_auth_available(env) is expected


def test_bare_mode_false_omits_flags():
    cmd, prompt_file = _build_cmd("hello", "10", _cfg(bare_mode=False), env=API_KEY_ENV)
    assert "--bare" not in cmd
    assert "--exclude-dynamic-system-prompt-sections" not in cmd
    assert prompt_file is None


# ---------------------------------------------------------------------------
# C. --mcp-config injection (bare only; a full session discovers MCP itself)
# ---------------------------------------------------------------------------

def test_bare_mode_injects_mcp_config_when_file_exists(tmp_path):
    """--mcp-config is added when .mcp.json is found at cwd."""
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text('{"mcpServers":{}}')

    cmd, _ = _build_cmd("hi", "5", _cfg(bare_mode=True, cwd=str(tmp_path)), env=API_KEY_ENV)
    assert "--mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == str(mcp_file)


def test_bare_mode_no_mcp_config_when_file_missing(tmp_path):
    """--mcp-config is NOT added when no .mcp.json exists in cwd."""
    cmd, _ = _build_cmd("hi", "5", _cfg(bare_mode=True, cwd=str(tmp_path)), env=API_KEY_ENV)
    assert "--mcp-config" not in cmd


def test_bare_mode_explicit_mcp_config_path(tmp_path):
    """Explicit mcp_config in cfg overrides auto-discovery."""
    explicit_path = str(tmp_path / "custom.mcp.json")
    cmd, _ = _build_cmd(
        "hi", "5", _cfg(bare_mode=True, mcp_config=explicit_path), env=API_KEY_ENV
    )
    assert "--mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == explicit_path


@pytest.mark.parametrize("cfg_bare", [None, False], ids=["default", "explicit_false"])
def test_non_bare_never_adds_mcp_config(tmp_path, cfg_bare):
    """A full session discovers .mcp.json itself; --mcp-config is never added."""
    (tmp_path / ".mcp.json").write_text("{}")
    cfg = _cfg(cwd=str(tmp_path)) if cfg_bare is None else _cfg(bare_mode=False, cwd=str(tmp_path))
    cmd, _ = _build_cmd("hi", "5", cfg, env=API_KEY_ENV)
    assert "--mcp-config" not in cmd


def test_dropped_bare_opt_in_adds_no_mcp_config(tmp_path):
    (tmp_path / ".mcp.json").write_text("{}")
    cmd, _ = _build_cmd("hi", "5", _cfg(bare_mode=True, cwd=str(tmp_path)), env=OAUTH_ENV)
    assert "--mcp-config" not in cmd


# ---------------------------------------------------------------------------
# D. Cmd structure integrity
# ---------------------------------------------------------------------------

def test_cmd_starts_with_claude_p():
    """cmd always starts with ['claude', '-p', <prompt_or_ref>]."""
    cmd, _ = _build_cmd("test prompt", "10", _cfg())
    assert cmd[0] == "claude"
    assert cmd[1] == "-p"
    assert cmd[2] == "test prompt"


@pytest.mark.parametrize(
    ("cfg", "env"),
    [
        ({}, OAUTH_ENV),
        ({"bare_mode": True}, API_KEY_ENV),
        ({"resume_session_id": "s1", "output_format": "json"}, OAUTH_ENV),
    ],
    ids=["default", "bare", "resume"],
)
def test_never_passes_allowed_tools(cfg, env):
    """The session keeps its own permission configuration: no OS tool allowlist."""
    cmd, _ = _build_cmd("hi", "3", cfg, env=env)
    assert "--allowedTools" not in cmd
    assert "--allowed-tools" not in cmd


@pytest.mark.parametrize(
    ("cfg", "env"), [({}, OAUTH_ENV), ({"bare_mode": True}, API_KEY_ENV)], ids=["full", "bare"]
)
def test_max_turns_preserved(cfg, env):
    """--max-turns value is passed through unchanged."""
    cmd, _ = _build_cmd("hi", "15", cfg, env=env)
    assert "--max-turns" in cmd
    idx = cmd.index("--max-turns")
    assert cmd[idx + 1] == "15"


# ---------------------------------------------------------------------------
# E. Long prompt → temp file
# ---------------------------------------------------------------------------

def test_long_prompt_uses_temp_file():
    """Prompts > 4000 chars are written to a temp file, cmd references @path."""
    long_prompt = "x" * 4001
    cmd, prompt_file = _build_cmd(long_prompt, "10", _cfg())
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
    cmd, prompt_file = _build_cmd(short_prompt, "10", _cfg())
    assert prompt_file is None
    assert cmd[2] == short_prompt


def test_long_prompt_boundary():
    """Exactly 4001 chars triggers temp file; 4000 chars does not."""
    cmd_4000, pf_4000 = _build_cmd("z" * 4000, "10", _cfg())
    cmd_4001, pf_4001 = _build_cmd("z" * 4001, "10", _cfg())
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

    def tracking_build_cmd(prompt, max_turns, cfg, env=None):
        cmd, pf = original_build_cmd(prompt, max_turns, cfg, env=env)
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
