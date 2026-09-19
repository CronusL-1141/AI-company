"""Bind forward-only local activity samples to contemporaneous native quotas.

The binding identifies a local login source, not complete cross-device account
coverage. Authentication files are only stat'ed. Config is read for the bounded
provider endpoint/auth-mode mapping and its digest; full config and credentials
are never emitted or persisted. Successful snapshots use the existing source fence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path

from aiteam.clock import from_timestamp
from aiteam.services.codex_account_capture import capture_plan_quota
from aiteam.services.codex_local_usage import local_provider_mapping_digest
from aiteam.services.plan_pricing import prediction_cycle_total, pricing_sample_total
from aiteam.services.pricing import _precision, catalog_digest, load_catalog, quote_requests
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.types import (
    PlanUsageSnapshot,
    PricingAccount,
    PricingCatalog,
    PricingPlanSample,
    PricingPlanSnapshot,
    PricingQuotaSnapshot,
    PricingQuoteRequest,
    PricingUsageEntry,
)

_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _source_fence(root: Path) -> tuple[tuple[int, ...] | None, ...]:
    """File identity is a within-capture race fence, not config semantics."""
    metadata = []
    for name in ("auth.json", "config.toml"):
        path = root / name
        if path.is_symlink():
            raise OSError("local source metadata is not a regular file")
        try:
            stat = path.stat()
        except FileNotFoundError:
            metadata.append(None)
            continue
        if not path.is_file():
            raise OSError("local source metadata is not a regular file")
        metadata.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(metadata)


def _local_source() -> tuple[Path, str]:
    root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve(strict=True)
    if not root.is_dir() or not any((root / name).is_dir() for name in ("sessions", "archived_sessions")):
        raise OSError("local source is unavailable")
    fence = _source_fence(root)
    if fence[0] is None:
        raise OSError("local source generation is unavailable")
    generation = hashlib.sha256(json.dumps(
        ["local-jsonl-v3-semantic-provider", str(root), fence[0], local_provider_mapping_digest(root)],
        separators=(",", ":"),
    ).encode()).hexdigest()
    if _source_fence(root) != fence:
        raise OSError("local source changed during capture")
    return root, generation


def _sample_source() -> tuple[Path, str, tuple[tuple[int, ...] | None, ...]]:
    root, generation = _local_source()
    return root, generation, _source_fence(root)


def _local_snapshots(
    plans: list[PlanUsageSnapshot], *, total: int | None = None,
    scope: str | None = None, binding: datetime | None = None,
    provider_configured_at: datetime | None = None,
) -> list[PlanUsageSnapshot]:
    return [PlanUsageSnapshot.model_validate({
        **plan.model_dump(mode="python"), "source": "codex_local_logs",
        "activity_tokens": total, "activity_scope": scope,
        "activity_observed_at": plan.observed_at if total is not None else None,
        "activity_binding_at": binding,
        "activity_provider_configured_at": provider_configured_at,
    }) for plan in plans]


async def _capture_local_token_account(
    *, repository: AccountUsageRepository,
    read_views: dict | None = None,
) -> tuple[PricingAccount, list[PricingQuotaSnapshot], list[PlanUsageSnapshot]]:
    """Sample only local events after a persisted account/source baseline.

    No historical usage is assigned when connecting an account. Changing account,
    login metadata, effective provider mapping or source directory starts a fresh
    zero baseline. Failure breaks the segment instead of reporting fake zero.
    """
    try:
        source_before = await asyncio.to_thread(_sample_source)
    except (OSError, ValueError):
        source_before = None
    account, quotas, plans = await capture_plan_quota()
    unavailable = _local_snapshots(plans)
    try:
        source = await asyncio.to_thread(_sample_source)
        if source_before != source:
            return account, quotas, unavailable
        root, generation, fence = source
        scope = hashlib.sha256(f"{account.account_key}:{generation}".encode()).hexdigest()
        end = max(plan.observed_at for plan in plans)
        history = await repository.list_plan_snapshots(account.account_key)
        latest = max(history, key=lambda item: (item.observed_at, item.snapshot_id), default=None)
        total = 0
        binding = end
        configured_at = from_timestamp(fence[1][3] / 1_000_000_000) if fence[1] else None
        if (
            latest is not None and latest.source == "codex_local_logs"
            and latest.activity_scope == scope and latest.activity_tokens is not None
            and latest.activity_binding_at is not None
        ):
            if latest.observed_at >= end:
                return account, quotas, unavailable
            from aiteam.services.codex_local_usage import read_local_usage_and_pricing, read_local_usage_delta

            # Reuse the mapping's persisted evidence boundary, not the mtime of
            # a later identical config rewrite. Scope equality binds it to this
            # account, auth generation, directory and mapping semantics.
            configured_at = latest.activity_provider_configured_at
            options = {}
            if configured_at is not None:
                options["provider_mapping_evidence"] = (
                    await asyncio.to_thread(local_provider_mapping_digest, root), configured_at,
                )
            if read_views is None:
                delta, counts = await read_local_usage_delta(root, latest.observed_at, end, **options)
            else:
                delta, entries, counts = await read_local_usage_and_pricing(root, latest.observed_at, end, **options)
                read_views[(latest.observed_at, end)] = (entries, counts)
            if type(delta) is not int or not 0 <= delta <= _MAX_SAFE_INTEGER:
                return account, quotas, unavailable
            # An unfinished relevant row cannot be silently lost at the next
            # time boundary. Restart the segment after a complete later read.
            if counts.get("partial_tail", 0):
                return account, quotas, unavailable
            total = latest.activity_tokens + delta
            binding = latest.activity_binding_at
        if configured_at is not None and configured_at > binding:
            return account, quotas, unavailable
        if total > _MAX_SAFE_INTEGER or await asyncio.to_thread(_sample_source) != source:
            return account, quotas, unavailable
        return account, quotas, _local_snapshots(
            plans, total=total, scope=scope, binding=binding, provider_configured_at=configured_at,
        )
    except (OSError, ValueError):
        return account, quotas, unavailable


def _price_snapshot(plan: PlanUsageSnapshot, **values: object) -> PricingPlanSnapshot:
    """Bridge by quota identity without adding money to the old token schema."""
    return PricingPlanSnapshot.model_validate({
        **{name: getattr(plan, name) for name in (
            "snapshot_id", "account_key", "limit_id", "window_duration_ms",
            "resets_at", "observed_at", "used_percent",
        )},
        "source": "codex_local_logs", "activity_scope": None,
        "activity_binding_at": None, **values,
    })


def _main_bucket_entries(
    entries: list[PricingUsageEntry], catalog: PricingCatalog,
) -> list[PricingUsageEntry]:
    """The separately metered Spark model must never inflate the main bucket."""
    result = []
    for entry in entries:
        model = entry.request.model
        while model in catalog.aliases:
            model = catalog.aliases[model]
        if model != "gpt-5.3-codex-spark":
            result.append(entry)
    return result


def _price_interval(
    entries: list[PricingUsageEntry], catalog: PricingCatalog,
    start: datetime, end: datetime, *, usage_complete: bool,
) -> PricingPlanSample:
    # Choose the context band separately for every complete request. Cached
    # input participates in the threshold; session totals never select a band.
    quotes = [
        quote_requests(PricingQuoteRequest(requests=[entry.request for entry in entries[offset:offset + 1000]]),
                       catalog)
        for offset in range(0, len(entries), 1000)
    ]
    return PricingPlanSample(
        pricing_mode="standard_equivalent", catalog_version=catalog.version,
        catalog_sha256=catalog_digest(catalog), interval_start=start, interval_end=end,
        entries=entries, quotes=quotes,
        complete=usage_complete and all(quote.complete for quote in quotes),
    )


async def _capture_prices(
    plans: list[PlanUsageSnapshot], *, repository: AccountUsageRepository,
    generation: str, catalog: PricingCatalog, read_views: dict,
) -> list[PricingPlanSnapshot]:
    if not plans:
        return []
    history = await repository.list_plan_price_snapshots(plans[0].account_key)
    latest_by_window = {}
    for previous in sorted(history, key=lambda value: (value.observed_at, value.snapshot_id)):
        latest_by_window[(previous.limit_id, previous.window_duration_ms)] = previous
    intervals: dict[tuple[datetime, datetime], PricingPlanSample] = {}
    results = []
    digest = catalog_digest(catalog)
    for plan in plans:
        if plan.limit_id != "codex":
            results.append(_price_snapshot(plan))
            continue
        scope = hashlib.sha256(json.dumps([
            "local-usd-v1", plan.account_key, generation, plan.limit_id,
            "standard_equivalent", digest,
        ], separators=(",", ":")).encode()).hexdigest()
        previous = latest_by_window.get((plan.limit_id, plan.window_duration_ms))
        start = binding = plan.observed_at
        cumulative = Decimal(0)
        if previous is not None:
            if previous.observed_at >= plan.observed_at:
                results.append(_price_snapshot(plan))
                continue
            start = previous.observed_at
            if previous.activity_scope == scope and previous.activity_binding_at is not None:
                binding, cumulative = previous.activity_binding_at, previous.activity_usd
            else:
                binding = start if (start, plan.observed_at) in read_views else plan.observed_at
                cumulative = None
        key = start, plan.observed_at
        sample = None
        if key in read_views or (previous is None and plan.activity_tokens is not None):
            if key not in intervals and start == plan.observed_at:
                intervals[key] = _price_interval([], catalog, start, plan.observed_at, usage_complete=True)
            elif key not in intervals:
                entries, counts = read_views[key]
                entries = _main_bucket_entries(entries, catalog)
                intervals[key] = await asyncio.to_thread(
                    _price_interval, entries, catalog, start, plan.observed_at,
                    usage_complete=not (counts.get("pricing_incomplete", 0) or counts.get("partial_tail", 0)),
                )
            sample = intervals[key]
        amount = None
        if sample is not None and sample.complete and cumulative is not None:
            interval_amount = pricing_sample_total(sample)
            with localcontext() as context:
                context.prec = _precision([cumulative, interval_amount])
                amount = cumulative + interval_amount
        candidate = _price_snapshot(
            plan, activity_scope=scope, activity_binding_at=binding,
            activity_usd=amount, pricing=sample,
        )
        window_history = [item for item in history if (
            item.account_key == plan.account_key and item.limit_id == plan.limit_id
            and item.window_duration_ms == plan.window_duration_ms
        )]
        results.append(candidate.model_copy(update={
            "prediction_activity_usd": prediction_cycle_total([*window_history, candidate]),
        }))
    return results


async def capture_local_plan_account(
    *, repository: AccountUsageRepository,
) -> tuple[PricingAccount, list[PricingQuotaSnapshot], list[PlanUsageSnapshot], list[PricingPlanSnapshot]]:
    """Capture allowance, token facts and independent forward-only API dollars.

    The current catalog defines a standard API equivalent, not a measured
    subscription charge or a guessed Fast tier. Old token snapshots stay intact.
    The caller commits all three observations in the same source-fenced transaction.
    """
    try:
        before = await asyncio.to_thread(_sample_source)
    except (OSError, ValueError):
        before = None
    read_views: dict = {}
    account, quotas, plans = await _capture_local_token_account(repository=repository, read_views=read_views)
    unavailable = [_price_snapshot(plan) for plan in plans]
    try:
        source = await asyncio.to_thread(_sample_source)
        if source != before:
            return account, quotas, plans, unavailable
        _, generation, _ = source
        catalog = await asyncio.to_thread(load_catalog)
        prices = await _capture_prices(plans, repository=repository, generation=generation,
                                       catalog=catalog, read_views=read_views)
        if await asyncio.to_thread(_sample_source) != source:
            return account, quotas, plans, unavailable
        return account, quotas, plans, prices
    except (OSError, ValueError):
        return account, quotas, plans, unavailable
