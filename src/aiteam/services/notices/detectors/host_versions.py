"""E15 host_version_mismatch: the Claude Code plugin and the Codex adapter differ.

Both hosts start the same shared service, and the autostart kills and restarts
a running service whose version differs from its own, so two different
package versions keep restarting each other.

* Claude Code side: the version of ``ai-team-os`` in ``installed_plugins.json``
  (a source install has no plugin version and is not judged here).
* Codex side: a version field in the adapter's install receipt.

When either side is missing nothing is judged and nothing is cleared.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from aiteam.services.notices import install_kind
from aiteam.services.notices.detectors import DetectContext, Finding, NoDataError, Scope

CODEX_OBSERVER_DIRNAME = "ai-team-os-observer"
CODEX_RECEIPT_NAME = ".aiteam-codex-install.json"
# TODO(batch C): the receipt does not record a package version yet; Codex names
# the field when the adapter starts writing it. Until then this tuple matches
# nothing and E15 stays silent.
CODEX_VERSION_FIELDS: tuple[str, ...] = ("aiteam_version",)
_MAX_RECEIPT_BYTES = 256 * 1024


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def codex_adapter_version() -> str:
    """Package version recorded by the Codex adapter install, "" when unknown."""
    path = codex_home() / "hooks" / CODEX_OBSERVER_DIRNAME / CODEX_RECEIPT_NAME
    try:
        if path.stat().st_size > _MAX_RECEIPT_BYTES:
            return ""
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return ""
    if not isinstance(receipt, dict):
        return ""
    for name in CODEX_VERSION_FIELDS:
        value = receipt.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip().lstrip("v")
    return ""


def cc_plugin_version() -> str:
    """Version of the installed Claude Code plugin, "" for a source install or none."""
    kind = install_kind.detect_sync("cc")
    return kind.plugin_version.strip().lstrip("v") if kind.kind == "cc-plugin" else ""


class HostVersionsDetector:
    """One key per version pair; the ledger applies the 24-hour cooldown."""

    name = "host_versions"
    catalog_ids = ("host_version_mismatch",)
    timing = frozenset({"session_start"})
    hosts = frozenset({"cc", "codex"})
    timeout_s = 0.3

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=("host_version_mismatch:",))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        cc, codex = await asyncio.gather(
            asyncio.to_thread(cc_plugin_version), asyncio.to_thread(codex_adapter_version),
        )
        if not cc or not codex:
            raise NoDataError
        if cc == codex:
            return []
        return [Finding(
            catalog_id="host_version_mismatch",
            key=f"host_version_mismatch:{cc}:{codex}",
            params={"cc": f"v{cc}", "cx": f"v{codex}"},
        )]
