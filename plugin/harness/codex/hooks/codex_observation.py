"""Read bounded Codex thread metadata without opening transcripts."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from uuid import UUID

ROOT_SOURCES = {"cli", "exec", "vscode"}
MAX_SESSION_META_BYTES = 65_536
IDENTITY_FIELDS = {
    "harness", "codex_observation", "model", "transcript_path", "agent_name",
    "agent_id", "agent_type", "parent_thread_id", "tool_call_id", "tool_use_id",
    "source_observed_at", "timestamp",
}


def _thread_id(value: object) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("invalid_thread_id")
    return value


def _text(value: object, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError("invalid_metadata")
    return value


def _parent(row: sqlite3.Row | dict) -> tuple[str | None, str]:
    source = _text(row["source"], 4096)
    try:
        source = json.loads(source)
    except ValueError:
        pass
    if isinstance(source, str) and source in ROOT_SOURCES:
        if row["agent_path"] not in (None, "", "/root"):
            raise ValueError("conflicting_root")
        return None, source
    if isinstance(source, dict):
        spawn = source.get("subagent", {}).get("thread_spawn", {})
        if isinstance(spawn, dict):
            return _thread_id(spawn.get("parent_thread_id")), "subagent"
    raise ValueError("unknown_source")


def _session_meta_observation(payload: dict) -> dict | None:
    """Read one bounded metadata line only when state lookup is unavailable."""
    try:
        actor_id = _thread_id(payload.get("agent_id") or payload.get("session_id"))
        session_id = _thread_id(payload.get("session_id"))
        candidate = _text(payload.get("agent_transcript_path") or payload.get("transcript_path"), 2048)
        path = Path(candidate)
        if not candidate or not path.is_absolute():
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            line = stream.readline(MAX_SESSION_META_BYTES + 1)
        if len(line) > MAX_SESSION_META_BYTES:
            return None
        record = json.loads(line)
        metadata = record.get("payload")
        if record.get("type") != "session_meta" or not isinstance(metadata, dict):
            return None
        if metadata.get("id") != actor_id:
            return None
        parent_id, source_kind = _parent({
            "source": json.dumps(metadata.get("source")), "agent_path": metadata.get("agent_path"),
        })
        if payload.get("parent_thread_id") not in (None, parent_id):
            return None
        if metadata.get("parent_thread_id") not in (None, parent_id):
            return None
        spawn = {}
        if parent_id:
            spawn = metadata["source"]["subagent"]["thread_spawn"]
            # Without the state chain, only a proven direct child has a known root.
            if type(spawn.get("depth")) is not int or spawn["depth"] != 1:
                return None
            if parent_id == actor_id or session_id not in {actor_id, parent_id}:
                return None
            name = _text(metadata.get("agent_nickname") or spawn.get("agent_nickname"), 200)
            if not name.strip():
                return None
        else:
            if session_id != actor_id:
                return None
            name = "Codex Leader"
        return {
            "version": 1, "actor_id": actor_id, "root_session_id": parent_id or actor_id,
            "parent_thread_id": parent_id, "source_kind": source_kind, "agent_name": name,
            "agent_role": _text(metadata.get("agent_role") or spawn.get("agent_role"), 200),
            "model": _text(metadata.get("model"), 200), "transcript_path": str(path),
            "cli_version": _text(metadata.get("cli_version"), 100),
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


def read_observation(payload: dict, state_path: Path | None = None) -> dict | None:
    """Resolve only the exact actor and up to eight exact parent rows."""
    try:
        actor_id = _thread_id(payload.get("agent_id") or payload.get("session_id"))
        session_id = _thread_id(payload.get("session_id"))
        path = state_path or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "state_5.sqlite"
        deadline = time.monotonic() + 0.075
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.025)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
            rows = []
            seen = set()
            current = actor_id
            parent_id = None
            source_kind = ""
            for _ in range(8):
                if current in seen or time.monotonic() > deadline:
                    raise ValueError("invalid_parent_chain")
                seen.add(current)
                row = db.execute(
                    "SELECT id, substr(rollout_path,1,2049) AS rollout_path, "
                    "substr(source,1,4097) AS source, substr(agent_nickname,1,201) AS agent_nickname, "
                    "substr(agent_role,1,201) AS agent_role, substr(agent_path,1,2049) AS agent_path, "
                    "substr(model,1,201) AS model, substr(cli_version,1,101) AS cli_version "
                    "FROM threads WHERE id=? LIMIT 1", (current,),
                ).fetchone()
                if row is None and not rows:
                    raise LookupError("missing_actor")
                if row is None or row["id"] != current:
                    raise ValueError("missing_thread")
                parent, source = _parent(row)
                rows.append(row)
                if len(rows) == 1:
                    parent_id, source_kind = parent, source
                if parent is None:
                    break
                current = parent
            else:
                raise ValueError("parent_depth_exceeded")
        if session_id not in seen or payload.get("parent_thread_id") not in (None, parent_id):
            raise ValueError("payload_identity_mismatch")
        actor = rows[0]
        name = _text(actor["agent_nickname"], 200) if parent_id else "Codex Leader"
        if parent_id and not name.strip():
            raise ValueError("missing_nickname")
        transcript = _text(actor["rollout_path"], 2048)
        if transcript and not Path(transcript).is_absolute():
            raise ValueError("invalid_transcript_path")
        return {
            "version": 1, "actor_id": actor_id, "root_session_id": rows[-1]["id"],
            "parent_thread_id": parent_id, "source_kind": source_kind,
            "agent_name": name, "agent_role": _text(actor["agent_role"], 200),
            "model": _text(actor["model"], 200), "transcript_path": transcript,
            "cli_version": _text(actor["cli_version"], 100),
        }
    except (OSError, sqlite3.Error, LookupError):
        return _session_meta_observation(payload)
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None


def enrich_payload(payload: dict, state_path: Path | None = None) -> dict:
    enriched = {key: value for key, value in payload.items() if key != "codex_observation"}
    enriched["harness"] = "codex"
    observation = read_observation(payload, state_path)
    if observation is not None:
        enriched.update({
            "codex_observation": observation,
            "session_id": observation["root_session_id"],
            "agent_id": observation["actor_id"] if observation["parent_thread_id"] else "",
            "parent_thread_id": observation["parent_thread_id"],
            "agent_name": observation["agent_name"], "model": observation["model"],
            "transcript_path": observation["transcript_path"],
        })
    return enriched


def preserve_identity(payload: dict, essential_fields: set[str], limit: int) -> dict:
    """Keep bounded identity metadata when the legacy transport trims a large body."""
    size = len(json.dumps(payload).encode())
    if size <= limit:
        return payload
    result = {key: value for key, value in payload.items() if key in essential_fields | IDENTITY_FIELDS}
    result.update(_stripped=True, _original_size=size)
    for key in ("tool_input", "tool_response", "tool_output"):
        if len(json.dumps(result).encode()) <= limit:
            break
        result.pop(key, None)
    return result
