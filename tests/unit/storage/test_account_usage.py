"""Account evidence must survive sessions and concurrent duplicate imports."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import (
    PlanUsageSnapshot,
    PricingAccount,
    PricingQuotaSnapshot,
    PricingRequestLine,
    PricingUsageBatch,
    PricingUsageEntry,
)

ACCOUNT_KEY = "a" * 64
OTHER_KEY = "b" * 64
START = datetime(2026, 9, 13, 8, tzinfo=UTC)
END = START + timedelta(hours=1)
RESET = START + timedelta(days=2)
WEEK_MS = 604800000


def account(**updates) -> PricingAccount:
    return PricingAccount.model_validate({
        "account_key": ACCOUNT_KEY, "label": "Local account", "created_at": START,
        **updates,
    })


def snapshot(snapshot_id="start", **updates) -> PricingQuotaSnapshot:
    return PricingQuotaSnapshot.model_validate({
        "snapshot_id": snapshot_id, "account_key": ACCOUNT_KEY, "limit_id": "codex",
        "used_percent": "10", "window_duration_ms": WEEK_MS,
        "resets_at": RESET, "observed_at": START, "source": "user_import",
        **updates,
    })


def entry(request_id="request-1", occurred_at=END) -> PricingUsageEntry:
    return PricingUsageEntry(
        occurred_at=occurred_at,
        request=PricingRequestLine(
            request_id=request_id, model="gpt-5.6-sol", service_tier="standard",
            input_tokens=1200, output_tokens=40, cached_input_tokens=200,
        ),
    )


def batch(batch_id="batch-1", **updates) -> PricingUsageBatch:
    return PricingUsageBatch.model_validate({
        "batch_id": batch_id, "account_key": ACCOUNT_KEY,
        "start_snapshot_id": "start", "end_snapshot_id": "end",
        "coverage": "local_only", "coverage_statement": "This workstation only.",
        "coverage_confirmed_at": None, "entries": [entry()],
        **updates,
    })


@pytest.fixture
async def repositories(tmp_path):
    db_path = tmp_path / "account-usage.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    first = AccountUsageRepository(db_url)
    second = AccountUsageRepository(db_url)
    await first.init_db()
    yield first, second, db_path
    await engine_pool.get_engine(db_url).dispose()


async def seed(repository, *, end_updates=None):
    await repository.save_capture(account(), [
        snapshot(), snapshot("end", observed_at=END, used_percent="15", **(end_updates or {})),
    ])


def rows(db_path, table):
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        return connection.execute(f'SELECT * FROM "{table}"').fetchall()


async def test_initialization_creates_only_own_tables(repositories):
    first, _, db_path = repositories
    await first.init_db()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        table_names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
    assert table_names == {
        "account_usage_accounts", "account_usage_snapshots", "account_usage_batches",
        "account_usage_request_dedupe", "account_plan_snapshots", "account_plan_price_snapshots",
        "account_plan_price_anchors",
    }


@pytest.mark.parametrize("db_url", [None, "", "  ", "postgresql+asyncpg://localhost/test"])
def test_database_selection_is_explicit(db_url):
    with pytest.raises(ValueError):
        AccountUsageRepository(db_url)


async def test_cross_session_readback_and_alias_update_preserve_evidence(repositories):
    first, second, db_path = repositories
    await seed(first)
    original_batch = batch(
        coverage="account_complete", coverage_statement="All clients are included.",
        coverage_confirmed_at=END + timedelta(minutes=5),
    )
    await first.add_batch(original_batch)
    await second.upsert_account(account(label="Renamed account", created_at=END))

    assert await first.get_account(ACCOUNT_KEY) == account(label="Renamed account")
    assert await second.get_snapshot("start") == snapshot()
    assert await second.get_batch("batch-1") == original_batch
    assert await first.list_accounts() == [account(label="Renamed account")]
    assert [value.snapshot_id for value in await second.list_snapshots(ACCOUNT_KEY)] == ["start", "end"]
    assert await second.list_batches(ACCOUNT_KEY) == [original_batch]
    assert len(rows(db_path, "account_usage_request_dedupe")) == 1


async def test_account_keys_are_validated_before_storage_even_after_model_copy(repositories):
    first, second, db_path = repositories
    invalid = account().model_copy(update={"account_key": "raw-account-id@example.test"})
    with pytest.raises(ValueError):
        await first.upsert_account(invalid)
    assert await second.list_accounts() == []
    assert rows(db_path, "account_usage_accounts") == []


async def test_missing_reads_are_empty_and_do_not_create_accounts(repositories):
    first, _, db_path = repositories
    assert await first.get_account(ACCOUNT_KEY) is None
    assert await first.get_snapshot("missing") is None
    assert await first.get_batch("missing") is None
    assert await first.list_snapshots(ACCOUNT_KEY) == []
    assert await first.list_batches(ACCOUNT_KEY) == []
    assert rows(db_path, "account_usage_accounts") == []


async def test_snapshot_without_account_is_rejected(repositories):
    first, second, _ = repositories
    with pytest.raises(ValueError, match="account does not exist"):
        await first.add_snapshot(snapshot())
    assert await second.get_snapshot("start") is None


async def test_snapshot_idempotence_and_conflict_are_persistent(repositories):
    first, second, db_path = repositories
    await first.upsert_account(account())
    original = snapshot(used_percent="10.125")
    await first.add_snapshot(original)
    assert await second.add_snapshot(original) == original
    with pytest.raises(ValueError, match="different content"):
        await second.add_snapshot(snapshot(used_percent="10.126"))
    assert await first.get_snapshot("start") == original
    assert len(rows(db_path, "account_usage_snapshots")) == 1


async def test_snapshot_index_and_identity_handle_equivalent_timezones(repositories):
    first, second, db_path = repositories
    local_timezone = timezone(timedelta(hours=8))
    local = snapshot(observed_at=START.astimezone(local_timezone))
    await first.save_capture(account(), [local])
    assert await second.add_snapshot(snapshot()) == local
    raw = rows(db_path, "account_usage_snapshots")[0]
    assert raw[2] == "2026-09-13 08:00:00.000000"
    assert (await second.get_snapshot("start")).observed_at == START


async def test_capture_conflict_rolls_back_account_alias_and_all_new_snapshots(repositories):
    first, second, _ = repositories
    await first.save_capture(account(), [snapshot()])
    with pytest.raises(ValueError, match="different content"):
        await second.save_capture(account(label="Must roll back"), [
            snapshot("fresh"), snapshot(used_percent="99"),
        ])
    assert await first.get_account(ACCOUNT_KEY) == account()
    assert await first.get_snapshot("fresh") is None
    assert await first.get_snapshot("start") == snapshot()


async def test_capture_rejects_cross_account_before_creating_alias(repositories):
    first, second, _ = repositories
    with pytest.raises(ValueError, match="captured account"):
        await first.save_capture(account(), [snapshot(account_key=OTHER_KEY)])
    assert await second.list_accounts() == []


async def test_batch_replay_is_idempotent_but_confirmation_is_immutable(repositories):
    first, second, db_path = repositories
    await seed(first)
    original = batch()
    await first.add_batch(original)
    assert await second.add_batch(original) == original
    with pytest.raises(ValueError, match="different content"):
        await second.add_batch(batch(
            coverage="account_complete", coverage_statement="All clients included.",
            coverage_confirmed_at=END,
        ))
    assert await first.get_batch("batch-1") == original
    assert len(rows(db_path, "account_usage_batches")) == 1
    assert len(rows(db_path, "account_usage_request_dedupe")) == 1


async def test_duplicate_request_in_another_batch_rolls_back_every_claim(repositories):
    first, second, db_path = repositories
    await seed(first)
    await first.add_batch(batch())
    with pytest.raises(ValueError, match="another account batch"):
        await second.add_batch(batch("rejected", entries=[entry("fresh"), entry()]))
    assert await first.get_batch("rejected") is None
    assert len(rows(db_path, "account_usage_request_dedupe")) == 1
    await second.add_batch(batch("accepted", entries=[entry("fresh")]))
    assert len(rows(db_path, "account_usage_request_dedupe")) == 2


async def test_same_request_id_cannot_belong_to_different_accounts(repositories):
    first, second, db_path = repositories
    await seed(first)
    await first.add_batch(batch())
    await second.save_capture(account(account_key=OTHER_KEY), [
        snapshot("other-start", account_key=OTHER_KEY),
        snapshot("other-end", account_key=OTHER_KEY, observed_at=END),
    ])
    other = batch(
        "other-batch", account_key=OTHER_KEY,
        start_snapshot_id="other-start", end_snapshot_id="other-end",
    )
    with pytest.raises(ValueError, match="another account batch"):
        await second.add_batch(other)
    assert [value.batch_id for value in await first.list_batches(ACCOUNT_KEY)] == ["batch-1"]
    assert await first.list_batches(OTHER_KEY) == []
    assert len(rows(db_path, "account_usage_request_dedupe")) == 1


async def test_capture_requires_observations_without_creating_an_account(repositories):
    first, second, _ = repositories
    with pytest.raises(ValueError, match="at least one snapshot"):
        await first.save_capture(account(), [])
    assert await second.list_accounts() == []


async def test_capture_preserves_alias_changed_since_capture_started(repositories):
    first, second, _ = repositories
    await first.upsert_account(account())
    await second.upsert_account(account(label="User renamed"))
    captured_account, captured_snapshots = await first.save_capture(account(), [snapshot()])
    assert captured_account == account(label="User renamed")
    assert captured_snapshots == [snapshot()]
    assert await second.get_account(ACCOUNT_KEY) == account(label="User renamed")


async def test_concurrent_capture_cannot_overwrite_account_rename(repositories):
    first, second, _ = repositories
    await first.upsert_account(account())
    await asyncio.gather(
        first.save_capture(account(), [snapshot()]),
        second.upsert_account(account(label="User renamed")),
    )
    assert await first.get_account(ACCOUNT_KEY) == account(label="User renamed")
    assert await second.get_snapshot("start") == snapshot()


@pytest.mark.parametrize("end_updates, batch_updates, reason", [
    ({"limit_id": "other"}, {}, "quota bucket"),
    ({"window_duration_ms": 18000000}, {}, "weekly window"),
    ({"resets_at": RESET + timedelta(days=7)}, {}, "quota reset"),
    ({"observed_at": START}, {}, "strictly increasing"),
    ({"observed_at": RESET}, {}, "inside the same reset window"),
    ({}, {"entries": [entry(occurred_at=START)]}, "request timestamps"),
    ({}, {"entries": [entry(occurred_at=END + timedelta(microseconds=1))]}, "request timestamps"),
    ({}, {"account_key": OTHER_KEY}, "same account"),
])
async def test_batch_window_validation_runs_inside_storage(
    repositories, end_updates, batch_updates, reason,
):
    first, second, db_path = repositories
    await first.save_capture(account(), [
        snapshot(), snapshot("end", **{"observed_at": END, **end_updates}),
    ])
    with pytest.raises(ValueError, match=reason):
        await second.add_batch(batch(**batch_updates))
    assert await first.list_batches(ACCOUNT_KEY) == []
    assert rows(db_path, "account_usage_request_dedupe") == []


async def test_batch_missing_snapshots_cannot_leave_request_claims(repositories):
    first, second, db_path = repositories
    await first.upsert_account(account())
    with pytest.raises(ValueError, match="both batch snapshots"):
        await first.add_batch(batch())
    assert await second.get_batch("batch-1") is None
    assert rows(db_path, "account_usage_request_dedupe") == []


async def test_concurrent_snapshot_replays_are_all_idempotent(repositories):
    first, second, db_path = repositories
    await first.upsert_account(account())
    original = snapshot()
    results = await asyncio.gather(*[
        repository.add_snapshot(original) for repository in [first, second] * 4
    ])
    assert results == [original] * 8
    assert len(rows(db_path, "account_usage_snapshots")) == 1


async def test_concurrent_conflicting_snapshots_have_one_winner(repositories):
    first, second, db_path = repositories
    await first.upsert_account(account())
    results = await asyncio.gather(
        first.add_snapshot(snapshot(used_percent="20")),
        second.add_snapshot(snapshot(used_percent="30")),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert sum(isinstance(result, PricingQuotaSnapshot) for result in results) == 1
    assert (await first.get_snapshot("start")).used_percent in {Decimal(20), Decimal(30)}
    assert len(rows(db_path, "account_usage_snapshots")) == 1


async def test_concurrent_batch_replays_are_all_idempotent(repositories):
    first, second, db_path = repositories
    await seed(first)
    original = batch()
    results = await asyncio.gather(*[
        repository.add_batch(original) for repository in [first, second] * 4
    ])
    assert results == [original] * 8
    assert len(rows(db_path, "account_usage_batches")) == 1
    assert len(rows(db_path, "account_usage_request_dedupe")) == 1


async def test_concurrent_batches_cannot_claim_the_same_request(repositories):
    first, second, db_path = repositories
    await seed(first)
    results = await asyncio.gather(
        first.add_batch(batch("contender-a")), second.add_batch(batch("contender-b")),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert sum(isinstance(result, PricingUsageBatch) for result in results) == 1
    saved = await second.list_batches(ACCOUNT_KEY)
    assert len(saved) == 1
    claims = rows(db_path, "account_usage_request_dedupe")
    assert len(claims) == 1
    assert claims[0][2] == saved[0].batch_id


async def test_distinct_database_urls_do_not_share_account_state(repositories, tmp_path):
    first, _, _ = repositories
    await first.upsert_account(account())
    other_url = f"sqlite+aiosqlite:///{tmp_path / 'other.db'}"
    other = AccountUsageRepository(other_url)
    try:
        await other.init_db()
        assert await other.list_accounts() == []
    finally:
        await engine_pool.get_engine(other_url).dispose()


async def test_concurrent_first_initialization_uses_database_arbitration(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'new-concurrent.db'}"
    first, second = AccountUsageRepository(db_url), AccountUsageRepository(db_url)
    try:
        await asyncio.gather(first.init_db(), second.init_db())
        await first.upsert_account(account())
        assert await second.get_account(ACCOUNT_KEY) == account()
    finally:
        await engine_pool.get_engine(db_url).dispose()


def plan_snapshot(quota=None, **updates):
    quota = quota or snapshot()
    return PlanUsageSnapshot.model_validate({
        "snapshot_id": quota.snapshot_id, "account_key": quota.account_key,
        "limit_id": quota.limit_id, "window_duration_ms": quota.window_duration_ms,
        "resets_at": quota.resets_at, "observed_at": quota.observed_at,
        "used_percent": int(quota.used_percent),
        "activity_observed_at": quota.observed_at, "activity_tokens": 123456,
        "activity_scope": "c" * 64, "source": "codex_account_activity",
        **updates,
    })


async def test_plan_capture_persists_across_sessions_without_changing_existing_ledger(repositories):
    first, second, db_path = repositories
    await seed(first)
    await first.add_batch(batch())
    original_quota = await second.get_snapshot("start")
    original_batch = await second.get_batch("batch-1")
    plan = plan_snapshot()
    result = await first.save_capture(account(), [snapshot()], plan_snapshots=[plan])
    assert result == (account(), [snapshot()])
    assert await second.list_plan_snapshots(ACCOUNT_KEY) == [plan]
    assert await second.list_plan_snapshots(OTHER_KEY) == []
    assert await second.get_snapshot("start") == original_quota
    assert await second.get_batch("batch-1") == original_batch
    assert len(rows(db_path, "account_usage_request_dedupe")) == 1


async def test_plan_snapshot_replays_are_immutable_and_concurrent(repositories):
    first, second, db_path = repositories
    await first.save_capture(account(), [snapshot()])
    plan = plan_snapshot()
    assert await asyncio.gather(
        first.add_plan_snapshot(plan), second.add_plan_snapshot(plan),
    ) == [plan, plan]
    with pytest.raises(ValueError, match="different content"):
        await second.add_plan_snapshot(plan_snapshot(activity_tokens=123457))
    assert await first.list_plan_snapshots(ACCOUNT_KEY) == [plan]
    assert len(rows(db_path, "account_plan_snapshots")) == 1


@pytest.mark.parametrize("updates", [
    {"limit_id": "different"}, {"window_duration_ms": 18000000},
    {"resets_at": RESET + timedelta(days=1)},
    {"observed_at": START + timedelta(seconds=1)}, {"used_percent": 11},
])
async def test_plan_metadata_must_match_its_quota_snapshot(repositories, updates):
    first, second, _ = repositories
    await first.save_capture(account(), [snapshot()])
    with pytest.raises(ValueError, match="must match the quota"):
        await first.add_plan_snapshot(plan_snapshot(**updates))
    assert await second.list_plan_snapshots(ACCOUNT_KEY) == []


async def test_plan_record_requires_an_existing_account_and_quota(repositories):
    first, second, _ = repositories
    with pytest.raises(ValueError, match="account does not exist"):
        await first.add_plan_snapshot(plan_snapshot())
    await first.upsert_account(account())
    with pytest.raises(ValueError, match="corresponding quota"):
        await first.add_plan_snapshot(plan_snapshot())
    assert await second.list_plan_snapshots(ACCOUNT_KEY) == []


async def test_mixed_account_plan_capture_is_rejected_atomically(repositories):
    first, second, _ = repositories
    other = snapshot("other", account_key=OTHER_KEY)
    with pytest.raises(ValueError, match="captured account"):
        await first.save_capture(account(), [snapshot()], plan_snapshots=[plan_snapshot(other)])
    assert await second.list_accounts() == []
    assert await second.list_snapshots(ACCOUNT_KEY) == []
    assert await second.list_plan_snapshots(ACCOUNT_KEY) == []


async def test_plan_conflict_rolls_back_new_quota_in_same_capture(repositories):
    first, second, _ = repositories
    original = plan_snapshot()
    await first.save_capture(account(), [snapshot()], plan_snapshots=[original])
    fresh = snapshot("fresh", observed_at=END)
    with pytest.raises(ValueError, match="different content"):
        await second.save_capture(
            account(), [fresh, snapshot()],
            plan_snapshots=[plan_snapshot(fresh), plan_snapshot(activity_tokens=2)],
        )
    assert await first.get_snapshot("fresh") is None
    assert await first.list_plan_snapshots(ACCOUNT_KEY) == [original]


async def test_plan_records_accept_short_window_and_missing_activity(repositories):
    first, second, _ = repositories
    quota = snapshot(window_duration_ms=18000000)
    plan = plan_snapshot(quota, activity_tokens=None, activity_observed_at=None, activity_scope=None)
    await first.save_capture(account(), [quota], plan_snapshots=[plan])
    assert await second.list_plan_snapshots(ACCOUNT_KEY) == [plan]


async def test_plan_capture_cannot_attach_an_unrelated_existing_quota(repositories):
    first, second, _ = repositories
    await first.save_capture(account(), [snapshot()])
    with pytest.raises(ValueError, match="in the capture"):
        await first.save_capture(account(), [snapshot("new")], plan_snapshots=[plan_snapshot()])
    assert await second.get_snapshot("new") is None
    assert await second.list_plan_snapshots(ACCOUNT_KEY) == []
