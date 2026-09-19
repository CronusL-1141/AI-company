"""Bounded, read-only local Codex token deltas, without account attribution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import threading
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

from pydantic import ValidationError

from aiteam.clock import from_timestamp
from aiteam.services.codex_usage_export import CodexUsageExportError, _unique_object, parse_usage_time
from aiteam.types import PricingRequestLine, PricingUsageEntry

_MAX_FILES = 2000
_MAX_DIRECTORY_ENTRIES = 40000
_MAX_BYTES = 512 * 1024 * 1024
_MAX_LINE_BYTES = 8 * 1024 * 1024
_MAX_USAGE_RECORDS = 200000
_MAX_SCAN_SECONDS = 8.0
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_PRICING_ENTRIES = 10000
_OFFICIAL_CHATGPT_ENDPOINT = "https://chatgpt.com/backend-api/codex"
_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
           "output_tokens", "reasoning_output_tokens")


class CodexLocalUsageError(ValueError):
    """A static diagnostic; never includes source paths, identifiers, or text."""

    def __init__(self, code: str = "invalid_log") -> None:
        self.code = code
        super().__init__({
            "invalid_window": "本机日志采样时间窗口无效。",
            "unreadable": "本机日志无法安全读取。",
            "invalid_log": "本机日志包含损坏或不完整的已完成记录。",
            "conflict": "本机日志存在重复记录冲突。",
            "rollback": "本机日志累计计数或事件时间发生回退。",
            "limit": "本机日志读取超过安全预算；未返回部分结果。",
            "cancelled": "本机日志读取已停止。",
        }.get(code, "本机日志读取失败。"))


@dataclass
class _Budget:
    stop: threading.Event
    counts: Counter[str] = field(default_factory=Counter)
    deadline: float = field(default_factory=lambda: time.monotonic() + _MAX_SCAN_SECONDS)
    aliases: frozenset[str] = frozenset()
    alias_configured_at: datetime | None = None

    def check(self) -> None:
        if self.stop.is_set():
            raise CodexLocalUsageError("cancelled")
        if time.monotonic() > self.deadline:
            raise CodexLocalUsageError("limit")

    def add(self, key: str, amount: int, limit: int) -> None:
        self.check()
        self.counts[key] += amount
        if self.counts[key] > limit:
            raise CodexLocalUsageError("limit")


@dataclass(frozen=True)
class _Usage:
    values: tuple[int, ...]

    @property
    def tokens(self) -> int:
        return self.values[0] + self.values[3]

    def subtract(self, previous: _Usage) -> _Usage:
        if any(new < old for new, old in zip(self.values, previous.values, strict=True)):
            raise CodexLocalUsageError("rollback")
        return _Usage(tuple(new - old for new, old in zip(self.values, previous.values, strict=True)))


@dataclass(frozen=True)
class _Ledger:
    response_id: str
    timestamp: datetime
    usage: _Usage
    provider: str | None
    thread_id: str | None
    model: str | None = field(default=None, compare=False)
    service_tier: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class _Legacy:
    timestamp: datetime
    total: object
    last: object
    provider: str | None


@dataclass(frozen=True)
class _Increment:
    timestamp: datetime
    usage: _Usage
    provider: str | None


@dataclass
class _Session:
    session_id: str | None = None
    parent_id: str | None = None
    is_child: bool = False
    started_at: datetime | None = None
    ledger: list[_Ledger] = field(default_factory=list)
    legacy: list[_Legacy] = field(default_factory=list)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _custom_auth_key(key: str) -> bool:
    return (
        key in {"env_key", "http_headers", "env_http_headers"}
        or "bearer" in key.lower() or ("auth" in key.lower() and key != "requires_openai_auth")
    )


def _mapping_digest(config: dict | None, aliases: frozenset[str]) -> str:
    selected = config.get("model_provider", "openai") if config is not None else None
    providers = config.get("model_providers") if config is not None else None
    provider = providers.get(selected) if isinstance(providers, dict) and isinstance(selected, str) else None
    route = {
        key: value for key, value in provider.items()
        if key in {"base_url", "wire_api", "requires_openai_auth"} or _custom_auth_key(key)
    } if isinstance(provider, dict) else None
    summary = [
        "chatgpt-provider-mapping-v2", config is not None, _OFFICIAL_CHATGPT_ENDPOINT,
        selected, config.get("forced_login_method") if config is not None else None,
        route, sorted(aliases),
    ]
    return hashlib.sha256(json.dumps(summary, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _provider_aliases(
    descriptor: int,
) -> tuple[frozenset[str], datetime | None, tuple[int, ...] | None, str]:
    """Read only the bounded provider mapping; never open authentication files."""
    try:
        child = os.open("config.toml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    except FileNotFoundError:
        return frozenset(), None, None, _mapping_digest({}, frozenset())
    with os.fdopen(child, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise CodexLocalUsageError("unreadable")
        if before.st_size > _MAX_CONFIG_BYTES:
            raise CodexLocalUsageError("limit")
        raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise CodexLocalUsageError("limit")
        if _stat_signature(before) != _stat_signature(os.fstat(stream.fileno())):
            raise CodexLocalUsageError("unreadable")
    configured_at = from_timestamp(before.st_mtime)
    signature = _stat_signature(before)
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        # An invalid mapping grants no alias capability. Native openai log
        # records still retain their existing provider provenance.
        return frozenset(), configured_at, signature, _mapping_digest(None, frozenset())
    providers = config.get("model_providers")
    aliases: set[str] = set()
    if config.get("forced_login_method") == "chatgpt" and isinstance(providers, dict):
        for name, provider in providers.items():
            if not isinstance(provider, dict) or name == "openai":
                continue
            custom_auth = any(_custom_auth_key(key) for key in provider)
            if (
                provider.get("base_url") == _OFFICIAL_CHATGPT_ENDPOINT
                and provider.get("requires_openai_auth") is True
                and provider.get("wire_api") == "responses" and not custom_auth
            ):
                aliases.add(name)
    accepted = frozenset(aliases)
    return accepted, configured_at, signature, _mapping_digest(config, accepted)


def _verify_provider_config(descriptor: int, expected: tuple[int, ...] | None) -> None:
    try:
        current = _stat_signature(os.stat("config.toml", dir_fd=descriptor, follow_symlinks=False))
    except FileNotFoundError:
        current = None
    if current != expected:
        raise CodexLocalUsageError("unreadable")


def local_provider_mapping_digest(codex_home: Path) -> str:
    """Fingerprint the accepted mapping semantics without persisting config."""
    try:
        descriptor = _open_directory(codex_home)
        try:
            _, _, signature, digest = _provider_aliases(descriptor)
            _verify_provider_config(descriptor, signature)
        finally:
            os.close(descriptor)
        return digest
    except (OSError, ValueError):
        raise CodexLocalUsageError("unreadable") from None


def _event_provider(
    provider: str | None, evidence_at: datetime | None, timestamp: datetime, budget: _Budget,
) -> str | None:
    if provider not in budget.aliases:
        return provider
    # A historical provider ID may once have meant a different endpoint. Require
    # persisted metadata/settings after this mapping became effective. A trusted
    # persisted mapping timestamp survives unrelated configuration rewrites.
    # A child's later metadata cannot reclassify its earlier inherited history.
    if (
        evidence_at is None or budget.alias_configured_at is None
        or evidence_at < budget.alias_configured_at or timestamp < evidence_at
    ):
        budget.counts["stale_alias_evidence_records_skipped"] += 1
        return provider
    budget.counts["official_alias_records_recognized"] += 1
    return "openai"


def _timestamp(value: object) -> datetime:
    try:
        return parse_usage_time(value)
    except CodexUsageExportError:
        raise CodexLocalUsageError("invalid_log") from None


def _usage(value: object, *, ledger: bool = False) -> _Usage:
    required = _FIELDS[:4] if ledger else ("input_tokens", "output_tokens")
    if not isinstance(value, dict) or any(key not in value for key in required):
        raise CodexLocalUsageError("invalid_log")
    numbers = tuple(value.get(key, 0) for key in _FIELDS)
    if any(type(number) is not int or number < 0 for number in numbers):
        raise CodexLocalUsageError("invalid_log")
    usage = _Usage(numbers)
    if numbers[1] > numbers[0] or numbers[2] > numbers[0] or numbers[4] > numbers[3]:
        raise CodexLocalUsageError("invalid_log")
    if "total_tokens" in value and (
        type(value["total_tokens"]) is not int or value["total_tokens"] != usage.tokens
    ):
        raise CodexLocalUsageError("invalid_log")
    return usage


def _read_file(stream: BinaryIO, budget: _Budget) -> _Session:
    session = _Session()
    provider: str | None = None
    provider_since: datetime | None = None
    provider_evidence_at: datetime | None = None
    current_model: str | None = None
    current_turn: str | None = None
    current_service_tier: str | None = None
    metadata_seen = False
    while True:
        budget.check()
        raw = stream.readline(_MAX_LINE_BYTES + 1)
        if not raw:
            break
        budget.add("bytes_read", len(raw), _MAX_BYTES)
        if len(raw) > _MAX_LINE_BYTES:
            raise CodexLocalUsageError("limit")
        # A writer may still be producing the last JSON object. Only terminated
        # lines belong to this read; a complete malformed line is never skipped.
        if not raw.endswith(b"\n"):
            budget.counts["partial_tail"] += 1
            break
        if not raw.strip():
            continue
        try:
            row = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            raise CodexLocalUsageError("invalid_log") from None
        if not isinstance(row, dict):
            raise CodexLocalUsageError("invalid_log")
        kind = row.get("type")
        payload = row.get("payload")
        if kind not in {"session_meta", "turn_context", "token_usage_record", "event_msg"}:
            continue
        if not isinstance(payload, dict):
            raise CodexLocalUsageError("invalid_log")
        if kind == "session_meta":
            if metadata_seen:
                raise CodexLocalUsageError("conflict")
            metadata_seen = True
            current_model = current_turn = None
            current_service_tier = _text(payload.get("service_tier"))
            session.session_id = _text(payload.get("id")) or _text(payload.get("session_id"))
            session.started_at = _timestamp(row.get("timestamp"))
            provider_evidence_at = session.started_at
            provider = _text(payload.get("model_provider"))
            source = payload.get("source")
            subagent = source.get("subagent") if isinstance(source, dict) else None
            spawned = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
            session.parent_id = (
                _text(payload.get("forked_from_id")) or _text(payload.get("parent_thread_id"))
                or (_text(spawned.get("parent_thread_id")) if isinstance(spawned, dict) else None)
            )
            session.is_child = bool(session.parent_id) or subagent is not None
        elif kind == "turn_context":
            current_model = _text(payload.get("model"))
            current_turn = _text(payload.get("turn_id"))
            if "service_tier" in payload:
                current_service_tier = _text(payload.get("service_tier"))
        elif kind == "event_msg" and payload.get("type") == "thread_settings_applied":
            settings = payload.get("thread_settings")
            # This exact field is present in native persisted settings events.
            # Absence preserves the current provider; explicit null clears it.
            if isinstance(settings, dict) and "model_provider_id" in settings:
                timestamp = _timestamp(row.get("timestamp"))
                if provider_since is not None and timestamp < provider_since:
                    raise CodexLocalUsageError("rollback")
                provider_since = timestamp
                provider_evidence_at = timestamp
                provider = _text(settings["model_provider_id"])
                budget.counts["provider_settings"] += 1
            if isinstance(settings, dict) and "service_tier" in settings:
                current_service_tier = _text(settings["service_tier"])
                budget.counts["service_tier_settings"] += 1
        elif kind == "token_usage_record":
            budget.add("usage_records", 1, _MAX_USAGE_RECORDS)
            response_id = _text(payload.get("response_id"))
            if response_id is None:
                raise CodexLocalUsageError("invalid_log")
            timestamp = _timestamp(row.get("timestamp"))
            if provider_since is not None and timestamp < provider_since:
                raise CodexLocalUsageError("rollback")
            model = _text(payload.get("model"))
            turn = _text(payload.get("turn_id"))
            if model is None and turn is not None and turn == current_turn:
                model = current_model
            session.ledger.append(_Ledger(
                response_id, timestamp, _usage(payload.get("usage"), ledger=True),
                _event_provider(provider, provider_evidence_at, timestamp, budget),
                _text(payload.get("thread_id")), model,
                _text(payload.get("service_tier")) or current_service_tier,
            ))
        elif kind == "event_msg" and payload.get("type") == "token_count":
            budget.add("usage_records", 1, _MAX_USAGE_RECORDS)
            info = payload.get("info")
            if info is None:
                budget.counts["quota_only_events"] += 1
                continue
            if not isinstance(info, dict):
                raise CodexLocalUsageError("invalid_log")
            timestamp = _timestamp(row.get("timestamp"))
            if provider_since is not None and timestamp < provider_since:
                raise CodexLocalUsageError("rollback")
            session.legacy.append(_Legacy(
                timestamp, info.get("total_token_usage"),
                info.get("last_token_usage"), _event_provider(provider, provider_evidence_at, timestamp, budget),
            ))
    return session


def _open_directory(path: Path) -> int:
    """Open every component without following links, including ancestors."""
    path = path.expanduser().absolute()
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _scan_directory(descriptor: int, start: datetime, budget: _Budget, sessions: list[_Session]) -> None:
    budget.check()
    with os.scandir(descriptor) as entries:
        for entry in entries:
            budget.add("directory_entries", 1, _MAX_DIRECTORY_ENTRIES)
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                budget.counts["symlinks_skipped"] += 1
                continue
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    _scan_directory(child, start, budget, sessions)
                finally:
                    os.close(child)
            elif entry.name.endswith(".jsonl") and stat.S_ISREG(metadata.st_mode):
                budget.add("files_considered", 1, _MAX_FILES)
                if metadata.st_mtime <= start.timestamp():
                    budget.counts["old_files_skipped"] += 1
                    continue
                if metadata.st_size > _MAX_BYTES - budget.counts["bytes_read"]:
                    raise CodexLocalUsageError("limit")
                child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                with os.fdopen(child, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (
                        opened.st_dev, opened.st_ino
                    ) != (metadata.st_dev, metadata.st_ino):
                        raise CodexLocalUsageError("unreadable")
                    budget.counts["files_read"] += 1
                    sessions.append(_read_file(stream, budget))
                    if os.fstat(stream.fileno()).st_size < opened.st_size:
                        raise CodexLocalUsageError("rollback")


def _increments(session: _Session, budget: _Budget) -> list[_Increment]:
    previous: _Usage | None = None
    previous_time: datetime | None = None
    result: list[_Increment] = []
    for event in session.legacy:
        budget.check()
        if previous_time is not None and event.timestamp < previous_time:
            raise CodexLocalUsageError("rollback")
        previous_time = event.timestamp
        total = _usage(event.total) if event.total is not None else None
        last = _usage(event.last) if event.last is not None else None
        if total is None:
            # Without a cumulative anchor, a repeated snapshot is indistinguishable
            # from a new completion. Do not turn that ambiguity into token usage.
            if last is not None:
                budget.counts["unanchored_legacy_events_skipped"] += 1
                continue
            raise CodexLocalUsageError("invalid_log")
        delta = total.subtract(previous) if previous is not None else total
        if previous == total:
            budget.counts["duplicate_cumulative_snapshots"] += 1
        else:
            used = last if last is not None else delta
            if used.tokens:
                result.append(_Increment(event.timestamp, used, event.provider))
        previous = total
    return result


def _eligible(provider: str | None, budget: _Budget) -> bool:
    if provider == "openai":
        return True
    budget.counts["unknown_provider_events_skipped" if provider is None else "other_provider_events_skipped"] += 1
    return False


def _pricing_gap(budget: _Budget, key: str, count: int = 1) -> None:
    if count:
        budget.counts[key] += count
        budget.counts["pricing_incomplete"] = 1


def _append_pricing_entry(event: _Ledger, entries: list[PricingUsageEntry], budget: _Budget) -> None:
    if event.model is None:
        _pricing_gap(budget, "pricing_missing_model")
        return
    try:
        service_tier = event.service_tier or "standard"
        budget.counts["pricing_service_tier_observed" if event.service_tier else "pricing_service_tier_assumed"] += 1
        request = PricingRequestLine.model_validate({
            "request_id": event.response_id, "model": event.model, "service_tier": service_tier,
            **dict(zip(_FIELDS[:4], event.usage.values[:4], strict=True)),
        })
        entry = PricingUsageEntry(occurred_at=event.timestamp, request=request)
    except ValidationError:
        _pricing_gap(budget, "pricing_invalid_requests")
        return
    entries.append(entry)
    if len(entries) > _MAX_PRICING_ENTRIES:
        raise CodexLocalUsageError("limit")


def _sum_sessions(
    sessions: list[_Session], start: datetime, end: datetime, budget: _Budget,
    pricing_entries: list[PricingUsageEntry] | None = None,
) -> int:
    ledger: dict[str, _Ledger] = {}
    original_responses: set[str] = set()
    grouped: dict[str, _Session] = {}
    ledger_session_ids = {session.session_id for session in sessions if session.ledger and session.session_id}
    total = 0
    for session in sessions:
        budget.check()
        if session.ledger:
            budget.counts["ledger_files"] += 1
            budget.counts["token_count_events_ignored"] += len(session.legacy)
            for event in session.ledger:
                budget.check()
                # A copied response retains its original owner. Its replay time
                # and the child's provider cannot conflict with the owner's row.
                if event.thread_id and session.session_id and event.thread_id != session.session_id:
                    budget.counts["inherited_response_records_skipped"] += 1
                    continue
                previous = ledger.get(event.response_id)
                if previous is not None:
                    if previous != event or (pricing_entries is not None and (
                        previous.model != event.model or previous.service_tier != event.service_tier
                    )):
                        raise CodexLocalUsageError("conflict")
                    budget.counts["duplicate_response_records"] += 1
                ledger[event.response_id] = event
                if session.is_child and event.thread_id is None and session.parent_id not in ledger_session_ids:
                    budget.counts["unanchored_child_response_records_skipped"] += 1
                    if pricing_entries is not None and start < event.timestamp <= end and event.provider == "openai":
                        _pricing_gap(budget, "pricing_unidentified_events")
                else:
                    original_responses.add(event.response_id)
            continue
        if session.session_id in ledger_session_ids:
            budget.counts["legacy_copies_of_ledger_sessions_skipped"] += 1
            continue
        if session.session_id is None:
            budget.counts["unidentified_legacy_files_skipped"] += 1
            if pricing_entries is not None:
                _pricing_gap(budget, "pricing_unidentified_events", sum(
                    start < event.timestamp <= end and event.provider == "openai" for event in session.legacy
                ))
            continue
        previous_session = grouped.get(session.session_id)
        if previous_session is not None:
            if (session.parent_id, session.is_child, session.started_at) != (
                previous_session.parent_id, previous_session.is_child, previous_session.started_at,
            ):
                raise CodexLocalUsageError("conflict")
            common = min(len(previous_session.legacy), len(session.legacy))
            if previous_session.legacy[:common] != session.legacy[:common]:
                raise CodexLocalUsageError("conflict")
            budget.counts["duplicate_session_files"] += 1
            if len(previous_session.legacy) >= len(session.legacy):
                continue
        grouped[session.session_id] = session
    for response_id in original_responses:
        budget.check()
        event = ledger[response_id]
        if not start < event.timestamp <= end:
            budget.counts["out_of_window_events"] += 1
        elif _eligible(event.provider, budget):
            total += event.usage.tokens
            budget.counts["ledger_events_counted"] += 1
            if pricing_entries is not None:
                _append_pricing_entry(event, pricing_entries, budget)
    streams = {key: _increments(session, budget) for key, session in grouped.items()}
    for key, session in grouped.items():
        events = streams[key]
        if pricing_entries is not None:
            _pricing_gap(budget, "pricing_unidentified_events", sum(
                event.total is None and event.last is not None
                and start < event.timestamp <= end and event.provider == "openai"
                for event in session.legacy
            ))
        if session.is_child:
            parent = streams.get(session.parent_id or "")
            prefix = [event for event in parent or [] if event.timestamp <= session.started_at]
            # Only a complete, nonempty parent prefix establishes an exact replay
            # boundary. Missing parents, partial matches and cycles are skipped.
            ancestors: set[str] = {key}
            ancestor = session.parent_id
            while ancestor in grouped and ancestor not in ancestors:
                budget.check()
                ancestors.add(ancestor)
                ancestor = grouped[ancestor].parent_id
            cycle = ancestor in ancestors
            if cycle or not prefix or len(events) < len(prefix) or any(
                child.usage != original.usage or child.provider != original.provider
                for child, original in zip(events, prefix)
            ):
                budget.counts["ambiguous_legacy_children_skipped"] += 1
                if pricing_entries is not None:
                    _pricing_gap(budget, "pricing_unidentified_events", sum(
                        start < event.timestamp <= end and event.provider == "openai" for event in events
                    ))
                continue
            budget.counts["replayed_legacy_events_skipped"] += len(prefix)
            events = events[len(prefix):]
        for event in events:
            budget.check()
            if not start < event.timestamp <= end:
                budget.counts["out_of_window_events"] += 1
            elif _eligible(event.provider, budget):
                total += event.usage.tokens
                budget.counts["legacy_events_counted"] += 1
                if pricing_entries is not None:
                    _pricing_gap(budget, "pricing_legacy_events")
    return total


def _read_sync(
    codex_home: Path, start: datetime, end: datetime, stop: threading.Event,
    *, pricing_entries: list[PricingUsageEntry] | None = None,
    provider_mapping_evidence: tuple[str, datetime] | None = None,
) -> tuple[int, dict[str, int]]:
    budget = _Budget(stop)
    sessions: list[_Session] = []
    try:
        descriptor = _open_directory(codex_home)
        try:
            aliases, configured_at, config_signature, mapping_digest = _provider_aliases(descriptor)
            if provider_mapping_evidence is not None:
                expected_digest, evidenced_at = provider_mapping_evidence
                if expected_digest != mapping_digest or evidenced_at > start:
                    raise CodexLocalUsageError("unreadable")
                configured_at = evidenced_at
            if aliases and configured_at is not None and configured_at <= start:
                budget.aliases = aliases
                budget.alias_configured_at = configured_at
                budget.counts["official_provider_aliases"] = len(aliases)
            elif aliases:
                budget.counts["alias_configuration_newer_than_window"] += 1
            for area in ("sessions", "archived_sessions"):
                budget.check()
                try:
                    metadata = os.stat(area, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    budget.counts["missing_session_directories"] += 1
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    budget.counts["symlinks_skipped"] += 1
                    continue
                child = os.open(area, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    _scan_directory(child, start, budget, sessions)
                finally:
                    os.close(child)
            _verify_provider_config(descriptor, config_signature)
        finally:
            os.close(descriptor)
        total = _sum_sessions(sessions, start, end, budget, pricing_entries)
        budget.check()
        return total, dict(budget.counts)
    except CodexLocalUsageError:
        raise
    except (OSError, ValueError, RecursionError):
        raise CodexLocalUsageError("unreadable") from None


def _read_combined_sync(
    codex_home: Path, start: datetime, end: datetime, stop: threading.Event,
    *, provider_mapping_evidence: tuple[str, datetime] | None = None,
) -> tuple[int, list[PricingUsageEntry], dict[str, int]]:
    entries: list[PricingUsageEntry] = []
    total, counts = _read_sync(
        codex_home, start, end, stop, pricing_entries=entries,
        provider_mapping_evidence=provider_mapping_evidence,
    )
    counts["pricing_incomplete"] = int(bool(counts.get("pricing_incomplete") or counts.get("partial_tail")))
    counts["pricing_entries"] = len(entries)
    return total, sorted(entries, key=lambda entry: (entry.occurred_at, entry.request.request_id)), counts


def _read_pricing_sync(
    codex_home: Path, start: datetime, end: datetime, stop: threading.Event,
    *, provider_mapping_evidence: tuple[str, datetime] | None = None,
) -> tuple[list[PricingUsageEntry], dict[str, int]]:
    _, entries, counts = _read_combined_sync(
        codex_home, start, end, stop, provider_mapping_evidence=provider_mapping_evidence,
    )
    return entries, counts


async def read_local_usage_delta(
    codex_home: Path, since: datetime, until: datetime,
    *, provider_mapping_evidence: tuple[str, datetime] | None = None,
) -> tuple[int, dict[str, int]]:
    """Return input (including cache) plus output in ``(since, until]``.

    This is a local sample, not an account-wide usage claim. Provider provenance
    comes from session metadata and persisted ``model_provider_id`` settings.
    Explicit ChatGPT aliases require stable official endpoint/auth configuration
    and provider evidence newer than that mapping. A caller may supply its
    persisted mapping digest and original configured-at time to preserve this
    boundary across unrelated rewrites. A mismatched mapping rejects the read.
    This is a local mapping
    estimate, not per-request proof of the endpoint or authentication used.
    A ledger file uses globally identified responses exclusively. Legacy forks
    require an exact available parent prefix; ambiguous logs are diagnosed.
    Complete corruption, conflicts and resource limits reject the entire read.
    Cancellation joins this call's cooperative reader before it propagates.
    """
    return cast(tuple[int, dict[str, int]], await _read_async(
        codex_home, since, until, pricing=False, provider_mapping_evidence=provider_mapping_evidence,
    ))


async def read_local_pricing_usage(
    codex_home: Path, since: datetime, until: datetime,
    *, provider_mapping_evidence: tuple[str, datetime] | None = None,
) -> tuple[list[PricingUsageEntry], dict[str, int]]:
    """Return bounded identified responses for local request pricing.

    No rates or dollar amounts are calculated here. An explicit native
    ``service_tier`` is preserved; records without one fall back to ``standard``.
    Payload models win; otherwise fallback requires the matching persisted turn
    context. Spark and unknown catalog models retain their names for the caller's
    bucket/price rules.
    ``pricing_incomplete == 1`` forbids treating the returned entries as a complete
    interval: missing models, legacy/ambiguous usage and partial tails cause gaps.
    Conflicts, corruption and resource limits raise rather than truncate the output.
    """
    return cast(
        tuple[list[PricingUsageEntry], dict[str, int]],
        await _read_async(
            codex_home, since, until, pricing=True, provider_mapping_evidence=provider_mapping_evidence,
        ),
    )


async def _read_async(
    codex_home: Path, since: datetime, until: datetime, *, pricing: bool, combined: bool = False,
    provider_mapping_evidence: tuple[str, datetime] | None = None,
) -> tuple[int | list[PricingUsageEntry], dict[str, int]] | tuple[int, list[PricingUsageEntry], dict[str, int]]:
    if not isinstance(since, datetime) or not isinstance(until, datetime) or any(
        value.tzinfo is None or value.utcoffset() is None for value in (since, until)
    ):
        raise CodexLocalUsageError("invalid_window")
    start, end = since.astimezone(UTC), until.astimezone(UTC)
    if start > end:
        raise CodexLocalUsageError("invalid_window")
    if provider_mapping_evidence is not None and (
        not isinstance(provider_mapping_evidence, tuple) or len(provider_mapping_evidence) != 2
        or not isinstance(provider_mapping_evidence[0], str)
        or not isinstance(provider_mapping_evidence[1], datetime)
        or provider_mapping_evidence[1].tzinfo is None or provider_mapping_evidence[1].utcoffset() is None
        or provider_mapping_evidence[1] > start
    ):
        raise CodexLocalUsageError("unreadable")
    if start == end:
        if combined:
            return 0, [], {"baseline_only": 1, "pricing_incomplete": 0, "pricing_entries": 0}
        if pricing:
            return [], {"baseline_only": 1, "pricing_incomplete": 0, "pricing_entries": 0}
        return 0, {"baseline_only": 1}
    stop = threading.Event()
    read_sync = _read_combined_sync if combined else (_read_pricing_sync if pricing else _read_sync)
    options = {} if provider_mapping_evidence is None else {"provider_mapping_evidence": provider_mapping_evidence}
    worker = asyncio.create_task(asyncio.to_thread(read_sync, codex_home, start, end, stop, **options))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        stop.set()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if worker.done() and not worker.cancelled():
            worker.exception()
        raise


async def read_local_usage_and_pricing(
    codex_home: Path, since: datetime, until: datetime,
    *, provider_mapping_evidence: tuple[str, datetime] | None = None,
) -> tuple[int, list[PricingUsageEntry], dict[str, int]]:
    """Return tokens and pricing evidence from one bounded scan of the interval.

    Both outputs share the same parsed records, even if a writer appends a row
    before the call returns. Pricing gaps remain explicit in the diagnostics;
    an incomplete entry list must not be used as a complete dollar numerator.
    """
    return cast(
        tuple[int, list[PricingUsageEntry], dict[str, int]],
        await _read_async(
            codex_home, since, until, pricing=True, combined=True, provider_mapping_evidence=provider_mapping_evidence,
        ),
    )
