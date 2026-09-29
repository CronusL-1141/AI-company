"""Incremental per-window summaries of immutable plan and price snapshots.

Every derived value the account monitor needs (latest sample of a window, cycle
anchor, known cycle dollars, request de-duplication, local token run start) used to
be rebuilt from the account's entire history on each round, on the event loop: the
2026-09-29 stalls were three full-history reads of ~18K JSON rows every 30 seconds
(task 551aee38). The history is append-only and each value only depends on the
previous state and the next snapshot, so it is kept here as a small state that
advances one snapshot at a time.

One step function per kind is the single source of truth. The full-history
functions in ``plan_pricing`` / ``plan_capacity`` fold the same steps over a sorted
list, the storage layer applies them to each appended row inside the insert
transaction, and the off-loop rebuild folds them over a database snapshot. Three
paths, one arithmetic: the stored Decimal representation is identical, not merely
numerically equal.

Nothing here performs I/O.
"""

from __future__ import annotations

from collections.abc import Container, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

from aiteam.clock import parse_utc
from aiteam.services.pricing import _precision, _request_amount
from aiteam.types import PlanUsageSnapshot, PricingPlanSample, PricingPlanSnapshot, PricingUsageEntry

# The separately metered Spark bucket never contributes to the main-bucket cycle.
SPARK_MODEL = "gpt-5.3-codex-spark"

SUMMARY_VERSION = 1

WindowKey = tuple[str, int]


def window_key(snapshot: PlanUsageSnapshot | PricingPlanSnapshot) -> WindowKey:
    return snapshot.limit_id, snapshot.window_duration_ms


def order_key(snapshot: PlanUsageSnapshot | PricingPlanSnapshot) -> tuple[datetime, str]:
    """The one ordering every consumer uses: observation time, then snapshot ID."""
    return snapshot.observed_at, snapshot.snapshot_id


def pricing_sample_total(sample: PricingPlanSample) -> Decimal | None:
    """Verify persisted request prices and return only a complete interval sum."""
    sample = PricingPlanSample.model_validate(sample.model_dump(mode="python"))
    requests = {entry.request.request_id: entry.request for entry in sample.entries}
    for quote in sample.quotes:
        for item in quote.items:
            if item.status != "priced":
                continue
            request, record = requests[item.request_id], item.rate_record
            if (
                (request.cached_input_tokens and record.rates.cached_input is None)
                or (request.cache_write_input_tokens and record.rates.cache_write is None)
                or _request_amount(request, record) != item.amount_usd
            ):
                raise ValueError("persisted request amount does not match its exact rate record")
    if not sample.complete:
        return None
    amounts = [quote.total_usd for quote in sample.quotes]
    with localcontext() as context:
        context.prec = _precision(amounts)
        return sum(amounts, Decimal(0))


# ---------------------------------------------------------------- price cycles


@dataclass(frozen=True)
class PricingMeta:
    """What an estimate reports about the cycle's latest pricing evidence."""

    pricing_mode: str
    catalog_version: str
    catalog_sha256: str

    @classmethod
    def of(cls, sample: PricingPlanSample) -> PricingMeta:
        return cls(sample.pricing_mode, sample.catalog_version, sample.catalog_sha256)


@dataclass(frozen=True)
class CycleState:
    """Known main-bucket prices since a cycle anchor.

    ``baseline_*`` identify the anchor snapshot. ``total`` is the running sum with
    the exact Decimal representation the full fold produces. ``pricing`` is the
    metadata of the last sample seen in the cycle, ``reason`` the last recorded
    gap. ``priced`` counts the distinct request IDs contributing to ``total`` and
    ``assumed`` those among them whose service tier was assumed.
    """

    baseline_id: str
    baseline_observed_at: datetime
    baseline_used_percent: int
    total: Decimal
    pricing: PricingMeta | None
    reason: str | None
    assumed: int
    priced: int


@dataclass(frozen=True)
class CycleStep:
    state: CycleState
    reset: bool  # a new cycle starts at this snapshot: the de-duplication set is cleared
    counted: tuple[tuple[str, bool], ...]  # (request_id, tier_assumed) newly counted


def _tier_assumed(entry: PricingUsageEntry) -> bool:
    return entry.service_tier_source == "standard_assumption" or (
        entry.service_tier_source is None and entry.request.service_tier == "standard"
    )


