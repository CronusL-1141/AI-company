"""E30 asset_backup_attention: what the workflow backup of this machine's folders could not do.

Two kinds, the stop first: an enrolled folder that cannot be read (variant
``folder_missing``), an OS data folder that is not there (``store_missing``), or a
backup paused because this node is an edge of the multi-host sync (``paused_edge``,
one per enrolled folder of this machine), then
files it could not keep: held back by the secret scan (until the user allows or
excludes them), over the size limit, links (never followed), unreadable files and
folders. One key per set: a new item, or one resolved, supersedes the notice.
Reads the service's cached comparison lists, not the table.
"""

from __future__ import annotations

import hashlib

from aiteam.services.notices.detectors import DetectContext, Finding, NoDataError, Scope


class AssetBackupDetector:
    """Aggregate notice: "the workflow backup skipped N files, latest: <file>"."""

    name = "asset_backup"
    catalog_ids = ("asset_backup_attention",)
    timing = frozenset({"session_start", "prompt"})
    hosts = frozenset({"cc"})
    timeout_s = 0.3

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=("asset_backup_attention:",))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        from aiteam.api.deps import asset_backup_if_started
        from aiteam.assets.service import FOLDER_MISSING, PAUSED_EDGE, STORE_MISSING

        backup = asset_backup_if_started()
        if backup is None:
            raise NoDataError
        items = await backup.attention()
        if not items:
            return []
        identity = sorted(f"{item.root.id}|{item.rel_path}|{item.status}" for item in items)
        digest = hashlib.sha256("\n".join(identity).encode()).hexdigest()[:8]
        first = items[0]
        stop = first.status in (FOLDER_MISSING, STORE_MISSING, PAUSED_EDGE)
        where = (str(backup.data_dir) if first.status == STORE_MISSING else first.root.path) if stop \
            else first.rel_path
        return [Finding(
            catalog_id="asset_backup_attention",
            key=f"asset_backup_attention:{digest}",
            params={"n": len(items), "file": where},
            variant=first.status if stop else "",
        )]
