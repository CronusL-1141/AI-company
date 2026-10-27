"""How OS was installed for a host, and where its baseline files live.

The answer picks the update command users see (E09) and, later, the baseline
installed copies are compared against. It is decided here, from files, and not
by the hook: a plugin user's session is usually served by the main-chain copy
under ``<config folder>/hooks/ai-team-os/``, which has no ``CLAUDE_PLUGIN_ROOT``,
so a hook-side guess always said "source install".

Rules (docs/user-notice-design.md E09, docs/config-dir-compat-design.md §4.3),
all within the session's Claude Code config folder:

* host ``codex`` -> ``codex``;
* host ``cc``: ``installed_plugins.json`` lists ``ai-team-os@<marketplace>`` and
  settings ``enabledPlugins`` does not switch that key off -> ``cc-plugin``;
  otherwise the main chain's owner (``aiteam.paths.main_chain_owner``: the
  ``.chain-owner.json`` record, or for a chain installed before owner records
  the older machine-wide rule) is ``source`` -> ``cc-source``, its baseline from
  the record, else ``<OS data dir>/install_path.txt``; anything else (``plugin``
  with the plugin gone, or nothing recorded) -> ``unknown``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from aiteam.paths import cc_config_dir, main_chain_owner, os_data_dir

PLUGIN_KEY_PREFIX = "ai-team-os@"
_MAX_JSON_BYTES = 1024 * 1024


@dataclass(frozen=True)
class InstallKind:
    """Installation kind plus the folder its files come from (when known)."""

    kind: str  # "cc-plugin" / "cc-source" / "codex" / "unknown"
    baseline: str = ""
    plugin_version: str = ""

    @property
    def variant(self) -> str:
        """Catalog variant name for E09."""
        return {"cc-plugin": "cc_plugin", "cc-source": "cc_source", "codex": "codex"}.get(
            self.kind, "unknown",
        )


# Re-exported from aiteam.paths (one resolver, docs/config-dir-compat-design.md §5.1).
# os_data_dir raises DataDirMissing, a FileNotFoundError, when there is none.
__all__ = ["InstallKind", "cc_config_dir", "detect", "detect_sync", "os_data_dir"]


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _plugin_entry(config: Path) -> tuple[str, dict] | None:
    plugins = _read_json(config / "plugins" / "installed_plugins.json").get("plugins")
    if not isinstance(plugins, dict):
        return None
    for key, value in plugins.items():
        if isinstance(key, str) and key.startswith(PLUGIN_KEY_PREFIX):
            entries = value if isinstance(value, list) else [value]
            first = next((item for item in entries if isinstance(item, dict)), {})
            return key, first
    return None


def _plugin_switched_off(config: Path, key: str) -> bool:
    enabled = _read_json(config / "settings.json").get("enabledPlugins")
    return isinstance(enabled, dict) and enabled.get(key) is False


def detect_sync(host: str) -> InstallKind:
    """Blocking detection; call through :func:`detect` from async code."""
    if host == "codex":
        return InstallKind("codex")
    config = cc_config_dir()
    plugin = _plugin_entry(config)
    if plugin is not None and not _plugin_switched_off(config, plugin[0]):
        entry = plugin[1]
        return InstallKind(
            "cc-plugin",
            baseline=str(entry.get("installPath") or ""),
            plugin_version=str(entry.get("version") or ""),
        )
    owner = main_chain_owner()
    if owner.get("installed_by") != "source":
        return InstallKind("unknown")
    return InstallKind("cc-source", baseline=str(owner.get("baseline") or "") or _recorded_source() or "")


def _recorded_source() -> str | None:
    """The checkout install.py recorded in the data directory (install_path.txt); None without one.

    Only where the source tree is: who owns the chain is ``main_chain_owner``'s answer.
    """
    try:
        return (os_data_dir() / "install_path.txt").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


async def detect(host: str) -> InstallKind:
    """Installation kind for ``host`` (file reads run in a worker thread)."""
    return await asyncio.to_thread(detect_sync, host)
