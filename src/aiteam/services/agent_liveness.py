"""Metadata rules; transition contract: docs/codex-leader-liveness-design.md."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from aiteam.clock import ensure_utc

AUTO_OFFLINE_KEY = "_aiteam_auto_offline"
AUTO_OFFLINE_REASONS = frozenset({
    "heartbeat_timeout", "config_liveness", "startup_reconciliation", "session_auto_close",
})
MAX_SOURCE_OBSERVATION_AGE = timedelta(seconds=60)


def without_auto_offline(config: dict) -> dict:
    return {key: value for key, value in config.items() if key != AUTO_OFFLINE_KEY}


def with_auto_offline(config: dict, reason: str, occurred_at: datetime) -> dict:
    if reason not in AUTO_OFFLINE_REASONS or occurred_at.tzinfo is None:
        raise ValueError("invalid_auto_offline_transition")
    return {**without_auto_offline(config), AUTO_OFFLINE_KEY: {
        "version": 1, "reason": reason, "nonce": uuid4().hex,
        "occurred_at": ensure_utc(occurred_at).isoformat(),
    }}


def _parse_source_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ensure_utc(parsed) if parsed.tzinfo is not None else None


def automatic_offline_at(config: dict) -> datetime | None:
    marker = config.get(AUTO_OFFLINE_KEY)
    if not isinstance(marker, dict) or set(marker) != {"version", "reason", "nonce", "occurred_at"}:
        return None
    if type(marker["version"]) is not int or marker["version"] != 1:
        return None
    if not isinstance(marker["reason"], str) or marker["reason"] not in AUTO_OFFLINE_REASONS:
        return None
    if not isinstance(marker["nonce"], str) or len(marker["nonce"]) != 32:
        return None
    return _parse_source_time(marker["occurred_at"])


def source_activity_time(value: object, now: datetime) -> tuple[datetime | None, str]:
    """Require source time; receiving an old event now cannot make it fresh."""
    if value is None:
        return None, "source_observed_at_missing"
    source_time = _parse_source_time(value)
    if source_time is None:
        return None, "source_observed_at_invalid"
    if source_time > now:
        return None, "source_observed_at_future"
    if now - source_time > MAX_SOURCE_OBSERVATION_AGE:
        return None, "source_observed_at_stale"
    return source_time, ""


AUTO_CLOSE_KEY = "_aiteam_auto_close"


def without_auto_close(config: dict) -> dict:
    return {key: value for key, value in config.items() if key != AUTO_CLOSE_KEY}


def with_auto_close(config: dict, agent_id: str, offline_nonce: str, now: datetime) -> dict:
    return {**without_auto_close(config), AUTO_CLOSE_KEY: {
        "version": 1, "nonce": uuid4().hex, "occurred_at": ensure_utc(now).isoformat(),
        "leader_agent_id": agent_id, "offline_nonce": offline_nonce,
    }}


def automatic_close_at(config: dict, agent_id: str, offline_nonce: str) -> datetime | None:
    marker = config.get(AUTO_CLOSE_KEY)
    if not isinstance(marker, dict) or set(marker) != {
        "version", "nonce", "occurred_at", "leader_agent_id", "offline_nonce",
    }:
        return None
    if (type(marker["version"]) is not int or marker["version"] != 1
            or not isinstance(marker["nonce"], str) or len(marker["nonce"]) != 32
            or marker["leader_agent_id"] != agent_id or marker["offline_nonce"] != offline_nonce):
        return None
    return _parse_source_time(marker["occurred_at"])


def codex_owned_container(team, leader) -> bool:
    """Exact ownership includes historical short names, never prefix-only owners."""
    native = leader.session_id
    return bool(
        native and leader.harness == "codex" and leader.source == "hook"
        and leader.role == "leader" and leader.cc_tool_use_id == native
        and leader.team_id == team.id and leader.project_id
        and leader.project_id == team.project_id and team.leader_agent_id == leader.id
        and team.config.get("kind") == "session"
        and team.config.get("owner_session_id") == native
        and team.name in {f"session-{native}", f"session-{native[:8]}"}
    )


def protected_codex_container(team, members) -> bool:
    """Keep residual root evidence after pointer/config removal; never classify by a child.

    Real CC births have no native tool ID and may have an unbound team project.
    A currently valid CC owner therefore wins over residual Codex roster entries.
    See docs/codex-leader-liveness-design.md for the identity-removal limit.
    """
    leader = next((member for member in members if member.id == team.leader_agent_id), None)
    if (leader is not None and leader.role == "leader" and leader.source == "hook"
            and leader.harness in (None, "claude-code") and leader.session_id
            and leader.cc_tool_use_id in (None, leader.session_id)
            and team.config.get("kind") == "session"
            and team.config.get("owner_session_id") == leader.session_id
            and team.name in {f"session-{leader.session_id}", f"session-{leader.session_id[:8]}"}):
        return False
    if (team.config.get("kind") == "session" and leader is not None
            and leader.role == "leader" and leader.source == "hook" and leader.harness == "codex"
            and leader.session_id and leader.cc_tool_use_id == leader.session_id):
        return True
    if AUTO_CLOSE_KEY in team.config:
        return True
    return any(
        member.role == "leader" and member.source == "hook" and member.harness == "codex"
        and member.session_id and member.cc_tool_use_id == member.session_id
        and member.project_id and member.project_id == team.project_id
        and team.name in {f"session-{member.session_id}", f"session-{member.session_id[:8]}"}
        for member in members
    )
