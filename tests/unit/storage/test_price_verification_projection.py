"""Latest-rate metadata is projected without a second full snapshot decode."""

from datetime import timedelta

import pytest

from aiteam.services.pricing import price_verification_signals
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import PricingPlanSnapshot

from .._plan_pricing_fixtures import BASE, KEY, account, catalog, entry, price_snapshot, quota


@pytest.mark.asyncio
async def test_projection_preserves_distinct_used_rates_aliases_and_missing_prices(tmp_path, monkeypatch):
    repository = AccountUsageRepository(f"sqlite+aiosqlite:///{tmp_path / 'projection.sqlite'}")
    await repository.init_db()
    price_catalog = catalog()
    price_catalog.verified_at = BASE
    price_catalog.records[0].verified_at = BASE - timedelta(days=31)
    price_catalog.records[1].verified_at = BASE
    value = price_snapshot(at=BASE + timedelta(seconds=1), entries=[
        entry("old", model="model-alias"),
        entry("new", model="model-alias", input_tokens=100001),
        entry("future", model="model-b"),
        entry("unknown", model="unknown"),
        entry("duplicate-rate", model="model-alias"),
    ], price_catalog=price_catalog, complete=False)
    expected = price_verification_signals(
        [item for quote in value.pricing.quotes for item in quote.items], now=BASE + timedelta(seconds=2),
    )
    try:
        await repository.save_capture(account(), [quota(value)], pricing_plan_snapshots=[value])

        async def no_full_read(*args, **kwargs):
            raise AssertionError("verification metadata must not read the full snapshot")

        monkeypatch.setattr(repository, "get_price_snapshot", no_full_read)
        projected = await repository.price_verification_for_snapshot(
            KEY, value.snapshot_id, now=BASE + timedelta(seconds=2),
        )
        assert projected == expected
        assert {signal.model for signal in projected} == {"model-alias", "model-b", "unknown"}
        assert next(signal for signal in projected if signal.model == "model-alias").status == "stale"
        assert await repository.price_verification_for_snapshot("b" * 64, value.snapshot_id, now=BASE) is None
        assert await repository.price_verification_for_snapshot(KEY, "missing", now=BASE) is None
    finally:
        await engine_pool.get_engine(repository._db_url).dispose()


@pytest.mark.asyncio
async def test_maximum_sample_multiple_windows_does_not_redecode_full_payload(tmp_path, monkeypatch):
    repository = AccountUsageRepository(f"sqlite+aiosqlite:///{tmp_path / 'maximum.sqlite'}")
    await repository.init_db()
    template = price_snapshot(at=BASE + timedelta(seconds=1), entries=[
        entry(f"max-{index}") for index in range(10000)
    ])
    assert len(template.pricing.entries) == 10000 and len(template.pricing.quotes) == 10
    snapshots = []
    try:
        for window in (18000000, 604800000):
            data = template.model_dump(mode="python")
            data.update(snapshot_id=f"max-{window}", window_duration_ms=window,
                        resets_at=BASE + timedelta(hours=2))
            value = PricingPlanSnapshot.model_validate(data)
            await repository.save_capture(account(), [quota(value)], pricing_plan_snapshots=[value])
            snapshots.append(value)

        def no_full_validation(*args, **kwargs):
            raise AssertionError("metadata projection must not validate PricingPlanSnapshot")

        monkeypatch.setattr(PricingPlanSnapshot, "model_validate", no_full_validation)
        for value in snapshots:
            signals = await repository.price_verification_for_snapshot(
                KEY, value.snapshot_id, now=BASE + timedelta(days=30),
            )
            assert len(signals) == 1 and signals[0].model == "model-a" and signals[0].status == "stale"
    finally:
        await engine_pool.get_engine(repository._db_url).dispose()
