"""AI Team OS — MCP Server.

Provides MCP tools that call corresponding API endpoints on the local
FastAPI server (localhost:8000) via HTTP.
MCP Server runs in stdio mode, fully decoupled from the FastAPI process.

Tools are organized in src/aiteam/mcp/tools/ submodules and registered
via register_all(mcp) at import time.
"""

from __future__ import annotations

from fastmcp import FastMCP

# Shared infrastructure — extracted to _base.py
from aiteam.mcp._base import (  # noqa: F401
    API_URL,
    PROJECT_DIR,
    _api_call,
    _init_session_project,
    _resolve_project_id,
    _resolve_team_id,
    _session_project_id,
    logger,
)

# Auto-start infrastructure — extracted to _autostart.py
from aiteam.mcp._autostart import (  # noqa: F401
    _cleanup_api,
    _ensure_api_running,
    _get_running_api_version,
    _is_api_healthy,
    _is_port_open,
    _kill_port_occupant,
    _read_pid_file,
    _write_pid_file,
)

mcp = FastMCP(
    name="ai-team-os",
    instructions="AI Agent Team Operating System — 项目管理、团队创建、Agent管理、会议协作、任务执行、记忆搜索",
)

# Register all tools from submodules
from aiteam.mcp.tools import register_all  # noqa: E402

register_all(mcp)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    _ensure_api_running()
    _init_session_project()
    mcp.run()
