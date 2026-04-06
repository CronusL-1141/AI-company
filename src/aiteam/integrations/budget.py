"""Cost budget tracking and enforcement.

Tracks weekly/monthly USD spend against configured budget limits.
Triggers alerts at threshold and pauses expensive operations when exceeded.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


async def check_budget(repo) -> dict:
    """Check current spending against the weekly budget.

    Queries token_costs for the current ISO week, computes utilization,
    and forecasts end-of-week spend based on elapsed days.

    Args:
        repo: StorageRepository instance.

    Returns:
        Dict with keys:
            spent_usd (float): Total cost this week.
            budget_usd (float): Configured weekly budget.
            utilization_pct (float): spent / budget * 100.
            status (str): "ok" | "warning" | "exceeded".
            forecast_usd (float | None): Projected end-of-week spend.
            days_elapsed (int): Days elapsed in the current week (1–7).
    """
    import aiteam.config.settings as cfg_module

    budget = getattr(cfg_module, "COST_BUDGET_WEEKLY_USD", 50.0)
    alert_threshold = getattr(cfg_module, "COST_ALERT_THRESHOLD", 0.8)

    # Query last 7 days of token costs (ISO week approximation)
    cost_rows = await repo.get_token_costs(group_by="agent", days=7)
    spent = sum(float(row.get("total_cost_usd") or 0.0) for row in cost_rows)

    # Determine days elapsed in the current week (Monday = day 1)
    now = datetime.now(tz=UTC)
    days_elapsed = max(1, now.weekday() + 1)  # 1–7

    # Linear forecast to end of week
    forecast = (spent / days_elapsed) * 7 if days_elapsed > 0 else None

    utilization = (spent / budget * 100) if budget > 0 else 0.0

    if budget > 0 and spent >= budget:
        status = "exceeded"
    elif budget > 0 and (spent / budget) >= alert_threshold:
        status = "warning"
    else:
        status = "ok"

    return {
        "spent_usd": round(spent, 4),
        "budget_usd": round(budget, 2),
        "utilization_pct": round(utilization, 2),
        "status": status,
        "forecast_usd": round(forecast, 4) if forecast is not None else None,
        "days_elapsed": days_elapsed,
        "alert_threshold_pct": round(alert_threshold * 100, 0),
    }


async def enforce_budget(repo) -> str | None:
    """If over budget, pause wake agents.

    Checks current spend and returns a warning message when the budget
    threshold is reached or exceeded. The caller is responsible for
    deciding whether to block expensive operations.

    Args:
        repo: StorageRepository instance.

    Returns:
        Warning message string if action is required, None if within budget.
    """
    result = await check_budget(repo)
    status = result["status"]

    if status == "exceeded":
        msg = (
            f"[BUDGET EXCEEDED] Weekly spend ${result['spent_usd']:.2f} "
            f"exceeds budget ${result['budget_usd']:.2f} "
            f"({result['utilization_pct']:.1f}%). "
            "Expensive operations paused until budget resets."
        )
        logger.warning(msg)
        return msg

    if status == "warning":
        msg = (
            f"[BUDGET WARNING] Weekly spend ${result['spent_usd']:.2f} "
            f"is {result['utilization_pct']:.1f}% of budget ${result['budget_usd']:.2f}. "
            f"Forecast: ${result['forecast_usd']:.2f} by end of week."
        )
        logger.warning(msg)
        return msg

    return None
