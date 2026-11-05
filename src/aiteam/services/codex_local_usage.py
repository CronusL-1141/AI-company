"""Shared primitives for local Codex logs: safe access, provider mapping, token arithmetic.

The prediction no longer scans these logs itself: it consumes the usage journal
(``codex_usage_projection``). The thread usage reader (``codex_rollout_usage``),
the journal recorder and the projection share what remains here.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from aiteam.clock import from_timestamp
from aiteam.services.codex_usage_export import CodexUsageExportError, parse_usage_time

_MAX_LINE_BYTES = 8 * 1024 * 1024
_MAX_SCAN_SECONDS = 8.0
_MAX_CONFIG_BYTES = 1024 * 1024
_OFFICIAL_CHATGPT_ENDPOINT = "https://chatgpt.com/backend-api/codex"
_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
           "output_tokens", "reasoning_output_tokens")
_T = TypeVar("_T")


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
    """Cooperative cancellation and a time limit for a bounded computation, plus its counts."""

    stop: threading.Event
    counts: Counter[str] = field(default_factory=Counter)
    deadline: float = field(default_factory=lambda: time.monotonic() + _MAX_SCAN_SECONDS)

    def check(self) -> None:
        if self.stop.is_set():
            raise CodexLocalUsageError("cancelled")
        if time.monotonic() > self.deadline:
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
class _Legacy:
    timestamp: datetime
    total: object
    last: object
    provider: str | None
    # What the caller reads this event from; carried to its increment, never compared.
    origin: object = field(default=None, compare=False)


@dataclass(frozen=True)
class _Increment:
    timestamp: datetime
    usage: _Usage
    provider: str | None
    origin: object = field(default=None, compare=False)


@dataclass
class _Session:
    session_id: str | None = None
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


def in_codex_home(codex_home: Path, action: Callable[[int], _T]) -> _T:
    """Run ``action`` on the home's directory descriptor; any failure is "unreadable"."""
    try:
        descriptor = _open_directory(codex_home)
        try:
            return action(descriptor)
        finally:
            os.close(descriptor)
    except CodexLocalUsageError:
        raise
    except (OSError, ValueError):
        raise CodexLocalUsageError("unreadable") from None


def local_provider_mapping_digest(codex_home: Path) -> str:
    """Fingerprint the accepted mapping semantics without persisting config."""
    def digest(descriptor: int) -> str:
        _, _, signature, value = _provider_aliases(descriptor)
        _verify_provider_config(descriptor, signature)
        return value

    try:
        return in_codex_home(codex_home, digest)
    except CodexLocalUsageError:
        raise CodexLocalUsageError("unreadable") from None


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
                result.append(_Increment(event.timestamp, used, event.provider, event.origin))
        previous = total
    return result
