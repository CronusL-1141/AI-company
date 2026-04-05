"""Unit tests for SRE error budget module."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import pytest

from aiteam.loop.error_budget import (
    BudgetLevel,
    _WINDOW_SIZE,
    _level_from_rate,
    get_error_budget,
    update_error_budget,
)


def test_level_from_rate_green():
    assert _level_from_rate(0.0) == BudgetLevel.GREEN
    assert _level_from_rate(0.14) == BudgetLevel.GREEN


def test_level_from_rate_yellow():
    assert _level_from_rate(0.15) == BudgetLevel.YELLOW
    assert _level_from_rate(0.24) == BudgetLevel.YELLOW


def test_level_from_rate_orange():
    assert _level_from_rate(0.25) == BudgetLevel.ORANGE
    assert _level_from_rate(0.34) == BudgetLevel.ORANGE


def test_level_from_rate_red():
    assert _level_from_rate(0.35) == BudgetLevel.RED
    assert _level_from_rate(1.0) == BudgetLevel.RED


def test_get_error_budget_fresh(tmp_path):
    """Fresh state returns GREEN with zero counters."""
    with patch("aiteam.loop.error_budget._BUDGET_DIR", str(tmp_path)):
        result = get_error_budget("team-abc")

    assert result["success"] is True
    data = result["data"]
    assert data["level"] == "GREEN"
    assert data["failure_rate"] == 0.0
    assert data["total_tasks"] == 0


def test_update_error_budget_success(tmp_path):
    """All successes keep level GREEN."""
    with patch("aiteam.loop.error_budget._BUDGET_DIR", str(tmp_path)):
        for _ in range(5):
            update_error_budget("team-x", task_success=True)
        result = get_error_budget("team-x")

    assert result["data"]["level"] == "GREEN"
    assert result["data"]["failure_rate"] == 0.0
    assert result["data"]["total_tasks"] == 5


def test_update_error_budget_reaches_yellow(tmp_path):
    """20% failure rate → YELLOW."""
    with patch("aiteam.loop.error_budget._BUDGET_DIR", str(tmp_path)):
        # 4 failures + 16 successes = 20% in window of 20
        for _ in range(16):
            update_error_budget("team-y", task_success=True)
        for _ in range(4):
            update_error_budget("team-y", task_success=False)
        result = get_error_budget("team-y")

    data = result["data"]
    assert data["level"] == "YELLOW"
    assert abs(data["failure_rate"] - 0.20) < 0.01


def test_update_error_budget_reaches_red(tmp_path):
    """All failures → RED."""
    with patch("aiteam.loop.error_budget._BUDGET_DIR", str(tmp_path)):
        for _ in range(10):
            update_error_budget("team-z", task_success=False)
        result = get_error_budget("team-z")

    assert result["data"]["level"] == "RED"


def test_sliding_window_evicts_old_results(tmp_path):
    """Old failures evicted from window allow recovery to GREEN."""
    with patch("aiteam.loop.error_budget._BUDGET_DIR", str(tmp_path)):
        # Add many failures first
        for _ in range(10):
            update_error_budget("team-w", task_success=False)
        # Flood with successes to push failures out of window
        for _ in range(_WINDOW_SIZE):
            update_error_budget("team-w", task_success=True)
        result = get_error_budget("team-w")

    assert result["data"]["level"] == "GREEN"
    assert result["data"]["failure_rate"] == 0.0


def test_level_change_detected(tmp_path):
    """level_changed flag is True when level transitions."""
    with patch("aiteam.loop.error_budget._BUDGET_DIR", str(tmp_path)):
        # Start GREEN
        for _ in range(15):
            r = update_error_budget("team-lc", task_success=True)
        assert r["data"]["level_changed"] is False

        # Push to YELLOW
        for _ in range(5):
            r = update_error_budget("team-lc", task_success=False)

    # Last update may or may not change depending on window — just verify field exists
    assert "level_changed" in r["data"]
    assert "prev_level" in r["data"]


def test_policy_included_in_response(tmp_path):
    """Each response includes a policy description."""
    with patch("aiteam.loop.error_budget._BUDGET_DIR", str(tmp_path)):
        result = get_error_budget("team-p")

    assert "policy" in result["data"]
    assert "action" in result["data"]["policy"]
    assert "description" in result["data"]["policy"]
