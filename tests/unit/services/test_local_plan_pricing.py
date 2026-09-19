"""Native per-response dollars cross real persistence without touching live data."""

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from aiteam.services import local_plan_capture as capture
from aiteam.services.plan_pricing import estimate_pricing_plan_capacity
from aiteam.services.pricing import load_catalog
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import PlanUsageSnapshot, PricingAccount, PricingQuotaSnapshot

START = datetime(2026, 9, 15, tzinfo=UTC)
KEY = "a" * 64
REAL_LOCAL_SOURCE = capture._local_source


@pytest_asyncio.fixture
async def priced(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    (root / "sessions").mkdir(parents=True)
    log = root / "sessions" / "real-format.jsonl"
    log.write_text(json.dumps({"type": "session_meta", "timestamp": START.isoformat(),
                              "payload": {"id": "one", "model_provider": "openai"}}) + "\n")
    url = f"sqlite+aiosqlite:///{tmp_path / 'prices.sqlite'}"
    repo = AccountUsageRepository(url)
    await repo.init_db()
    state = SimpleNamespace(root=root, log=log, now=START, percent=20, repo=repo, url=url,
                            generation="one", catalog=load_catalog())

    async def tokens():
        account = PricingAccount(account_key=KEY, label="fixture", created_at=START)
        quotas, plans = [], []
        for bucket, minutes in (("codex", 300), ("codex", 10080), ("bengaifox", 10080)):
            data = dict(snapshot_id=str(uuid4()), account_key=KEY, limit_id=bucket,
                        window_duration_ms=minutes * 60_000, observed_at=state.now,
                        resets_at=START + timedelta(milliseconds=minutes * 60_000),
                        used_percent=state.percent)
            quotas.append(PricingQuotaSnapshot(**data, source="codex_app_server"))
            plans.append(PlanUsageSnapshot(**data, source="codex_local_logs", activity_tokens=0,
                                           activity_scope="b" * 64, activity_binding_at=START,
                                           activity_observed_at=state.now))
        return account, quotas, plans

    monkeypatch.setattr(capture, "capture_plan_quota", tokens)
    monkeypatch.setattr(capture, "_local_source", lambda: (root, state.generation))
    monkeypatch.setattr(capture, "load_catalog", lambda: state.catalog)
    yield state
    await engine_pool.get_engine(url).dispose()


def append_response(state, model, *, input_tokens, cached=0, output=0, writes=0):
    row = {"type": "token_usage_record", "timestamp": state.now.isoformat(), "payload": {
        "response_id": str(uuid4()), "model": model, "usage": {
            "input_tokens": input_tokens, "cached_input_tokens": cached,
            "cache_write_input_tokens": writes, "output_tokens": output,
            "reasoning_output_tokens": output // 2,
        },
    }}
    with state.log.open("a") as stream:
        stream.write(json.dumps(row) + "\n")
    os.utime(state.log, (state.now.timestamp(), state.now.timestamp()))


async def save(state):
    account, quotas, plans, prices = await capture.capture_local_plan_account(repository=state.repo)
    await state.repo.save_capture(account, quotas, plan_snapshots=plans, pricing_plan_snapshots=prices)
    return prices


@pytest.mark.asyncio
async def test_mixed_models_cache_and_one_percent_predict_dollars_after_reopen(priced):
    baseline = await save(priced)
    assert all(p.activity_usd == 0 for p in baseline if p.limit_id == "codex")
    priced.now += timedelta(seconds=10)
    append_response(priced, "gpt-6-astra", input_tokens=150000, cached=100000, output=1000)
    append_response(priced, "gpt-5.6-sol", input_tokens=150000, cached=50000, writes=1000, output=2000)
    priced.percent += 1
    current = await save(priced)
    main = [p for p in current if p.limit_id == "codex"]
    # Two short requests must not be merged into one long-context request.
    assert all(p.activity_usd == Decimal("1.111") for p in main)
    assert len(main[0].pricing.entries) == 2
    assert all(item.rate_record.max_input_tokens == 272000 for item in main[0].pricing.quotes[0].items)
    await engine_pool.get_engine(priced.url).dispose()
    reopened = AccountUsageRepository(priced.url)
    history = await reopened.list_plan_price_snapshots(KEY)
    estimates = estimate_pricing_plan_capacity(history, now=priced.now)
    main_estimates = [p for p in estimates if p.limit_id == "codex"]
    assert all(p.estimated_total_usd == Decimal("111.1") for p in main_estimates)
    assert all(p.used_percent == 21 for p in main_estimates)
    assert all(p.status == "estimated" for p in main_estimates)
    # Separate Spark quota cannot use the main account's dollar counter.
    assert next(p for p in current if p.limit_id == "bengaifox").activity_usd is None


@pytest.mark.asyncio
async def test_long_context_uses_full_input_including_cache_and_whole_request_rates(priced):
    await save(priced)
    priced.now += timedelta(seconds=1)
    append_response(priced, "gpt-6-astra", input_tokens=272001, cached=270000, output=1000)
    priced.percent += 1
    result = await save(priced)
    sample = result[0]
    assert sample.activity_usd == Decimal("0.65502")
    rate = sample.pricing.quotes[0].items[0].rate_record
    assert rate.min_input_tokens == 272001
    assert rate.rates.input == Decimal("20")
    assert rate.rates.output == Decimal("75")


@pytest.mark.asyncio
async def test_spark_response_is_not_priced_into_the_main_bucket(priced):
    await save(priced)
    priced.now += timedelta(seconds=1)
    append_response(priced, "gpt-5.3-codex-spark", input_tokens=1000, output=50)
    append_response(priced, "gpt-6-astra", input_tokens=1000)
    result = await save(priced)
    assert result[0].activity_usd == Decimal("0.01")
    assert [entry.request.model for entry in result[0].pricing.entries] == ["gpt-6-astra"]


@pytest.mark.asyncio
async def test_missing_model_keeps_known_prediction_cumulative_and_original_binding(priced):
    baseline = await save(priced)
    priced.now += timedelta(seconds=1)
    append_response(priced, "gpt-6-astra", input_tokens=1000)
    append_response(priced, "future-unknown-model", input_tokens=1000)
    priced.percent += 1
    gap = await save(priced)
    assert gap[0].activity_usd is None
    assert gap[0].pricing.complete is False
    assert gap[0].pricing.quotes[0].priced_subtotal_usd == Decimal("0.01")
    estimates = estimate_pricing_plan_capacity(await priced.repo.list_plan_price_snapshots(KEY), now=priced.now)
    assert all(p.estimated_total_usd == Decimal("1") for p in estimates if p.limit_id == "codex")
    assert gap[0].prediction_activity_usd == Decimal(".01")
    priced.now += timedelta(seconds=1)
    priced.percent += 1
    append_response(priced, "gpt-6-astra", input_tokens=2000)
    rebound = await save(priced)
    assert rebound[0].activity_usd is None and rebound[0].pricing.complete
    assert rebound[0].prediction_activity_usd == Decimal(".03")
    assert rebound[0].activity_binding_at == baseline[0].activity_binding_at
    await engine_pool.get_engine(priced.url).dispose()
    history = await AccountUsageRepository(priced.url).list_plan_price_snapshots(KEY)
    estimates = estimate_pricing_plan_capacity(history, now=priced.now)
    assert all(p.estimated_total_usd == Decimal("1.5") for p in estimates if p.limit_id == "codex")


@pytest.mark.asyncio
async def test_catalog_hash_change_preserves_prediction_anchor_without_rewriting(priced):
    first = await save(priced)
    priced.now += timedelta(seconds=1)
    append_response(priced, "gpt-6-astra", input_tokens=1000)
    previous = await save(priced)
    priced.catalog = priced.catalog.model_copy(update={"version": "test-revised-catalog"})
    priced.now += timedelta(seconds=1)
    current = await save(priced)
    assert current[0].activity_usd is None
    assert current[0].prediction_activity_usd == previous[0].prediction_activity_usd == Decimal(".01")
    assert current[0].activity_scope != previous[0].activity_scope
    assert current[0].activity_binding_at == previous[0].observed_at
    history = await priced.repo.list_plan_price_snapshots(KEY)
    assert next(p for p in history if p.snapshot_id == first[0].snapshot_id) == first[0]
    assert next(p for p in history if p.snapshot_id == previous[0].snapshot_id) == previous[0]


@pytest.mark.asyncio
async def test_partial_frozen_view_prices_known_rows_and_missing_next_view_keeps_anchor(priced, monkeypatch):
    from aiteam.services import codex_local_usage

    baseline = await save(priced)
    original = codex_local_usage.read_local_usage_and_pricing

    async def partial_view(*args, **kwargs):
        delta, entries, counts = await original(*args, **kwargs)
        return delta, entries, {**counts, "partial_tail": 1}

    monkeypatch.setattr(codex_local_usage, "read_local_usage_and_pricing", partial_view)
    priced.now += timedelta(seconds=1)
    priced.percent += 1
    append_response(priced, "gpt-6-astra", input_tokens=1000)
    partial = await save(priced)
    assert partial[0].activity_usd is None and not partial[0].pricing.complete
    assert partial[0].prediction_activity_usd == Decimal(".01")
    monkeypatch.setattr(codex_local_usage, "read_local_usage_and_pricing", original)
    priced.now += timedelta(seconds=1)
    priced.percent += 1
    missing = await save(priced)
    assert missing[0].pricing is None and missing[0].activity_usd is None
    assert missing[0].prediction_activity_usd == Decimal(".01")
    assert missing[0].activity_binding_at == baseline[0].activity_binding_at
    estimates = estimate_pricing_plan_capacity(await priced.repo.list_plan_price_snapshots(KEY), now=priced.now)
    assert all(p.estimated_total_usd == Decimal(".5") for p in estimates if p.limit_id == "codex")


@pytest.mark.asyncio
async def test_source_change_during_price_scan_discards_dollars(priced, monkeypatch):
    await save(priced)
    priced.now += timedelta(seconds=1)
    from aiteam.services import codex_local_usage
    actual = codex_local_usage.read_local_usage_and_pricing

    async def changed(*args):
        result = await actual(*args)
        priced.generation = "changed"
        return result

    monkeypatch.setattr(codex_local_usage, "read_local_usage_and_pricing", changed)
    result = await save(priced)
    assert all(p.activity_usd is None and p.pricing is None for p in result)


@pytest.mark.asyncio
async def test_one_frozen_read_prevents_token_price_view_race(priced, monkeypatch):
    from aiteam.services import codex_local_usage

    await save(priced)
    combined = codex_local_usage.read_local_usage_and_pricing
    reads = 0

    async def append_after_frozen(*args):
        nonlocal reads
        reads += 1
        frozen = await combined(*args)
        if reads == 1:
            # A completed row arrives after the frozen read. Pricing must not
            # reopen the file and assign a different view to the same cutoff.
            append_response(priced, "gpt-6-astra", input_tokens=1000)
        return frozen

    async def forbidden_second_read(*args):
        raise AssertionError("capture must not scan token and price views separately")

    monkeypatch.setattr(codex_local_usage, "read_local_usage_and_pricing", append_after_frozen)
    monkeypatch.setattr(codex_local_usage, "read_local_usage_delta", forbidden_second_read)
    monkeypatch.setattr(codex_local_usage, "read_local_pricing_usage", forbidden_second_read)
    priced.now += timedelta(seconds=10)
    first = await save(priced)
    assert reads == 1
    assert first[0].activity_usd == 0 and first[0].pricing.entries == []
    token_history = await priced.repo.list_plan_snapshots(KEY)
    assert all(item.activity_tokens == 0 for item in token_history)
    priced.now += timedelta(seconds=10)
    append_response(priced, "gpt-6-astra", input_tokens=2000)
    second = await save(priced)
    assert reads == 2 and second[0].activity_usd == Decimal("0.02")
    assert sum(entry.request.input_tokens for entry in second[0].pricing.entries) == 2000
    token_history = await priced.repo.list_plan_snapshots(KEY)
    assert all(item.activity_tokens == 2000 for item in token_history if item.observed_at == priced.now)


def real_local_source(state, monkeypatch):
    """Exercise real config/stat/reader code; only the remote quota is a fixture."""
    (state.root / "auth.json").write_text("synthetic credentials, never read")
    config = state.root / "config.toml"
    config.write_text(
        'forced_login_method = "chatgpt"\nmodel_provider = "fixture"\n'
        '[model_providers.fixture]\nbase_url = "https://chatgpt.com/backend-api/codex"\n'
        'requires_openai_auth = true\nwire_api = "responses"\n'
    )
    configured_at = START - timedelta(minutes=1)
    os.utime(config, (configured_at.timestamp(), configured_at.timestamp()))
    state.log.write_text(json.dumps({"type": "session_meta", "timestamp": START.isoformat(),
                                    "payload": {"id": "one", "model_provider": "fixture"}}) + "\n")
    monkeypatch.setenv("CODEX_HOME", str(state.root))
    monkeypatch.setattr(capture, "_local_source", REAL_LOCAL_SOURCE)
    return config, configured_at


@pytest.mark.asyncio
@pytest.mark.parametrize("rewrite", ["touch", "mcp", "atomic_replace"])
async def test_config_rewrite_preserves_dollars_alias_evidence_and_binding_after_reopen(priced, monkeypatch, rewrite):
    config, configured_at = real_local_source(priced, monkeypatch)
    first = await save(priced)
    baseline_scope = first[0].activity_scope
    for index in (1, 2):
        # The real session metadata predates this rewrite. Moving the alias
        # boundary to its new mtime would wrongly exclude both new responses.
        new_time = (START + timedelta(seconds=index * 10 - 1)).timestamp()
        if rewrite == "mcp":
            config.write_text(config.read_text() + f'\n[mcp_servers.fixture{index}]\nurl = "http://localhost:1/"\n')
        elif rewrite == "atomic_replace":
            replacement = config.with_suffix(".replacement")
            replacement.write_text(config.read_text() + "\n# formatting only\n")
            replacement.replace(config)
        os.utime(config, (new_time, new_time))
        await engine_pool.get_engine(priced.url).dispose()
        priced.repo = AccountUsageRepository(priced.url)
        priced.now = START + timedelta(seconds=index * 10)
        priced.percent = 20 + index
        append_response(priced, "gpt-6-astra", input_tokens=1000)
        current = await save(priced)
        main = next(item for item in current if item.limit_id == "codex")
        assert main.activity_scope == baseline_scope
        assert main.activity_binding_at == START
        assert main.activity_usd == Decimal("0.01") * index
        history = await priced.repo.list_plan_snapshots(KEY)
        assert all(item.activity_provider_configured_at == configured_at for item in history)
        assert all(item.activity_tokens == 1000 * index for item in history if item.observed_at == priced.now)
    estimates = estimate_pricing_plan_capacity(await priced.repo.list_plan_price_snapshots(KEY), now=priced.now)
    assert all(item.estimated_total_usd == Decimal("1") for item in estimates if item.limit_id == "codex")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["route", "auth"])