def advance_cycle(
    state: CycleState | None,
    previous_used_percent: int | None,
    snapshot: PricingPlanSnapshot,
    seen: Container[str] = (),
) -> CycleStep:
    """Advance one window's cycle by the next snapshot in ``order_key`` order.

    A cycle starts at the window's first snapshot and again whenever the quota
    percentage falls below the previous observation. The anchor's own sample never
    contributes; later samples add each priced, not yet counted, post-anchor
    main-bucket request once. ``seen`` must contain every request ID already counted
    in this cycle that also appears in ``snapshot`` (callers may pass exactly that
    intersection instead of the whole set).

    Stored samples are re-verified (``pricing_sample_total``) exactly as the full
    fold does, so a corrupted row fails the same way on every path.
    """
    reset = state is None or previous_used_percent is None or snapshot.used_percent < previous_used_percent
    if reset:
        baseline_id, baseline_observed_at = snapshot.snapshot_id, snapshot.observed_at
        baseline_used_percent = snapshot.used_percent
        total, pricing, reason, assumed, priced = Decimal(0), None, None, 0, 0
        seen = ()
    else:
        baseline_id, baseline_observed_at = state.baseline_id, state.baseline_observed_at
        baseline_used_percent = state.baseline_used_percent
        total, pricing, reason = state.total, state.pricing, state.reason
        assumed, priced = state.assumed, state.priced
    counted: list[tuple[str, bool]] = []
    sample = snapshot.pricing
    if sample is None:
        reason = "pricing_unavailable"
    else:
        pricing_sample_total(sample)
        pricing = PricingMeta.of(sample)
        if not sample.complete:
            reason = "pricing_incomplete"
        if not reset:
            requests = {entry.request.request_id: entry for entry in sample.entries}
            added: set[str] = set()
            amounts = [total]
            for quote in sample.quotes:
                for item in quote.items:
                    if (item.status != "priced" or item.request_id in seen or item.request_id in added
                            or not baseline_observed_at < requests[item.request_id].occurred_at
                            or SPARK_MODEL in (item.model, item.canonical_model)):
                        continue
                    added.add(item.request_id)
                    amounts.append(item.amount_usd)
                    counted.append((item.request_id, _tier_assumed(requests[item.request_id])))
            with localcontext() as context:
                context.prec = _precision(amounts)
                total = sum(amounts, Decimal(0))
    return CycleStep(
        CycleState(
            baseline_id=baseline_id, baseline_observed_at=baseline_observed_at,
            baseline_used_percent=baseline_used_percent, total=total, pricing=pricing, reason=reason,
            assumed=assumed + sum(1 for _, flag in counted if flag), priced=priced + len(counted),
        ),
        reset,
        tuple(counted),
    )


def fold_cycle(snapshots: Iterable[PricingPlanSnapshot]) -> tuple[CycleState | None, set[str]]:
    """Fold an ordered window history; returns the final state and its counted IDs."""
    state: CycleState | None = None
    previous: PricingPlanSnapshot | None = None
    seen: set[str] = set()
    for snapshot in snapshots:
        step = advance_cycle(state, previous.used_percent if previous else None, snapshot, seen)
        if step.reset:
            seen = set()
        seen.update(request_id for request_id, _ in step.counted)
        state, previous = step.state, snapshot
    return state, seen


def anchor_matches(snapshot: PricingPlanSnapshot, anchor: Any) -> bool:
    """A manual anchor names exactly one saved observation, field for field."""
    return all(getattr(snapshot, field) == getattr(anchor, field) for field in (
        "snapshot_id", "account_key", "limit_id", "window_duration_ms", "resets_at", "observed_at", "used_percent",
    ))


# ---------------------------------------------------------------- plan runs


def is_local_sample(snapshot: PlanUsageSnapshot) -> bool:
    return (
        snapshot.source == "codex_local_logs"
        and snapshot.activity_tokens is not None
        and snapshot.activity_scope is not None
        and snapshot.activity_binding_at is not None
        and snapshot.activity_binding_at <= snapshot.observed_at
        and snapshot.activity_observed_at == snapshot.observed_at
    )


def local_run_breaks(previous: PlanUsageSnapshot, snapshot: PlanUsageSnapshot) -> bool:
    """Whether ``snapshot`` cannot continue the local token run ending at ``previous``."""
    return (
        snapshot.activity_scope != previous.activity_scope
        or snapshot.activity_binding_at != previous.activity_binding_at
        or snapshot.observed_at <= previous.observed_at
        or snapshot.activity_tokens < previous.activity_tokens
        or snapshot.used_percent < previous.used_percent
    )


def advance_run_start(
    run_start_id: str | None, previous: PlanUsageSnapshot | None, snapshot: PlanUsageSnapshot,
) -> str | None:
    """The first snapshot of the unbroken local run that ends at ``snapshot``.

    Ignores the quota window: an estimate starts its interval at the later of this
    snapshot and the first observation inside the current window.
    """
    if not is_local_sample(snapshot):
        return None
    if previous is None or not is_local_sample(previous) or run_start_id is None:
        return snapshot.snapshot_id
    if local_run_breaks(previous, snapshot):
        return snapshot.snapshot_id
    return run_start_id


# ---------------------------------------------------------------- serialisation


def _iso(value: datetime) -> str:
    return value.isoformat()


def _time(raw: str) -> datetime:
    value = parse_utc(raw)
    if value is None:
        raise ValueError("summary timestamp is missing")
    return value


