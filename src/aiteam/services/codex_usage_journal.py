"""Bounded native JSONL scanning with a strict persisted-field whitelist."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from aiteam.clock import utc_now
from aiteam.services.codex_initial_settings import initial_settings_match, row_time
from aiteam.services.codex_local_usage import _open_directory
from aiteam.services.codex_usage_export import _unique_object, parse_usage_time
from aiteam.types import (
    CodexUsageContext,
    CodexUsageObservation,
    CodexUsageScanBatch,
    CodexUsageSourceCursor,
    CodexUsageTokens,
)

_EMPTY = hashlib.sha256(b"").hexdigest()
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")


def source_namespace(codex_home: Path) -> str:
    """Identify a local root without storing its path in the database."""
    return hashlib.sha256(str(codex_home.expanduser().absolute()).encode()).hexdigest()


def _open_file(path: Path) -> BinaryIO:
    parent = _open_directory(path.parent)
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    stream = os.fdopen(descriptor, "rb")
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        stream.close()
        raise ValueError("unsafe_journal_source")
    return stream


def _source_id(namespace: str, metadata: os.stat_result) -> str:
    identity = [namespace, metadata.st_dev, metadata.st_ino, getattr(metadata, "st_birthtime", None)]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def probe_source(path: Path, namespace: str) -> str:
    with _open_file(path) as stream:
        return _source_id(namespace, os.fstat(stream.fileno()))


def _walk(path: Path) -> Iterator[Path | None]:
    """Yield an entry marker as well as files so discovery itself is bounded."""
    try:
        descriptor = _open_directory(path)
    except OSError:
        return
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                yield None
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    yield from _walk(path / entry.name)
                elif entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                    yield path / entry.name
    finally:
        os.close(descriptor)


class CodexJournalDiscovery:
    """Continue discovery across ticks instead of repeatedly favoring early files."""

    def __init__(self, codex_home: Path) -> None:
        self._root = codex_home
        self._iterator: Iterator[Path | None] | None = None
        self.budget_exhausted = False

    def _files(self) -> Iterator[Path | None]:
        for name in ("sessions", "archived_sessions"):
            yield from _walk(self._root / name)

    def next_paths(self, *, max_files: int, max_entries: int, deadline: float) -> list[Path]:
        self.budget_exhausted = True
        if self._iterator is None:
            self._iterator = self._files()
        paths = []
        for _ in range(max_entries):
            if time.monotonic() >= deadline:
                break
            try:
                path = next(self._iterator)
            except StopIteration:
                self._iterator = None
                self.budget_exhausted = False
                break
            if path is not None:
                paths.append(path)
                if len(paths) >= max_files:
                    break
        return paths

    def close(self) -> None:
        if self._iterator is not None:
            self._iterator.close()
            self._iterator = None


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _tokens(value: object, diagnostics: list[str]) -> CodexUsageTokens | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        diagnostics.append("invalid_tokens")
        return None
    selected = {}
    for field in CodexUsageTokens.model_fields:
        if field in value:
            number = value[field]
            if type(number) is int and number >= 0:
                selected[field] = number
            else:
                diagnostics.append("invalid_tokens")
    return CodexUsageTokens(**selected)


def _decode(raw: bytes, context: CodexUsageContext) -> tuple[CodexUsageContext, dict | None]:
    pending_turn_at = context.initial_settings_turn_at
    if pending_turn_at is not None:
        context = context.model_copy(update={"initial_settings_turn_at": None})
    try:
        row = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        return CodexUsageContext(), {"kind": "diagnostic", "diagnostics": ["invalid_json"]}
    if not isinstance(row, dict):
        return CodexUsageContext(), {"kind": "diagnostic", "diagnostics": ["invalid_schema"]}
    kind, payload = row.get("type"), row.get("payload")
    if kind not in ("session_meta", "turn_context", "event_msg", "token_usage_record"):
        return context, None
    if not isinstance(payload, dict):
        return CodexUsageContext(), {"kind": "diagnostic", "diagnostics": ["invalid_schema"]}
    if kind == "session_meta":
        source = payload.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        parent = (_identifier(payload.get("parent_thread_id")) or _identifier(payload.get("forked_from_id"))
                  or (_identifier(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else None))
        started_at = row_time(row)
        return CodexUsageContext(
            session_id=_identifier(payload.get("id")) or _identifier(payload.get("session_id")),
            parent_thread_id=parent,
            provider=_identifier(payload.get("model_provider")),
            turn_context_seen=False,
            usage_seen=False,
            thread_service_tier=_identifier(payload.get("service_tier")),
            thread_service_tier_source="session_meta" if "service_tier" in payload else None,
            provider_evidence_at=started_at,
            session_child=bool(parent) or subagent is not None,
            session_source=_identifier(source),
            session_started_at=started_at,
            first_turn_seen=False,
            thread_service_tier_present="service_tier" in payload,
        ), None
    if kind == "turn_context":
        turn_id = _identifier(payload.get("turn_id"))
        explicit_tier = "service_tier" in payload
        initial_turn_at = None
        if (context.session_id and context.turn_context_seen is False and context.usage_seen is False
                and not explicit_tier and context.thread_service_tier_source is None
                and turn_id and _identifier(payload.get("model"))):
            initial_turn_at = row_time(row)
        # Only a cursor that saw the session start can name its first turn.
        first_turn = {"first_turn_seen": True, "first_turn_id": turn_id} if context.first_turn_seen is False else {}
        return context.model_copy(update={
            "initial_settings_turn_at": initial_turn_at,
            "turn_context_seen": True,
            "turn_id": turn_id, "model": _identifier(payload.get("model")),
            "service_tier": (
                _identifier(payload["service_tier"]) if explicit_tier else context.thread_service_tier
            ),
            "service_tier_source": (
                "matching_turn_context" if explicit_tier else context.thread_service_tier_source
            ),
            "service_tier_present": True if explicit_tier else context.thread_service_tier_present,
            "service_tier_turn_id": turn_id,
            **first_turn,
        }), None
    if kind == "event_msg" and payload.get("type") == "thread_settings_applied":
        settings = payload.get("thread_settings")
        if initial_settings_match(row, turn_at=pending_turn_at,
                                  session_id=context.session_id, model=context.model):
            context = context.model_copy(update={
                "service_tier": _identifier(settings["service_tier"]),
                "service_tier_source": "thread_settings",
                "service_tier_present": True,
            })
        if isinstance(settings, dict) and "model_provider_id" in settings:
            context = context.model_copy(update={
                "provider": _identifier(settings["model_provider_id"]), "provider_evidence_at": row_time(row),
            })
        if isinstance(settings, dict) and "service_tier" in settings:
            context = context.model_copy(update={
                "thread_service_tier": _identifier(settings["service_tier"]),
                "thread_service_tier_source": "thread_settings",
                "thread_service_tier_present": True,
            })
        return context, None
    if kind != "token_usage_record" and payload.get("type") != "token_count":
        return context, None
    context = context.model_copy(update={"usage_seen": True})
    info = payload.get("info")
    if kind == "event_msg" and info is None:
        return context, None
    diagnostics: list[str] = []
    try:
        occurred_at = parse_usage_time(row.get("timestamp"))
    except ValueError:
        occurred_at = None
        diagnostics.append("invalid_timestamp")
    turn_id = _identifier(payload.get("turn_id"))
    model = _identifier(payload.get("model"))
    model_source = "payload" if model is not None else None
    if model is None and turn_id is not None and turn_id == context.turn_id and context.model:
        model, model_source = context.model, "matching_turn_context"
    if "model_provider" in payload:
        # The record states its own provider: it is evidence at its own time.
        provider, provider_evidence_at = _identifier(payload.get("model_provider")), occurred_at
    else:
        provider, provider_evidence_at = context.provider, context.provider_evidence_at
    # A turn snapshot wins over later settings changes, including explicit null.
    # Without a matching turn, do not attach another turn's tier to this request.
    service_tier, service_tier_source, service_tier_present = None, None, None
    if "service_tier" in payload:
        service_tier, service_tier_source = _identifier(payload["service_tier"]), "payload"
        service_tier_present = True
    elif turn_id is not None and turn_id == context.service_tier_turn_id:
        service_tier, service_tier_source = context.service_tier, context.service_tier_source
        service_tier_present = context.service_tier_present
    elif (
        context.turn_context_seen is False
        and context.turn_id is None and context.service_tier_turn_id is None
        and context.service_tier_source is None
    ):
        service_tier, service_tier_source = context.thread_service_tier, context.thread_service_tier_source
        service_tier_present = context.thread_service_tier_present
    fact = {
        "kind": "ledger" if kind == "token_usage_record" else "legacy",
        "occurred_at": occurred_at, "response_id": _identifier(payload.get("response_id")),
        "session_id": context.session_id, "thread_id": _identifier(payload.get("thread_id")),
        "parent_thread_id": context.parent_thread_id, "turn_id": turn_id,
        "model": model, "model_source": model_source, "provider": provider,
        "service_tier": service_tier, "service_tier_source": service_tier_source,
        "service_tier_present": service_tier_present, "provider_evidence_at": provider_evidence_at,
        "session_child": context.session_child, "session_source": context.session_source,
        "session_started_at": context.session_started_at, "first_turn_id": context.first_turn_id,
    }
    if kind == "token_usage_record":
        fact["usage"] = _tokens(payload.get("usage"), diagnostics)
        if fact["response_id"] is None:
            diagnostics.append("missing_response_id")
    elif isinstance(info, dict):
        fact["total_token_usage"] = _tokens(info.get("total_token_usage"), diagnostics)
        fact["last_token_usage"] = _tokens(info.get("last_token_usage"), diagnostics)
    else:
        diagnostics.append("invalid_schema")
    if model is None:
        diagnostics.append("missing_model")
    if provider is None:
        diagnostics.append("missing_provider")
    if service_tier is None:
        diagnostics.append("missing_service_tier")
    fact["diagnostics"] = sorted(set(diagnostics))
    return context, fact


# Native rows serialise these leading fields in this order; anything else is unknown.
_ROW_KIND = re.compile(
    rb'\{\s*(?:"timestamp"\s*:\s*"[^"\\]{0,64}"\s*,\s*)?(?:"ordinal"\s*:\s*[0-9]{1,20}\s*,\s*)?'
    rb'"type"\s*:\s*"([a-z_]{1,64})"(?:\s*,\s*"payload"\s*:\s*\{\s*"type"\s*:\s*"([A-Za-z_]{1,64})")?',
)
_CONTEXT_KINDS = frozenset({"session_meta", "turn_context", "token_usage_record"})
_CONTEXT_EVENTS = frozenset({"thread_settings_applied", "token_count"})


def _sniff_kind(prefix: bytes) -> str | None:
    """The record kind named by an oversized line's opening bytes, if recognisable."""
    match = _ROW_KIND.match(prefix[:512])
    if match is None:
        return None
    kind, event = match.group(1).decode(), match.group(2)
    return kind if event is None else f"{kind}/{event.decode()}"


