"""CC launch intent and exact native output-channel binding, without new polling."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import bindparam, func, select, text
from sqlalchemy.engine import make_url

from aiteam.services.codex_exec_launch_parser import classify_bash_launches
from aiteam.services.codex_rollout_usage import resolve_codex_home
from aiteam.services.codex_usage_journal import parse_usage_time, source_namespace
from aiteam.storage.codex_exec_launch import CodexExecLaunchRepository
from aiteam.storage.connection import default_db_url, get_session
from aiteam.storage.models import CodexExecLaunchModel
from aiteam.types import CodexExecLaunchObservation, CodexLaunchTierEvidence

_START_HEADER = re.compile(
    r"\AOpenAI Codex v[^\r\n]{1,80}\r?\n--------\r?\n(?P<fields>[\s\S]{1,3500}?)\r?\n--------\r?\n"
)
_HEADER_ID = re.compile(r"^session id:\s*([0-9a-f-]{36})\s*$", re.M)
_MODEL_REFRESH_DIAGNOSTIC = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z ERROR "
    r"codex_models_manager::manager: failed to refresh available models: request timed out\r?\n\Z"
)
_MAX_RECORDS = 1000


def _home(configured: Path | None = None) -> Path:
    """Canonical root, so a bound namespace equals the one evidence readers compute."""
    return resolve_codex_home(configured)


def _native_thread_start(home: Path, thread_id: str) -> datetime | None:
    """Verify an exact native exec ID against its own first rollout metadata line."""
    try:
        if str(UUID(thread_id)) != thread_id:
            return None
        deadline = time.monotonic() + 0.075
        with sqlite3.connect((home / "state_5.sqlite").absolute().as_uri() + "?mode=ro", uri=True, timeout=0.025) as db:
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
            row = db.execute("SELECT rollout_path FROM threads WHERE id=? LIMIT 1", (thread_id,)).fetchone()
        if row is None:
            return None
        path = Path(row[0])
        # Physical containment: native rows may name the configured, unresolved root.
        if not path.is_absolute() or not path.parent.resolve().is_relative_to(home.expanduser().resolve()):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            raw = stream.readline(65537)
        if len(raw) > 65536 or not raw.endswith(b"\n"):
            return None
        meta = json.loads(raw)
        payload = meta.get("payload", {})
        if (
            meta.get("type") != "session_meta"
            or payload.get("id") != thread_id
            or payload.get("source") != "exec"
            or payload.get("forked_from_id")
            or payload.get("parent_thread_id")
        ):
            return None
        return parse_usage_time(meta.get("timestamp"))
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return None


def _stdout_thread(stdout: object) -> tuple[str | None, list[str]]:
    if not isinstance(stdout, str):
        return None, []
    threads, turns = [], []
    # The host may truncate an item body after earlier complete identity lines.
    for line in stdout[:4096].splitlines(keepends=True):
        if not line.endswith("\n"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started":
            threads.append(event.get("thread_id"))
        if event.get("type") in {"turn.started", "turn.completed"} and isinstance(event.get("turn_id"), str):
            turns.append(event["turn_id"])
    if len(threads) != 1 or not isinstance(threads[0], str):
        return None, []
    return threads[0], list(dict.fromkeys(turns))[:100]


def _output_thread(path_text: str) -> tuple[str | None, str, str | None]:
    try:
        path = Path(path_text)
        # Explicit log files in the local user's home or temporary directory only.
        parent = path.parent.resolve()
        roots = (Path.home().resolve(), Path("/tmp").resolve())
        if (
            not path.is_absolute()
            or not any(parent.is_relative_to(root) for root in roots)
            or path.suffix not in {".log", ".out"}
        ):
            return None, "unsafe_output_file", None
        fd = os.open(parent / path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                return None, "unsafe_output_file", None
            header = stream.read(4096).decode("utf-8", errors="replace")
            file_key = hashlib.sha256(f"{info.st_dev}:{info.st_ino}".encode()).hexdigest()
        # Only native bounded diagnostics may precede the closed startup header.
        # Never search for a banner inside arbitrary user/tool content.
        prefix_bytes, stdin_notice, diagnostics = 0, False, 0
        while not header.startswith("OpenAI Codex v"):
            line, separator, remaining = header.partition("\n")
            if not separator:
                return None, "invalid_output_header", file_key
            raw_line = line + separator
            prefix_bytes += len(raw_line.encode("utf-8"))
            if prefix_bytes > 768:
                return None, "invalid_output_header", file_key
            if raw_line in {"Reading additional input from stdin...\n", "Reading additional input from stdin...\r\n"}:
                if stdin_notice or diagnostics:
                    return None, "invalid_output_header", file_key
                stdin_notice = True
            elif diagnostics < 4 and _MODEL_REFRESH_DIAGNOSTIC.fullmatch(raw_line):
                diagnostics += 1
            else:
                return None, "invalid_output_header", file_key
            header = remaining
        block = _START_HEADER.match(header)
        ids = _HEADER_ID.findall(block["fields"]) if block else []
        if len(ids) == 1:
            return ids[0], "bound", file_key
        return None, "invalid_output_header", file_key
    except FileNotFoundError:
        return None, "output_pending", None
    except OSError:
        return None, "unsafe_output_file", None


async def observe_cc_bash_launch(db_url: str, payload: dict, *, at: datetime, project_id: str | None = None) -> bool:
    """Use existing complete Pre/Post payloads. Do not rewrite the caller's command.

    Returns True after a completion was recorded: the caller then schedules
    :func:`reconcile_output_launches` off the hook response path.
    """
    if payload.get("harness") == "codex" or payload.get("tool_name") != "Bash":
        return False
    session_id, tool_id = payload.get("session_id"), payload.get("tool_use_id")
    if not isinstance(session_id, str) or not isinstance(tool_id, str) or not session_id or not tool_id:
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    launches = classify_bash_launches(tool_input.get("command"))
    if not launches:
        return False
    event = payload.get("hook_event_name")
    phase = {"PreToolUse": "pre", "PostToolUse": "post", "PostToolUseFailure": "fail"}.get(event)
    if phase is None:
        return False
    db_url = db_url or default_db_url()
    repository = CodexExecLaunchRepository(db_url)
    await repository.init_db()
    for index, launch in enumerate(launches):
        launch_id = hashlib.sha256(json.dumps([session_id, tool_id, index], separators=(",", ":")).encode()).hexdigest()
        metadata = {key: value for key, value in launch.metadata.items() if key not in {"json_output", "ephemeral"}}
        reason, state, thread, namespace, turns = launch.binding_reason, "planned", None, None, []
        existing = await repository.get(launch_id)
        started = existing.started_at if existing and existing.pre_seen else at
        background = tool_input.get("run_in_background") is True or launch.background
        if phase in {"post", "fail"}:
            state = "failed" if phase == "fail" else "unbound"
            if reason == "awaiting_completion" and tool_input.get("run_in_background") is not True:
                output = payload.get("tool_response", {})
                thread, turns = _stdout_thread(output.get("stdout") if isinstance(output, dict) else None)
                reason = "missing_thread_started"
                if thread:
                    home = await asyncio.to_thread(_home, launch.codex_home)
                    native_start = await asyncio.to_thread(_native_thread_start, home, thread)
                    if native_start is not None and native_start >= started:
                        namespace, reason, state = source_namespace(home), "bound", "bound"
                    else:
                        thread, turns, reason = None, [], "native_thread_unverified"
            elif reason == "awaiting_completion":
                reason = "background_or_redirect"
            if phase == "fail" and state != "bound":
                reason = "tool_failed"
        output_path = (
            (await asyncio.to_thread(launch.output_path.parent.resolve)) / launch.output_path.name
            if launch.output_path
            else None
        )
        try:
            value = CodexExecLaunchObservation(
                launch_id=launch_id,
                project_id=project_id,
                caller_session_id=session_id,
                tool_use_id=tool_id,
                command_index=index,
                **metadata,
                state=state,
                binding_reason=reason,
                pre_seen=phase == "pre",
                source_namespace=namespace,
                thread_id=thread,
                turn_ids=turns,
                started_at=started,
                finished_at=at if phase in {"post", "fail"} and not background else None,
                output_path=str(output_path) if output_path else None,
                codex_home=str(launch.codex_home) if launch.codex_home else None,
                execution_failed=phase == "fail",
                background=background,
            )
        except ValidationError:
            continue
        await repository.record(value, phase)
    return phase in {"post", "fail"}


async def reconcile_output_launches(db_url: str, *, codex_home: Path | None = None) -> None:
    """At an existing hook/capture, examine at most 20 log headers (80 KiB total)."""
    db_url = db_url or default_db_url()
    home = await asyncio.to_thread(_home, codex_home)
    repository = CodexExecLaunchRepository(db_url)
    async with get_session(db_url) as session:
        tables = set(await session.scalars(text("SELECT name FROM sqlite_master WHERE type='table'")))
        if "codex_exec_launches" not in tables:
            return
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT payload FROM codex_exec_launches "
                        "WHERE json_extract(payload,'$.output_path') IS NOT NULL "
                        "AND state IN ('planned','unbound','failed') "
                        "AND json_extract(payload,'$.pre_seen')=1 "
                        "ORDER BY started_at DESC LIMIT 20"
                    )
                )
            )
            .scalars()
            .all()
        )
        candidates = [
            CodexExecLaunchObservation.model_validate(raw if isinstance(raw, dict) else json.loads(raw)) for raw in rows
        ]
        path_keys = {hashlib.sha256(item.output_path.encode()).hexdigest() for item in candidates}
        counts = dict(
            (
                await session.execute(
                    select(
                        CodexExecLaunchModel.output_path_key,
                        func.count(),
                    )
                    .where(CodexExecLaunchModel.output_path_key.in_(path_keys))
                    .group_by(CodexExecLaunchModel.output_path_key)
                )
            ).all()
        )
    for item in candidates:
        thread, reason, file_key = None, "output_path_conflict", None
        if counts[hashlib.sha256(item.output_path.encode()).hexdigest()] == 1:
            thread, reason, file_key = await asyncio.to_thread(_output_thread, item.output_path)
        if item.exec_mode != "new":
            thread, reason = None, "complex_shell"
        namespace = None
        if thread:
            candidate_home = await asyncio.to_thread(_home, Path(item.codex_home)) if item.codex_home else home
            native_start = await asyncio.to_thread(_native_thread_start, candidate_home, thread)
            if native_start is None:
                thread, reason = None, "native_thread_unverified"
            elif native_start < item.started_at:
                thread, reason = None, "stale_output_file"
            else:
                namespace = source_namespace(candidate_home)
        update = {
            "state": "bound" if thread else "unbound",
            "thread_id": thread,
            "source_namespace": namespace,
            "binding_reason": "bound" if thread else reason,
            "output_file_key": file_key,
        }
        # A row stuck in the same verdict is not rewritten on every hook and sample.
        if all(getattr(item, key) == value for key, value in update.items()):
            continue
        await repository.record(item.model_copy(update=update), "reconcile")


def _recent_thread_ids(home: Path, since: datetime) -> list[str] | None:
    """Only prune by an indexed native update boundary; it does not assign tier."""
    try:
        deadline = time.monotonic() + 0.1
        with sqlite3.connect((home / "state_5.sqlite").absolute().as_uri() + "?mode=ro", uri=True, timeout=0.025) as db:
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
            columns = {row[1]: row[2] for row in db.execute("PRAGMA table_info(threads)")}
            indexes = {row[1] for row in db.execute("PRAGMA index_list(threads)")}
            if columns.get("updated_at") != "INTEGER" or "idx_threads_updated_at" not in indexes:
                return None
            index_columns = [row[2] for row in db.execute("PRAGMA index_info(idx_threads_updated_at)")]
            if not index_columns or index_columns[0] != "updated_at":
                return None
            rows = db.execute(
                "SELECT id FROM threads INDEXED BY idx_threads_updated_at WHERE updated_at>=? "
                "ORDER BY updated_at LIMIT ?",
                (math.floor(since.timestamp()), _MAX_RECORDS + 1),
            ).fetchall()
        return None if len(rows) > _MAX_RECORDS else [row[0] for row in rows]
    except (OSError, sqlite3.Error):
        return None


def _spool_rows(home: Path, since: datetime, until: datetime, thread_ids: list[str] | None = None) -> list[dict] | None:
    path = home / "data/ai-team-os/external-exec.db"
    if not path.exists():
        return []
    try:
        deadline = time.monotonic() + 0.1
        with sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.025) as db:
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
            batches = (
                [None]
                if thread_ids is None
                else [thread_ids[index : index + 400] for index in range(0, len(thread_ids), 400)]
            )
            rows = []
            for batch in batches:
                restriction = (" AND thread_id IN (" + ",".join("?" for _ in batch) + ")") if batch else ""
                rows.extend(
                    db.execute(
                        "SELECT json_remove(payload,'$.receipts') FROM external_exec_invocations "
                        "WHERE json_extract(payload,'$.source_namespace')=? "
                        "AND json_extract(payload,'$.thread_id') IS NOT NULL "
                        "AND julianday(json_extract(payload,'$.started_at'))<=julianday(?) "
                        "AND (json_extract(payload,'$.finished_at') IS NULL "
                        "OR julianday(json_extract(payload,'$.finished_at'))>=julianday(?)) "
                        + restriction
                        + " ORDER BY started_at DESC LIMIT ?",
                        (
                            source_namespace(home),
                            until.isoformat(),
                            since.isoformat(),
                            *(batch or []),
                            _MAX_RECORDS + 1,
                        ),
                    ).fetchall()
                )
                if len(rows) > _MAX_RECORDS:
                    return None
        return [] if len(rows) > _MAX_RECORDS else [json.loads(row[0]) for row in rows]
    except (OSError, sqlite3.Error, ValueError):
        return None  # Unreadable/over-budget is not proof that no competing launch exists.


async def load_launch_tier_evidence(
    db_url: str, *, codex_home: Path, since: datetime, until: datetime
) -> list[CodexLaunchTierEvidence]:
    """Load bounded exact new-exec bindings; refresh explicit log channels on demand."""
    db_url = db_url or default_db_url()
    await reconcile_output_launches(db_url, codex_home=codex_home)
    thread_ids = await asyncio.to_thread(_recent_thread_ids, codex_home, since)
    rows: list[tuple[str, dict]] = []
    async with get_session(db_url) as session:
        tables = set(await session.scalars(text("SELECT name FROM sqlite_master WHERE type='table'")))
        for table, source in (
            ("external_exec_invocations", "wrapper_thread_started"),
            ("codex_exec_launches", "cc_stdout_thread_started"),
        ):
            if table not in tables:
                continue
            batches = (
                [None]
                if thread_ids is None
                else [thread_ids[index : index + 400] for index in range(0, len(thread_ids), 400)]
            )
            result = []
            for batch in batches:
                restriction = " AND thread_id IN :threads" if batch else ""
                statement = text(
                    f"SELECT json_remove(payload,'$.receipts') FROM {table} "
                    "WHERE json_extract(payload,'$.source_namespace')=:namespace "
                    "AND json_extract(payload,'$.thread_id') IS NOT NULL "
                    "AND julianday(json_extract(payload,'$.started_at'))<=julianday(:until) "
                    "AND (json_extract(payload,'$.finished_at') IS NULL "
                    "OR julianday(json_extract(payload,'$.finished_at'))>=julianday(:since)) "
                    + restriction
                    + " ORDER BY started_at DESC LIMIT :limit"
                )
                values = {
                    "limit": _MAX_RECORDS + 1,
                    "namespace": source_namespace(codex_home),
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                }
                if batch:
                    statement = statement.bindparams(bindparam("threads", expanding=True))
                    values["threads"] = batch
                result.extend((await session.execute(statement, values)).scalars().all())
                if len(result) > _MAX_RECORDS:
                    return []  # Resource-limited is unknown; never silently truncate coverage.
            rows.extend((source, json.loads(raw) if isinstance(raw, str) else raw) for raw in result)
    # Local wrapper evidence survives API unavailability. Never initialize a spool.
    own_db = make_url(db_url).database
    spool = codex_home / "data/ai-team-os/external-exec.db"
    if own_db != str(spool.absolute()):
        local_rows = await asyncio.to_thread(_spool_rows, codex_home, since, until, thread_ids)
        if local_rows is None:
            return []
        rows.extend(("wrapper_thread_started", row) for row in local_rows)
    namespace = source_namespace(codex_home)
    unique: dict[str, CodexLaunchTierEvidence] = {}
    conflicts: set[str] = set()
    owners: dict[str, set[str]] = {}
    for _, row in rows:
        if row.get("source_namespace") == namespace and row.get("thread_id"):
            owners.setdefault(row["thread_id"], set()).add(row.get("launch_id") or row.get("invocation_id"))
    conflicts.update(thread for thread, launches in owners.items() if len(launches) != 1)
    for source, row in rows:
        if row.get("source_namespace") != namespace or not row.get("thread_id") or row.get("ephemeral"):
            continue
        if row.get("exec_mode") != "new" or row.get("requested_service_tier") is None:
            continue
        if source == "cc_stdout_thread_started" and row.get("state") != "bound":
            continue
        if row.get("requested_metadata_source") not in {"wrapper_argv", "cc_bash_command"}:
            continue
        if "spawn_failed" in row.get("diagnostics", []):
            continue
        try:
            evidence = CodexLaunchTierEvidence(
                launch_id=row.get("launch_id") or row["invocation_id"],
                source_namespace=namespace,
                thread_id=row["thread_id"],
                requested_service_tier=row["requested_service_tier"],
                started_at=row["started_at"],
                finished_at=row.get("finished_at"),
                turn_ids=row.get("turn_ids", []),
                binding_source="cc_output_header" if row.get("output_path") else source,
            )
        except (ValidationError, KeyError):
            continue
        if evidence.started_at > until or (evidence.finished_at is not None and evidence.finished_at < since):
            continue
        previous = unique.get(evidence.thread_id)
        if (
            previous
            and previous.launch_id == evidence.launch_id
            and previous.model_dump(exclude={"finished_at", "turn_ids"})
            == evidence.model_dump(exclude={"finished_at", "turn_ids"})
        ):
            if previous.finished_at is None or evidence.finished_at == previous.finished_at:
                unique[evidence.thread_id] = evidence
            elif evidence.finished_at is not None:
                conflicts.add(evidence.thread_id)
        elif previous and previous != evidence:
            conflicts.add(evidence.thread_id)
        else:
            unique[evidence.thread_id] = evidence
    return [value for key, value in unique.items() if key not in conflicts]
