"""E31 sync_identity_refused: the node identity did not hold at startup.

How the node runs then is decided once at startup (aiteam.sync.node.decide) and kept
in aiteam.sync.context, so the check reads memory only. The variant says which case:

* ``held`` (the default variant): the database is this machine's sync node; it keeps the node's id and role,
  its sync channel is stopped;
* ``unbound``: the database belongs to a node on other hardware; rows get a temporary
  origin of this machine and nothing syncs;
* ``standalone``: a node.json was refused and the database never belonged to a sync
  node; the node runs standalone.
"""

from __future__ import annotations

from aiteam.services.notices.detectors import DetectContext, Finding, Scope
from aiteam.sync import context as sync_context

KEY = "sync_identity_refused"
HELD = "held"  # the default variant


class SyncIdentityDetector:
    """Report a node identity that did not hold at startup."""

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
        trouble = sync_context.identity_trouble()
        if trouble is None:
            return []
        mode, role = trouble
        return [Finding(catalog_id=KEY, key=KEY, params={"role": role}, variant="" if mode == HELD else mode)]
