"""Pure, versioned API-equivalent pricing without account or storage access."""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal, localcontext
from importlib.resources import files
from pathlib import Path
from typing import Any

from aiteam.types import (
    PricingCatalog,
    PricingQuoteItem,
    PricingQuoteRequest,
    PricingQuoteResponse,
    PricingRateRecord,
    PricingRequestLine,
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of silently discarding values."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate catalog JSON key: {key}")
        result[key] = value
    return result


def decode_pricing_json(content: str | bytes) -> Any:
    """Decode pricing input without silently accepting duplicate object keys."""
    return json.loads(content, object_pairs_hook=_unique_object)


def load_catalog(path: Path | None = None) -> PricingCatalog:
    """Read and validate an explicit, environment-selected, or bundled catalog."""
    if path is not None:
        content = path.read_text(encoding="utf-8")
    elif override := os.environ.get("AITEAM_PRICING_CATALOG"):
        content = Path(override).read_text(encoding="utf-8")
    else:
        content = files("aiteam").joinpath("data", "openai_pricing.json").read_text(encoding="utf-8")
    return PricingCatalog.model_validate(decode_pricing_json(content))


def catalog_digest(catalog: PricingCatalog) -> str:
    """Hash canonical JSON of the validated catalog, including source metadata."""
    serialized = json.dumps(
        catalog.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _precision(values: list[Decimal], extra_digits: int = 0) -> int:
    """Preserve all decimal places for positive sums and integer products."""
    nonzero = [value for value in values if value]
    if not nonzero:
        return 28
    return max(
        28,
        max(value.adjusted() for value in nonzero)
        - min(int(value.as_tuple().exponent) for value in nonzero)
        + extra_digits + len(str(len(values))) + 2,
    )


def _request_amount(line: PricingRequestLine, record: PricingRateRecord) -> Decimal:
    rates = record.rates
    components = [
        (line.input_tokens - line.cached_input_tokens - line.cache_write_input_tokens, rates.input),
        (line.cached_input_tokens, rates.cached_input),
        (line.cache_write_input_tokens, rates.cache_write),
        (line.output_tokens, rates.output),
    ]
    with localcontext() as context:
        context.prec = _precision(
            [rate for _, rate in components if rate is not None],
            max(len(str(tokens)) for tokens, _ in components),
        )
        amount = sum((Decimal(tokens) * rate for tokens, rate in components if rate is not None), Decimal(0))
        return amount / Decimal(1_000_000)


def quote_requests(request: PricingQuoteRequest, catalog: PricingCatalog) -> PricingQuoteResponse:
    """Price each request at its explicit tier and complete input context size.

    The catalog version is the pricing basis. Effective dates are retained as
    source metadata and are never inferred from the verification timestamp.
    """
    records: dict[str, list[PricingRateRecord]] = {}
    for record in catalog.records:
        records.setdefault(record.model, []).append(record)
    uncovered = {entry.model: entry.reason for entry in catalog.unpriced_models}
    items: list[PricingQuoteItem] = []
    missing_models: set[str] = set()
    for line in request.requests:
        canonical = line.model
        while canonical in catalog.aliases:
            canonical = catalog.aliases[canonical]
        tier = {"default": "standard", "priority": "fast"}.get(line.service_tier, line.service_tier)
        known_model = canonical in records or canonical in uncovered
        matched: PricingRateRecord | None = None
        amount: Decimal | None = None
        reason: str | None = None
        candidates = records.get(canonical, [])
        tier_candidates = [record for record in candidates if record.tier == tier]
        if not candidates:
            reason = uncovered.get(canonical, "model_not_in_catalog")
        elif not tier_candidates:
            reason = "service_tier_not_in_catalog"
        else:
            matched = next((record for record in tier_candidates if (
                record.min_input_tokens <= line.input_tokens
                and (record.max_input_tokens is None or line.input_tokens <= record.max_input_tokens)
            )), None)
            if matched is None:
                reason = "input_interval_not_in_catalog"
            elif line.cached_input_tokens and matched.rates.cached_input is None:
                reason = "cached_input_rate_not_in_catalog"
            elif line.cache_write_input_tokens and matched.rates.cache_write is None:
                reason = "cache_write_rate_not_in_catalog"
            else:
                amount = _request_amount(line, matched)
        if amount is None:
            missing_models.add(line.model)
        items.append(PricingQuoteItem(
            request_id=line.request_id, model=line.model,
            canonical_model=canonical if known_model else None,
            service_tier=line.service_tier, status="priced" if amount is not None else "unpriced",
            reason=reason, amount_usd=amount, rate_record=matched,
        ))
    amounts = [item.amount_usd for item in items if item.amount_usd is not None]
    with localcontext() as context:
        context.prec = _precision(amounts)
        subtotal = sum(amounts, Decimal(0))
    complete = len(amounts) == len(items)
    return PricingQuoteResponse(
        catalog_version=catalog.version, catalog_sha256=catalog_digest(catalog),
        verified_at=catalog.verified_at, request_count=len(items), priced_request_count=len(amounts),
        unpriced_request_count=len(items) - len(amounts), complete=complete,
        total_usd=subtotal if complete else None, priced_subtotal_usd=subtotal,
        items=items, missing_models=sorted(missing_models),
    )