async def test_real_route_or_login_change_starts_new_binding_without_backfill(priced, monkeypatch, change):
    config, _ = real_local_source(priced, monkeypatch)
    first = await save(priced)
    if change == "route":
        config.write_text(config.read_text().replace(
            "https://chatgpt.com/backend-api/codex", "https://example.invalid/v1",
        ))
        os.utime(config, (START.timestamp() + 1, START.timestamp() + 1))
    else:
        (priced.root / "auth.json").write_text("other synthetic credentials, never read")
    priced.now += timedelta(seconds=10)
    append_response(priced, "gpt-6-astra", input_tokens=1000)
    current = await save(priced)
    assert current[0].activity_scope != first[0].activity_scope
    assert current[0].activity_usd is None and current[0].pricing is None
    assert current[0].prediction_activity_usd == 0
    assert current[0].activity_binding_at == priced.now
    history = await priced.repo.list_plan_price_snapshots(KEY)
    assert next(item for item in history if item.snapshot_id == first[0].snapshot_id) == first[0]


@pytest.mark.asyncio
async def test_semantically_identical_rewrite_during_capture_still_trips_stat_fence(priced, monkeypatch):
    from aiteam.services import codex_local_usage

    config, _ = real_local_source(priced, monkeypatch)
    await save(priced)
    original = codex_local_usage.read_local_usage_and_pricing

    async def rewrite_after_read(*args, **kwargs):
        result = await original(*args, **kwargs)
        config.write_text(config.read_text() + "\n# racing rewrite\n")
        return result

    monkeypatch.setattr(codex_local_usage, "read_local_usage_and_pricing", rewrite_after_read)
    priced.now += timedelta(seconds=10)
    append_response(priced, "gpt-6-astra", input_tokens=1000)
    current = await save(priced)
    assert all(item.activity_usd is None and item.pricing is None for item in current)
    tokens = await priced.repo.list_plan_snapshots(KEY)
    assert all(item.activity_tokens is None for item in tokens if item.observed_at == priced.now)
