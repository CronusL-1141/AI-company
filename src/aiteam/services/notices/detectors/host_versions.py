"""E15 host_version_mismatch: the Claude Code plugin and the Codex adapter differ.

Both hosts start the same shared service, and the autostart kills and restarts
a running service whose version differs from its own, so two different
package versions keep restarting each other.

* Claude Code side: the version of ``ai-team-os`` in ``installed_plugins.json``
  (a source install has no plugin version and is not judged here).
* Codex side: ``aiteam_version`` in the adapter's install receipt.

When either side is missing nothing is judged and nothing is cleared. Each host
only hears about itself: a hit is bound to the older side, whose session gets
its own version and update steps; the newer side is not told.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from aiteam.services.notices import install_kind
from aiteam.services.notices.detectors import DetectContext, Finding, NoDataError, Scope

CODEX_OBSERVER_DIRNAME = "ai-team-os-observer"
CODEX_RECEIPT_NAME = ".aiteam-codex-install.json"
CODEX_VERSION_FIELDS: tuple[str, ...] = ("aiteam_version",)
_MAX_RECEIPT_BYTES = 256 * 1024
# A release segment ("1.14.0") and whatever follows it ("rc1", ".dev0", "+local").
_VERSION = re.compile(r"(\d+(?:\.\d+)*)(.*)", re.DOTALL)
_MAX_VERSION_CHARS = 64
# The CC side is only judged for plugin installs, so its update steps are the plugin's.
_VARIANT = {"cc": "cc_plugin", "codex": "codex"}


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


def _release(version: str) -> tuple[int, ...] | None:
    """Numeric release segment; any suffix is ignored (only used when segments differ)."""
    match = _VERSION.fullmatch(version) if len(version) <= _MAX_VERSION_CHARS else None
    if match is None:
        return None
    numbers = [int(part) for part in match.group(1).split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()  # "1.14" and "1.14.0" are the same release
    return tuple(numbers)


def older_side(cc: str, codex: str) -> str | None:
    """The host whose version is lower, or None when they are equal or have no order.

    Release segments compare as numbers ("1.9.0" < "1.10.0"). Versions that
    are not numeric, or share a release segment with different suffixes
    ("1.15.0" and "1.15.0rc1"), have no order here: telling either side it is
    the older one could be wrong, so neither is told.
    """
    left, right = _release(cc), _release(codex)
    if left is None or right is None or left == right:
        return None
    return "cc" if left < right else "codex"


class HostVersionsDetector:
    """One key per version pair, bound to the older host; the ledger applies the 24-hour cooldown."""

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
        older = older_side(cc, codex)
        if older is None:
            return []
        mine, other = (cc, codex) if older == "cc" else (codex, cc)
        return [Finding(
            catalog_id="host_version_mismatch",
            key=f"host_version_mismatch:{cc}:{codex}",
            params={"mine": f"v{mine}", "other": f"v{other}"},
            variant=_VARIANT[older],
            host=older,
        )]
