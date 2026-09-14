"""Local capture bindings cross real SQLite persistence without reading credentials."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from aiteam.services import codex_local_usage as reader
from aiteam.services import local_plan_capture as capture_module
from aiteam.services.codex_local_usage import read_local_usage_delta
from aiteam.services.plan_capacity import estimate_plan_capacity
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.models import AccountPlanSnapshotModel
from aiteam.types import PlanUsageSnapshot, PricingAccount, PricingQuotaSnapshot

KEY = "a" * 64
OTHER = "b" * 64
START = datetime(2026, 9, 14, 8, tzinfo=UTC)
MAX_SAFE = 9_007_199_254_740_991


def native_result(state):
    account = PricingAccount(account_key=state.key, label="Isolated account", created_at=START)
    quotas = []
    plans = []
    for minutes in (300, 10080):
        fields = {
            "snapshot_id": str(uuid4()), "account_key": state.key, "limit_id": "codex",
            "window_duration_ms": minutes * 60_000, "observed_at": state.now,
            "resets_at": START + (timedelta(hours=4) if minutes == 300 else timedelta(days=4)),
            "used_percent": state.percent,
        }
        quotas.append(PricingQuotaSnapshot(**fields, source="codex_app_server"))
        plans.append(PlanUsageSnapshot(
            **fields, activity_tokens=None, activity_scope=None, activity_observed_at=None,
        ))
    return account, quotas, plans


@pytest_asyncio.fixture
async def local(tmp_path, monkeypatch):
    root = tmp_path / "local-source"
    (root / "sessions").mkdir(parents=True)
    url = f"sqlite+aiosqlite:///{tmp_path / 'local-capture.sqlite'}"
    repository = AccountUsageRepository(url)
    await repository.init_db()
    state = SimpleNamespace(
        root=root, generation="generation-one", key=KEY, now=START, percent=20,
        repository=repository, url=url, delta=1_000_000, counts={}, reads=[],
    )

    async def quota_capture():
        return native_result(state)

    async def delta_capture(root, since, until):
        state.reads.append((root, since, until))
        return state.delta, dict(state.counts)

    monkeypatch.setattr(capture_module, "_local_source", lambda: (state.root, state.generation))
    monkeypatch.setattr(capture_module, "capture_plan_quota", quota_capture)
    monkeypatch.setattr(reader, "read_local_usage_delta", delta_capture)
    yield state
    await engine_pool.get_engine(url).dispose()


async def save_capture(state):
    result = await capture_module.capture_local_plan_account(repository=state.repository)
    await state.repository.save_capture(result[0], result[1], plan_snapshots=result[2])
    return result


def assert_unavailable(result):
    account, quotas, plans = result
    assert len(quotas) == len(plans) == 2
    for quota, plan in zip(quotas, plans, strict=True):
        assert quota.account_key == account.account_key == plan.account_key
        assert quota.snapshot_id == plan.snapshot_id
        assert quota.used_percent == plan.used_percent
        assert plan.source == "codex_local_logs"
        assert plan.activity_tokens is None
        assert plan.activity_scope is None
        assert plan.activity_observed_at is None
        assert plan.activity_binding_at is None


@pytest.mark.asyncio
async def test_first_binding_does_not_backfill_or_save_without_its_caller(local):
    result = await capture_module.capture_local_plan_account(repository=local.repository)
    assert local.reads == []
    assert await local.repository.list_plan_snapshots(KEY) == []
    assert await local.repository.list_snapshots(KEY) == []
    for quota, plan in zip(result[1], result[2], strict=True):
        assert plan.source == "codex_local_logs"
        assert plan.activity_tokens == 0
        assert plan.activity_binding_at == START
        assert plan.activity_observed_at == quota.observed_at == START
    await local.repository.save_capture(result[0], result[1], plan_snapshots=result[2])
    assert await local.repository.list_plan_snapshots(KEY) == sorted(result[2], key=lambda p: p.snapshot_id)


@pytest.mark.asyncio
async def test_repository_restart_restores_binding_and_exact_next_interval(local):
    await save_capture(local)
    local.now += timedelta(minutes=10)
    second = await save_capture(local)
    assert all(plan.activity_tokens == 1_000_000 for plan in second[2])
    await engine_pool.get_engine(local.url).dispose()
    local.repository = AccountUsageRepository(local.url)
    local.now += timedelta(minutes=10)
    local.delta = 500_000
    third = await save_capture(local)
    assert local.reads == [
        (local.root, START, START + timedelta(minutes=10)),
        (local.root, START + timedelta(minutes=10), START + timedelta(minutes=20)),
    ]
    assert all(plan.activity_tokens == 1_500_000 for plan in third[2])
    assert all(plan.activity_binding_at == START for plan in third[2])


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["account", "generation", "directory"])
async def test_account_or_source_change_starts_a_new_zero_binding(local, changed):
    await save_capture(local)
    local.now += timedelta(minutes=10)
    await save_capture(local)
    previous_reads = list(local.reads)
    local.now += timedelta(minutes=10)
    if changed == "account":
        local.key = OTHER
    elif changed == "generation":
        local.generation = "generation-two"
    else:
        local.root = local.root.parent / "other-source"
        local.generation = "generation-for-other-directory"
    current = await save_capture(local)
    assert local.reads == previous_reads
    assert all(plan.activity_tokens == 0 for plan in current[2])
    assert all(plan.activity_binding_at == local.now for plan in current[2])


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_on", [2, 3])
async def test_source_metadata_change_during_capture_discards_local_activity(local, monkeypatch, changed_on):
    await save_capture(local)
    local.now += timedelta(minutes=10)
    calls = 0

    def changing_source():
        nonlocal calls
        calls += 1
        generation = local.generation if calls < changed_on else "changed-mid-capture"
        return local.root, generation

    monkeypatch.setattr(capture_module, "_local_source", changing_source)
    result = await save_capture(local)
    assert_unavailable(result)
    assert len(local.reads) == (1 if changed_on == 3 else 0)


@pytest.mark.asyncio
async def test_unavailable_source_preserves_quota_without_fabricating_activity(local, monkeypatch):
    def unavailable_source():
        raise OSError("synthetic unavailable metadata")

    monkeypatch.setattr(capture_module, "_local_source", unavailable_source)
    assert_unavailable(await save_capture(local))
    assert local.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read_error", "partial_tail"])
async def test_failed_read_breaks_the_saved_segment_and_next_sample_rebinds(local, monkeypatch, failure):
    await save_capture(local)
    local.now += timedelta(minutes=10)

    async def unavailable_delta(root, since, until):
        local.reads.append((root, since, until))
        if failure == "read_error":
            raise reader.CodexLocalUsageError("unreadable")
        return 1_000_000, {"partial_tail": 1}

    monkeypatch.setattr(reader, "read_local_usage_delta", unavailable_delta)
    assert_unavailable(await save_capture(local))
    local.now += timedelta(minutes=10)
    current = await save_capture(local)
    assert len(local.reads) == 1
    assert all(plan.activity_tokens == 0 for plan in current[2])
    assert all(plan.activity_binding_at == local.now for plan in current[2])


@pytest.mark.asyncio
async def test_legacy_payload_is_read_without_rewriting_or_adopting_its_counter(local):
    account, quotas, old_plans = native_result(local)
    old_plans = [PlanUsageSnapshot.model_validate({
        **plan.model_dump(mode="python"), "activity_tokens": 99_000_000,
        "activity_scope": "f" * 64, "activity_observed_at": plan.observed_at,
    }) for plan in old_plans]
    await local.repository.save_capture(account, quotas, plan_snapshots=old_plans)
    before = {}
    async with local.repository._write_session() as session:
        for plan in old_plans:
            row = await session.get(AccountPlanSnapshotModel, plan.snapshot_id)
            payload = dict(row.payload)
            payload.pop("source")
            payload.pop("activity_binding_at")
            row.payload = payload
            before[plan.snapshot_id] = payload
    local.now += timedelta(minutes=10)
    current = await save_capture(local)
    assert local.reads == []
    assert all(plan.activity_tokens == 0 for plan in current[2])
    async with local.repository._write_session() as session:
        for identifier, payload in before.items():
            assert (await session.get(AccountPlanSnapshotModel, identifier)).payload == payload
    results = estimate_plan_capacity(old_plans, now=local.now)
    assert all(item.status == "unavailable" and item.estimated_total_tokens is None for item in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [-1, True, 1.5, "1", MAX_SAFE + 1])
async def test_invalid_reader_delta_does_not_become_a_valid_accumulated_counter(local, delta):
    await save_capture(local)
    local.now += timedelta(minutes=10)
    local.delta = 100
    await save_capture(local)
    local.now += timedelta(minutes=10)
    local.delta = delta
    assert_unavailable(await save_capture(local))


@pytest.mark.asyncio
async def test_safe_integer_overflow_preserves_quota_and_breaks_the_segment(local):
    await save_capture(local)
    local.now += timedelta(minutes=10)
    local.delta = MAX_SAFE - 1
    second = await save_capture(local)
    assert all(plan.activity_tokens == MAX_SAFE - 1 for plan in second[2])
    local.now += timedelta(minutes=10)
    local.delta = 2
    assert_unavailable(await save_capture(local))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["quota", "delta"])
async def test_cancellation_propagates_without_saving_partial_observations(local, monkeypatch, stage):
    await save_capture(local)
    before = await local.repository.list_plan_snapshots(KEY)
    local.now += timedelta(minutes=10)
    started = asyncio.Event()

    async def wait_until_cancelled(*args):
        started.set()
        await asyncio.Future()

    if stage == "quota":
        monkeypatch.setattr(capture_module, "capture_plan_quota", wait_until_cancelled)
    else:
        monkeypatch.setattr(reader, "read_local_usage_delta", wait_until_cancelled)
    pending = asyncio.create_task(save_capture(local))
    await asyncio.wait_for(started.wait(), timeout=2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert await local.repository.list_plan_snapshots(KEY) == before


def ledger_row(identifier, when, tokens):
    return {"type": "token_usage_record", "timestamp": when.isoformat(), "payload": {
        "response_id": identifier, "usage": {
            "input_tokens": tokens, "cached_input_tokens": 0,
            "cache_write_input_tokens": 0, "output_tokens": 0,
        },
    }}


@pytest.mark.asyncio
async def test_real_ledger_and_reopened_sqlite_produce_local_twenty_million_capacity(local, monkeypatch):
    monkeypatch.setattr(reader, "read_local_usage_delta", read_local_usage_delta)

    async def quota_capture():
        return native_result(local)

    monkeypatch.setattr(capture_module, "capture_plan_quota", quota_capture)
    monkeypatch.setattr(capture_module, "_local_source", lambda: (local.root, local.generation))
    log = local.root / "sessions" / "sample.jsonl"
    metadata = {"type": "session_meta", "timestamp": (START - timedelta(hours=1)).isoformat(),
                "payload": {"id": "local-ledger", "model_provider": "openai"}}
    log.write_text("\n".join(json.dumps(row) for row in [
        metadata, ledger_row("before-binding", START - timedelta(minutes=1), 50_000_000),
    ]) + "\n", encoding="utf-8")
    await save_capture(local)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(ledger_row("after-binding", START + timedelta(minutes=5), 1_000_000)) + "\n")
    await engine_pool.get_engine(local.url).dispose()
    local.repository = AccountUsageRepository(local.url)
    local.now += timedelta(minutes=10)
    local.percent += 5
    result = await save_capture(local)
    assert all(plan.activity_tokens == 1_000_000 for plan in result[2])
    reloaded = AccountUsageRepository(local.url)
    history = await reloaded.list_plan_snapshots(KEY)
    estimates = estimate_plan_capacity(history, now=local.now)
    assert len(estimates) == 2
    assert all(item.source == "codex_local_logs" and item.estimated_total_tokens == 20_000_000 for item in estimates)
    assert all(item.used_percent == 25 for item in estimates)


@pytest.mark.asyncio
async def test_real_partial_ledger_tail_invalidates_sample_and_complete_tail_cannot_backfill(local, monkeypatch):
    monkeypatch.setattr(reader, "read_local_usage_delta", read_local_usage_delta)
    await save_capture(local)
    log = local.root / "sessions" / "unfinished.jsonl"
    metadata = {"type": "session_meta", "timestamp": START.isoformat(),
                "payload": {"id": "unfinished-ledger", "model_provider": "openai"}}
    log.write_text(
        json.dumps(metadata) + "\n"
        + json.dumps(ledger_row("unfinished-response", START + timedelta(minutes=5), 1_000_000)),
        encoding="utf-8",
    )
    local.now += timedelta(minutes=10)
    assert_unavailable(await save_capture(local))
    with log.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    local.now += timedelta(minutes=10)
    rebound = await save_capture(local)
    assert all(plan.activity_tokens == 0 and plan.activity_binding_at == local.now for plan in rebound[2])


def test_source_generation_uses_metadata_without_reading_credential_contents(tmp_path, monkeypatch):
    root = tmp_path / "isolated-source"
    (root / "sessions").mkdir(parents=True)
    (root / "auth.json").write_text("synthetic auth fixture", encoding="utf-8")
    (root / "config.toml").write_text("synthetic config fixture", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(root))

    def forbidden_read(*args, **kwargs):
        raise AssertionError("source fingerprint must never read credential contents")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "read_text", forbidden_read)
        guard.setattr(Path, "read_bytes", forbidden_read)
        guard.setattr(Path, "open", forbidden_read)
        first = capture_module._local_source()
    (root / "config.toml").write_text("changed synthetic config fixture", encoding="utf-8")
    second = capture_module._local_source()
    assert first[0] == second[0] == root.resolve()
    assert first[1] != second[1]
