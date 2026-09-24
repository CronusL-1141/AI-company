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
import time
import urllib.error
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


def _tool_index(payload: dict, language: str = "") -> str:
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
        return module.render_catalog(
            payload, audience=audience, language=language or _notice_language(payload.get("cwd", "")),
        )
    except Exception:
        return ""


@lru_cache(maxsize=1)
def _user_notice():
    """Load the byte-identical shared core from this installed hook directory."""
    if "user_notice" in sys.modules:
        return sys.modules["user_notice"]
    spec = importlib.util.spec_from_file_location("user_notice", Path(__file__).with_name("user_notice.py"))
    if spec is None or spec.loader is None:
        raise ImportError("user_notice is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules["user_notice"] = module
    spec.loader.exec_module(module)
    return module


def _notice_language(cwd: str = "") -> str:
    return _user_notice().resolve_language_local("codex", cwd)


def _pending(notice, payload: dict, source: str):
    pending = notice.fetch_pending("codex", "SessionStart", source, payload,
                                   reader="leader-codex", timeout=2.0)
    if pending is None and notice.last_failure() == "unreachable":
        time.sleep(0.3)
        pending = notice.fetch_pending("codex", "SessionStart", source, payload,
                                       reader="leader-codex", timeout=2.0)
    return pending


def _api_down_notice(notice, payload: dict, source: str) -> tuple[str, str]:
    # E02/E06 belong to the other host's installer/main chain. Codex has E01.
    got = notice.claim_local(
        "api_down", {}, host="codex", session_id=str(payload.get("session_id") or ""),
        cwd=str(payload.get("cwd") or os.getcwd()), event=f"SessionStart:{source}",
        key="api_down", reliable=source == "startup",
    )
    notice.mark_api_down("codex")
    return got or ("", "")


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

    try:
        notice = _user_notice()
        source = payload.get("source") or "startup"
        source = source if isinstance(source, str) else "startup"
        is_child = bool(payload.get("agent_id") or payload.get("agent_type"))
        pending = None if is_child else _pending(notice, payload, source)
        line, note, delivery_ids = "", "", []
        if pending is not None:
            line, note, delivery_ids = pending.user_text, pending.model_text, pending.delivery_ids
        elif not is_child and notice.last_failure() == "unreachable":
            line, note = _api_down_notice(notice, payload, source)
        context = _context(payload)
        if catalog := _tool_index(payload, pending.language if pending is not None else ""):
            context += "\n\n" + catalog
        if note:
            context += "\n" + note
        notice.emit("codex", "SessionStart", user_text=line, model_text=context,
                    delivery_ids=delivery_ids)
    except Exception as error:
        print(f"[aiteam-codex-bootstrap] output failed: {type(error).__name__}", file=sys.stderr)


if __name__ == "__main__":
    main()
