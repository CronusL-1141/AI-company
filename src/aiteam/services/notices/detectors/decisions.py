"""E08 decisions_pending: real pending decisions waiting on the user.

One predicate decides what a "real" pending decision is, for the notice, the
session-start briefing (``GET /api/leader-briefings?real_only=true``) and
anything else that counts them: automatic permission-denial records are not
decisions, whatever their status.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta

from aiteam.services.notices.detectors import DetectContext, Finding, Scope
from aiteam.types import LeaderBriefing

AUTO_TAG = "auto:permission-denied"
AUTO_TITLE_PREFIX = "Agent denied:"
EXPIRY = timedelta(days=14)
# Expiry is a write; do it at most this often per process on the read paths.
_EXPIRY_EVERY_S = 600.0
_last_expiry: dict[str, float] = {}


def is_real_pending(briefing: LeaderBriefing) -> bool:
    """A pending briefing that actually waits on a human decision."""
    if briefing.status != "pending":
        return False
    if AUTO_TAG in (briefing.tags or []):
        return False
    return not (briefing.title or "").startswith(AUTO_TITLE_PREFIX)


def in_scope(briefing: LeaderBriefing, project_id: str) -> bool:
    """This project's rows plus rows bound to no project."""
    return (briefing.project_id or "") in ("", project_id or "")


async def expire_stale(repo, now: datetime, *, force: bool = False) -> int:
    """Move pending briefings older than 14 days to ``expired`` (status only)."""
    key = getattr(repo, "_db_url", "") or ""
    last = _last_expiry.get(key)
    if not force and last is not None and time.monotonic() - last < _EXPIRY_EVERY_S:
        return 0
    _last_expiry[key] = time.monotonic()
    return await repo.expire_stale_briefings(now - EXPIRY)


async def pending_decisions(repo, project_id: str, now: datetime) -> list[LeaderBriefing]:
    """Real pending decisions for a project, newest first (expires stale ones first)."""
    await expire_stale(repo, now)
    items = await repo.list_briefings(status="pending")
    return [item for item in items if is_real_pending(item) and in_scope(item, project_id)]


class DecisionsDetector:
    """Aggregate notice: "N decisions are waiting for you"."""

    name = "decisions"
    catalog_ids = ("decisions_pending",)
    timing = frozenset({"session_start", "prompt"})
    hosts = frozenset({"cc", "codex"})
    timeout_s = 0.3

    @staticmethod
    def _scope_id(ctx: DetectContext) -> str:
        """The project, or the unregistered folder, this count belongs to."""
        from aiteam.services.notices.detectors.registration import dir_scope

        if ctx.project_id:
            return ctx.project_id
        real = str(ctx.facts.get("real_dir") or "")
        return dir_scope(real) if real else ""

    def applies(self, ctx: DetectContext) -> bool:
        # Session-bound: without a project or folder the count has no audience.
        return bool(self._scope_id(ctx))

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=("decisions_pending:",), project_id=self._scope_id(ctx))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        scope_id = self._scope_id(ctx)
        items = await pending_decisions(ctx.repo, ctx.project_id, ctx.now)
        if not items:
            return []
        ids = ",".join(sorted(item.id for item in items))
        digest = hashlib.sha256(f"{scope_id}|{ids}".encode()).hexdigest()[:8]
        return [Finding(
            catalog_id="decisions_pending",
            key=f"decisions_pending:{digest}",
            params={"n": len(items), "title": items[0].title},
            project_id=scope_id,
        )]
