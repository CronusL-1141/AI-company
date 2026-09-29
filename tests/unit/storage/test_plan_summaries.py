"""Incremental plan and price summaries in storage (task 551aee38).

* exact: after every insert, the summary-based reads equal the frozen a4bab96
  full-history estimators over the same rows (anchors included);
* self-healing: an out-of-order insert or a row written around the maintenance
  makes the summary stale, and the next reader rebuilds it off the loop;
* bounded: the rows a round materializes do not grow with the history.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
from datetime import timedelta

import pytest

from aiteam.services.plan_summary import order_key
from aiteam.storage import account_usage as storage
from aiteam.storage import plan_summary_store as store
from aiteam.storage.account_usage import AccountUsageRepository, SummaryUnavailableError
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import PlanUsageSnapshot, PricingPlanAnchorReset, PricingPlanSnapshot, PricingQuotaSnapshot

from .. import _oracle_plan_capacity as oracle_capacity
from .. import _oracle_plan_pricing as oracle_pricing
from .._plan_history import WEEK, plan_history, price_history
from .._plan_pricing_fixtures import KEY, account


def quota_for(snapshot) -> PricingQuotaSnapshot:
    return PricingQuotaSnapshot(
        **{field: getattr(snapshot, field) for field in (
            "snapshot_id", "account_key", "limit_id", "window_duration_ms", "resets_at", "observed_at", "used_percent",
        )},
        source="codex_app_server",
    )


async def save(repository, snapshot) -> None:
    if isinstance(snapshot, PricingPlanSnapshot):
        await repository.save_capture(account(), [quota_for(snapshot)], pricing_plan_snapshots=[snapshot])
    else:
        await repository.save_capture(account(), [quota_for(snapshot)], plan_snapshots=[snapshot])


@pytest.fixture
async def repository(tmp_path):
    repo = AccountUsageRepository(f"sqlite+aiosqlite:///{tmp_path / 'summaries.sqlite'}")
    await repo.init_db()
    yield repo
    await engine_pool.get_engine(repo._db_url).dispose()


def mixed_history(seed: int, *, price: int = 50, plan: int = 30):
    rng = random.Random(seed)
    rows = (price_history(rng, price) + price_history(rng, 12, limit_id="codex_bengalfox", window=18_000_000)
            + plan_history(rng, plan) + plan_history(rng, 10, limit_id="codex_bengalfox", window=18_000_000))
    return sorted(rows, key=lambda row: (*order_key(row), type(row).__name__))


async def assert_matches_oracle(repository, now) -> None:
    prices = await repository.list_plan_price_snapshots(KEY)
    anchors = await repository.list_plan_anchors(KEY)
    expected = [e.model_dump(mode="json") for e in oracle_pricing.estimate_pricing_plan_capacity(
        prices, now=now, anchors=anchors)]
    assert [e.model_dump(mode="json") for e in await repository.pricing_plan_estimates(KEY, now=now)] == expected
    plans = await repository.list_plan_snapshots(KEY)
    expected = [e.model_dump(mode="json") for e in oracle_capacity.estimate_plan_capacity(plans, now=now)]
    assert [e.model_dump(mode="json") for e in await repository.plan_estimates(KEY, now=now)] == expected
    latest = max(plans, key=order_key, default=None)
    assert (await repository.latest_plan_snapshot(KEY)) == latest


def stored(location, kind):
    with sqlite3.connect(f"file:{location}?mode=ro", uri=True) as database:
        row = database.execute(
            "SELECT payload FROM account_plan_summaries WHERE account_key=? AND kind=?", (KEY, kind),
        ).fetchone()
        requests = sorted(database.execute(
            "SELECT limit_id, window_duration_ms, kind, request_id, cycle_id, snapshot_id"
            " FROM account_plan_cycle_requests WHERE account_key=?", (KEY,),
        ).fetchall())
    return (None if row is None else json.loads(row[0])), [list(item) for item in requests]


@pytest.mark.parametrize("seed", range(4))
async def test_every_insert_keeps_reads_equal_to_the_frozen_full_history(repository, monkeypatch, seed):
    rows = mixed_history(seed)
    anchor_at = len(rows) // 2
    for index, row in enumerate(rows):
        await save(repository, row)
        if index == anchor_at:
            # the anchor path: the window's latest saved observation becomes the start
            codex = [item for item in rows[: index + 1]
                     if isinstance(item, PricingPlanSnapshot) and item.limit_id == "codex"]
            monkeypatch.setattr(storage, "utc_now", lambda: codex[-1].observed_at + timedelta(seconds=1))
            await repository.reset_plan_anchor(KEY, PricingPlanAnchorReset(limit_id="codex", window_duration_ms=WEEK))
            monkeypatch.undo()
        if index % 7 == 0 or index == len(rows) - 1:
            for offset in (timedelta(seconds=1), timedelta(hours=2), timedelta(days=4)):
                await assert_matches_oracle(repository, row.observed_at + offset)
    # the off-loop rebuild folds the same rows to the same bytes
    location = repository._db_url.split("///", 1)[1]
    child = store.run_rebuild_child(location, KEY)
    for kind in ("plan", "price"):
        assert stored(location, kind)[0] == child[kind]
    assert stored(location, "price")[1] == sorted(child["requests"])


def restated(rows):
    """The same observations as a history without the removed rows would store them."""
    result = []
    for row in rows:
        row = row.model_copy(update={"activity_usd": None, "prediction_activity_usd": None})
        result.append(row.model_copy(update={
            "prediction_activity_usd": oracle_pricing.prediction_cycle_total([*result, row]),
        }))
    return result


async def test_out_of_order_insert_invalidates_and_the_next_read_rebuilds(repository):
    rows = [row for row in mixed_history(11) if isinstance(row, PricingPlanSnapshot) and row.limit_id == "codex"]
    late = rows.pop(len(rows) // 2)
    rows = restated(rows)
    for row in rows:
        await save(repository, row)
    location = repository._db_url.split("///", 1)[1]
    assert stored(location, "price")[0] is not None
    # Stored after later observations (e.g. a clock step): only without a prediction.
    await save(repository, late.model_copy(update={"prediction_activity_usd": None, "activity_usd": None}))
    assert stored(location, "price")[0] is None
    now = rows[-1].observed_at + timedelta(seconds=1)
    await assert_matches_oracle(repository, now)
    assert stored(location, "price")[0] is not None


async def test_a_row_written_around_the_maintenance_is_detected_by_the_fingerprint(repository):
    rows = [row for row in mixed_history(12) if isinstance(row, PricingPlanSnapshot) and row.limit_id == "codex"]
    extra = rows.pop()
    for row in rows:
        await save(repository, row)
    location = repository._db_url.split("///", 1)[1]
    with sqlite3.connect(location) as database:  # an older binary would write exactly this
        database.execute(
            "INSERT INTO account_usage_snapshots (snapshot_id, account_key, observed_at, payload) VALUES (?,?,?,?)",
            (extra.snapshot_id, KEY, extra.observed_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
             quota_for(extra).model_dump_json()),
        )
        database.execute(
            "INSERT INTO account_plan_price_snapshots (id, account_key, observed_at, payload) VALUES (?,?,?,?)",
            (extra.snapshot_id, KEY, extra.observed_at.strftime("%Y-%m-%d %H:%M:%S.%f"), extra.model_dump_json()),
        )
    async with store.get_session(repository._db_url) as session:
        assert await store.fresh_summary(session, "price", KEY) is None
    await assert_matches_oracle(repository, extra.observed_at + timedelta(seconds=1))


async def test_a_prediction_is_never_stored_against_an_unknown_cycle_state(repository):
    rows = [row for row in mixed_history(13) if isinstance(row, PricingPlanSnapshot) and row.limit_id == "codex"]
    for row in rows[:-1]:
        await save(repository, row)
    async with store.get_session(repository._db_url) as session:
        await store.invalidate_summary(session, KEY, "price")
    with pytest.raises(ValueError, match="prediction cumulative values"):
        await save(repository, rows[-1])
    await repository.summaries(KEY)  # rebuilt off the loop, then it is accepted
    await save(repository, rows[-1])


async def test_rebuild_runs_in_a_child_and_concurrent_readers_share_it(repository, monkeypatch):
    rows = [row for row in mixed_history(14) if isinstance(row, PricingPlanSnapshot)]
    for row in rows:
        await save(repository, row)
    async with store.get_session(repository._db_url) as session:
        await store.invalidate_summary(session, KEY, "price")

    def poisoned(*_args, **_kwargs):
        raise AssertionError("summary fold ran in the API interpreter")

    calls = []
    real = store.run_rebuild_child

    def spy(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(store, "compute_account_summaries", poisoned)
    monkeypatch.setattr(store, "run_rebuild_child", spy)
    results = await asyncio.gather(*(repository.summaries(KEY) for _ in range(5)))
    assert len(calls) == 1
    assert all(result.price.count == len(rows) for result in results)


async def test_a_rebuild_overtaken_by_a_write_is_discarded_and_redone(repository, monkeypatch):
    rows = [row for row in mixed_history(15) if isinstance(row, PricingPlanSnapshot) and row.limit_id == "codex"]
    extra = rows.pop()
    for row in rows:
        await save(repository, row)
    async with store.get_session(repository._db_url) as session:
        await store.invalidate_summary(session, KEY, "price")
    real = store.run_rebuild_child
    calls = []

    def racing(path, key):
        result = real(path, key)
        if not calls:  # a capture commits while the first fold was running
            with sqlite3.connect(path) as database:
                database.execute(
                    "INSERT INTO account_plan_price_snapshots (id, account_key, observed_at, payload)"
                    " VALUES (?,?,?,?)",
                    (extra.snapshot_id, KEY, extra.observed_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
                     extra.model_dump_json()),
                )
        calls.append(result)
        return result

    monkeypatch.setattr(store, "run_rebuild_child", racing)
    current = await repository.summaries(KEY)
    assert len(calls) == 2 and current.price.count == len(rows) + 1


async def test_in_memory_databases_are_refused_rather_than_folded_on_the_loop():
    with pytest.raises(SummaryUnavailableError):
        await store._rebuild("sqlite+aiosqlite://", KEY)


@pytest.mark.parametrize("small,large", [(20, 160)])
async def test_rows_read_per_round_do_not_grow_with_history(tmp_path, monkeypatch, small, large):
    """A round's reads are O(1): the same count of payload validations at any history length.

    Measured before the fix on production data: three full-history reads per
    round, ~18K rows each, 2 to 19 s on the event loop.
    """
    counts = []
    for size in (small, large):
        repo = AccountUsageRepository(f"sqlite+aiosqlite:///{tmp_path / f'rows-{size}.sqlite'}")
        await repo.init_db()
        rng = random.Random(size)
        prices = price_history(rng, size + 1)
        plans = plan_history(rng, size + 1, window=WEEK)
        for row in sorted(prices[:-1] + plans[:-1], key=lambda item: (*order_key(item), type(item).__name__)):
            await save(repo, row)
        await repo.summaries(KEY)  # steady state: summaries current
        validated = {"price": 0, "plan": 0}
        real_price, real_plan = PricingPlanSnapshot.model_validate, PlanUsageSnapshot.model_validate

        def count_price(value, *args, **kwargs):
            validated["price"] += 1
            return real_price(value, *args, **kwargs)

        def count_plan(value, *args, **kwargs):
            validated["plan"] += 1
            return real_plan(value, *args, **kwargs)

        monkeypatch.setattr(PricingPlanSnapshot, "model_validate", count_price)
        monkeypatch.setattr(PlanUsageSnapshot, "model_validate", count_plan)
        now = prices[-1].observed_at + timedelta(seconds=1)
        # what one monitor round and one page refresh read
        await repo.latest_plan_snapshot(KEY)
        state = await repo.summaries(KEY)
        window = state.price.windows[("codex", WEEK)]
        await repo.get_price_snapshot(window.last_id)
        await repo.preview_prediction(KEY, window, prices[-1])
        await save(repo, prices[-1])
        await save(repo, plans[-1])
        await repo.pricing_plan_estimates(KEY, now=now)
        await repo.plan_estimates(KEY, now=now)
        monkeypatch.undo()
        counts.append(dict(validated))
        await engine_pool.get_engine(repo._db_url).dispose()
    assert counts[0] == counts[1], f"reads grew with history: {counts}"
    assert counts[1]["price"] < 30 and counts[1]["plan"] < 30, counts
