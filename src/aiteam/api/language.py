"""Resolve Dashboard and startup-notice language without crossing host settings."""

from __future__ import annotations

import asyncio
import json
import locale
import os
import plistlib
import sys
from pathlib import Path
from typing import Literal


def _normalize_language(value: str) -> str:
    value = value.strip().lower()
    return "zh" if value.startswith("zh") or "chinese" in value or "中文" in value else "en"


def _cc_language(cwd: str | None) -> str | None:
    paths = []
    if cwd:
        project = Path(cwd).expanduser()
        paths.extend((project / ".claude/settings.local.json", project / ".claude/settings.json"))
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    paths.append((Path(config_dir).expanduser() if config_dir else Path.home() / ".claude") / "settings.json")
    for path in paths:
        try:
            if path.stat().st_size > 65536:
                continue
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        value = config.get("language") if isinstance(config, dict) else None
        if isinstance(value, str) and value.strip():
            return _normalize_language(value)
    return None


def _system_language() -> str | None:
    # AppleLanguages carries the user's UI preference; LANG often only describes
    # the shell that started the API. All file/locale work runs in a worker thread.
    if sys.platform == "darwin":
        try:
            path = Path.home() / "Library/Preferences/.GlobalPreferences.plist"
            preferences = plistlib.loads(path.read_bytes())
            languages = preferences.get("AppleLanguages", [])
            if isinstance(languages, list):
                for value in languages:
                    if isinstance(value, str) and value.strip():
                        return _normalize_language(value)
        except (OSError, ValueError, plistlib.InvalidFileException, AttributeError):
            pass
    for key in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        value = os.environ.get(key, "").strip()
        if value:
            return _normalize_language(value)
    try:
        value = locale.getlocale()[0]
    except (ValueError, TypeError):
        value = None
    return _normalize_language(value) if value else None


def _resolve_language(cwd: str | None, host: str, fallback_language: str | None) -> dict[str, str]:
    # Local import avoids a settings-router import cycle; the persisted mode is
    # the same source of truth for Dashboard, CC hooks and Codex hooks.
    from aiteam.api.routes.settings import _load_config

    mode = _load_config().get("language_mode", "follow")
    if mode in ("zh", "en"):
        return {"mode": mode, "effective": mode, "source": "dashboard"}
    if host == "cc":
        language = _cc_language(cwd)
        if language:
            return {"mode": "follow", "effective": language, "source": "cc_settings"}
    # Codex currently exposes no language setting. Never read CC settings for it.
    language = (
        _normalize_language(fallback_language)
        if fallback_language and fallback_language.strip()
        else _system_language()
    )
    return {
        "mode": "follow",
        "effective": language or "en",
        "source": "system" if language else "default",
    }


async def resolve_language(
    *,
    cwd: str | None = None,
    host: Literal["cc", "codex", "system"] = "system",
    fallback_language: str | None = None,
) -> dict[str, str]:
    """Resolve a persisted override, host preference, system locale, then English.

    A Dashboard without host context uses ``system``. Hooks pass their own host
    and may supply a local system locale (never a manual override) as fallback.
    """
    return await asyncio.to_thread(_resolve_language, cwd, host, fallback_language)
