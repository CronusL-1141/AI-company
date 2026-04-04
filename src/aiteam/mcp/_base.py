"""Shared infrastructure for AI Team OS MCP tools.

Contains the HTTP helper, project/team resolvers, and global state
that all tool modules depend on.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

API_URL = os.environ.get("AITEAM_API_URL", "http://localhost:8000")
# Project directory for DB isolation — set by Claude Code environment
PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR", "")

# Process-level project ID — resolved once at startup from cwd → root_path match.
# Safe because each CC session spawns its own MCP server subprocess.
# Set by _init_session_project() after API is ready, before mcp.run().
_session_project_id: str = ""


# ============================================================
# HTTP helper
# ============================================================


def _api_call(method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Unified API call helper using urllib standard library.

    Args:
        method: HTTP method (GET / POST / PUT / DELETE)
        path: API path, e.g., /api/teams
        data: Request body data (used for POST/PUT only)

    Returns:
        API response as a JSON dict
    """
    url = f"{API_URL}{urllib.parse.quote(path, safe='/?&=%')}"
    headers = {"Content-Type": "application/json"}
    if PROJECT_DIR:
        headers["X-Project-Dir"] = PROJECT_DIR
    # Project scope is NOT injected here — it caused infinite recursion.
    # Each MCP tool that needs project scope calls _resolve_project_id() itself.

    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass
        return {
            "success": False,
            "error": f"HTTP {e.code}: {e.reason}",
            "detail": error_body,
        }
    except urllib.error.URLError as e:
        return {
            "success": False,
            "error": f"无法连接到 AI Team OS API ({API_URL}): {e.reason}",
            "hint": "请确保 FastAPI 服务已启动: aiteam serve",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"请求失败: {e!s}",
        }


# ============================================================
# Resolvers
# ============================================================


def _resolve_team_id(team_id: str) -> str:
    """If team_id is empty, automatically get the active team ID from context_resolve.

    NOTE: This calls _api_call to get context. The context_resolve MCP tool
    in tools/infra.py has the full implementation; this resolver uses a
    lightweight version to avoid circular imports.
    """
    if team_id:
        return team_id
    # Lightweight context resolution — just find the first active team
    try:
        teams_data = _api_call("GET", "/api/teams")
        active_teams = [t for t in teams_data.get("data", []) if t.get("status") == "active"]
        if active_teams:
            return active_teams[0]["id"]
    except Exception:
        pass
    return ""


def _resolve_project_id(project_id: str) -> str:
    """Resolve project_id: explicit param > session constant > empty.

    _session_project_id is set once at startup from cwd matching.
    No dynamic resolution = no recursion risk.
    """
    if project_id:
        return project_id
    return _session_project_id


# ============================================================
# Session init
# ============================================================


def _init_session_project() -> None:
    """Resolve project_id once from cwd at startup. No recursion possible."""
    global _session_project_id
    try:
        projects = _api_call("GET", "/api/projects")
        cwd = os.getcwd().replace("\\", "/").rstrip("/").lower()
        for p in projects.get("data", []):
            rp = (p.get("root_path") or "").replace("\\", "/").rstrip("/").lower()
            if rp and (cwd == rp or cwd.startswith(rp + "/")):
                _session_project_id = p["id"]
                logger.info("Session project: %s (%s)", p.get("name"), p["id"][:8])
                return
        logger.info("No project match for cwd=%s", cwd)
    except Exception as e:
        logger.warning("Failed to resolve session project: %s", e)
