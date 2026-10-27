"""E28 data_dir_legacy: the OS data still lives in the legacy folder.

docs/config-dir-compat-design.md §4.1, §5.3. The data works where it is; an
update moves it during the install (after its safety checks), and the user may
ask for it sooner. When the last update tried and a check did not pass, the
installer recorded the reason code in the data directory's install registry
(installs.json), and the "gate" variant quotes it. Otherwise the plugin, which
never moves data on its own at a session start, gets the "plugin" variant.

The other data_dir_* entries are local: the hooks render them, because a
missing or split data directory is exactly when the API cannot be trusted to.
"""

from __future__ import annotations

import asyncio

from aiteam.paths import last_migration_attempt, resolve_data_dir
from aiteam.services.notices import install_kind
from aiteam.services.notices.detectors import DetectContext, Finding, Scope

# The manual entry of the same migration the updates run (same checks).
MIGRATE_COMMAND = "python3 -m aiteam.data_dir_migration --apply"


class DataDirDetector:
    """Report a data directory still in the legacy location."""

    name = "data_dir"
    catalog_ids = ("data_dir_legacy",)
    timing = frozenset({"session_start"})
    hosts = frozenset({"cc", "codex"})
    timeout_s = 0.3

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=("data_dir_legacy:",))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        resolution = await asyncio.to_thread(resolve_data_dir)
        if resolution.mode != "legacy":
            return []
        attempt = await asyncio.to_thread(last_migration_attempt)
        variant, reason = "", ""
        if attempt is not None and attempt.get("reason"):
            variant, reason = "gate", str(attempt["reason"])
        elif ctx.host == "cc" and (await install_kind.detect("cc")).kind == "cc-plugin":
            variant, reason = "plugin", "plugin_session_start"
        return [Finding(
            catalog_id="data_dir_legacy",
            key=f"data_dir_legacy:{reason or 'pending'}",
            params={"reason": reason, "command": MIGRATE_COMMAND},
            variant=variant,
        )]
