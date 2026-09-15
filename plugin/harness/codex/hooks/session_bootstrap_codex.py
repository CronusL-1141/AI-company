#!/usr/bin/env python3
"""Emit the Codex SessionStart context using the native JSON hook protocol.

This adapter deliberately has no Claude configuration or path dependency.  The
host-facing stdout is always one JSON document; diagnostics stay on stderr.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
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
    if cwd:
        request.add_header("X-Project-Dir", cwd)
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
        return "[AI Team OS] Codex 适配已加载；OS API 当前不可达。"
    return "[AI Team OS] Codex 适配已加载；OS API 可达。"


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
    try:
        print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
    except Exception as error:
        print(f"[aiteam-codex-bootstrap] output failed: {type(error).__name__}", file=sys.stderr)


if __name__ == "__main__":
    main()
