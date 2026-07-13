"""AI Team OS — MCP Server.

Provides MCP tools that call corresponding API endpoints on the local
FastAPI server (localhost:8000) via HTTP.
MCP Server runs in stdio mode, fully decoupled from the FastAPI process.

Tools are organized in src/aiteam/mcp/tools/ submodules and registered
via register_all(mcp) at import time.
"""

from __future__ import annotations

# fastmcp 3.x 默认在启动时连 PyPI 检查自身新版本；在设置了 SOCKS 代理（如 Clash）
# 且未装 socksio 的机器上，该检查会以 ImportError 炸掉整个 stdio server——CC 侧表现
# 为 "-32000 reconnect failed"。治理层不应在启动路径上访问外网，直接关掉。
# （运行时改 settings 属性而非环境变量：settings 在 import fastmcp 时已固化。）
import fastmcp  # noqa: E402
from fastmcp import FastMCP

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

fastmcp.settings.check_for_updates = "off"

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
    # 工具渐进式加载 P1：API 就绪后给近期高频工具挂 alwaysLoad meta 豁免 defer。
    # best-effort，API 不在/超时静默降级为全 defer（见 _alwaysload.apply_always_load_meta）。
    from aiteam.mcp._alwaysload import apply_always_load_meta

    apply_always_load_meta(mcp)
    mcp.run()
