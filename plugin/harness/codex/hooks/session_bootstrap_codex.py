#!/usr/bin/env python3
"""Emit the Codex SessionStart context using the native JSON hook protocol.

This adapter deliberately has no Claude configuration or path dependency.  The
host-facing stdout is always one JSON document; diagnostics stay on stderr.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import locale
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

_MAX_RESPONSE_BYTES = 65_536
_HTTP_TIMEOUT_SECONDS = 1.5


def _api_url() -> str:
    value = os.environ.get("AITEAM_API_URL")
    if value:
        return value.rstrip("/")
    # Keep runtime-path knowledge in the Codex adapter's own shared core.  The
    # adapter must follow the OS dynamic port without containing another
    # host's home-directory literal.
    core_path = Path(__file__).with_name("hook_core.py")
    spec = importlib.util.spec_from_file_location("_aiteam_codex_hook_core", core_path)
    if spec and spec.loader:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module._get_api_url()
    return "http://localhost:8000"


def _get(path: str, *, cwd: str = "") -> dict | None:
    request = urllib.request.Request(f"{_api_url()}{path}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            return None
        document = json.loads(raw)
        return document if isinstance(document, dict) else None
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return None


def _context(payload: dict) -> str:
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    health = _get("/api/health", cwd=cwd)
    if health is None:
        print("[aiteam-codex-bootstrap] api_unreachable", file=sys.stderr)
        return "[AI Team OS] Codex 适配已加载；启动检查时 OS API 尚未就绪，请以 MCP 连接结果为准。"
    return "[AI Team OS] Codex 适配已加载；OS API 可达。"


def _tool_index(payload: dict) -> str:
    try:
        spec = importlib.util.spec_from_file_location(
            "_aiteam_codex_tool_catalog", Path(__file__).with_name("tool_catalog_codex.py")
        )
        if spec is None or spec.loader is None:
            return ""
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Some hosts also identify child sessions at SessionStart. Never show
        # their management hints when that identity is present.
        audience = "subagent" if payload.get("agent_type") or payload.get("agent_id") else "main"
        return module.render_catalog(payload, audience=audience, language=_notice_language(payload.get("cwd", "")))
    except Exception:
        return ""



_NOTICE_HOST = "codex"


@lru_cache(maxsize=1)
def _system_language() -> str:
    """Read system preferences once; do not guess unsupported host config keys."""
    value = ""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/bin/defaults", "read", "-g", "AppleLanguages"],
                capture_output=True, text=True, timeout=0.2, check=False,
            )
            match = re.search(r'^\s*"?([a-zA-Z]{2,3}(?:[-_][a-zA-Z0-9]+)*)"?\s*,?\s*$',
                              result.stdout, re.MULTILINE)
            if result.returncode == 0 and match:
                value = match.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
    if not value:
        value = next((os.environ[key] for key in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG")
                      if os.environ.get(key)), "")
    if not value:
        try:
            value = locale.getlocale()[0] or "en"
        except (ValueError, TypeError):
            value = "en"
    return "zh" if re.split(r"[-_.:@]", value.lower())[0] == "zh" else "en"


def _claim_notice(payload: dict) -> bool:
    """Atomically claim one notice per host/session, across hook subprocesses."""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        # An unknown identity must never suppress unrelated sessions.
        return payload.get("source", "startup") not in ("resume", "compact")
    try:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    except UnicodeError:
        return False
    directory = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    directory = directory / "ai-team-os" / "release-notices" / _NOTICE_HOST
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(directory / digest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        return True
    except OSError:
        # No durable claim means no popup; never block startup or repeat it on resume.
        return False


def _notice_instruction(release: dict) -> str:
    context = release.get("additional_context")
    if isinstance(context, str) and 0 < len(context) < 8192:
        return context
    notice = release["notice"]
    if release.get("language") == "zh":
        return "用户已看到更新提醒：" + notice + "\n仅提醒；用户要求更新后再按其安装方式操作。"
    return "The user has seen this update notice: " + notice + "\nNotify only; update after the user requests it."


def _valid_release(release: object) -> bool:
    if not isinstance(release, dict) or release.get("status") != "update_available":
        return False
    notice = release.get("notice")
    return (isinstance(notice, str) and 0 < len(notice) < 256
            and not any(character in notice for character in "\r\n")
            and "http://" not in notice and "https://" not in notice)


@lru_cache(maxsize=16)
def _notice_language(cwd: str = "") -> str:
    # The verified Codex config and SessionStart schemas expose no native locale.
    fallback = _system_language()
    query = urllib.parse.urlencode({"host": "codex", "cwd": cwd, "fallback_language": fallback})
    result = _get("/api/settings/language?" + query)
    if isinstance(result, dict) and result.get("effective") in ("zh", "en"):
        return result["effective"]
    return fallback


def _update_notice(payload: dict | None = None) -> dict | None:
    payload = payload or {}
    if payload.get("agent_id") or payload.get("agent_type"):
        return None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    query = urllib.parse.urlencode({
        "host": "codex", "cwd": cwd, "fallback_language": _notice_language(cwd), "installation": "codex",
    })
    release = _get("/api/releases/latest?" + query)
    if _valid_release(release) and _claim_notice(payload):
        return release
    return None


def main() -> None:
    payload: dict = {}
    try:
        raw = sys.stdin.read(65_536)
        if raw.strip():
            value = json.loads(raw)
            if isinstance(value, dict):
                payload = value
    except Exception as error:
        print(f"[aiteam-codex-bootstrap] input ignored: {type(error).__name__}", file=sys.stderr)

    document = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _context(payload),
        }
    }
    if catalog := _tool_index(payload):
        document["hookSpecificOutput"]["additionalContext"] += "\n\n" + catalog
    release = _update_notice(payload)
    if release:
        document["systemMessage"] = release["notice"]
        document["hookSpecificOutput"]["additionalContext"] += (
            "\n" + _notice_instruction(release)
        )
    try:
        print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    except Exception as error:
        print(f"[aiteam-codex-bootstrap] output failed: {type(error).__name__}", file=sys.stderr)


if __name__ == "__main__":
    main()
