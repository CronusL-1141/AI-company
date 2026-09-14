"""Bind forward-only local activity samples to contemporaneous native quotas.

The binding identifies a local login source, not complete cross-device account
coverage. Credential file metadata detects source changes; contents are never
read. Successful snapshots are committed by the caller's existing source fence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from aiteam.services.codex_account_capture import capture_plan_quota
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.types import PlanUsageSnapshot, PricingAccount, PricingQuotaSnapshot

_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _local_source() -> tuple[Path, str]:
    root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve(strict=True)
    if not root.is_dir() or not any((root / name).is_dir() for name in ("sessions", "archived_sessions")):
        raise OSError("local source is unavailable")
    metadata: list[tuple[str, int, int, int]] = []
    for name in ("auth.json", "config.toml"):
        path = root / name
        if path.is_symlink():
            raise OSError("local source metadata is not a regular file")
        try:
            stat = path.stat()
        except FileNotFoundError:
            if name == "auth.json":
                raise OSError("local source generation is unavailable") from None
            continue
        if not path.is_file():
            raise OSError("local source metadata is not a regular file")
        metadata.append((name, stat.st_ino, stat.st_size, stat.st_mtime_ns))
    generation = hashlib.sha256(json.dumps(
        ["local-jsonl-v1", str(root), metadata], separators=(",", ":"),
    ).encode()).hexdigest()
    return root, generation


def _local_snapshots(
    plans: list[PlanUsageSnapshot], *, total: int | None = None,
    scope: str | None = None, binding: datetime | None = None,
) -> list[PlanUsageSnapshot]:
    return [PlanUsageSnapshot.model_validate({
        **plan.model_dump(mode="python"), "source": "codex_local_logs",
        "activity_tokens": total, "activity_scope": scope,
        "activity_observed_at": plan.observed_at if total is not None else None,
        "activity_binding_at": binding,
    }) for plan in plans]


async def capture_local_plan_account(
    *, repository: AccountUsageRepository,
) -> tuple[PricingAccount, list[PricingQuotaSnapshot], list[PlanUsageSnapshot]]:
    """Sample only local events after a persisted account/source baseline.

No historical usage is assigned when connecting an account. Changing account,
login/config metadata or source directory starts a fresh zero baseline. Failure
keeps the quota and breaks the activity segment instead of reporting fake zero.
"""
    try:
        source_before = await asyncio.to_thread(_local_source)
    except (OSError, ValueError):
        source_before = None
    account, quotas, plans = await capture_plan_quota()
    unavailable = _local_snapshots(plans)
    try:
        source = await asyncio.to_thread(_local_source)
        if source_before != source:
            return account, quotas, unavailable
        root, generation = source
        scope = hashlib.sha256(f"{account.account_key}:{generation}".encode()).hexdigest()
        end = max(plan.observed_at for plan in plans)
        history = await repository.list_plan_snapshots(account.account_key)
        latest = max(history, key=lambda item: (item.observed_at, item.snapshot_id), default=None)
        total = 0
        binding = end
        if (
            latest is not None and latest.source == "codex_local_logs"
            and latest.activity_scope == scope and latest.activity_tokens is not None
            and latest.activity_binding_at is not None
        ):
            if latest.observed_at >= end:
                return account, quotas, unavailable
            from aiteam.services.codex_local_usage import read_local_usage_delta

            delta, counts = await read_local_usage_delta(root, latest.observed_at, end)
            if type(delta) is not int or not 0 <= delta <= _MAX_SAFE_INTEGER:
                return account, quotas, unavailable
            # An unfinished relevant row cannot be silently lost at the next
            # time boundary. Restart the segment after a complete later read.
            if counts.get("partial_tail", 0):
                return account, quotas, unavailable
            total = latest.activity_tokens + delta
            binding = latest.activity_binding_at
        if total > _MAX_SAFE_INTEGER or await asyncio.to_thread(_local_source) != source:
            return account, quotas, unavailable
        return account, quotas, _local_snapshots(plans, total=total, scope=scope, binding=binding)
    except (OSError, ValueError):
        return account, quotas, unavailable
