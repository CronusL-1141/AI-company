"""Read local response usage ledgers without account binding or cumulative billing."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from aiteam.types import PricingRequestLine, PricingUsageEntry

_TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
)
_MAX_LINE_BYTES = 8 * 1024 * 1024
_MAX_ENTRIES = 1000


class CodexUsageExportError(ValueError):
    """A safe diagnostic with no source text or private file paths."""


def parse_usage_time(value: object) -> datetime:
    """Require an ISO timestamp carrying an explicit UTC offset."""
    if not isinstance(value, str):
        raise CodexUsageExportError("时间必须是包含时区的 ISO 时间。")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CodexUsageExportError("时间必须是包含时区的 ISO 时间。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CodexUsageExportError("时间必须包含时区，例如 Z 或 +08:00。")
    return parsed.astimezone(UTC)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _rows(path: Path, counts: Counter[str]) -> Iterator[dict | None]:
    with path.open("rb") as stream:
        while raw := stream.readline(_MAX_LINE_BYTES + 1):
            if len(raw) > _MAX_LINE_BYTES:
                while raw and not raw.endswith(b"\n"):
                    raw = stream.readline(_MAX_LINE_BYTES + 1)
                counts["oversized_records"] += 1
                yield None
                continue
            if not raw.strip():
                continue
            try:
                row = json.loads(raw, object_pairs_hook=_unique_object)
            except (ValueError, RecursionError):
                counts["invalid_json"] += 1
                yield None
                continue
            if not isinstance(row, dict):
                counts["invalid_json"] += 1
                yield None
                continue
            yield row


def export_codex_usage(
    session_root: Path, since: str, until: str, service_tier: str,
) -> tuple[list[PricingUsageEntry], dict[str, int]]:
    """Export identified per-response usage in (since, until], or reject conflicts.

    token_count has no stable response identity in supported rollouts. A file
    with response ledgers uses those ledgers exclusively; a legacy file with
    only token_count events reports the unidentified events instead of guessing.
    The caller supplies a pricing assumption, not an observed billing tier.
    """
    start, end = parse_usage_time(since), parse_usage_time(until)
    if start >= end:
        raise CodexUsageExportError("开始时间必须早于结束时间。")
    tiers = get_args(PricingRequestLine.model_fields["service_tier"].annotation)
    if service_tier not in tiers:
        raise CodexUsageExportError("不支持的 service-tier；请显式选择已支持的计价档位。")
    root = session_root.expanduser()
    if not root.is_dir():
        raise CodexUsageExportError("session-root 必须是可读目录。")

    counts: Counter[str] = Counter()
    entries: dict[str, PricingUsageEntry] = {}
    fingerprints: dict[str, bytes] = {}
    for path in sorted(root.rglob("*.jsonl")):
        if path.is_symlink():
            counts["symlinks_skipped"] += 1
            continue
        if not path.is_file():
            continue
        counts["files_read"] += 1
        current_model: str | None = None
        current_turn: str | None = None
        ledger_count = 0
        anonymous_events = 0
        for row in _rows(path, counts):
            if row is None:
                current_model = current_turn = None
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            kind = row.get("type")
            if kind == "session_meta":
                current_model = current_turn = None
            elif kind == "turn_context":
                current_model = _text(payload.get("model"))
                current_turn = _text(payload.get("turn_id"))
            elif kind == "event_msg" and payload.get("type") == "token_count":
                try:
                    timestamp = parse_usage_time(row.get("timestamp"))
                except CodexUsageExportError:
                    counts["unidentified_event_time"] += 1
                    continue
                if start < timestamp <= end:
                    anonymous_events += 1
            elif kind == "token_usage_record":
                ledger_count += 1
                try:
                    timestamp = parse_usage_time(row.get("timestamp"))
                except CodexUsageExportError:
                    counts["invalid_timestamp"] += 1
                    continue
                in_window = start < timestamp <= end
                if not in_window:
                    counts["out_of_window"] += 1
                else:
                    counts["ledger_records"] += 1
                response_id = _text(payload.get("response_id"))
                if response_id is None:
                    counts["missing_request_id"] += in_window
                    continue
                turn = _text(payload.get("turn_id"))
                model = _text(payload.get("model")) or current_model
                if turn and current_turn and turn != current_turn:
                    model = _text(payload.get("model"))
                if model is None:
                    counts["missing_model"] += in_window
                    continue
                usage = payload.get("usage")
                if not isinstance(usage, dict) or not all(key in usage for key in _TOKEN_FIELDS):
                    counts["invalid_tokens"] += in_window
                    continue
                try:
                    request = PricingRequestLine.model_validate({
                        "request_id": response_id,
                        "model": model,
                        "service_tier": service_tier,
                        **{key: usage[key] for key in _TOKEN_FIELDS},
                    })
                    entry = PricingUsageEntry(occurred_at=timestamp, request=request)
                except ValidationError:
                    counts["invalid_tokens"] += in_window
                    continue
                fingerprint = hashlib.sha256(entry.model_dump_json().encode()).digest()
                old = fingerprints.get(response_id)
                if old is not None:
                    if old != fingerprint:
                        raise CodexUsageExportError("发现重复 response_id 内容冲突；未输出任何导入数据。")
                    counts["duplicate_records"] += in_window
                    continue
                fingerprints[response_id] = fingerprint
                if not in_window:
                    continue
                entries[response_id] = entry
                if len(entries) > _MAX_ENTRIES:
                    raise CodexUsageExportError("结果超过 1000 条；请缩小时间区间，不会静默截断。")
        key = "token_count_events_ignored" if ledger_count else "unidentified_token_count"
        counts[key] += anonymous_events
    return sorted(entries.values(), key=lambda entry: (entry.occurred_at, entry.request.request_id)), dict(counts)
