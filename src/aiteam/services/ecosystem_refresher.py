"""Ecosystem incremental shallow-refresh service (v1.5.0-D).

Implements the periodic refresh + active-set recompute described in
``docs/v1.5.0-progressive-deep-review-design.md`` §6 / §7:

* ``EcosystemRefresher.shallow_refresh`` — the weekly cron entry point.
  Pulls the project's active set (top_n by stars), calls GitHub once per
  repo, writes an append-only ``EcosystemRepoStatusSnapshot``, and only
  re-queues a Stage 0 shallow-summary when the upstream repo has new
  pushes since last refresh.
* ``EcosystemRefresher.recompute_active_set`` — recompute which repos
  belong to the top_n active set after a scan run; promote climbers and
  demote drop-offs. Newly-active repos that lack a shallow summary are
  queued for Stage 0 immediately. Resurrected repos (formerly deleted /
  private but now alive again) clear failure flags, drop their
  ``lifecycle:deleted`` / ``lifecycle:private_now`` tags, and re-enter
  the queue.

Design constraints honoured here
--------------------------------
* **Active set only** — the refresher refuses to scan beyond the project
  ``top_n`` budget (decision §E in the design doc).
* **diff-based** — repos whose ``pushed_at <= last_shallow_refreshed_at``
  are skipped without spending an agent dispatch.
* **Failed flag handling** — 404 → ``mark_profile_deleted``,
  403 (non-rate-limit) → ``mark_profile_private``, 5xx / unknown →
  ``mark_profile_fetch_failure``. We never raise on a single repo
  failure; errors are collected into ``RefreshResult.errors`` so the
  caller can inspect them.
* **No commit / no push** — the service mutates DB rows only.
* **No subprocess** — GitHub access is funnelled through an injected
  ``gh_fetcher`` so unit tests can drive every branch.

Service is dispatch-aware: queueing a Stage 0 shallow-scan is delegated
to ``EcosystemShallowQueueWorker.queue_for_shallow`` so that Leader-side
agent dispatch logic stays in one place.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiteam.services.ecosystem_lifecycle import (
    LIFECYCLE_TAG_DELETED,
    LIFECYCLE_TAG_PRIVATE_NOW,
)
from aiteam.services.ecosystem_shallow_queue import (
    DispatchIntent,
    EcosystemShallowQueueWorker,
)
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemProjectSettings,
    EcosystemRepoProfile,
    EcosystemRepoStatusSnapshot,
    EcosystemScanRun,
    EcosystemScanStrategy,
)

logger = logging.getLogger(__name__)


# ============================================================
# Constants
# ============================================================

# Default refresh interval if the project has no settings row yet.
DEFAULT_REFRESH_INTERVAL_DAYS = 7

# Cron name registered via scheduler_create — kept here so the MCP/admin
# tooling can read it without hard-coding strings.
WEEKLY_REFRESH_CRON_NAME = "ecosystem_shallow_refresh_weekly"

# Event type emitted by the cron — a subscriber/MCP tool calls
# ``EcosystemRefresher.shallow_refresh`` when this event fires.
WEEKLY_REFRESH_EVENT_TYPE = "ecosystem.refresh.weekly"


def build_weekly_refresh_cron_payload(
    *,
    project_id: str,
    interval: str = "7 days",
) -> dict[str, Any]:
    """Return the canonical ``scheduler_create`` payload for the weekly refresh.

    Wires a scheduled ``emit_event`` action whose subscriber should call
    ``EcosystemRefresher.shallow_refresh`` for the given project. Kept
    here so the dashboard / admin scripts can register the cron without
    duplicating the magic strings.

    Example usage from an admin script::

        from aiteam.services.ecosystem_refresher import (
            build_weekly_refresh_cron_payload,
        )
        payload = build_weekly_refresh_cron_payload(project_id="proj-x")
        # then call MCP scheduler_create(**payload) or POST /api/scheduler.
    """
    return {
        "name": WEEKLY_REFRESH_CRON_NAME,
        "interval": interval,
        "action_type": "emit_event",
        "action_config": (
            '{"event_type": "' + WEEKLY_REFRESH_EVENT_TYPE + '",'
            ' "data": {"project_id": "' + project_id + '"}}'
        ),
        "description": (
            f"v1.5.0-D weekly ecosystem shallow refresh for {project_id}"
        ),
    }


# ============================================================
# GitHub fetch return shape (informal contract)
# ============================================================

# The injected gh_fetcher must return a dict with at least:
#   {
#     "http_status": int,
#     "stars": int (when 200),
#     "pushed_at": datetime | str | None (when 200),
#     "is_archived": bool (optional, default False),
#     "rate_limit_remaining": int | None (optional, only relevant for 403),
#     "error_message": str (optional, used when failure),
#   }
GhFetcher = Callable[[str], Awaitable[dict[str, Any]]]


# ============================================================
# Result types
# ============================================================


@dataclass
class RefreshResult:
    """Outcome of a single ``shallow_refresh`` invocation."""

    project_id: str
    active_total: int = 0
    refreshed: int = 0  # repos whose summary was actually re-queued
    skipped_no_diff: int = 0  # repos whose pushed_at hasn't moved
    snapshots_written: int = 0
    marked_deleted: int = 0
    marked_private: int = 0
    transient_errors: int = 0
    queued_intents: list[DispatchIntent] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    scan_run_id: str | None = None


@dataclass
class ActiveSetRecomputeResult:
    """Outcome of ``recompute_active_set``."""

    project_id: str
    active_total: int = 0
    promoted: int = 0  # was inactive → now active
    demoted: int = 0   # was active → now inactive
    queued_for_shallow: int = 0
    queued_intents: list[DispatchIntent] = field(default_factory=list)


@dataclass
class ResurrectionResult:
    """Outcome of a single resurrection attempt."""

    repo_id: str
    repo_full_name: str
    revived: bool
    queued: bool = False
    intent: DispatchIntent | None = None
    note: str = ""


# ============================================================
# Service
# ============================================================


class EcosystemRefresher:
    """Weekly incremental shallow-refresh + active-set recompute service.

    Args:
        repo: Shared StorageRepository (sqlite/in-memory).
        worker: ``EcosystemShallowQueueWorker`` used to enqueue Stage 0
            dispatches when an active repo gains a new push, when a
            previously-inactive repo enters the top_n, or when a
            deleted/private repo is revived.
        gh_fetcher: Async callable resolving ``owner/name`` → dict (see
            ``GhFetcher`` for the expected shape). When ``None`` the
            refresher only consumes cached data — useful in tests that
            stub GitHub behaviour.
        project_id: Explicit project scope; falls back to the repo's
            ``_project_scope`` so multi-project tests can scope each
            instance independently.
        scan_strategy: Strategy stamped on the synthetic ``ScanRun`` row
            backing each refresh batch (default ``INCREMENTAL``).
    """

    def __init__(
        self,
        repo: StorageRepository,
        worker: EcosystemShallowQueueWorker | None = None,
        *,
        gh_fetcher: GhFetcher | None = None,
        project_id: str = "",
        scan_strategy: EcosystemScanStrategy = EcosystemScanStrategy.INCREMENTAL,
    ) -> None:
        self._repo = repo
        self._worker = worker
        self._gh_fetcher = gh_fetcher
        self._project_id = project_id or repo._project_scope or ""
        self._scan_strategy = scan_strategy

    # ------------------------------------------------------------------
    # Public — shallow_refresh
    # ------------------------------------------------------------------

    async def shallow_refresh(
        self,
        *,
        triggered_by: str = "cron",
        notes: str = "",
    ) -> RefreshResult:
        """Run one weekly refresh cycle for the configured project.

        Steps:
          1. Resolve the project's ecosystem settings (auto-create on demand).
          2. Pull the active set (top_n by stars).
          3. For each profile, call GitHub once. Branch on http_status:
             * 200 → write snapshot; if ``pushed_at > last_shallow_refreshed_at``
               re-queue Stage 0; otherwise count as ``skipped_no_diff``.
             * 404 → ``mark_profile_deleted`` + add ``lifecycle:deleted`` tag.
             * 403 (non-rate-limit) → ``mark_profile_private`` + tag.
             * 403 (rate-limit) / 5xx / network → record transient error.
          4. Persist a synthetic ``EcosystemScanRun`` so analytics can
             attribute snapshots to a batch.

        Returns:
            ``RefreshResult`` with counts + dispatch intents the team-lead
            should hand to the Agent tool.
        """
        result = RefreshResult(project_id=self._project_id)
        if not self._project_id:
            result.errors.append("project_id 为空，refresher 无可识别项目")
            return result

        settings = await self._resolve_settings()

        # Stamp a scan run row so snapshots have a parent batch id.
        scan_run = EcosystemScanRun(
            strategy=self._scan_strategy,
            triggered_by=triggered_by,
            notes=notes
            or f"v1.5.0-D shallow refresh top_n={settings.top_n}",
            project_id=self._project_id,
        )
        await self._repo.create_scan_run(
            scan_run, project_id=self._project_id
        )
        result.scan_run_id = scan_run.id

        active_repos = await self._pull_active_repos(settings)
        result.active_total = len(active_repos)

        if not active_repos:
            await self._finalize_scan_run(scan_run.id, result)
            return result

        if self._gh_fetcher is None:
            result.errors.append(
                "gh_fetcher 未注入；refresher 跳过 GitHub 调用，仅记录批次"
            )
            await self._finalize_scan_run(scan_run.id, result)
            return result

        for profile in active_repos:
            try:
                await self._refresh_one(profile, scan_run.id, settings, result)
            except Exception as exc:  # graceful degradation
                logger.warning(
                    "refresher.refresh_failed repo=%s err=%s",
                    profile.repo_full_name,
                    exc,
                )
                result.errors.append(f"{profile.repo_full_name}: {exc!s}")

        await self._finalize_scan_run(scan_run.id, result)
        return result

    # ------------------------------------------------------------------
    # Public — recompute_active_set + resurrection
    # ------------------------------------------------------------------

    async def recompute_active_set(self) -> ActiveSetRecomputeResult:
        """Re-evaluate which profiles belong to the project's top_n active set.

        Promotion path: was ``is_active=False`` and now sits inside top_n
        with ``stars >= min_stars`` → flip to active and, if the repo has
        no shallow_summary yet, queue a Stage 0 dispatch.

        Demotion path: was ``is_active=True`` but is no longer top_n →
        flip to inactive (kept in DB per decision §D, append-only).
        """
        outcome = ActiveSetRecomputeResult(project_id=self._project_id)
        if not self._project_id:
            return outcome

        settings = await self._resolve_settings()

        # Pull every non-deleted / non-private profile to rank locally.
        profiles, _ = await self._repo.search_ecosystem_profiles_extended(
            limit=10_000,
            offset=0,
            sort="stars",
            project_id=self._project_id,
        )
        eligible = [
            p for p in profiles if not (p.is_deleted or p.is_private_now)
        ]
        # Stable rank by stars desc — ties broken by repo_full_name to keep
        # the output deterministic for tests.
        eligible.sort(key=lambda p: (-p.stars, p.repo_full_name))
        top_set: dict[str, int] = {}
        for rank, p in enumerate(eligible[: settings.top_n], start=1):
            if p.stars >= settings.min_stars:
                top_set[p.id] = rank

        outcome.active_total = len(top_set)
        for p in profiles:
            if p.is_deleted or p.is_private_now:
                # Failed profiles must never appear in the active set; flip
                # them off if a stale flag combination slipped through.
                if p.is_active or p.active_rank is not None:
                    await self._repo.update_profile_active_set(
                        p.id,
                        is_active=False,
                        active_rank=None,
                        project_id=self._project_id,
                    )
                continue
            new_active = p.id in top_set
            new_rank = top_set.get(p.id)
            if new_active == p.is_active and new_rank == p.active_rank:
                continue

            await self._repo.update_profile_active_set(
                p.id,
                is_active=new_active,
                active_rank=new_rank,
                project_id=self._project_id,
            )
            if new_active and not p.is_active:
                outcome.promoted += 1
                if not p.shallow_summary and self._worker is not None:
                    intent = await self._worker.queue_for_shallow(p.id)
                    if intent is not None:
                        outcome.queued_for_shallow += 1
                        outcome.queued_intents.append(intent)
            elif (not new_active) and p.is_active:
                outcome.demoted += 1

        return outcome

    async def resurrect(
        self,
        repo_id: str,
    ) -> ResurrectionResult:
        """Try to revive a previously deleted/private profile.

        Calls ``EcosystemShallowQueueWorker.revive_check_one`` (which
        already handles the GitHub probe + clearing failure flags), then,
        on success, drops the ``lifecycle:deleted`` / ``lifecycle:private_now``
        tags and re-queues a Stage 0 shallow scan so the resurrected repo
        gets a fresh summary.
        """
        profile = await self._repo.get_ecosystem_profile_by_id(
            repo_id, project_id=self._project_id or None
        )
        if profile is None:
            return ResurrectionResult(
                repo_id=repo_id,
                repo_full_name="<unknown>",
                revived=False,
                note="profile 不存在",
            )

        was_deleted = profile.is_deleted
        was_private = profile.is_private_now
        if not (was_deleted or was_private):
            return ResurrectionResult(
                repo_id=repo_id,
                repo_full_name=profile.repo_full_name,
                revived=False,
                note="profile 当前未处于 deleted/private 状态",
            )

        if self._worker is None:
            return ResurrectionResult(
                repo_id=repo_id,
                repo_full_name=profile.repo_full_name,
                revived=False,
                note="未注入 worker；resurrect 不可用",
            )

        revived = await self._worker.revive_check_one(repo_id)
        if not revived:
            return ResurrectionResult(
                repo_id=repo_id,
                repo_full_name=profile.repo_full_name,
                revived=False,
                note="GitHub 仍返回 deleted/private 状态",
            )

        # Drop lifecycle tags so the repo no longer shows up under
        # "已删除"/"已设私密" UI tabs.
        if was_deleted:
            await self._remove_lifecycle_tag(
                repo_id, LIFECYCLE_TAG_DELETED
            )
        if was_private:
            await self._remove_lifecycle_tag(
                repo_id, LIFECYCLE_TAG_PRIVATE_NOW
            )

        # Re-queue a fresh Stage 0 — the previous summary may be stale.
        intent = await self._worker.queue_for_shallow(
            repo_id, force_refresh=True
        )
        return ResurrectionResult(
            repo_id=repo_id,
            repo_full_name=profile.repo_full_name,
            revived=True,
            queued=intent is not None,
            intent=intent,
            note="复活成功，已撤 tag 并重新入队 Stage 0",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_settings(self) -> EcosystemProjectSettings:
        """Resolve project ecosystem settings with auto-create fallback."""
        existing = await self._repo.get_ecosystem_project_settings(
            self._project_id
        )
        if existing is not None:
            return existing
        return await self._repo.ensure_ecosystem_project_settings(
            self._project_id
        )

    async def _pull_active_repos(
        self,
        settings: EcosystemProjectSettings,
    ) -> list[EcosystemRepoProfile]:
        """Active set = top_n by stars, excluding deleted/private."""
        profiles, _ = await self._repo.search_ecosystem_profiles_extended(
            min_stars=settings.min_stars,
            limit=settings.top_n,
            offset=0,
            sort="stars",
            project_id=self._project_id,
        )
        return [
            p
            for p in profiles
            if p.is_active and not p.is_deleted and not p.is_private_now
        ]

    async def _refresh_one(
        self,
        profile: EcosystemRepoProfile,
        scan_run_id: str,
        settings: EcosystemProjectSettings,
        result: RefreshResult,
    ) -> None:
        """Process one profile: GitHub probe → snapshot → maybe queue Stage 0."""
        assert self._gh_fetcher is not None
        try:
            data = await self._gh_fetcher(profile.repo_full_name)
        except Exception as exc:  # pragma: no cover — defensive
            result.errors.append(
                f"{profile.repo_full_name}: gh_fetcher raised {exc!s}"
            )
            await self._repo.mark_profile_fetch_failure(
                profile.id,
                error_message=str(exc)[:240],
                project_id=self._project_id,
            )
            result.transient_errors += 1
            return

        http_status = int(data.get("http_status", 0))
        if http_status == 404:
            await self._repo.mark_profile_deleted(
                profile.id,
                error_message=str(data.get("error_message") or "GitHub 404"),
                project_id=self._project_id,
            )
            result.marked_deleted += 1
            return
        if http_status == 403:
            remaining = data.get("rate_limit_remaining")
            if remaining == 0:
                # Rate-limit: record transient error and keep going.
                await self._repo.mark_profile_fetch_failure(
                    profile.id,
                    error_message=str(
                        data.get("error_message") or "GitHub rate limit"
                    ),
                    project_id=self._project_id,
                )
                result.transient_errors += 1
                return
            await self._repo.mark_profile_private(
                profile.id,
                error_message=str(
                    data.get("error_message") or "GitHub 403 forbidden"
                ),
                project_id=self._project_id,
            )
            result.marked_private += 1
            return
        if http_status != 200:
            await self._repo.mark_profile_fetch_failure(
                profile.id,
                error_message=str(
                    data.get("error_message") or f"HTTP {http_status}"
                ),
                project_id=self._project_id,
            )
            result.transient_errors += 1
            return

        # 200 OK — write snapshot then evaluate diff.
        new_pushed_at = _coerce_datetime(data.get("pushed_at"))
        new_stars = int(data.get("stars", profile.stars))
        new_archived = bool(data.get("is_archived", profile.is_archived))
        snapshot = EcosystemRepoStatusSnapshot(
            project_id=self._project_id,
            repo_id=profile.id,
            scan_run_id=scan_run_id,
            stars=new_stars,
            pushed_at=new_pushed_at,
            is_archived=new_archived,
            is_active=profile.is_active,
            summary_at_time=profile.shallow_summary,
        )
        await self._repo.create_status_snapshot(
            snapshot, project_id=self._project_id
        )
        result.snapshots_written += 1

        if not _has_new_push(profile, new_pushed_at):
            result.skipped_no_diff += 1
            return

        if self._worker is None:
            return
        intent = await self._worker.queue_for_shallow(
            profile.id, force_refresh=True
        )
        if intent is not None:
            result.refreshed += 1
            result.queued_intents.append(intent)

    async def _finalize_scan_run(
        self,
        scan_run_id: str,
        result: RefreshResult,
    ) -> None:
        """Stamp the synthetic scan run with completion metadata."""
        await self._repo.update_scan_run(
            scan_run_id,
            completed_at=datetime.now(tz=UTC),
            repos_updated=result.refreshed,
            repos_skipped=result.skipped_no_diff,
            errors=result.errors[-50:],  # cap to avoid huge rows
            notes=(
                f"refreshed={result.refreshed} skipped={result.skipped_no_diff} "
                f"deleted={result.marked_deleted} private={result.marked_private} "
                f"transient={result.transient_errors}"
            ),
        )

    async def _remove_lifecycle_tag(
        self,
        repo_id: str,
        tag_name: str,
    ) -> None:
        """Drop a single lifecycle tag association if present."""
        tag = await self._repo.get_tag_by_name(tag_name)
        if tag is None:
            return
        await self._repo.remove_repo_tag(
            repo_id, tag.id, project_id=self._project_id or None
        )


# ============================================================
# Pure helpers (unit-testable)
# ============================================================


def _coerce_datetime(value: Any) -> datetime | None:
    """Convert ISO 8601 strings or datetime objects into UTC datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _has_new_push(
    profile: EcosystemRepoProfile,
    new_pushed_at: datetime | None,
) -> bool:
    """Return True when the upstream push beats our last refresh timestamp.

    First-time refresh (last_shallow_refreshed_at is None OR shallow_summary
    is empty) always counts as a diff so we generate the initial summary.
    """
    if not profile.shallow_summary:
        return True
    last = profile.last_shallow_refreshed_at
    if last is None:
        return True
    if new_pushed_at is None:
        return False
    last_aware = last if last.tzinfo else last.replace(tzinfo=UTC)
    new_aware = (
        new_pushed_at
        if new_pushed_at.tzinfo
        else new_pushed_at.replace(tzinfo=UTC)
    )
    return new_aware > last_aware


__all__ = [
    "EcosystemRefresher",
    "RefreshResult",
    "ActiveSetRecomputeResult",
    "ResurrectionResult",
    "DEFAULT_REFRESH_INTERVAL_DAYS",
    "WEEKLY_REFRESH_CRON_NAME",
    "WEEKLY_REFRESH_EVENT_TYPE",
    "build_weekly_refresh_cron_payload",
]