def _skip_oversized(kind: str | None, context: CodexUsageContext) -> tuple[CodexUsageContext, dict | None]:
    """Skip an unparsed oversized line, keeping the context only when it cannot change it.

    Long sessions write compacted history, tool output and completed items far
    beyond the line budget. Dropping the whole parse context there lost the
    session, provider and model of every later fact in the file.
    """
    root, _, event = (kind or "").partition("/")
    relevant = (
        kind is None or root in _CONTEXT_KINDS
        or (root == "event_msg" and (not event or event in _CONTEXT_EVENTS))
    )
    if relevant:
        return CodexUsageContext(), {"kind": "diagnostic", "diagnostics": ["oversized_line"]}
    return context.model_copy(update={"initial_settings_turn_at": None}), None


def _digest_at(stream: BinaryIO, start: int, length: int) -> str:
    stream.seek(start)
    return hashlib.sha256(stream.read(length)).hexdigest()


def scan_journal_file(
    path: Path, namespace: str, previous: CodexUsageSourceCursor | None,
    *, max_bytes: int = 2 * 1024 * 1024 + 32768, max_line_bytes: int = 1024 * 1024,
    deadline: float | None = None, max_observations: int = 1000,
) -> CodexUsageScanBatch:
    """Read one bounded file slice; callers run this function in a worker thread."""
    if max_bytes < 16384 or max_line_bytes <= 0 or max_observations <= 0:
        raise ValueError("invalid_journal_budget")
    deadline = time.monotonic() + 1 if deadline is None else deadline
    with _open_file(path) as stream:
        before = os.fstat(stream.fileno())
        source_id = _source_id(namespace, before)
        if previous is not None and (previous.source_id != source_id or previous.source_namespace != namespace):
            raise ValueError("journal_source_changed")
        revision = previous.revision if previous is not None else None
        cursor = previous.model_copy(deep=True) if previous is not None else CodexUsageSourceCursor(
            source_id=source_id, source_namespace=namespace, revision=0,
            chain_sha256=_EMPTY, checkpoint_sha256=_EMPTY, prefix_sha256=_EMPTY,
            context=CodexUsageContext(turn_context_seen=False),
        )
        read_offset = cursor.offset + cursor.pending_bytes
        checkpoint_length = min(4096, read_offset)
        checked_bytes = checkpoint_length + cursor.prefix_length
        changed = before.st_size < read_offset or (
            _digest_at(stream, read_offset - checkpoint_length, checkpoint_length) != cursor.checkpoint_sha256
            or _digest_at(stream, 0, cursor.prefix_length) != cursor.prefix_sha256
        )
        if changed:
            cursor = CodexUsageSourceCursor(
                source_id=source_id, source_namespace=namespace, revision=cursor.revision,
                generation=cursor.generation + 1, chain_sha256=_EMPTY,
                checkpoint_sha256=_EMPTY, prefix_sha256=_EMPTY,
                context=CodexUsageContext(turn_context_seen=False),
            )
            read_offset = 0
        stream.seek(read_offset)
        observations = []
        bytes_read = checked_bytes
        status = "caught_up"
        while stream.tell() < before.st_size:
            if (
                time.monotonic() >= deadline or bytes_read >= max_bytes - 8192
                or len(observations) >= max_observations
            ):
                status = "scan_limited"
                break
            remaining = min(max_line_bytes + 1, max_bytes - bytes_read - 8192)
            raw = stream.readline(remaining)
            bytes_read += len(raw)
            if not raw:
                break
            complete = raw.endswith(b"\n")
            oversized = cursor.pending_bytes > 0 or len(raw) > max_line_bytes
            if not complete and (len(raw) <= max_line_bytes or remaining < max_line_bytes + 1):
                status = "partial_tail" if stream.tell() >= before.st_size else "scan_limited"
                break
            line_hash = hashlib.sha256(raw).hexdigest()
            if cursor.pending_sha256 is not None:
                line_hash = hashlib.sha256((cursor.pending_sha256 + line_hash).encode()).hexdigest()
            if not complete:
                if not cursor.pending_bytes:
                    cursor.pending_kind = _sniff_kind(raw)
                cursor.pending_bytes += len(raw)
                cursor.pending_sha256 = line_hash
                status = "scan_limited"
                continue
            end = stream.tell()
            if oversized:
                kind = cursor.pending_kind if cursor.pending_bytes else _sniff_kind(raw)
                cursor.context, fact = _skip_oversized(kind, cursor.context)
            elif raw.strip():
                cursor.context, fact = _decode(raw, cursor.context)
            else:
                cursor.context = cursor.context.model_copy(update={"initial_settings_turn_at": None})
                fact = None
            cursor.chain_sha256 = hashlib.sha256((cursor.chain_sha256 + line_hash).encode()).hexdigest()
            if fact is not None:
                observation_id = hashlib.sha256(
                    f"{namespace}:{cursor.chain_sha256}:{cursor.offset}:{end}".encode(),
                ).hexdigest()
                observations.append(CodexUsageObservation(
                    observation_id=observation_id, source_namespace=namespace, source_id=source_id,
                    generation=cursor.generation, byte_start=cursor.offset, byte_end=end,
                    line_sha256=line_hash, **fact,
                ))
            cursor.offset = end
            cursor.pending_bytes = 0
            cursor.pending_sha256 = None
            cursor.pending_kind = None
            status = "caught_up"
        if os.fstat(stream.fileno()).st_size < before.st_size:
            raise ValueError("journal_source_changed")
        read_offset = cursor.offset + cursor.pending_bytes
        tail_length = min(4096, read_offset)
        cursor.checkpoint_sha256 = _digest_at(stream, read_offset - tail_length, tail_length)
        cursor.prefix_length = min(4096, read_offset)
        cursor.prefix_sha256 = _digest_at(stream, 0, cursor.prefix_length)
        bytes_read += tail_length + cursor.prefix_length
        cursor.revision = (revision or 0) + 1
        cursor.status = status
        cursor.checked_at = utc_now()
        return CodexUsageScanBatch(
            expected_revision=revision, cursor=cursor, observations=observations, bytes_read=bytes_read,
        )
