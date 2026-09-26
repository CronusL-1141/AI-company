"""Reaper tail steps must run every cycle even when the front steps exhaust the budget.

Steps run in a fixed order under one cycle deadline. When the front steps
(meeting expiry, stale teams, watermark backfill; seconds each under load) use
the whole budget, the steps at the end were skipped every cycle: scheduled tasks
and workflow ingest stopped running altogether, the 2026-09-15 incident shape.
Those tail steps take milliseconds, so each step keeps a minimum budget instead.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

import aiteam.api.state_reaper as reaper_mod
from aiteam.api.event_bus import EventBus
from aiteam.api.state_reaper import StateReaper
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

FRONT = (
    "_check_meeting_expiry", "_check_stale_teams", "_purge_spent_session_containers",
    "_check_agent_liveness", "_backfill_agent_watermarks",
)


@pytest_asyncio.fixture
async def reaper():
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    yield StateReaper(repo, EventBus(repo=repo))
    await close_db()


@pytest.mark.asyncio
async def test_tail_steps_run_every_cycle_when_front_steps_overrun(reaper, monkeypatch):
    # Production shape scaled down 10x: 30s cycle, 10s per step, 2s minimum. The
    # tail starts 0.4s before the deadline after five cancel round trips; a CPU-starved
    # runner has that much slack before the check turns into a timing flake.
    monkeypatch.setattr(reaper_mod, "REAPER_CYCLE_TIMEOUT", 3.0)
    monkeypatch.setattr(reaper_mod, "REAPER_STEP_TIMEOUT", 1.0)
    monkeypatch.setattr(reaper_mod, "REAPER_STEP_MIN_BUDGET", 0.2, raising=False)
    ran: list[tuple[int, str]] = []
    cycle = 0

    async def overrun(*_args):
        await asyncio.sleep(30)

    def recorder(name):
        async def step(*_args):
            ran.append((cycle, name))
        return step

    for name in FRONT:
        monkeypatch.setattr(reaper, name, overrun)
    monkeypatch.setattr(reaper, "_check_scheduled_tasks", recorder("scheduled_tasks"))
    monkeypatch.setattr(reaper, "_check_workflow_ingest", recorder("workflow_ingest"))

    for cycle in range(3):
        started = asyncio.get_running_loop().time()
        await reaper._reap_cycle()
        # The cycle stays within its deadline (plus scheduling slack).
        assert asyncio.get_running_loop().time() - started < reaper_mod.REAPER_CYCLE_TIMEOUT + 1.0

    assert ran == [(c, name) for c in range(3) for name in ("scheduled_tasks", "workflow_ingest")]


@pytest.mark.asyncio
async def test_budget_split_keeps_order_and_gives_front_steps_the_full_budget(reaper, monkeypatch):
    """Without pressure nothing changes: the first step still gets the whole step budget."""
    monkeypatch.setattr(reaper_mod, "REAPER_STEP_TIMEOUT", 0.3)
    monkeypatch.setattr(reaper_mod, "REAPER_STEP_MIN_BUDGET", 0.05, raising=False)
    budgets: list[tuple[str, float]] = []
    original = asyncio.wait_for

    async def spy(awaitable, timeout):
        budgets.append((getattr(awaitable, "__qualname__", ""), timeout))
        return await original(awaitable, timeout)

    async def quick():
        return None

    monkeypatch.setattr(reaper_mod.asyncio, "wait_for", spy)
    loop = asyncio.get_running_loop()
    await reaper._run_cycle_steps(
        tuple((f"s{i}", quick) for i in range(4)), deadline=loop.time() + 10,
    )
    assert [round(b, 2) for _, b in budgets] == [0.3, 0.3, 0.3, 0.3]
