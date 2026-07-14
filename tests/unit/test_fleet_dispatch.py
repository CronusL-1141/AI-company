"""Unit tests for fleet dispatch (fleet-layer design §4, P3 down-channel).

Covers the reachability gate (evaluate_dispatch_target), the --resume cmd build,
the operational-only prompt, and dispatch_to_session's per-session dedup / circuit
breaker / ledger — all without spawning a real `claude` subprocess.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiteam.api import session_probe
from aiteam.api.routes.fleet import evaluate_dispatch_target
from aiteam.api.wake_manager import (
    _build_cmd,
    _build_dispatch_prompt,
    WakeAgentManager,
)
from aiteam.types import WakeSession


# ---------------------------------------------------------------------------
# A. _build_cmd --resume support
# ---------------------------------------------------------------------------

def test_build_cmd_resume_adds_resume_and_json_and_drops_bare():
    """resume_session_id -> --resume + --output-format json, and non-bare by default."""
    cmd, pf = _build_cmd(
        "do X", "10", "Read",
        {"resume_session_id": "sess-abc", "output_format": "json"},
    )
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "sess-abc"
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    # A resume restores the full environment (hooks fire); bare would strip it.
    assert "--bare" not in cmd
    assert pf is None


def test_build_cmd_no_resume_keeps_bare_default():
    """Without resume, the scheduled-wake path keeps its bare-by-default behaviour."""
    cmd, _ = _build_cmd("do X", "10", "Read", {})
    assert "--resume" not in cmd
    assert "--bare" in cmd


def test_build_cmd_resume_explicit_bare_override_honored():
    """An explicit bare_mode=True is still honored even when resuming."""
    cmd, _ = _build_cmd(
        "x", "5", "Read",
        {"resume_session_id": "s1", "bare_mode": True},
    )
    assert "--resume" in cmd
    assert "--bare" in cmd


# ---------------------------------------------------------------------------
# B. Operational-only prompt
# ---------------------------------------------------------------------------

def test_dispatch_prompt_wraps_instruction_and_carries_guardrails():
    prompt = _build_dispatch_prompt("推进任务 T-42")
    assert "<dispatch-instruction>" in prompt
    assert "推进任务 T-42" in prompt
    # Strategic-decision guardrail must be present in the preamble.
    assert "不替用户拍板" in prompt


# ---------------------------------------------------------------------------
# C. Reachability gate (evaluate_dispatch_target)
# ---------------------------------------------------------------------------

def test_gate_expired_when_no_transcript(monkeypatch):
    monkeypatch.setattr(session_probe, "session_last_active", lambda r, s: None)
    gate = evaluate_dispatch_target("/root", "sess-x", 1800)
    assert gate["allowed"] is False
    assert gate["availability"] == "expired"


def test_gate_refuses_user_live_too_fresh(monkeypatch):
    now = datetime(2026, 7, 14, 12, 0, 0)
    fresh = now - timedelta(seconds=300)  # 5 min < 1800s guard
    monkeypatch.setattr(session_probe, "session_last_active", lambda r, s: fresh)
    gate = evaluate_dispatch_target("/root", "sess-x", 1800, now=now)
    assert gate["allowed"] is False
    assert gate["availability"] == "live"


def test_gate_allows_idle_resumable(monkeypatch):
    now = datetime(2026, 7, 14, 12, 0, 0)
    idle = now - timedelta(seconds=3600)  # 60 min >> 1800s guard
    monkeypatch.setattr(session_probe, "session_last_active", lambda r, s: idle)
    gate = evaluate_dispatch_target("/root", "sess-x", 1800, now=now)
    assert gate["allowed"] is True
    assert gate["availability"] == "idle_resumable"
    assert gate["idle_seconds"] == 3600


# ---------------------------------------------------------------------------
# D. dispatch_to_session — dedup / fuse / ledger
# ---------------------------------------------------------------------------

def _manager_with_repo(**repo_overrides):
    repo = AsyncMock()
    repo.get_consecutive_failures = AsyncMock(return_value=0)
    session = WakeSession(scheduled_task_id="", agent_name="fleet-dispatch-sess-1")
    repo.create_wake_session = AsyncMock(return_value=session)
    repo.update_wake_session = AsyncMock(return_value=session)
    for k, v in repo_overrides.items():
        setattr(repo, k, v)
    return WakeAgentManager(repo=repo, event_bus=MagicMock()), repo, session


def test_dispatch_per_session_dedup_skips_without_spawn():
    """A second dispatch to the same session is skipped before any subprocess spawn."""
    mgr, repo, _ = _manager_with_repo()
    mgr._active_sessions["fleet-dispatch-sess-1"] = MagicMock()  # in-flight

    async def run():
        with patch(
            "aiteam.api.wake_manager.asyncio.create_subprocess_exec",
            side_effect=AssertionError("must not spawn on dedup"),
        ):
            return await mgr.dispatch_to_session("sess-1", "推进任务")

    result = asyncio.run(run())
    assert result["status"] == "skipped_concurrent"
    repo.create_wake_session.assert_not_called()


def test_dispatch_fused_skips_without_spawn():
    mgr, repo, _ = _manager_with_repo(
        get_consecutive_failures=AsyncMock(return_value=3)
    )

    async def run():
        with patch(
            "aiteam.api.wake_manager.asyncio.create_subprocess_exec",
            side_effect=AssertionError("must not spawn when fused"),
        ):
            return await mgr.dispatch_to_session("sess-1", "推进任务")

    result = asyncio.run(run())
    assert result["status"] == "fused"


def test_dispatch_started_records_ledger_metadata():
    """A successful dispatch spawns, returns started, and ledgers dispatch metadata."""
    mgr, repo, session = _manager_with_repo()

    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"{}", b""))

    async def run():
        with patch(
            "aiteam.api.wake_manager.asyncio.create_subprocess_exec",
            AsyncMock(return_value=fake_proc),
        ):
            res = await mgr.dispatch_to_session(
                "sess-1", "推进任务 T-9", cwd="/tmp/proj"
            )
            # Let the fire-and-forget tracking task settle, then clean it up.
            await asyncio.sleep(0)
            for t in list(mgr._active_sessions.values()):
                t.cancel()
            return res

    result = asyncio.run(run())
    assert result["status"] == "started"
    assert result["target_session_id"] == "sess-1"
    assert result["mode"] == "resume"
    repo.create_wake_session.assert_awaited()
    # Ledger metadata carries the dispatch kind + target on the triage_result field.
    meta_calls = [
        c for c in repo.update_wake_session.await_args_list
        if "triage_result" in c.kwargs
    ]
    assert meta_calls, "dispatch must ledger triage_result metadata"
    meta = meta_calls[0].kwargs["triage_result"]
    assert "fleet_dispatch" in meta and "sess-1" in meta


def test_dispatch_rejects_empty_inputs():
    mgr, _, _ = _manager_with_repo()
    assert asyncio.run(mgr.dispatch_to_session("", "x"))["status"] == "error_config"
    assert asyncio.run(mgr.dispatch_to_session("s", "  "))["status"] == "error_config"
