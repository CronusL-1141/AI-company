"""How OS was installed for a host, and where its baseline files live.

The answer picks the update command users see (E09) and, later, the baseline
installed copies are compared against. It is decided here, from files, and not
by the hook: a plugin user's session is usually served by the main-chain copy
under ``~/.claude/hooks/ai-team-os/``, which has no ``CLAUDE_PLUGIN_ROOT``, so a
hook-side guess always said "source install".

Rules (docs/user-notice-design.md E09):

* host ``codex`` -> ``codex``;
* host ``cc``: ``installed_plugins.json`` lists ``ai-team-os@<marketplace>`` and
  settings ``enabledPlugins`` does not switch that key off -> ``cc-plugin``;
  otherwise ``<OS data dir>/install_path.txt`` exists -> ``cc-source``;
  otherwise ``unknown``.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

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


def cc_config_dir() -> Path:
    """Claude Code config folder: ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def os_data_dir() -> Path:
    """OS data folder (same place as hook_core and the database)."""
    return Path.home() / ".claude" / "data" / "ai-team-os"


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
    install_path = os_data_dir() / "install_path.txt"
    try:
        baseline = install_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return InstallKind("unknown")
    return InstallKind("cc-source", baseline=baseline)


async def detect(host: str) -> InstallKind:
    """Installation kind for ``host`` (file reads run in a worker thread)."""
    return await asyncio.to_thread(detect_sync, host)