def cycle_to_json(state: CycleState | None) -> dict[str, Any] | None:
    if state is None:
        return None
    return {
        "baseline_id": state.baseline_id,
        "baseline_observed_at": _iso(state.baseline_observed_at),
        "baseline_used_percent": state.baseline_used_percent,
        "total": str(state.total),
        "pricing": None if state.pricing is None else {
            "pricing_mode": state.pricing.pricing_mode,
            "catalog_version": state.pricing.catalog_version,
            "catalog_sha256": state.pricing.catalog_sha256,
        },
        "reason": state.reason,
        "assumed": state.assumed,
        "priced": state.priced,
    }


def cycle_from_json(raw: dict[str, Any] | None) -> CycleState | None:
    if raw is None:
        return None
    pricing = raw["pricing"]
    return CycleState(
        baseline_id=raw["baseline_id"],
        baseline_observed_at=_time(raw["baseline_observed_at"]),
        baseline_used_percent=int(raw["baseline_used_percent"]),
        total=Decimal(raw["total"]),
        pricing=None if pricing is None else PricingMeta(
            pricing["pricing_mode"], pricing["catalog_version"], pricing["catalog_sha256"],
        ),
        reason=raw["reason"],
        assumed=int(raw["assumed"]),
        priced=int(raw["priced"]),
    )


@dataclass
class PriceWindow:
    limit_id: str
    window_duration_ms: int
    count: int
    last_id: str
    last_observed_at: datetime
    last_used_percent: int
    auto: CycleState | None
    manual: CycleState | None = None
    # The saved anchor this window was folded against: (snapshot_id, revision).
    anchor: tuple[str, int] | None = None

    @property
    def key(self) -> WindowKey:
        return self.limit_id, self.window_duration_ms

    def to_json(self) -> dict[str, Any]:
        return {
            "limit_id": self.limit_id, "window_duration_ms": self.window_duration_ms,
            "count": self.count, "last_id": self.last_id,
            "last_observed_at": _iso(self.last_observed_at), "last_used_percent": self.last_used_percent,
            "auto": cycle_to_json(self.auto), "manual": cycle_to_json(self.manual),
            "anchor": None if self.anchor is None else {"snapshot_id": self.anchor[0], "revision": self.anchor[1]},
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> PriceWindow:
        anchor = raw["anchor"]
        return cls(
            limit_id=raw["limit_id"], window_duration_ms=int(raw["window_duration_ms"]),
            count=int(raw["count"]), last_id=raw["last_id"],
            last_observed_at=_time(raw["last_observed_at"]), last_used_percent=int(raw["last_used_percent"]),
            auto=cycle_from_json(raw["auto"]), manual=cycle_from_json(raw["manual"]),
            anchor=None if anchor is None else (anchor["snapshot_id"], int(anchor["revision"])),
        )


@dataclass
class PlanWindow:
    limit_id: str
    window_duration_ms: int
    count: int
    last_id: str
    last_observed_at: datetime
    run_start_id: str | None

    @property
    def key(self) -> WindowKey:
        return self.limit_id, self.window_duration_ms

    def to_json(self) -> dict[str, Any]:
        return {
            "limit_id": self.limit_id, "window_duration_ms": self.window_duration_ms,
            "count": self.count, "last_id": self.last_id,
            "last_observed_at": _iso(self.last_observed_at), "run_start_id": self.run_start_id,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> PlanWindow:
        return cls(
            limit_id=raw["limit_id"], window_duration_ms=int(raw["window_duration_ms"]),
            count=int(raw["count"]), last_id=raw["last_id"],
            last_observed_at=_time(raw["last_observed_at"]), run_start_id=raw["run_start_id"],
        )


@dataclass
class Summary:
    """All windows of one account and snapshot table, plus the table fingerprint.

    ``count`` and ``last_id`` are the fingerprint: the account's row count in the
    table and the ID of its latest row. Rows are immutable and never deleted, so any
    write that bypassed the incremental maintenance changes the fingerprint and the
    summary is rebuilt instead of trusted.
    """

    kind: str
    count: int
    last_id: str | None
    windows: dict[WindowKey, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "version": SUMMARY_VERSION, "kind": self.kind, "count": self.count, "last_id": self.last_id,
            "windows": [window.to_json() for _, window in sorted(self.windows.items())],
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Summary | None:
        """None for a payload of another version: it is rebuilt, never guessed at."""
        if not isinstance(raw, dict) or raw.get("version") != SUMMARY_VERSION:
            return None
        kind = raw["kind"]
        factory = PriceWindow.from_json if kind == "price" else PlanWindow.from_json
        windows = [factory(item) for item in raw["windows"]]
        return cls(kind=kind, count=int(raw["count"]), last_id=raw["last_id"],
                   windows={window.key: window for window in windows})

    @classmethod
    def empty(cls, kind: str) -> Summary:
        return cls(kind=kind, count=0, last_id=None, windows={})
