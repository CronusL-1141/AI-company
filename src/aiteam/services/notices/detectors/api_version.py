"""E14 api_version_stale: the running service is older than the installed package."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import aiteam
from aiteam.services.notices.detectors import DetectContext, Finding, NoDataError, Scope

_VERSION = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)
_cache: dict[str, object] = {}


def _package_init() -> Path:
    return Path(aiteam.__file__).with_name("__init__.py")


def _disk_version() -> str | None:
    """Re-read ``__version__`` from the package on disk (editable installs too)."""
    path = _package_init()
    try:
        mtime = path.stat().st_mtime_ns
        if _cache.get("mtime") == mtime:
            return _cache.get("version")  # type: ignore[return-value]
        match = _VERSION.search(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None
    version = match.group(1) if match else None
    _cache.update(mtime=mtime, version=version)
    return version


class ApiVersionDetector:
    """Compare the in-process version with the one on disk."""

    name = "api_version"
    catalog_ids = ("api_version_stale",)
    timing = frozenset({"session_start", "prompt"})
    hosts = frozenset({"cc", "codex"})
    timeout_s = 0.3

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=("api_version_stale:",))

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        disk = await asyncio.to_thread(_disk_version)
        if not disk:
            raise NoDataError
        running = aiteam.__version__
        if disk == running:
            return []
        return [Finding(
            catalog_id="api_version_stale",
            key=f"api_version_stale:{running}:{disk}",
            params={"old": f"v{running}", "ver": f"v{disk}"},
        )]
