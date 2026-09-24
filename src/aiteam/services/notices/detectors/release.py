"""E09 release_available: a newer stable release exists.

Uses the shared ``release_checker`` (6-hour cache, 1-second network budget) and
the API-side installation detection, so the command matches how OS was really
installed. Nothing is fetched or cleared when the check has no answer.
"""

from __future__ import annotations

import aiteam
from aiteam.services.notices import install_kind
from aiteam.services.notices.detectors import DetectContext, Finding, NoDataError, Scope


class ReleaseDetector:
    """One key per host and release version; a newer release supersedes it."""

    name = "release"
    catalog_ids = ("release_available",)
    timing = frozenset({"session_start"})
    hosts = frozenset({"cc", "codex"})
    timeout_s = 1.0

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=(f"release_available:{ctx.host}:",))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        from aiteam.api.release_updates import release_checker

        kind = await install_kind.detect(ctx.host)
        status = await release_checker.check(aiteam.__version__, "en", kind.kind)
        if status.status == "unknown" or not status.latest_version:
            raise NoDataError
        if status.status != "update_available":
            return []
        return [Finding(
            catalog_id="release_available",
            key=f"release_available:{ctx.host}:{status.latest_version}",
            params={
                "ver": f"v{status.latest_version}",
                "old": f"v{status.current_version}",
                "url": status.release_url or "",
            },
            variant=kind.variant,
            # The command belongs to this host's installation: never show it to the other host.
            host=ctx.host,
        )]
