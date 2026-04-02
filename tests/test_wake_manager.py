"""Tests for WakeAgentManager."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aiteam.api.wake_manager import (
    WakeAgentManager,
    _build_prompt,
    _clean_env,
    _validate_uuid,
    WAKE_TOOL_PRESETS,
)
from aiteam.types import WakeSession, ScheduledTask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sched_task(agent_name="test-agent", task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", **config_overrides):
    cfg = {"agent_name": agent_name, **config_overrides}
    mock = MagicMock()
    mock.id = task_id
    mock.action_config = cfg
    mock.name = f"wake-{agent_name}"
    return mock


def _make_mock_proc(returncode=0, stdout=b"done", stderr=b""):
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


def _make_mock_repo(consecutive_failures=0, has_work=True):
    repo = AsyncMock()
    repo.get_consecutive_failures = AsyncMock(return_value=consecutive_failures)
    repo.has_actionable_tasks = AsyncMock(return_value=(has_work, "1 actionable tasks" if has_work else "no actionable tasks"))
    session = WakeSession(
        scheduled_task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        agent_name="test-agent",
    )
    repo.create_wake_session = AsyncMock(return_value=session)
    repo.update_wake_session = AsyncMock(return_value=session)
    return repo


def _make_manager(repo=None, failures=0):
    if repo is None:
        repo = _make_mock_repo(failures)
    event_bus = MagicMock()
    return WakeAgentManager(repo=repo, event_bus=event_bus), repo


# ---------------------------------------------------------------------------
# A. Basic flow
# ---------------------------------------------------------------------------

async def test_try_wake_starts_subprocess():
    """Valid task starts subprocess and returns 'started'."""
    manager, repo = _make_manager()
    proc = _make_mock_proc()

    with patch("aiteam.api.wake_manager.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
        result = await manager.try_wake(_make_sched_task())

    assert result == "started"
    mock_exec.assert_called_once()
    # Clean up background task
    await manager.shutdown()


async def test_try_wake_missing_agent_name():
    """Empty agent_name returns 'error_config'."""
    manager, _ = _make_manager()
    task = _make_sched_task(agent_name="")
    result = await manager.try_wake(task)
    assert result == "error_config"


async def test_try_wake_invalid_uuid():
    """Non-UUID task id returns 'error_config'."""
    manager, _ = _make_manager()
    task = _make_sched_task(task_id="not-a-valid-uuid")
    result = await manager.try_wake(task)
    assert result == "error_config"


# ---------------------------------------------------------------------------
# B. Concurrency control
# ---------------------------------------------------------------------------

async def test_per_agent_lock():
    """Agent already in _active_sessions returns 'skipped_concurrent'."""
    manager, _ = _make_manager()
    # Simulate an already-running task for this agent
    fake_task = asyncio.create_task(asyncio.sleep(10))
    manager._active_sessions["test-agent"] = fake_task

    result = await manager.try_wake(_make_sched_task())
    assert result == "skipped_concurrent"

    fake_task.cancel()
    try:
        await fake_task
    except asyncio.CancelledError:
        pass


async def test_max_concurrent():
    """When semaphore is exhausted, returns 'skipped_max_concurrent'."""
    manager, _ = _make_manager()

    # Exhaust the semaphore by acquiring all slots
    acquired = []
    for _ in range(manager._semaphore._value):
        await manager._semaphore.acquire()
        acquired.append(True)

    try:
        result = await manager.try_wake(_make_sched_task())
        assert result == "skipped_max_concurrent"
    finally:
        for _ in acquired:
            manager._semaphore.release()


# ---------------------------------------------------------------------------
# C. Circuit breaker
# ---------------------------------------------------------------------------

async def test_circuit_breaker_fuse():
    """3 consecutive failures triggers fuse, returns 'fused'."""
    manager, _ = _make_manager(failures=3)
    result = await manager.try_wake(_make_sched_task())
    assert result == "fused"


async def test_circuit_breaker_below_threshold():
    """2 consecutive failures is below threshold, proceeds normally."""
    manager, _ = _make_manager(failures=2)
    proc = _make_mock_proc()

    with patch("aiteam.api.wake_manager.asyncio.create_subprocess_exec", return_value=proc):
        result = await manager.try_wake(_make_sched_task())

    assert result == "started"
    await manager.shutdown()


# ---------------------------------------------------------------------------
# D. Subprocess lifecycle
# ---------------------------------------------------------------------------

async def test_normal_completion():
    """Subprocess exits 0 → outcome='completed' recorded."""
    manager, repo = _make_manager()
    proc = _make_mock_proc(returncode=0, stdout=b"all good")

    with patch("aiteam.api.wake_manager.asyncio.create_subprocess_exec", return_value=proc):
        await manager.try_wake(_make_sched_task())

    # Wait for background tracking task to finish
    await asyncio.sleep(0.1)
    # Drain remaining active tasks
    if manager._active_sessions:
        await manager.shutdown()

    repo.update_wake_session.assert_called()
    call_kwargs = repo.update_wake_session.call_args[1]
    assert call_kwargs.get("outcome") == "completed"


async def test_error_exit_code():
    """Subprocess exits non-zero → outcome='error'."""
    manager, repo = _make_manager()
    proc = _make_mock_proc(returncode=1, stdout=b"fail")

    with patch("aiteam.api.wake_manager.asyncio.create_subprocess_exec", return_value=proc):
        await manager.try_wake(_make_sched_task())

    await asyncio.sleep(0.1)
    if manager._active_sessions:
        await manager.shutdown()

    repo.update_wake_session.assert_called()
    call_kwargs = repo.update_wake_session.call_args[1]
    assert call_kwargs.get("outcome") == "error"


async def test_timeout_kills_process():
    """communicate() timeout → terminate+kill called, outcome='timeout'."""
    manager, repo = _make_manager()

    proc = _make_mock_proc()
    # Make communicate raise TimeoutError
    proc.communicate = AsyncMock(side_effect=TimeoutError())
    proc.wait = AsyncMock(return_value=None)

    with patch("aiteam.api.wake_manager.asyncio.create_subprocess_exec", return_value=proc):
        with patch("aiteam.api.wake_manager.asyncio.wait_for", side_effect=TimeoutError()):
            await manager.try_wake(_make_sched_task())

    await asyncio.sleep(0.1)
    if manager._active_sessions:
        await manager.shutdown()

    proc.terminate.assert_called()
    repo.update_wake_session.assert_called()
    call_kwargs = repo.update_wake_session.call_args[1]
    assert call_kwargs.get("outcome") == "timeout"


# ---------------------------------------------------------------------------
# E. Shutdown
# ---------------------------------------------------------------------------

async def test_shutdown_cancels_active():
    """Shutdown cancels running tracking tasks."""
    manager, _ = _make_manager()

    task_started = asyncio.Event()
    cancelled = []

    async def slow_task():
        task_started.set()
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    task = asyncio.create_task(slow_task())
    manager._active_sessions["agent-a"] = task
    await task_started.wait()  # ensure coroutine is running before shutdown

    await manager.shutdown()

    assert len(cancelled) == 1
    assert manager.active_count == 0


async def test_shutdown_empty():
    """Shutdown with no active sessions completes without error."""
    manager, _ = _make_manager()
    assert manager.active_count == 0
    await manager.shutdown()  # Should not raise


# ---------------------------------------------------------------------------
# F. Session recording
# ---------------------------------------------------------------------------

async def test_session_recorded_on_success():
    """create_wake_session and update_wake_session both called on success."""
    manager, repo = _make_manager()
    proc = _make_mock_proc(returncode=0)

    with patch("aiteam.api.wake_manager.asyncio.create_subprocess_exec", return_value=proc):
        result = await manager.try_wake(_make_sched_task())

    assert result == "started"
    repo.create_wake_session.assert_called_once()

    await asyncio.sleep(0.1)
    if manager._active_sessions:
        await manager.shutdown()

    repo.update_wake_session.assert_called_once()


async def test_session_recorded_on_error():
    """Session is also recorded when subprocess fails to start."""
    manager, repo = _make_manager()

    with patch(
        "aiteam.api.wake_manager.asyncio.create_subprocess_exec",
        side_effect=OSError("claude not found"),
    ):
        result = await manager.try_wake(_make_sched_task())

    assert result == "error_start"
    repo.create_wake_session.assert_called_once()
    repo.update_wake_session.assert_called_once()
    call_kwargs = repo.update_wake_session.call_args[1]
    assert call_kwargs.get("outcome") == "error"


# ---------------------------------------------------------------------------
# G. Helper functions
# ---------------------------------------------------------------------------

def test_build_prompt_default():
    """No template → default prompt contains expected text."""
    task = _make_sched_task()
    prompt = _build_prompt(task)
    assert "test-agent" in prompt
    assert "AI Team OS" in prompt


def test_build_prompt_custom():
    """Custom template with {agent_name} placeholder is substituted."""
    task = _make_sched_task(prompt_template="Hello {agent_name}, wake up!")
    prompt = _build_prompt(task)
    assert prompt == "Hello test-agent, wake up!"


def test_build_prompt_with_context():
    """task_context is wrapped in XML tags."""
    task = _make_sched_task(task_context="do the thing")
    prompt = _build_prompt(task)
    assert "<task-context>" in prompt
    assert "do the thing" in prompt
    assert "</task-context>" in prompt


def test_clean_env_whitelist():
    """_clean_env inherits env but excludes sensitive variables."""
    env = _clean_env()
    for key in ("DATABASE_URL", "SECRET_KEY", "AITEAM_API_URL"):
        assert key not in env
    assert "PATH" in env
    env2 = _clean_env("/some/path")
    assert env2.get("CLAUDE_PROJECT_DIR") == "/some/path"


def test_tool_presets():
    """safe preset excludes Bash; with_bash preset includes Bash."""
    assert "Bash" not in WAKE_TOOL_PRESETS["safe"]
    assert "Bash" in WAKE_TOOL_PRESETS["with_bash"]
    # with_bash is a superset of safe
    for tool in WAKE_TOOL_PRESETS["safe"]:
        assert tool in WAKE_TOOL_PRESETS["with_bash"]


def test_validate_uuid():
    """_validate_uuid accepts valid UUIDs and rejects invalid ones."""
    assert _validate_uuid("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") is True
    assert _validate_uuid("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE") is True
    assert _validate_uuid("not-a-uuid") is False
    assert _validate_uuid("") is False
    assert _validate_uuid("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee") is False  # too short
