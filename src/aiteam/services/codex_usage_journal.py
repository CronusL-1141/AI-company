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
        return CodexUsageContext(
            session_id=_identifier(payload.get("id")) or _identifier(payload.get("session_id")),
            parent_thread_id=(_identifier(payload.get("parent_thread_id"))
                              or _identifier(payload.get("forked_from_id"))
                              or (_identifier(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else None)),
            provider=_identifier(payload.get("model_provider")),
        ), None
    if kind == "turn_context":
        return context.model_copy(update={
            "turn_id": _identifier(payload.get("turn_id")), "model": _identifier(payload.get("model")),
        }), None
    if kind == "event_msg" and payload.get("type") == "thread_settings_applied":
        settings = payload.get("thread_settings")
        if isinstance(settings, dict) and "model_provider_id" in settings:
            context = context.model_copy(update={"provider": _identifier(settings["model_provider_id"])})
        return context, None
    if kind != "token_usage_record" and payload.get("type") != "token_count":
        return context, None
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
    provider = _identifier(payload.get("model_provider")) if "model_provider" in payload else context.provider
    fact = {
        "kind": "ledger" if kind == "token_usage_record" else "legacy",
        "occurred_at": occurred_at, "response_id": _identifier(payload.get("response_id")),
        "session_id": context.session_id, "thread_id": _identifier(payload.get("thread_id")),
        "parent_thread_id": context.parent_thread_id, "turn_id": turn_id,
        "model": model, "model_source": model_source, "provider": provider,
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
    fact["diagnostics"] = sorted(set(diagnostics))
    return context, fact


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
                cursor.pending_bytes += len(raw)
                cursor.pending_sha256 = line_hash
                status = "scan_limited"
                continue
            end = stream.tell()
            if oversized:
                cursor.context = CodexUsageContext()
                fact = {"kind": "diagnostic", "diagnostics": ["oversized_line"]}
            elif raw.strip():
                cursor.context, fact = _decode(raw, cursor.context)
            else:
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
