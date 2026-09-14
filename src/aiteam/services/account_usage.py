"""Pure account-sample pricing and explicitly conditional quota extrapolation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, localcontext

from aiteam.services.pricing import quote_requests
from aiteam.types import (
    PricingAccountEstimate,
    PricingCatalog,
    PricingQuotaSnapshot,
    PricingQuoteRequest,
    PricingUsageBatch,
)

_WEEK_DURATION_MS = 10080 * 60 * 1000
_EXTRAPOLATION_ASSUMPTION = (
    "全周比例外推要求用户确认已包含同期所有客户端用量，并假设后续模型、服务档位和用量组合与样本一致。"
    "结果仅为条件 API 等值估算，不是官方额度价值或实际账单；覆盖声明未经系统独立验证。"
)


def validate_batch_window(
    batch: PricingUsageBatch,
    start: PricingQuotaSnapshot,
    end: PricingQuotaSnapshot,
) -> None:
    """Validate snapshot identity and the exact weekly sample interval."""
    if batch.start_snapshot_id != start.snapshot_id or batch.end_snapshot_id != end.snapshot_id:
        raise ValueError("batch snapshot IDs must match the supplied snapshots")
    if batch.account_key != start.account_key or start.account_key != end.account_key:
        raise ValueError("batch and both snapshots must belong to the same account")
    if start.limit_id != end.limit_id:
        raise ValueError("snapshots must use the same quota bucket")
    if start.window_duration_ms != _WEEK_DURATION_MS or end.window_duration_ms != _WEEK_DURATION_MS:
        raise ValueError("snapshots must both describe a 10080-minute weekly window")
    if start.resets_at != end.resets_at:
        raise ValueError("snapshots must not cross a quota reset")
    if start.observed_at >= end.observed_at:
        raise ValueError("snapshot observation times must be strictly increasing")
    window_start = start.resets_at - timedelta(milliseconds=_WEEK_DURATION_MS)
    if start.observed_at < window_start or end.observed_at >= start.resets_at:
        raise ValueError("snapshot observation times must remain inside the same reset window")
    if any(not (start.observed_at < entry.occurred_at <= end.observed_at) for entry in batch.entries):
        raise ValueError("all request timestamps must be in (start.observed_at, end.observed_at]")


def estimate_batch(
    batch: PricingUsageBatch,
    start: PricingQuotaSnapshot,
    end: PricingQuotaSnapshot,
    catalog: PricingCatalog,
) -> PricingAccountEstimate:
    """Quote a sample after validating its exact account and weekly interval."""
    validate_batch_window(batch, start, end)
    quote = quote_requests(PricingQuoteRequest(requests=[entry.request for entry in batch.entries]), catalog)
    with localcontext() as context:
        context.prec = max(
            28,
            max(len(start.used_percent.as_tuple().digits), len(end.used_percent.as_tuple().digits)) + 4,
            -min(int(start.used_percent.as_tuple().exponent), int(end.used_percent.as_tuple().exponent)) + 4,
        )
        delta = end.used_percent - start.used_percent

    estimated_full_week_usd: Decimal | None = None
    if not quote.complete:
        status = "unavailable"
        explanation = "样本包含未定价请求，无法计算完整样本价格或全周等值；已定价小计仍可查看。"
    elif delta <= 0:
        status = "unavailable"
        explanation = "区间额度使用百分比没有增加，无法据此外推全周等值。"
    elif delta <= 1:
        status = "unavailable"
        explanation = "额度变化不超过 1 个百分点，百分比量化和记账延迟会放大误差，因此仅展示样本价格。"
    elif batch.coverage == "local_only":
        status = "sample_only"
        explanation = "当前仅声明本地样本，尚未确认已包含同期所有客户端用量，因此仅展示样本价格。"
    else:
        status = "conditional"
        with localcontext() as context:
            context.prec = max(
                28,
                len(quote.total_usd.as_tuple().digits) + len(delta.as_tuple().digits) + 8,
            )
            estimated_full_week_usd = Decimal(100) * quote.total_usd / delta
        explanation = "按样本 API 等值 × 100 ÷ 额度变化百分点计算，满足用户确认覆盖和价格完整条件。"

    return PricingAccountEstimate(
        batch_id=batch.batch_id, account_key=batch.account_key,
        start_snapshot_id=start.snapshot_id, end_snapshot_id=end.snapshot_id,
        coverage=batch.coverage, coverage_statement=batch.coverage_statement,
        coverage_confirmed_at=batch.coverage_confirmed_at,
        interval_start=start.observed_at, interval_end=end.observed_at,
        delta_used_percent=delta, quote=quote, estimated_full_week_usd=estimated_full_week_usd,
        status=status, reason=explanation + _EXTRAPOLATION_ASSUMPTION,
    )
