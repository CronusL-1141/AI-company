"""E32 feedback_intro: feedback to the OS maintainers can be given from a session.

Said once per host: at the first session start where this API process registers the
feedback tools, read the way the tool registration reads them (AITEAM_TOOLSETS and
AITEAM_READONLY of this process; ``aiteam.mcp.tools.toolsets.tool_registered``). The
API's HTTP MCP, which Codex uses, is registered from exactly this environment, and a
Claude Code session's MCP server starts the API with its own environment. Under
``AITEAM_TOOLSETS=default`` or the read-only profile there is nothing to offer, so
nothing is said and an open row clears.

Once the line went out to a host, the detector stops returning it: the row clears and
the notice lists stay free of it (``dedup=once`` already keeps it from being shown again).
"""

from __future__ import annotations

import os

from aiteam.mcp.tools.toolsets import tool_registered
from aiteam.services.notices.detectors import DetectContext, Finding, Scope

CATALOG_ID = "feedback_intro"


def offered() -> bool:
    """Whether this process registers feedback_draft (the tool the notice tells the model to use)."""
    return tool_registered("feedback", "feedback_draft", os.environ)


class FeedbackIntroDetector:
    """One key per host; nothing once that host was told, or while the tools are not on offer."""

    name = "feedback_intro"
    catalog_ids = (CATALOG_ID,)
    timing = frozenset({"session_start"})
    hosts = frozenset({"cc", "codex"})
    timeout_s = 0.3

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(keys=(f"{CATALOG_ID}:{ctx.host}",))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        key = f"{CATALOG_ID}:{ctx.host}"
        if not offered():
            return []
        told = await ctx.repo.list_notice_deliveries(host=ctx.host, keys=[key])
        if any(row.emitted_at is not None for row in told):
            return []
        return [Finding(catalog_id=CATALOG_ID, key=key, host=ctx.host)]
