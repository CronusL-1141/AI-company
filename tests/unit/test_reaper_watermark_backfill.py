"""Regression tests: the watermark backfill must not stall the API.

2026-09-15 incident. ``_backfill_agent_watermarks`` resolved every unmeasured
agent with two wide globs over ``~/.claude/projects``. With ~2000 project slugs
and 443 permanently unresolvable rows that came to ~40s of synchronous
``opendir`` per 60s cycle, executed directly on the event loop, so every HTTP
request and every hook POST queued behind it (measured: ``/api/health`` went from
0.8ms to 0.3-5s, and hooks hit their 1.5s client timeout 1267 times).

Three properties are asserted here, each mapping to one half of the fix:

* the blocking half really runs in a worker thread (loop stays responsive),
* a row that can never resolve is written off instead of re-probed forever,
* a slow step cannot starve the steps queued behind it in the same cycle.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from aiteam.api import state_reaper as reaper_mod
from aiteam.api.event_bus import EventBus
from aiteam.api.state_reaper import StateReaper
from aiteam.clock import utc_now
from aiteam.storage.repository import StorageRepository
from aiteam.types import AgentStatus


@pytest_asyncio.fixture()
async def repo():
    from aiteam.storage.connection import close_db

    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r
    await close_db()


@pytest_asyncio.fixture()
async def reaper(repo: StorageRepository):
    bus = EventBus(repo)
    bus.emit = AsyncMock()
    return StateReaper(repo, bus)


async def _make_agent(
    repo: StorageRepository,
    team_id: str,
    ccid: str,
    *,
    status: AgentStatus = AgentStatus.OFFLINE,
    age: timedelta = timedelta(hours=2),
):
    agent = await repo.create_agent(
        team_id=team_id, name=f"a-{ccid}", role="workflow-subagent",
        source="hook", cc_tool_use_id=ccid, session_id="sess-test",
    )
    await repo.update_agent(
        agent.id, status=status, last_active_at=utc_now() - age
    )
    return agent


class TestOffLoopExecution:
    @pytest.mark.asyncio
    async def test_blocking_scan_does_not_block_the_event_loop(
        self, repo: StorageRepository, reaper: StateReaper, monkeypatch
    ):
        """The scan half must be awaited through a thread, not inlined.

        Without this, the reaper's own 30s timeout cannot fire either: the old
        code had no await point inside the scan, so ``wait_for`` could only
        cancel it after it had already finished.
        """
        team = await repo.create_team(name="t-offloop", mode="coordinate")
        await _make_agent(repo, team.id, "offloop1")

        def slow_scan(candidates):
            time.sleep(0.4)  # stands in for a real tree walk + transcript reads
            return [], set()

        monkeypatch.setattr(reaper, "_scan_watermarks", slow_scan)

        gaps: list[float] = []

        async def heartbeat():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.02)  # establish a baseline before measuring
        gaps.clear()
        try:
            await reaper._backfill_agent_watermarks(repo)
        finally:
            # Drain before cancelling: a starved heartbeat records its gap only
            # once it gets the loop back.
            await asyncio.sleep(0.02)
            beat.cancel()
            with pytest.raises(asyncio.CancelledError):
                await beat

        assert gaps, "heartbeat never ran"
        assert max(gaps) < 0.2, f"event loop blocked for {max(gaps):.2f}s during backfill"


    @pytest.mark.asyncio
    async def test_many_unresolvable_rows_keep_the_loop_responsive(
        self, repo: StorageRepository, reaper: StateReaper, monkeypatch, tmp_path
    ):
        """End-to-end guard, written to run against the pre-fix code too.

        No internals are stubbed: real agent rows, a real projects tree, the real
        scan. Before the fix this took ~18ms per unresolved row (two full walks of
        a 400-slug tree) straight on the event loop; after it, one walk serves all
        of them.
        """
        tree = reaper_tmp_tree(monkeypatch, tmp_path)
        team = await repo.create_team(name="t-perf", mode="coordinate")
        for i in range(50):
            await _make_agent(repo, team.id, f"absent-{i}", age=timedelta(minutes=1))

        gaps: list[float] = []

        async def heartbeat():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.02)  # let the heartbeat establish a baseline
        gaps.clear()
        started = time.monotonic()
        try:
            await reaper._backfill_agent_watermarks(repo)
        finally:
            elapsed = time.monotonic() - started
            # Let the heartbeat resume and record the gap it was starved by.
            # Cancelling first would throw that observation away — the whole
            # point of the measurement.
            await asyncio.sleep(0.02)
            beat.cancel()
            with pytest.raises(asyncio.CancelledError):
                await beat

        # No gap recorded means the whole backfill finished inside one heartbeat
        # tick, so its own wall time is the tighter bound on any blocking.
        worst = max(gaps) if gaps else elapsed
        assert worst < 0.2, (
            f"50 unresolvable rows blocked the loop for {worst:.2f}s (tree={tree})"
        )


def reaper_tmp_tree(monkeypatch, tmp_path) -> str:
    """Plant a projects tree wide enough that a per-row tree walk is measurable.

    Rooted under pytest's tmp_path so the 400 slug dirs are cleaned up; an
    mkdtemp here would leave them behind on every run.
    """
    from aiteam.api import agent_context

    root = tmp_path / "projects"
    for i in range(400):
        (root / f"-slug-{i}" / "sess-test" / "subagents").mkdir(parents=True)
    monkeypatch.setattr(agent_context, "_projects_dir", lambda: root)
    return str(root)


class TestMissLedger:
    @pytest.mark.asyncio
    async def test_dead_agent_is_written_off_after_miss_limit(
        self, repo: StorageRepository, reaper: StateReaper, monkeypatch
    ):
        """A long-offline row whose transcript never appears stops being probed."""
        team = await repo.create_team(name="t-writeoff", mode="coordinate")
        agent = await _make_agent(repo, team.id, "gone1")

        seen: list[int] = []

        def missing_scan(candidates):
            seen.append(len(candidates))
            return [], {c.agent_id for c in candidates}

        monkeypatch.setattr(reaper, "_scan_watermarks", missing_scan)

        for _ in range(3):
            await reaper._backfill_agent_watermarks(repo)

        assert seen[0] == 1 and seen[1] == 1, "first two cycles should still probe"
        # Third cycle: the row is written off, so there are no candidates left and
        # the scan is not even entered.
        assert len(seen) == 2, f"expected the scan to be skipped, got {seen}"
        assert reaper._ctx_probe_miss[agent.id] == reaper_mod.CTX_PROBE_MISS_LIMIT

    @pytest.mark.asyncio
    async def test_live_agent_is_probed_despite_misses(
        self, repo: StorageRepository, reaper: StateReaper, monkeypatch
    ):
        """A busy agent's transcript may simply not be on disk yet — keep probing."""
        team = await repo.create_team(name="t-live", mode="coordinate")
        await _make_agent(repo, team.id, "live1", status=AgentStatus.BUSY)

        seen: list[int] = []

        def missing_scan(candidates):
            seen.append(len(candidates))
            return [], {c.agent_id for c in candidates}

        monkeypatch.setattr(reaper, "_scan_watermarks", missing_scan)
        for _ in range(4):
            await reaper._backfill_agent_watermarks(repo)
        assert seen == [1, 1, 1, 1], f"live agent stopped being probed: {seen}"

    @pytest.mark.asyncio
    async def test_recently_dead_agent_is_probed_despite_misses(
        self, repo: StorageRepository, reaper: StateReaper, monkeypatch
    ):
        """Offline but inside the grace window: a late SubagentStop may still land."""
        team = await repo.create_team(name="t-fresh", mode="coordinate")
        await _make_agent(repo, team.id, "fresh1", age=timedelta(minutes=1))

        seen: list[int] = []

        def missing_scan(candidates):
            seen.append(len(candidates))
            return [], {c.agent_id for c in candidates}

        monkeypatch.setattr(reaper, "_scan_watermarks", missing_scan)
        for _ in range(4):
            await reaper._backfill_agent_watermarks(repo)
        assert seen == [1, 1, 1, 1], f"agent inside grace window skipped: {seen}"

    @pytest.mark.asyncio
    async def test_resolved_agent_clears_its_miss_history(
        self, repo: StorageRepository, reaper: StateReaper, monkeypatch
    ):
        team = await repo.create_team(name="t-clear", mode="coordinate")
        agent = await _make_agent(repo, team.id, "flap1")

        monkeypatch.setattr(
            reaper, "_scan_watermarks", lambda c: ([], {x.agent_id for x in c})
        )
        await reaper._backfill_agent_watermarks(repo)
        assert reaper._ctx_probe_miss[agent.id] == 1

        monkeypatch.setattr(reaper, "_scan_watermarks", lambda c: ([], set()))
        await reaper._backfill_agent_watermarks(repo)
        assert agent.id not in reaper._ctx_probe_miss

    @pytest.mark.asyncio
    async def test_miss_ledger_is_capped(self, reaper: StateReaper, monkeypatch):
        monkeypatch.setattr(reaper_mod, "CTX_PROBE_MISS_MAX_ENTRIES", 10)
        reaper._ctx_probe_miss = {f"a{i}": 1 for i in range(20)}
        reaper._record_ctx_probe_results([], set())
        assert len(reaper._ctx_probe_miss) == 10


class TestCycleStepIsolation:
    @pytest.mark.asyncio
    async def test_slow_step_does_not_starve_later_steps(
        self, reaper: StateReaper, monkeypatch
    ):
        """The 2026-09-15 silent failure: steps behind a stalled one never ran."""
        monkeypatch.setattr(reaper_mod, "REAPER_STEP_TIMEOUT", 0.05)
        ran: list[str] = []

        async def hangs():
            ran.append("hang-start")
            await asyncio.sleep(5)
            ran.append("hang-end")

        async def quick():
            ran.append("quick")

        await reaper._run_cycle_steps((("hangs", hangs), ("quick", quick)))
        assert ran == ["hang-start", "quick"], ran

    @pytest.mark.asyncio
    async def test_failing_step_does_not_stop_the_cycle(self, reaper: StateReaper):
        ran: list[str] = []

        async def boom():
            raise RuntimeError("step blew up")

        async def quick():
            ran.append("quick")

        await reaper._run_cycle_steps((("boom", boom), ("quick", quick)))
        assert ran == ["quick"]
