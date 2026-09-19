#!/usr/bin/env python3
"""用量呈现面注册表 —— I12（量纲白名单）与 I13（覆盖率同屏红线）共用的真相源。

为什么要有一张手写的注册表，而不是让机检自己去猜"哪些是呈现面"：

* 猜不准。``tokens`` 这个词在 ingest、parser、repository 里到处都是，那些是**计算面**，
  与"给人看的数字"是两回事。把计算面也扫进来，机检就会变成一个天天误报的东西，而
  天天误报的机检等于没有机检。
* 更重要的是，**注册表本身就是那道闸**。四类量纲的白名单只有在"没有未申报的呈现面"
  这个前提下才是封闭的（§4.4：白名单只需确认四个合法值，新增量纲必须显式过审）。
  所以两个检查都强制双向比对：漏申报 = 红，申报了却不存在 = 也红。

规格：docs/token-attribution-v1-design.md §4.4 / §1.1 / §2.5。
本模块只有数据，没有副作用，供 check_usage_dimensions.py 与 check_usage_coverage.py 导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 量纲白名单 —— 封闭集合，新增必须显式过审（P1）
# ---------------------------------------------------------------------------
# 旧用量呈现只以 token 用量表达，不做任何跨量纲换算（不换算成金额、不换算成人力工时、
# 不换算成"相当于多少次会议"）。合法量纲穷举如下，共四类：
ALLOWED_DIMENSIONS: dict[str, str] = {
    "token": "token 数（四层分列：input / output / cache_creation / cache_read）",
    "count": "次数（派工数、工具调用数、agent 数……）",
    "duration_ms": "时长毫秒",
    "percent": "百分比",
}

# 非用量的数值字段（序号、字节游标、信任分……）不适用四类量纲，必须**逐个具名豁免**
# 并写明理由——豁免是显式的，不存在"看着不像用量所以自动跳过"这条路。
NON_USAGE = ""

# ---------------------------------------------------------------------------
# 第五类量纲的安全网（次要机制，不是白名单本身）
# ---------------------------------------------------------------------------
# 白名单的封闭性由"注册表必须完整"保证；下面这张词表只是额外一道网，用来在
# 一个**尚未登记**的文件里抓住金额/工时这类换算量纲。它是网，不是判据——判据永远
# 是上面那四个值。独立 Pricing 契约仅按具名注册表与 AST 字段边界放行，不扩展旧词表。
#
# 匹配按**标识符切词**而非子串：``isPending`` 里藏着 "spend"、``statusDone`` 里藏着
# "usd"，子串匹配一跑就是几十条假阳性，而天天误报的机检等于没有机检。
FORBIDDEN_UNIT_WORDS: frozenset[str] = frozenset({
    "cost", "price", "usd", "cny", "rmb", "dollar", "yuan", "fee",
    "billing", "spend", "spent", "budget", "quota", "money", "credit",
    "manhour", "workday", "manday", "fte",
})


@dataclass(frozen=True)
class FieldSpec:
    """一个数值字段的量纲与口径申报。

    ``dimension`` 为 :data:`NON_USAGE` 时表示"申报为非用量字段"，此时 ``note`` 必填。
    ``metric`` 只对 token 量纲有意义且必填——token 数脱离口径没有意义（§0.2）。
    """

    dimension: str
    metric: str = ""
    note: str = ""


# Pricing contracts have an independent, closed vocabulary. They do not extend
# the token presentation vocabulary or grant any frontend exemption.
PRICING_DIMENSIONS = frozenset({"token", "count", "money_usd", "usd_per_million", "percent", "duration_ms"})
PRICING_NON_NUMERIC = "non_numeric"


@dataclass(frozen=True)
class PricingSurface:
    """An independent pricing schema with every field explicitly registered."""

    model: str
    fields: dict[str, FieldSpec]


PRICING_SURFACES: tuple[PricingSurface, ...] = (
    PricingSurface("PricingRates", {
        "input": FieldSpec("usd_per_million"),
        "cached_input": FieldSpec("usd_per_million"),
        "cache_write": FieldSpec("usd_per_million"),
        "output": FieldSpec("usd_per_million"),
    }),
    PricingSurface("PricingRateRecord", {
        "model": FieldSpec(PRICING_NON_NUMERIC, note="Exact canonical model identity"),
        "tier": FieldSpec(PRICING_NON_NUMERIC, note="API service tier"),
        "min_input_tokens": FieldSpec("token", metric="usage_sum", note="Per-request interval start"),
        "max_input_tokens": FieldSpec("token", metric="usage_sum", note="Per-request interval end"),
        "rates": FieldSpec(PRICING_NON_NUMERIC, note="Registered PricingRates contract"),
        "source_url": FieldSpec(PRICING_NON_NUMERIC, note="Public pricing source"),
        "verified_at": FieldSpec(PRICING_NON_NUMERIC, note="Source verification timestamp"),
        "effective_from": FieldSpec(PRICING_NON_NUMERIC, note="Published start, unknown stays null"),
        "effective_until": FieldSpec(PRICING_NON_NUMERIC, note="Published end, unknown stays null"),
        "notes": FieldSpec(PRICING_NON_NUMERIC, note="Source and applicability limitations"),
    }),
    PricingSurface("PricingUnpricedModel", {
        "model": FieldSpec(PRICING_NON_NUMERIC, note="Exact unpriced model identity"),
        "reason": FieldSpec(PRICING_NON_NUMERIC, note="Why no price is available"),
        "source_url": FieldSpec(PRICING_NON_NUMERIC, note="Source for the stated limitation"),
    }),
    PricingSurface("PricingCatalog", {
        "schema_version": FieldSpec(PRICING_NON_NUMERIC, note="Literal schema version, not a count"),
        "version": FieldSpec(PRICING_NON_NUMERIC, note="Price catalog version"),
        "currency": FieldSpec(PRICING_NON_NUMERIC, note="Currency identity"),
        "verified_at": FieldSpec(PRICING_NON_NUMERIC, note="Catalog verification timestamp"),
        "records": FieldSpec(PRICING_NON_NUMERIC, note="Registered PricingRateRecord contracts"),
        "aliases": FieldSpec(PRICING_NON_NUMERIC, note="Exact source-supported model aliases"),
        "unpriced_models": FieldSpec(PRICING_NON_NUMERIC, note="Registered PricingUnpricedModel contracts"),
    }),
    PricingSurface("PricingRequestLine", {
        "request_id": FieldSpec(PRICING_NON_NUMERIC, note="Distinct request identity"),
        "model": FieldSpec(PRICING_NON_NUMERIC, note="Requested model identity"),
        "service_tier": FieldSpec(PRICING_NON_NUMERIC, note="Requested API service tier"),
        "input_tokens": FieldSpec("token", metric="usage_sum"),
        "output_tokens": FieldSpec("token", metric="usage_sum"),
        "cached_input_tokens": FieldSpec("token", metric="usage_sum"),
        "cache_write_input_tokens": FieldSpec("token", metric="usage_sum"),
    }),
    PricingSurface("PricingQuoteRequest", {
        "requests": FieldSpec(PRICING_NON_NUMERIC, note="Individual PricingRequestLine usage records"),
    }),
    PricingSurface("PricingQuoteItem", {
        "request_id": FieldSpec(PRICING_NON_NUMERIC, note="Request being priced"),
        "model": FieldSpec(PRICING_NON_NUMERIC, note="Original model identity"),
        "canonical_model": FieldSpec(PRICING_NON_NUMERIC, note="Resolved exact model, null if unknown"),
        "service_tier": FieldSpec(PRICING_NON_NUMERIC, note="Requested API service tier"),
        "status": FieldSpec(PRICING_NON_NUMERIC, note="Priced or unpriced state"),
        "reason": FieldSpec(PRICING_NON_NUMERIC, note="Missing-price explanation"),
        "amount_usd": FieldSpec("money_usd"),
        "rate_record": FieldSpec(PRICING_NON_NUMERIC, note="Exact PricingRateRecord applied"),
    }),
    PricingSurface("PricingQuoteResponse", {
        "basis": FieldSpec(PRICING_NON_NUMERIC, note="API-equivalent pricing basis"),
        "currency": FieldSpec(PRICING_NON_NUMERIC, note="Currency identity"),
        "catalog_version": FieldSpec(PRICING_NON_NUMERIC, note="Catalog version used"),
        "catalog_sha256": FieldSpec(PRICING_NON_NUMERIC, note="Exact catalog content identity"),
        "verified_at": FieldSpec(PRICING_NON_NUMERIC, note="Catalog verification timestamp"),
        "request_count": FieldSpec("count"),
        "priced_request_count": FieldSpec("count"),
        "unpriced_request_count": FieldSpec("count"),
        "complete": FieldSpec(PRICING_NON_NUMERIC, note="Whether every request has a price"),
        "total_usd": FieldSpec("money_usd", note="Null when the quote is incomplete"),
        "priced_subtotal_usd": FieldSpec("money_usd", note="Only requests with a known price"),
        "items": FieldSpec(PRICING_NON_NUMERIC, note="Registered PricingQuoteItem contracts"),
        "missing_models": FieldSpec(PRICING_NON_NUMERIC, note="Model identities still lacking a price"),
    }),
    PricingSurface("PricingAccount", {
        "account_key": FieldSpec(PRICING_NON_NUMERIC, note="One-way account identity digest"),
        "label": FieldSpec(PRICING_NON_NUMERIC, note="Local account display label"),
        "created_at": FieldSpec(PRICING_NON_NUMERIC, note="Account registration timestamp"),
    }),
    PricingSurface("PricingQuotaSnapshot", {
        "snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Immutable snapshot identity"),
        "account_key": FieldSpec(PRICING_NON_NUMERIC, note="Account identity digest"),
        "limit_id": FieldSpec(PRICING_NON_NUMERIC, note="Native usage bucket identity"),
        "used_percent": FieldSpec("percent"),
        "window_duration_ms": FieldSpec("duration_ms"),
        "resets_at": FieldSpec(PRICING_NON_NUMERIC, note="Usage window reset timestamp"),
        "observed_at": FieldSpec(PRICING_NON_NUMERIC, note="Snapshot observation timestamp"),
        "source": FieldSpec(PRICING_NON_NUMERIC, note="Snapshot source identity"),
    }),
    PricingSurface("PricingUsageEntry", {
        "occurred_at": FieldSpec(PRICING_NON_NUMERIC, note="Individual request usage timestamp"),
        "request": FieldSpec(PRICING_NON_NUMERIC, note="Registered PricingRequestLine contract"),
    }),
    PricingSurface("PricingUsageBatch", {
        "batch_id": FieldSpec(PRICING_NON_NUMERIC, note="Immutable request batch identity"),
        "account_key": FieldSpec(PRICING_NON_NUMERIC, note="Account identity digest"),
        "start_snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Interval start snapshot identity"),
        "end_snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Interval end snapshot identity"),
        "coverage": FieldSpec(PRICING_NON_NUMERIC, note="Local sample or user-confirmed account coverage"),
        "coverage_statement": FieldSpec(PRICING_NON_NUMERIC, note="User confirmation evidence"),
        "coverage_confirmed_at": FieldSpec(PRICING_NON_NUMERIC, note="Explicit coverage confirmation timestamp"),
        "entries": FieldSpec(PRICING_NON_NUMERIC, note="Registered PricingUsageEntry records"),
    }),
    PricingSurface("PricingAccountEstimate", {
        "batch_id": FieldSpec(PRICING_NON_NUMERIC, note="Immutable priced batch identity"),
        "account_key": FieldSpec(PRICING_NON_NUMERIC, note="Account identity digest"),
        "start_snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Interval start snapshot identity"),
        "end_snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Interval end snapshot identity"),
        "coverage": FieldSpec(PRICING_NON_NUMERIC, note="Coverage confirmation status"),
        "coverage_statement": FieldSpec(PRICING_NON_NUMERIC, note="Coverage confirmation evidence"),
        "coverage_confirmed_at": FieldSpec(PRICING_NON_NUMERIC, note="Coverage confirmation timestamp"),
        "interval_start": FieldSpec(PRICING_NON_NUMERIC, note="Sample interval start"),
        "interval_end": FieldSpec(PRICING_NON_NUMERIC, note="Sample interval end"),
        "delta_used_percent": FieldSpec("percent"),
        "quote": FieldSpec(PRICING_NON_NUMERIC, note="Registered PricingQuoteResponse contract"),
        "estimated_full_week_usd": FieldSpec("money_usd", note="Conditional equivalent, null when ineligible"),
        "status": FieldSpec(PRICING_NON_NUMERIC, note="Conditional estimate eligibility"),
        "reason": FieldSpec(PRICING_NON_NUMERIC, note="Reason an estimate is unavailable"),
    }),
    PricingSurface("PricingMonitorSettings", {
        "enabled": FieldSpec(PRICING_NON_NUMERIC, note="Saved choice; verified new accounts start prediction"),
        "interval_ms": FieldSpec("duration_ms", note="User-selected capture interval in milliseconds"),
    }),
    PricingSurface("PricingMonitorState", {
        "account_key": FieldSpec(PRICING_NON_NUMERIC, note="Monitored account identity digest"),
        "settings": FieldSpec(PRICING_NON_NUMERIC, note="Registered PricingMonitorSettings contract"),
        "revision": FieldSpec("count", note="Configuration revision used to reject stale collectors"),
        "status": FieldSpec(PRICING_NON_NUMERIC, note="Monitor lifecycle status"),
        "runtime_running": FieldSpec(PRICING_NON_NUMERIC, note="Live runner state, independent of enabled settings"),
        "last_started_at": FieldSpec(PRICING_NON_NUMERIC, note="Latest capture start timestamp"),
        "last_finished_at": FieldSpec(PRICING_NON_NUMERIC, note="Latest capture completion timestamp"),
        "next_run_at": FieldSpec(PRICING_NON_NUMERIC, note="Next due timestamp, unknown remains null"),
        "last_error": FieldSpec(PRICING_NON_NUMERIC, note="Curated capture error or pause reason"),
    }),
    PricingSurface("PricingPlanSample", {
        "pricing_mode": FieldSpec(PRICING_NON_NUMERIC, note="Explicit standard API equivalent, not observed charge"),
        "catalog_version": FieldSpec(PRICING_NON_NUMERIC, note="Immutable rate catalog version"),
        "catalog_sha256": FieldSpec(PRICING_NON_NUMERIC, note="Exact rate catalog content hash"),
        "interval_start": FieldSpec(PRICING_NON_NUMERIC, note="Exclusive request observation cutoff"),
        "interval_end": FieldSpec(PRICING_NON_NUMERIC, note="Inclusive request observation cutoff"),
        "entries": FieldSpec(PRICING_NON_NUMERIC, note="Registered per-response PricingUsageEntry records"),
        "quotes": FieldSpec(PRICING_NON_NUMERIC, note="PricingQuoteResponse records with applied rates"),
        "complete": FieldSpec(PRICING_NON_NUMERIC, note="All relevant usage is identified and priced"),
    }),
    PricingSurface("PricingPlanSnapshot", {
        "snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Immutable identity shared with native quota observation"),
        "account_key": FieldSpec(PRICING_NON_NUMERIC, note="One-way account identity digest"),
        "limit_id": FieldSpec(PRICING_NON_NUMERIC, note="Native usage bucket identity"),
        "window_duration_ms": FieldSpec("duration_ms"),
        "resets_at": FieldSpec(PRICING_NON_NUMERIC, note="Usage window reset timestamp"),
        "observed_at": FieldSpec(PRICING_NON_NUMERIC, note="Quota observation and usage cutoff"),
        "used_percent": FieldSpec("percent"),
        "source": FieldSpec(PRICING_NON_NUMERIC, note="Local native response evidence source"),
        "activity_scope": FieldSpec(PRICING_NON_NUMERIC, note="Account source bucket mode and rate hash binding"),
        "activity_binding_at": FieldSpec(PRICING_NON_NUMERIC, note="Forward-only dollar baseline timestamp"),
        "activity_usd": FieldSpec("money_usd", note="Complete cumulative equivalent within the binding"),
        "prediction_activity_usd": FieldSpec("money_usd", note="Known priced cycle sum; missing contributes zero"),
        "pricing": FieldSpec(PRICING_NON_NUMERIC, note="Independent PricingPlanSample evidence"),
    }),
    PricingSurface("PricingPlanAnchorReset", {
        "limit_id": FieldSpec(PRICING_NON_NUMERIC, note="Explicitly selected main quota bucket"),
        "window_duration_ms": FieldSpec("duration_ms"),
    }),
    PricingSurface("PricingPlanAnchor", {
        "account_key": FieldSpec(PRICING_NON_NUMERIC, note="Verified account digest"),
        "limit_id": FieldSpec(PRICING_NON_NUMERIC, note="Selected quota bucket"),
        "window_duration_ms": FieldSpec("duration_ms"),
        "snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Server-selected latest saved observation"),
        "observed_at": FieldSpec(PRICING_NON_NUMERIC, note="Saved observation time, not reset action time"),
        "used_percent": FieldSpec("percent"),
        "resets_at": FieldSpec(PRICING_NON_NUMERIC, note="Official allowance cycle boundary"),
        "reset_at": FieldSpec(PRICING_NON_NUMERIC, note="Explicit manual action time"),
        "revision": FieldSpec("count", note="Idempotent persisted anchor revision"),
    }),
    PricingSurface("PricingPlanCapacityEstimate", {
        "account_key": FieldSpec(PRICING_NON_NUMERIC, note="One-way account identity digest"),
        "limit_id": FieldSpec(PRICING_NON_NUMERIC, note="Native usage bucket identity"),
        "window_duration_ms": FieldSpec("duration_ms"),
        "resets_at": FieldSpec(PRICING_NON_NUMERIC, note="Usage window reset timestamp"),
        "observed_at": FieldSpec(PRICING_NON_NUMERIC, note="Latest quota observation timestamp"),
        "used_percent": FieldSpec("percent"),
        "estimated_total_usd": FieldSpec("money_usd", note="Conditional sample-equivalent full window capacity"),
        "prediction_basis": FieldSpec(PRICING_NON_NUMERIC, note="Explicit fixed-cycle missing-zero assumption"),
        "delta_usd": FieldSpec("money_usd", note="Complete priced usage over exactly the percentage interval"),
        "last_estimated_total_usd": FieldSpec("money_usd", note="Latest valid same-window same-basis estimate"),
        "last_estimate_observed_at": FieldSpec(PRICING_NON_NUMERIC, note="Latest valid estimate timestamp"),
        "delta_used_percent": FieldSpec("percent"),
        "start_snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Aligned interval baseline identity"),
        "end_snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Aligned interval latest identity"),
        "interval_start": FieldSpec(PRICING_NON_NUMERIC, note="Aligned interval baseline timestamp"),
        "status": FieldSpec(PRICING_NON_NUMERIC, note="Estimate eligibility"),
        "source": FieldSpec(PRICING_NON_NUMERIC, note="Native local response source"),
        "pricing_mode": FieldSpec(PRICING_NON_NUMERIC, note="Explicit API equivalent assumption"),
        "catalog_version": FieldSpec(PRICING_NON_NUMERIC, note="Rate version used throughout this binding"),
        "catalog_sha256": FieldSpec(PRICING_NON_NUMERIC, note="Content hash used throughout this binding"),
        "reason_code": FieldSpec(PRICING_NON_NUMERIC, note="Unavailable or incomplete evidence reason"),
    }),
)


@dataclass(frozen=True)
class PricingFrontendSurface:
    """Exact frontend path, identifiers and complete shared schema declarations."""

    path: str
    identifiers: dict[str, FieldSpec]
    interfaces: tuple[str, ...] = ()
    local_interfaces: dict[str, dict[str, FieldSpec]] = field(default_factory=dict)


# Account presentation is separate from the historical token dashboard.
PRICING_FRONTEND_SURFACES: tuple[PricingFrontendSurface, ...] = (
    PricingFrontendSurface(
        "dashboard/src/api/accountUsage.ts",
        identifiers={
            "PricingAccount": FieldSpec(PRICING_NON_NUMERIC, note="Shared account schema"),
            "PricingQuotaSnapshot": FieldSpec(PRICING_NON_NUMERIC, note="Shared snapshot schema"),
            "PricingRequestLine": FieldSpec(PRICING_NON_NUMERIC, note="Shared request schema"),
            "PricingUsageEntry": FieldSpec(PRICING_NON_NUMERIC, note="Shared timed request schema"),
            "PricingUsageBatch": FieldSpec(PRICING_NON_NUMERIC, note="Shared immutable batch schema"),
            "PricingRates": FieldSpec(PRICING_NON_NUMERIC, note="Shared per-million rates schema"),
            "PricingRateRecord": FieldSpec(PRICING_NON_NUMERIC, note="Shared rate provenance schema"),
            "PricingQuoteItem": FieldSpec(PRICING_NON_NUMERIC, note="Shared quote item schema"),
            "PricingQuoteResponse": FieldSpec(PRICING_NON_NUMERIC, note="Shared quote schema"),
            "PricingAccountEstimate": FieldSpec(PRICING_NON_NUMERIC, note="Shared conditional estimate schema"),
            "PricingMonitorSettings": FieldSpec(PRICING_NON_NUMERIC, note="Shared monitor settings schema"),
            "PricingMonitorState": FieldSpec(PRICING_NON_NUMERIC, note="Shared monitor state schema"),
            "input_tokens": FieldSpec("token", metric="usage_sum"),
            "output_tokens": FieldSpec("token", metric="usage_sum"),
            "cached_input_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_write_input_tokens": FieldSpec("token", metric="usage_sum"),
            "min_input_tokens": FieldSpec("token", metric="usage_sum"),
            "max_input_tokens": FieldSpec("token", metric="usage_sum"),
            "amount_usd": FieldSpec("money_usd"),
            "total_usd": FieldSpec("money_usd"),
            "priced_subtotal_usd": FieldSpec("money_usd"),
            "estimated_full_week_usd": FieldSpec("money_usd"),
            "USD": FieldSpec(PRICING_NON_NUMERIC, note="Literal currency identity"),
        },
        interfaces=(
            "PricingAccount", "PricingQuotaSnapshot", "PricingRequestLine", "PricingUsageEntry",
            "PricingUsageBatch", "PricingRates", "PricingRateRecord", "PricingQuoteItem",
            "PricingQuoteResponse", "PricingAccountEstimate",
            "PricingMonitorSettings", "PricingMonitorState",
        ),
        local_interfaces={"AccountUsageDetail": {
            "account": FieldSpec(PRICING_NON_NUMERIC, note="Account response wrapper"),
            "snapshots": FieldSpec(PRICING_NON_NUMERIC, note="Snapshot response wrapper"),
            "estimates": FieldSpec(PRICING_NON_NUMERIC, note="Estimate response wrapper"),
        }},
    ),
    PricingFrontendSurface(
        "dashboard/src/lib/account-usage.ts",
        identifiers={
            "QuotaTrend": FieldSpec(PRICING_NON_NUMERIC, note="Local percentage and time trend result"),
            "quotaTrends": FieldSpec(PRICING_NON_NUMERIC, note="Derive percentage and time trends without money"),
            "PricingQuotaSnapshot": FieldSpec(PRICING_NON_NUMERIC, note="Snapshot pair validation"),
            "PricingUsageBatch": FieldSpec(PRICING_NON_NUMERIC, note="Immutable batch construction"),
            "PricingUsageEntry": FieldSpec(PRICING_NON_NUMERIC, note="Timed request validation"),
            "input_tokens": FieldSpec("token", metric="usage_sum"),
            "output_tokens": FieldSpec("token", metric="usage_sum"),
            "cached_input_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_write_input_tokens": FieldSpec("token", metric="usage_sum"),
        },
        local_interfaces={
            "QuotaTrend": {
                "account_key": FieldSpec(PRICING_NON_NUMERIC, note="Trend account identity"),
                "limit_id": FieldSpec(PRICING_NON_NUMERIC, note="Trend quota bucket identity"),
                "resets_at": FieldSpec(PRICING_NON_NUMERIC, note="Trend reset timestamp"),
                "start_snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Trend start snapshot identity"),
                "end_snapshot_id": FieldSpec(PRICING_NON_NUMERIC, note="Trend end snapshot identity"),
                "interval_start": FieldSpec(PRICING_NON_NUMERIC, note="Trend sample interval start"),
                "interval_end": FieldSpec(PRICING_NON_NUMERIC, note="Trend sample interval end"),
                "window_duration_ms": FieldSpec("duration_ms"),
                "interval_ms": FieldSpec("duration_ms"),
                "delta_used_percent": FieldSpec("percent"),
                "estimated_exhaustion_at": FieldSpec(PRICING_NON_NUMERIC, note="Conditional exhaustion timestamp"),
                "status": FieldSpec(PRICING_NON_NUMERIC, note="Conditional trend eligibility"),
            },
            "AccountImportDraft": {
                "accountKey": FieldSpec(PRICING_NON_NUMERIC, note="Selected account identity"),
                "startId": FieldSpec(PRICING_NON_NUMERIC, note="Selected start snapshot identity"),
                "endId": FieldSpec(PRICING_NON_NUMERIC, note="Selected end snapshot identity"),
                "raw": FieldSpec(PRICING_NON_NUMERIC, note="Untrusted JSON input text"),
                "statement": FieldSpec(PRICING_NON_NUMERIC, note="User confirmation statement"),
                "confirmationKey": FieldSpec(PRICING_NON_NUMERIC, note="Bound confirmation identity"),
                "confirmedAt": FieldSpec(PRICING_NON_NUMERIC, note="Confirmation timestamp"),
            },
            "AccountImportAction": {
                "type": FieldSpec(PRICING_NON_NUMERIC, note="Reducer action identity"),
                "field": FieldSpec(PRICING_NON_NUMERIC, note="Edited draft field identity"),
                "value": FieldSpec(PRICING_NON_NUMERIC, note="Edited input text"),
                "key": FieldSpec(PRICING_NON_NUMERIC, note="Confirmation identity"),
                "at": FieldSpec(PRICING_NON_NUMERIC, note="Confirmation timestamp"),
            },
        },
    ),
    PricingFrontendSurface(
        "dashboard/src/pages/AccountUsagePage.tsx",
        identifiers={
            "PricingAccount": FieldSpec(PRICING_NON_NUMERIC, note="Account identity used by monitor settings"),
            "PricingPlanCapacityPanel": FieldSpec(PRICING_NON_NUMERIC, note="Independent monetary plan panel"),
        },
    ),
    PricingFrontendSurface(
        "dashboard/src/api/pricingPlanUsage.ts",
        identifiers={
            "PricingPlanCapacityEstimate": FieldSpec(PRICING_NON_NUMERIC, note="Registered independent plan estimate"),
            "PricingPlanAnchorReset": FieldSpec(PRICING_NON_NUMERIC, note="Registered window reset request"),
            "PricingPlanAnchor": FieldSpec(PRICING_NON_NUMERIC, note="Registered saved reset boundary"),
            "prediction_basis": FieldSpec(PRICING_NON_NUMERIC, note="Explicit fixed-cycle prediction contract"),
            "estimated_total_usd": FieldSpec("money_usd"),
            "delta_usd": FieldSpec("money_usd"),
            "last_estimated_total_usd": FieldSpec("money_usd"),
            "last_estimate_observed_at": FieldSpec(PRICING_NON_NUMERIC, note="Latest valid estimate timestamp"),
        },
        interfaces=("PricingPlanCapacityEstimate", "PricingPlanAnchorReset", "PricingPlanAnchor"),
        local_interfaces={"AccountPlanPricingDetail": {
            "pricing_plan_estimates": FieldSpec(PRICING_NON_NUMERIC, note="Independent monetary window estimates"),
            "plan_estimates": FieldSpec(PRICING_NON_NUMERIC, note="Legacy percentage fallback, not money"),
        }},
    ),
    PricingFrontendSurface(
        "dashboard/src/components/usage/PricingPlanCapacityPanel.tsx",
        identifiers={
            "PricingPlanCapacityPanel": FieldSpec(PRICING_NON_NUMERIC, note="Independent monetary plan panel"),
            "PricingPlanCapacityEstimate": FieldSpec(PRICING_NON_NUMERIC, note="Registered reset eligibility input"),
            "PricingPlanAnchorResetButton": FieldSpec(PRICING_NON_NUMERIC, note="Explicit window reset action"),
            "prediction_basis": FieldSpec(PRICING_NON_NUMERIC, note="Explicit fixed-cycle prediction contract"),
            "formatPlanUsd": FieldSpec(PRICING_NON_NUMERIC, note="Decimal formatting, not a pricing calculator"),
            "estimated_total_usd": FieldSpec("money_usd"),
            "delta_usd": FieldSpec("money_usd"),
            "last_estimated_total_usd": FieldSpec("money_usd"),
            "last_estimate_observed_at": FieldSpec(PRICING_NON_NUMERIC, note="Latest valid estimate timestamp"),
            "planDollarCapacity": FieldSpec(PRICING_NON_NUMERIC, note="Registered monetary capacity label"),
            "money_usd": FieldSpec(PRICING_NON_NUMERIC, note="Explicit display dimension label"),
        },
    ),
)

# Existing translation files retain their old guards; only these exact message
# properties can carry the explicitly registered account-page vocabulary.
PRICING_I18N_FIELDS: dict[str, dict[str, dict[str, FieldSpec]]] = {
    "dashboard/src/i18n/en.ts": {
        key: {word: FieldSpec(PRICING_NON_NUMERIC, note="Account page translation word") for word in words}
        for key, words in {
            "accountUsage.subtitle": ("quota", "costs"),
            "accountUsage.captured": ("quota",),
            "accountUsage.captureNotice": ("quota",),
            "accountUsage.noAccounts": ("quota",),
            "accountUsage.snapshots": ("quota",),
            "accountUsage.snapshotNotice": ("Quota", "quota"),
            "accountUsage.bucket": ("Quota",),
            "accountUsage.importNotice": ("quota", "costs"),
            "accountUsage.startSnapshot": ("quota",),
            "accountUsage.endSnapshot": ("quota",),
            "accountUsage.sampleCost": ("sampleCost", "cost"),
            "accountUsage.delta": ("Quota",),
            "accountUsage.priceSource": ("priceSource", "Price"),
            "accountUsage.verifiedAt": ("Prices",),
            "accountUsage.extrapolation": ("quota",),
            "accountUsage.conditionalNotice": ("quota", "costs"),
            "accountUsage.conditionalRequirement": ("quota", "costs"),
            "accountUsage.notBill": ("quota",),
            "accountUsage.monitorNoCosts": ("monitorNoCosts", "quota", "Costs"),
            "accountUsage.trendTitle": ("Quota",),
            "accountUsage.trendNotice": ("quota",),
            "accountUsage.trendResetFirst": ("quota",),
            "accountUsage.planDollarCapacity": ("planDollarCapacity", "USD"),
        }.items()
    },
    "dashboard/src/i18n/zh.ts": {
        "accountUsage.planDollarCapacity": {
            "planDollarCapacity": FieldSpec(PRICING_NON_NUMERIC, note="Independent monetary plan capacity label"),
        },
        "accountUsage.sampleCost": {"sampleCost": FieldSpec(PRICING_NON_NUMERIC, note="Sample equivalent label")},
        "accountUsage.priceSource": {"priceSource": FieldSpec(PRICING_NON_NUMERIC, note="Price provenance label")},
        "accountUsage.monitorNoCosts": {
            "monitorNoCosts": FieldSpec(PRICING_NON_NUMERIC, note="Monitor scope limitation label"),
        },
    },
}
for _language in ("en", "zh"):
    for _message in ("jsonExample", "jsonHelp"):
        PRICING_I18N_FIELDS[f"dashboard/src/i18n/{_language}.ts"][f"accountUsage.{_message}"] = {
            name: FieldSpec("token", metric="usage_sum")
            for name in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens")
        }


@dataclass(frozen=True)
class PySurface:
    """API schema 侧的一个呈现面（``aiteam.types`` 里的一个 Pydantic 模型）。

    ``kind``:

    * ``row``：一行一条事实的记录面。覆盖率在这里的正确形态是 **no-data 可与 zero
      区分**（列可为 None）。
    * ``aggregate``：跨行聚合面。数值必与分母、未归因同层返回，否则就是"局部冒充
      全貌"（§2.5 / §4.4 红线）。
    """

    model: str
    kind: str
    fields: dict[str, FieldSpec]
    # row 面上"0 兼表未采集"的已知缺口：字段 -> 理由（含何时以何种方式收口）。
    # 申报不是豁免——每次机检都会把它打印出来，让缺口保持可见。
    coverage_gap: dict[str, str] = field(default_factory=dict)
    # aggregate 面必须具备的同层覆盖率字段（分子分母 + 未归因分类）。
    coverage_fields: tuple[str, ...] = ()


# Original attribution surfaces plus independent native activity facts.
PY_SURFACES: tuple[PySurface, ...] = (
    PySurface(
        # 唯一的聚合面。它与三个 row 面的根本区别：row 面一行一条事实，聚合面把多行
        # 揉成一个数——而"揉"这个动作正是局部冒充全貌的发生现场。所以这里的守卫比
        # row 面严：必须与分母、未归因分类同层返回（AGGREGATE_REQUIRED_FIELDS），
        # 且**不允许申报 coverage_gap**——归因数字的分母没有例外。
        model="TokenAttribution",
        kind="aggregate",
        fields={
            # 四层恒为 usage_sum，且这不是"暂时如此"：ctx_last 在结构上进不来。
            # workflow_agents.tokens 在 ingest 时就把四字段加成了一个数，四层分解
            # 从未被保存过，而本结构强制四层分列、刻意无合计字段——要把 ctx_last
            # 塞进来只能挑一层硬塞或凭空造四层。所以 ctx_last 侧只报覆盖率
            # （UsageCoverageRow，零 token 字段），metric 字段仍必填以钉死口径。
            "input_tokens": FieldSpec("token", metric="usage_sum"),
            "output_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_creation_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_read_tokens": FieldSpec("token", metric="usage_sum"),
            "dispatches_attributed": FieldSpec("count", note="分子：本 scope 内已测到用量的派工数"),
            "dispatches_total": FieldSpec("count", note="分母：本 scope 内的派工总数（含没数据的行）"),
            # 原因码 -> 派工数的映射。申报为 count 不是将就：这里要记的正是"它的值与
            # 分母同单位"——分子加上这个 dict 的各项之和必须等于分母（契约测试钉住）。
            # 工具调用级的 by_design 因此进不来：52,119 条活动与派工不是同一个单位。
            "unattributed_reasons": FieldSpec("count", note="未归因派工按原因码分类计数，值与分母同单位"),
        },
        coverage_fields=("dispatches_attributed", "dispatches_total", "unattributed_reasons"),
    ),
    PySurface(
        model="Agent",
        kind="row",
        fields={
            "trust_score": FieldSpec(NON_USAGE, note="信任分 0~1，agent 治理域，与用量无关"),
            "ctx_tokens": FieldSpec("token", metric="ctx_watermark"),
            "ctx_window": FieldSpec("token", metric="ctx_watermark"),
            "ctx_pct": FieldSpec("percent", metric="ctx_watermark"),
            "input_tokens": FieldSpec("token", metric="usage_sum"),
            "output_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_creation_tokens": FieldSpec("token", metric="usage_sum"),
            "cache_read_tokens": FieldSpec("token", metric="usage_sum"),
            # 子集层（types.TOKEN_SUBSET_LAYERS）：已经计在 output_tokens 里，量纲
            # 与口径都与四层相同，但**永不参与求和**。登记它是为了让"多出来的这一列
            # 是什么"有一个书面答案——不登记的话 I12 会红，而红了之后最省事的修法
            # 恰好是把它当第五层加进去。
            "reasoning_output_tokens": FieldSpec("token", metric="usage_sum"),
        },
    ),
    PySurface(
        model="WorkflowRun",
        kind="row",
        fields={
            "planned_agent_count": FieldSpec("count"),
            "dynamic_nodes": FieldSpec("count"),
            "agent_count": FieldSpec("count"),
            "total_tokens": FieldSpec("token", metric="ctx_last"),
            "total_tool_calls": FieldSpec("count"),
            "duration_ms": FieldSpec("duration_ms"),
            "journal_offset": FieldSpec(NON_USAGE, note="journal.jsonl 已消费字节水位，是内部游标不是呈现量纲"),
            "live_tokens": FieldSpec("token", metric="ctx_last"),
        },
        coverage_gap={
            "total_tokens": "非 Optional，0 兼表'未采到'与'真的 0'（ctx_last 侧历史遗产）。"
                            "阶段 0 只正名不改数据；未归因的如实呈现由阶段 2 的覆盖率结构 + 阶段 5 的未归因抽屉承担。",
        },
    ),
    PySurface(
        model="WorkflowAgent",
        kind="row",
        fields={
            "phase_index": FieldSpec(NON_USAGE, note="阶段序号，不是数量"),
            "tokens": FieldSpec("token", metric="ctx_last"),
            "tool_calls": FieldSpec("count"),
            "duration_ms": FieldSpec("duration_ms"),
        },
        coverage_gap={
            "tokens": "非 Optional，0 兼表'未采到'与'真的 0'（实测 3,182 行里 273 行为 0）。"
                      "同上：阶段 0 不改数据，未归因由阶段 2/5 如实呈现。",
        },
    ),
    PySurface(
        model="PlanUsageSnapshot",
        kind="row",
        fields={
            "window_duration_ms": FieldSpec("duration_ms"),
            "used_percent": FieldSpec("percent"),
            "activity_tokens": FieldSpec("token", metric="native_activity", note="Provider counter at one observation"),
        },
    ),
    PySurface(
        model="PlanCapacityEstimate",
        kind="row",
        fields={
            "window_duration_ms": FieldSpec("duration_ms"),
            "used_percent": FieldSpec("percent"),
            "estimated_total_tokens": FieldSpec(
                "token", metric="native_activity", note="Conditional capacity estimate from one paired interval",
            ),
            "delta_tokens": FieldSpec(
                "token", metric="native_activity", note="Measured counter delta for that interval",
            ),
            "delta_used_percent": FieldSpec("percent"),
        },
    ),
    PySurface(
        model="CodexUsageTokens",
        kind="row",
        fields={name: FieldSpec("token", metric="native_activity", note="Raw native counter, not additive billing")
                for name in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                             "output_tokens", "reasoning_output_tokens", "total_tokens")},
    ),
    PySurface(
        model="CodexUsageObservation",
        kind="row",
        fields={
            "generation": FieldSpec(NON_USAGE, note="File generation identity, not resource consumption"),
            "byte_start": FieldSpec(NON_USAGE, note="Internal file cursor offset, not a usage dimension"),
            "byte_end": FieldSpec(NON_USAGE, note="Internal file cursor offset, not a usage dimension"),
        },
    ),
)

# ---------------------------------------------------------------------------
# 非数值观测列 —— 刻意不进 PySurface.fields
# ---------------------------------------------------------------------------
# I12 的双向比对只覆盖**数值**字段（check_usage_dimensions._numeric_fields 只收
# int/float），所以字符串类的观测列进 fields 会当场被判成"注册表申报了不存在的
# 字段"——实测过，是一条假红。但它们也不能就这么无声地加上去：一个没人申报过的
# 新列，与一个被删掉却忘了清注册表的旧列，在事后看是同一副样子。
#
# 于是给它们一张自己的表：这里只记"这一列是什么、为什么不是量纲"，由
# tests/unit/test_usage_metric_invariants.py 断言它与 types.py 实际字段一一对应。
# 申报是显式的，代价只是一行字——不存在"看着不像用量所以自动跳过"这条路。
NON_NUMERIC_OBSERVATION_COLUMNS: dict[str, str] = {
    "CodexUsageObservation.usage": "单响应原生计数对象；数值在 CodexUsageTokens 登记，不在此求和。",
    "CodexUsageObservation.total_token_usage": "原生累计计数对象；是已登记结构，不是额外可加总层。",
    "CodexUsageObservation.last_token_usage": "原生末次计数对象；是已登记结构，不与累计计数相加。",
    "Agent.harness": "承载会话的宿主 CLI（HarnessId）。维度标签，不是数值；"
                     "NULL = 未标注，不等于 claude-code。",
    "Agent.harness_version": "承载内核版本字符串。只取 rollout / state 库的同名字段，"
                             "刻意不从 `codex --version` 取（那报的是另一个二进制）。",
    "Agent.dispatch_call_id": "派工调用 id，把子 agent 行接回是哪次派工叫起来的。"
                              "身份键不是数量；刻意无 UNIQUE（来源链可失落亦可重名）。",
    "UsageCoverageRow.harness": "这一行覆盖率属于哪个 harness。分列维度，不是数值；"
                                "两个 harness 的分子分母禁止相加。",
    "AgentActivity.turn_id": "轮次身份。CC 载荷无此概念故 CC 行恒 NULL；"
                             "Codex 侧据此区分主轮与挂不上主轮的工具调用。",
}

# ---------------------------------------------------------------------------
# 前端呈现面
# ---------------------------------------------------------------------------
# 前端没有类型内省可用，只能按文本扫。规则同样双向：dashboard/src 下任何出现 token
# 标识符的文件都必须在这张表里；表里列了却不存在的文件也是红（改名/删文件后注册表
# 腐烂，会让机检的覆盖面无声缩水）。
FRONTEND_SURFACES: tuple[str, ...] = (
    "dashboard/src/api/planUsage.ts",
    "dashboard/src/api/projects.ts",
    # 阶段 5：/usage 页的取数层与四个呈现组件。四层用量只出现在这几个文件里，
    # 且每一处都与分母同屏（I13 的前端臂逐文件核对）。
    "dashboard/src/api/usage.ts",
    "dashboard/src/api/workflows.ts",
    "dashboard/src/components/shared/ContextWatermarkBar.tsx",
    "dashboard/src/components/usage/AttributionCard.tsx",
    "dashboard/src/components/usage/AttributionDrill.tsx",
    "dashboard/src/components/usage/SingleProbeCard.tsx",
    "dashboard/src/components/usage/PlanCapacityPanel.tsx",
    "dashboard/src/i18n/en.ts",
    "dashboard/src/i18n/zh.ts",
    "dashboard/src/pages/AgentLivePage.tsx",
    "dashboard/src/pages/ProjectDetailPage.tsx",
    "dashboard/src/pages/TeamDetailPage.tsx",
    "dashboard/src/pages/WorkflowsPage.tsx",
    "dashboard/src/types/index.ts",
)

# 前端允许出现的 token 标识符（含展示用的 i18n 键与字面量）→ 量纲。
# 未登记的标识符即红：新加一个 token 数值到页面上，必须在这里说清它是什么量纲。
FRONTEND_IDENTIFIERS: dict[str, str] = {
    "tokens": "token",
    "Tokens": "token",
    "ctx_tokens": "token",
    "total_tokens": "token",
    "totalTokens": "token",
    "live_tokens": "token",
    "colTokens": "token",
    "fmtTokens": "token",
    # 阶段 5 新增。四层分列是硬要求，所以四个层名各自具名登记 —— 没有一个
    # "总量"标识符可登记，因为呈现面上不存在这样一个字段（§1.2）。
    "input_tokens": "token",
    "output_tokens": "token",
    "cache_creation_tokens": "token",
    "cache_read_tokens": "token",
    "formatTokenCount": "token",
    # Native activity capacity is a separate metric, not a request usage sum.
    "estimated_total_tokens": "token",
    "delta_tokens": "token",
    "formatPlanTokens": "token",
    # 展示文案里的量纲词本身。i18n 文件是呈现面，"token"这个词出现在给人看的
    # 句子里时，它指的就是 token 量纲 —— 登记它，第五类量纲词表才拦得住
    # "折算成金额/工时"那类改写（那种改写恰恰会先出现在文案里）。
    "token": "token",
    "Token": "token",
}

# ---------------------------------------------------------------------------
# 覆盖率同屏（I13）用的标记词
# ---------------------------------------------------------------------------
# "任何呈现面上的 token 数值，若其所在 scope 的 C_measure < 100%，必须在同屏同级
# 显示未归因部分"（§4.4 红线）。机检认得的"同屏未归因标注"就是下面这几个词——
# 出现其一即视为该呈现面把未归因这件事说出来了。
COVERAGE_MARKERS: tuple[str, ...] = (
    "dispatches_total", "dispatches_attributed", "unattributed", "coverage",
    "未归因", "覆盖率",
)

# aggregate 面必须同层具备的两样东西：分母，以及未归因的分类计数（§2.5 的
# TokenAttribution 结构）。少任何一样，数值就能脱离分母被单独渲染。
AGGREGATE_REQUIRED_FIELDS: tuple[str, ...] = ("dispatches_total", "unattributed_reasons")

# 阶段 5 已把口径徽标铺满：WorkflowsPage 的三处 ctx_last、ContextWatermarkBar 的
# ctx_watermark（AgentLive / ProjectDetail / TeamDetail 三页共用）、/usage 页各处的
# usage_sum，前端已无裸 token 数值。
#
# 但**剩一个真缺口**，如实登记在这里、每次机检打印一次，不让它变成默认状态：
# WorkflowsPage 展示的 `total_tokens` / `tokens` 是非 Optional 的 0 兼表"未采到"与
# "真的是 0"（后端 row 面的同名缺口在 PY_SURFACES 里另有申报）。徽标解决了"这是
# 什么口径"，解决不了"这个 0 是不是没数据"——那件事今天只在 /usage 的覆盖率矩阵上
# 说得清（workflow 自报那一行的分子分母）。要收口就得让那两列可空，属数据层改动。
FRONTEND_COVERAGE_GAP = (
    "WorkflowsPage 的 ctx_last 数值已带口径徽标，但 0 仍兼表'未采到'与'真的是 0'"
    "（列非 Optional）；该路径的真实分子分母只在 /usage 覆盖率矩阵上可见。"
)
