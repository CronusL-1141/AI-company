"""Unit tests for Watchdog heartbeat functions."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from aiteam.loop.watchdog import (
    HEARTBEAT_TIMEOUT_MINUTES,
    agent_heartbeat,
    watchdog_check_heartbeats,
)


def test_agent_heartbeat_creates_file(tmp_path):
    """agent_heartbeat stores a JSON file with correct fields."""
    with patch("aiteam.loop.watchdog._HEARTBEAT_DIR", str(tmp_path)):
        result = agent_heartbeat("agent-001", agent_name="backend-dev", team_id="team-1")

    assert result["success"] is True
    data = result["data"]
    assert data["agent_id"] == "agent-001"
    assert data["agent_name"] == "backend-dev"
    assert data["team_id"] == "team-1"
    assert "last_heartbeat" in data

    # Verify file was written
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    with open(files[0]) as f:
        stored = json.load(f)
    assert stored["agent_id"] == "agent-001"


def test_watchdog_check_empty_dir(tmp_path):
    """Empty heartbeat dir returns empty alive/dead lists."""
    with patch("aiteam.loop.watchdog._HEARTBEAT_DIR", str(tmp_path)):
        result = watchdog_check_heartbeats()

    assert result["success"] is True
    assert result["data"]["total"] == 0
    assert result["data"]["alive"] == []
    assert result["data"]["dead"] == []


def test_watchdog_check_alive_agent(tmp_path):
    """Agent with recent heartbeat appears in alive list."""
    with patch("aiteam.loop.watchdog._HEARTBEAT_DIR", str(tmp_path)):
        agent_heartbeat("agent-alive", agent_name="worker", team_id="t1")
        result = watchdog_check_heartbeats()

    data = result["data"]
    assert data["alive_count"] == 1
    assert data["dead_count"] == 0
    assert data["alive"][0]["agent_id"] == "agent-alive"
    assert data["alive"][0]["status"] == "alive"


def test_watchdog_check_dead_agent(tmp_path):
    """Agent with stale heartbeat appears in dead list."""
    # Write a heartbeat file with an old timestamp
    os.makedirs(str(tmp_path), exist_ok=True)
    old_time = (
        datetime.now(UTC) - timedelta(minutes=HEARTBEAT_TIMEOUT_MINUTES + 1)
    ).isoformat()
    record = {
        "agent_id": "agent-dead",
        "agent_name": "stale-worker",
        "team_id": "t1",
        "last_heartbeat": old_time,
    }
    with open(os.path.join(str(tmp_path), "agent-dead.json"), "w") as f:
        json.dump(record, f)

    with patch("aiteam.loop.watchdog._HEARTBEAT_DIR", str(tmp_path)):
        result = watchdog_check_heartbeats()

    data = result["data"]
    assert data["dead_count"] == 1
    assert data["alive_count"] == 0
    assert data["dead"][0]["status"] == "dead"
    assert data["dead"][0]["agent_id"] == "agent-dead"


def test_watchdog_check_mixed(tmp_path):
    """Mixed alive and dead agents counted correctly."""
    os.makedirs(str(tmp_path), exist_ok=True)
    old_time = (
        datetime.now(UTC) - timedelta(minutes=HEARTBEAT_TIMEOUT_MINUTES + 10)
    ).isoformat()

    # Write one dead record manually
    dead_record = {"agent_id": "dead-1", "agent_name": "d", "team_id": "t", "last_heartbeat": old_time}
    with open(os.path.join(str(tmp_path), "dead-1.json"), "w") as f:
        json.dump(dead_record, f)

    with patch("aiteam.loop.watchdog._HEARTBEAT_DIR", str(tmp_path)):
        # Write one fresh heartbeat
        agent_heartbeat("alive-1", agent_name="a", team_id="t")
        result = watchdog_check_heartbeats()

    data = result["data"]
    assert data["total"] == 2
    assert data["alive_count"] == 1
    assert data["dead_count"] == 1


def test_watchdog_check_timeout_included(tmp_path):
    """Response always includes the timeout_minutes field."""
    with patch("aiteam.loop.watchdog._HEARTBEAT_DIR", str(tmp_path)):
        result = watchdog_check_heartbeats()

    assert result["data"]["timeout_minutes"] == HEARTBEAT_TIMEOUT_MINUTES
