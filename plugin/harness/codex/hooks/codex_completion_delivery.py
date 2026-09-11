"""Bounded, event-driven redelivery of identified Codex completion observations."""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import hook_core

MAX_PENDING = 256
MAX_ATTEMPTS = 4
PRIMARY_TIMEOUT = 1.5
REPLAY_BUDGET = 0.35


def _identifier(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value) else ""


def _metadata(payload: dict) -> dict | None:
    if payload.get("harness") != "codex" or payload.get("hook_event_name") != "PostToolUse":
        return None
    call_id = _identifier(payload.get("tool_call_id")) or _identifier(payload.get("tool_use_id"))
    session_id = _identifier(payload.get("session_id"))
    tool_name = _identifier(payload.get("tool_name"))
    if not call_id or not session_id or not tool_name:
        return None
    metadata = {
        "harness": "codex", "hook_event_name": "PostToolUse",
        "session_id": session_id, "tool_name": tool_name, "tool_call_id": call_id,
        "_codex_completion_replay": True,
    }
    for field in ("agent_id", "agent_type", "turn_id"):
        value = _identifier(payload.get(field))
        if value:
            metadata[field] = value
    observed = payload.get("_codex_completion_observed_at")
    if isinstance(observed, str) and len(observed) <= 64:
        try:
            instant = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            if instant.tzinfo is not None and instant <= datetime.now(UTC):
                metadata["_codex_completion_observed_at"] = instant.astimezone(UTC).isoformat()
        except ValueError:
            pass
    return metadata


@contextlib.contextmanager
def _outbox(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path, timeout=0.025)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS completions ("
                "id TEXT PRIMARY KEY, api_url TEXT NOT NULL, payload TEXT NOT NULL, "
                "attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
            )
            yield connection


def _diagnostic(reason: str) -> None:
    sys.stderr.write(f"[aiteam-codex-completion] {reason}\n")


def _post(payload: dict, api_url: str, timeout: float) -> str:
    """A HTTP success alone is not acknowledgement of persistent correlation."""
    try:
        data = json.dumps(payload).encode()
        if len(data) > hook_core.MAX_PAYLOAD_BYTES:
            payload = _metadata(payload)
            data = json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{api_url}/api/hooks/event", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read(16385))
        call_id = _identifier(payload.get("tool_call_id")) or _identifier(payload.get("tool_use_id"))
        if (isinstance(result, dict) and result.get("completion_recorded") is True
                and result.get("tool_call_id") == call_id and result.get("activity_id")):
            return "posted"
        _diagnostic("completion_unconfirmed")
        return "error"
    except (TimeoutError, urllib.error.URLError):
        _diagnostic("completion_unreachable")
        return "post_unreachable"
    except (OSError, ValueError):
        _diagnostic("completion_unconfirmed")
        return "error"


def post_event(
    payload: dict, api_url: str, state_dir: Path,
    *, extra_essential_fields: frozenset[str] = frozenset(),
) -> str:
    """Deliver normally, then spend at most one small attempt on this outbox.

    Queue records never contain tool input/output, credentials, or transcripts.
    Exhausted records remain unconfirmed. No timer or process resumes them.
    """
    path = state_dir / "completion-outbox.sqlite3"
    metadata = _metadata(payload)
    current_id = None
    queued = False
    if metadata is not None:
        # Capture once, before network work; queued replays keep this original value.
        payload = {**payload, "_codex_completion_observed_at": datetime.now(UTC).isoformat()}
        metadata = _metadata(payload)
        current_id = str(uuid5(NAMESPACE_URL, json.dumps([
            api_url, metadata["session_id"], metadata.get("agent_id", ""),
            metadata["tool_call_id"], metadata["tool_name"],
        ], separators=(",", ":"))))
        try:
            with _outbox(path) as connection:
                existing = connection.execute(
                    "SELECT id FROM completions WHERE id=?", (current_id,),
                ).fetchone()
                count, = connection.execute("SELECT COUNT(*) FROM completions").fetchone()
                if existing or count < MAX_PENDING:
                    connection.execute(
                        "INSERT OR IGNORE INTO completions(id,api_url,payload,created_at) "
                        "VALUES (?,?,?,?)",
                        (current_id, api_url, json.dumps(metadata), time.time()),
                    )
                    connection.execute(
                        "UPDATE completions SET attempts=attempts+1 WHERE id=?", (current_id,),
                    )
                    queued = True
                else:
                    _diagnostic("outbox_full")
        except (OSError, sqlite3.Error):
            _diagnostic("outbox_unavailable")
        state = _post(payload, api_url, PRIMARY_TIMEOUT)
        if queued and state == "posted":
            try:
                with _outbox(path) as connection:
                    connection.execute("DELETE FROM completions WHERE id=?", (current_id,))
            except (OSError, sqlite3.Error):
                _diagnostic("ack_not_persisted")
    else:
        state = str(hook_core.post_event(
            payload, api_url, extra_essential_fields=extra_essential_fields,
        ))

    if not path.exists():
        return state
    deadline = time.monotonic() + REPLAY_BUDGET
    try:
        with _outbox(path) as connection:
            row = connection.execute(
                "SELECT id,payload FROM completions WHERE api_url=? AND attempts<? "
                "AND id!=? ORDER BY created_at,id LIMIT 1",
                (api_url, MAX_ATTEMPTS, current_id or ""),
            ).fetchone()
            if row:
                connection.execute("UPDATE completions SET attempts=attempts+1 WHERE id=?", (row[0],))
        remaining = deadline - time.monotonic()
        if row and remaining > 0:
            replay_state = _post(json.loads(row[1]), api_url, remaining)
            if replay_state == "posted":
                with _outbox(path) as connection:
                    connection.execute("DELETE FROM completions WHERE id=?", (row[0],))
                _diagnostic("completion_replayed")
                if row[0] == current_id:
                    state = "posted"
    except (OSError, sqlite3.Error, ValueError):
        _diagnostic("replay_unconfirmed")
    return state
