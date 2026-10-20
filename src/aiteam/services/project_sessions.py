"""Read-only project session history and explicit running-task attribution.

Transcript freshness is activity evidence, never proof of a busy or ended host.
Container completion and automatic offline transitions retain session identity.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from aiteam.api import session_probe, session_registry
from aiteam.clock import ensure_utc, from_timestamp
from aiteam.storage.repository import StorageRepository
from aiteam.types import Agent, HarnessId, TaskStatus, Team

_NATIVE_TAIL_BYTES = 200_000
_NATIVE_HEADER_BYTES = 65_536
# History rows in one summary. Live sessions are always returned, even past this.
MAX_SUMMARY_SESSIONS = 20


def _codex_activity(leader: Agent, now: datetime) -> tuple[str, str] | None:
    """Exact native identity plus a bounded, coherent lifecycle-tail observation.

    A completed turn is idle, not a closed thread. If the end marker predates
    later DB activity, it cannot override that activity. Unknown/truncated data
    preserves the independent DB observation rather than inventing a status.
    """
    if leader.harness != HarnessId.CODEX or not leader.transcript_path:
        return None
    try:
        with Path(leader.transcript_path).open("rb") as stream:
            before = os.fstat(stream.fileno())
            first = stream.readline(_NATIVE_HEADER_BYTES + 1)
            if len(first) > _NATIVE_HEADER_BYTES or not first.endswith(b"\n"):
                return None
            metadata = json.loads(first)
            if (not isinstance(metadata, dict) or metadata.get("type") != "session_meta"
                    or not isinstance(metadata.get("payload"), dict)
                    or metadata["payload"].get("id") != leader.session_id):
                return None
            offset = max(0, before.st_size - _NATIVE_TAIL_BYTES)
            stream.seek(offset)
            tail = stream.read(_NATIVE_TAIL_BYTES)
            after = os.fstat(stream.fileno())
        def signature(stat):
            return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
        if signature(before) != signature(after) or not tail.endswith(b"\n"):
            return None
        lines = tail.splitlines()
        if offset:
            lines = lines[1:]  # the bounded seek may bisect the first record
        latest = None
        previous = None
        current_turn = None
        for raw in lines:
            if not raw.strip():
                continue
            record = json.loads(raw)
            if not isinstance(record, dict):
                return None
            payload = record.get("payload")
            if record.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            event = payload.get("type")
            if event not in {"task_started", "task_complete", "turn_aborted"}:
                continue
            timestamp = datetime.fromisoformat(record["timestamp"])
            if timestamp.tzinfo is None:
                return None
            timestamp = ensure_utc(timestamp)
            if timestamp > now or (previous is not None and timestamp < previous):
                return None
            previous = timestamp
            turn_id = payload.get("turn_id")
            if event == "task_started":
                current_turn = turn_id
            elif current_turn and turn_id and turn_id != current_turn:
                continue  # delayed completion for an older turn is not this turn's end
            latest = ("busy" if event == "task_started" else "idle", timestamp)
        if latest is None:
            return None
        state, timestamp = latest
        observed = ensure_utc(leader.last_active_at) if leader.last_active_at else None
        if observed is not None and timestamp < observed:
            return None
        return state, timestamp.isoformat()
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def _time(value: datetime | None) -> str | None:
    return ensure_utc(value).isoformat() if value else None


def _fresh(value: str | None, now: datetime) -> bool:
    try:
        timestamp = ensure_utc(datetime.fromisoformat(value)) if value else None
        return bool(timestamp and timedelta(0) <= now - timestamp < session_probe.LIVE_WINDOW)
    except (TypeError, ValueError):
        return False


def _file_history(root_path: str) -> tuple[dict[str, dict], dict[str, dict], bool]:
    """Only stat historical transcripts; content/context reads stay in the live probe."""
    rows: dict[str, dict] = {}
    registry: dict[str, dict] = {}
    complete = True
    try:
        if root_path:
            directory = session_probe._claude_projects_dir() / session_probe.project_slug(root_path)
            if directory.is_dir():
                for path in directory.glob("*.jsonl"):
                    try:
                        if path.is_file():
                            rows[path.stem] = {"last_active_at": _time(from_timestamp(path.stat().st_mtime))}
                    except OSError:
                        complete = False
        records = session_registry.read_sessions()
        # Prefer a living registry row if a resumed session appears in two pids.
        for record in records:
            if not record.session_id:
                continue
            row = registry.setdefault(record.session_id, {})
            alive = session_registry.process_alive(record.pid)
            if row.get("cc_process_alive") is True:
                continue
            row.update(cc_pid=record.pid, cc_status=record.status or None, cc_process_alive=alive)
    except Exception:  # noqa: BLE001 - history is an observation, not an availability gate
        complete = False
    return rows, registry, complete


def _empty(session_id: str) -> dict:
    return {
        "session_id": session_id, "name": f"Session-{session_id[:8]}", "model": "",
        "harness": None, "status": "unknown", "activity_state": "unknown",
        "status_source": "identity_only", "current_task": "", "last_active_at": None,
        "started_at": None, "live": False, "ctx_tokens": None, "ctx_window": None,
        "ctx_pct": None, "in_flight_tasks": None, "in_flight_task_ids": [],
        "cc_pid": None, "cc_status": None, "cc_process_alive": None, "observation_at": None,
    }


async def project_sessions(
    repo: StorageRepository, project_id: str, root_path: str,
    teams: list[Team], probes: list[dict], now: datetime,
) -> dict:
    file_rows, registry, complete = await asyncio.to_thread(_file_history, root_path)
    rows = {session_id: _empty(session_id) for session_id in file_rows}
    for session_id, metadata in file_rows.items():
        rows[session_id].update(metadata, harness=HarnessId.CLAUDE_CODE.value,
                                status_source="activity_only")

    # A container's identity survives a missing/rebound Leader. Its lifecycle is
    # deliberately not projected as host-session status.
    team_by_id = {team.id: team for team in teams}
    for team in teams:
        owner = team.config.get("owner_session_id")
        if team.config.get("kind") != "session" or not isinstance(owner, str) or not owner.strip():
            continue
        row = rows.setdefault(owner, _empty(owner))
        recorded = _time(team.created_at)
        if recorded and (not row["started_at"] or recorded < row["started_at"]):
            row["started_at"] = recorded

    try:
        leaders = [leader for leader in await repo.find_agents_by_role("leader")
                   if leader.project_id == project_id and leader.session_id and leader.session_id.strip()]
    except Exception:  # noqa: BLE001
        leaders = []
        complete = False
    leaders.sort(key=lambda leader: (_time(leader.last_active_at) or "", _time(leader.created_at), leader.id),
                 reverse=True)
    latest: dict[str, Agent] = {}
    for leader in leaders:
        session_id = leader.session_id
        row = rows.setdefault(session_id, _empty(session_id))
        recorded = _time(leader.created_at)
        if recorded and (not row["started_at"] or recorded < row["started_at"]):
            row["started_at"] = recorded
        if session_id in latest:
            continue
        latest[session_id] = leader
        row.update(name=leader.name, model=leader.model or "",
                   harness=leader.harness.value if leader.harness is not None else row["harness"],
                   status=str(leader.status), current_task=leader.current_task or "",
                   status_source="db_observation")
        observed = _time(leader.last_active_at)
        if observed and (not row["last_active_at"] or observed > row["last_active_at"]):
            row["last_active_at"] = observed
        row["activity_state"] = (
            "busy" if leader.status == "busy" and _fresh(observed, now)
            else "idle" if leader.status == "waiting"
            else "offline" if leader.status == "offline" else "unknown"
        )

    # The bounded tail read only refines a recent DB observation; old history
    # keeps its DB status instead of costing a file read per request.
    native = await asyncio.to_thread(
        lambda: {session_id: _codex_activity(leader, now) for session_id, leader in latest.items()
                 if leader.harness == HarnessId.CODEX and _fresh(_time(leader.last_active_at), now)},
    )
    for session_id, observation in native.items():
        if observation is None:
            continue
        state, observed_at = observation
        row = rows[session_id]
        row.update(activity_state=state, status="busy" if state == "busy" else "waiting",
                   status_source="codex_rollout", observation_at=observed_at)
        if not row["last_active_at"] or observed_at > row["last_active_at"]:
            row["last_active_at"] = observed_at

    for probe in probes:
        session_id = probe["session_id"]
        row = rows.setdefault(session_id, _empty(session_id))
        row.update(name=f"CEO-{probe['name']}", model=probe.get("model") or row["model"],
                   harness=HarnessId.CLAUDE_CODE.value)
        for field in ("ctx_tokens", "ctx_window", "ctx_pct", "cc_pid", "cc_process_alive"):
            if field in probe:
                row[field] = probe[field]
        if "cc_status" in probe:
            row["cc_status"] = probe["cc_status"] or None
        # A transcript mtime older than a DB observation must not move activity backwards.
        observed = probe.get("last_active_at")
        if observed and (not row["last_active_at"] or observed > row["last_active_at"]):
            row["last_active_at"] = observed

    for row in rows.values():
        row.update(registry.get(row["session_id"], {}))
        # Registry status is a host-reported activity observation. PID liveness
        # alone is not used: the existing probe cannot guard against PID reuse.
        if row.get("cc_status") in {"busy", "idle"} and row.get("cc_process_alive") is not False:
            row["activity_state"] = row["cc_status"]
            row["status"] = "busy" if row["cc_status"] == "busy" else "waiting"
            row["status_source"] = "cc_registry"
        fresh = _fresh(row["last_active_at"], now)
        if row["activity_state"] == "busy" and not fresh:
            row["activity_state"] = "unknown"
        row["live"] = row["activity_state"] == "busy" and fresh

    # Only exact persisted identifiers carry attribution. A display label in
    # assigned_to and historical worked_on links cannot prove a current executor.
    try:
        running = await repo.list_tasks_by_project(project_id, status=TaskStatus.RUNNING)
        assigned_agents: dict[str, Agent | None] = {}
        unattributed = 0
        task_ids: dict[str, set[str]] = {session_id: set() for session_id in rows}
        for task in running:
            owner = None
            if task.assigned_to:
                if task.assigned_to not in assigned_agents:
                    assigned_agents[task.assigned_to] = await repo.get_agent(task.assigned_to)
                agent = assigned_agents[task.assigned_to]
                agent_team = team_by_id.get(agent.team_id) if agent else None
                if agent and (agent.project_id == project_id or (not agent.project_id and agent_team)):
                    owner = agent.session_id
            else:
                team = team_by_id.get(task.team_id)
                owner = team.config.get("owner_session_id") if team else None
            if owner in task_ids:
                task_ids[owner].add(task.id)
            else:
                unattributed += 1
        for session_id, row in rows.items():
            row["in_flight_task_ids"] = sorted(task_ids[session_id])
            row["in_flight_tasks"] = len(task_ids[session_id])
    except Exception:  # noqa: BLE001 - failed measurements must remain unknown
        unattributed = None

    ordered = sorted(rows.values(), key=lambda row: (row["last_active_at"] or "", row["session_id"]),
                     reverse=True)
    # Current cards and counts come from the full history, never from the capped page.
    current = [row for row in ordered if row["live"] and row["activity_state"] == "busy"]
    kept = {row["session_id"] for row in current}
    history = [row for row in ordered if row["session_id"] not in kept]
    kept.update(row["session_id"] for row in history[:max(0, MAX_SUMMARY_SESSIONS - len(kept))])
    sessions = [row for row in ordered if row["session_id"] in kept]
    return {"sessions": sessions, "sessions_complete": complete, "session_count": len(ordered),
            "sessions_truncated": len(sessions) < len(ordered),
            "leaders": current, "leader": current[0] if current else None,
            "last_activity_at": ordered[0]["last_active_at"] if ordered else None,
            "unattributed_running_tasks": unattributed}
