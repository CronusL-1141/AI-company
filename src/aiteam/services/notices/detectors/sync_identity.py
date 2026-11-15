"""E31 sync_identity_refused: node.json declares a sync role this machine refused.

The process then runs standalone (aiteam.sync.node.load_node_status): starting the
API matters more than syncing, but the user must hear that writes stopped reaching
the center. The refusal is decided once at startup and kept in aiteam.sync.context,
so the check reads memory only.
"""

from __future__ import annotations

from aiteam.services.notices.detectors import DetectContext, Finding, Scope
from aiteam.sync import context as sync_context

KEY = "sync_identity_refused"


class SyncIdentityDetector:
    """Report a node identity refused at startup."""

    name = "sync_identity"
    catalog_ids = (KEY,)
    timing = frozenset({"session_start", "prompt"})
    hosts = frozenset({"cc", "codex"})
    timeout_s = 0.3

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(keys=(KEY,))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        role = sync_context.refused_role()
        return [Finding(catalog_id=KEY, key=KEY, params={"role": role})] if role else []
