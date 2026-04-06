"""AI Team OS — SRE Error Budget tracker.

Implements a 4-level autonomy model based on a sliding window failure rate
over the last N tasks. Updates are triggered by task completion events.

Levels:
    GREEN  (<15%)   — normal autonomous operation
    YELLOW (15-25%) — warning, reduce autonomy
    ORANGE (25-35%) — severe, every action needs Leader approval
    RED    (>35%)   — stop, wait for human intervention
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# Sliding window size
_WINDOW_SIZE = 20

# Default storage path for error budget state (JSON file per team)
_BUDGET_DIR = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "error_budget")


class BudgetLevel(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    ORANGE = "ORANGE"
    RED = "RED"


@dataclass
class BudgetState:
    team_id: str
    level: BudgetLevel
    failure_rate: float
    total_tasks: int
    failed_tasks: int
    window_size: int
    recent_results: list[bool]  # True = success, False = failure
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "level": self.level.value,
            "failure_rate": round(self.failure_rate, 4),
            "total_tasks": self.total_tasks,
            "failed_tasks": self.failed_tasks,
            "window_size": self.window_size,
            "recent_results": self.recent_results,
            "updated_at": self.updated_at,
            "policy": _level_policy(self.level),
        }


def _level_from_rate(rate: float) -> BudgetLevel:
    """Map failure rate to budget level."""
    if rate < 0.15:
        return BudgetLevel.GREEN
    if rate < 0.25:
        return BudgetLevel.YELLOW
    if rate < 0.35:
        return BudgetLevel.ORANGE
    return BudgetLevel.RED


def _level_policy(level: BudgetLevel) -> dict[str, Any]:
    """Return human-readable policy for the given level."""
    policies = {
        BudgetLevel.GREEN: {
            "action": "normal",
            "description": "Normal autonomous operation — no restrictions.",
        },
        BudgetLevel.YELLOW: {
            "action": "warn",
            "description": "Warning — reduce autonomy, increase checkpoints.",
        },
        BudgetLevel.ORANGE: {
            "action": "approve",
            "description": "Severe — every significant action requires Leader approval.",
        },
        BudgetLevel.RED: {
            "action": "halt",
            "description": "Stopped — wait for human intervention before proceeding.",
        },
    }
    return policies[level]


def _budget_file(team_id: str) -> str:
    os.makedirs(_BUDGET_DIR, exist_ok=True)
    safe_id = team_id.replace("/", "_").replace("\\", "_")
    return os.path.join(_BUDGET_DIR, f"{safe_id}.json")


def _load_state(team_id: str) -> BudgetState:
    path = _budget_file(team_id)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            return BudgetState(
                team_id=raw["team_id"],
                level=BudgetLevel(raw["level"]),
                failure_rate=raw["failure_rate"],
                total_tasks=raw["total_tasks"],
                failed_tasks=raw["failed_tasks"],
                window_size=raw.get("window_size", _WINDOW_SIZE),
                recent_results=raw.get("recent_results", []),
                updated_at=raw["updated_at"],
            )
        except Exception as exc:
            logger.warning("Error loading budget state for %s: %s", team_id, exc)

    # Return a fresh GREEN state
    return BudgetState(
        team_id=team_id,
        level=BudgetLevel.GREEN,
        failure_rate=0.0,
        total_tasks=0,
        failed_tasks=0,
        window_size=_WINDOW_SIZE,
        recent_results=[],
        updated_at=datetime.now(UTC).isoformat(),
    )


def _save_state(state: BudgetState) -> None:
    path = _budget_file(state.team_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, ensure_ascii=False, indent=2)


def get_error_budget(team_id: str) -> dict[str, Any]:
    """Query current error budget state for a team.

    Args:
        team_id: Team ID (use "global" for cross-team aggregate)

    Returns:
        BudgetState dict with level, failure_rate, policy
    """
    state = _load_state(team_id)
    return {"success": True, "data": state.to_dict()}


def update_error_budget(team_id: str, task_success: bool) -> dict[str, Any]:
    """Update error budget based on a task result.

    Appends to the sliding window (last _WINDOW_SIZE tasks) and recomputes level.

    Args:
        team_id: Team ID
        task_success: True if task succeeded, False if failed

    Returns:
        Updated BudgetState dict with new level
    """
    state = _load_state(team_id)

    # Append result to sliding window
    state.recent_results.append(task_success)
    if len(state.recent_results) > _WINDOW_SIZE:
        state.recent_results = state.recent_results[-_WINDOW_SIZE:]

    state.total_tasks += 1
    if not task_success:
        state.failed_tasks += 1

    # Recompute failure rate from window
    window = state.recent_results
    failures_in_window = sum(1 for r in window if not r)
    state.failure_rate = failures_in_window / len(window) if window else 0.0
    state.window_size = len(window)

    prev_level = state.level
    state.level = _level_from_rate(state.failure_rate)
    state.updated_at = datetime.now(UTC).isoformat()

    _save_state(state)

    result = state.to_dict()
    result["level_changed"] = prev_level != state.level
    result["prev_level"] = prev_level.value

    if result["level_changed"]:
        logger.warning(
            "Error budget level changed: %s -> %s (team=%s, rate=%.1f%%)",
            prev_level.value,
            state.level.value,
            team_id,
            state.failure_rate * 100,
        )

    return {"success": True, "data": result}
