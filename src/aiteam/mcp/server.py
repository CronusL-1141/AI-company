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

import aiteam

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

# instructions 是 tool search 唯一的服务器级描述——112 个工具能不能被搜到由这段话决定，
# 因此按「能力 → 入口工具」组织而非罗列名词。正文在 _instructions.py（长中文字面量当数据养）。
from aiteam.mcp._instructions import INSTRUCTIONS  # noqa: E402

# version 必须显式传：不传时 FastMCP 会把 **fastmcp 自身的版本**填进 initialize
# 响应的 serverInfo.version，客户端侧的版本门禁/遥测看到的就成了框架版本而非 OS
# 版本。这里运行时读 aiteam.__version__，不是新的版本号真相源（I2 锁步的五处仍是
# 五处字面量），天然跟随 src/aiteam/__init__.py。
mcp = FastMCP(
    name="ai-team-os",
    version=aiteam.__version__,
    instructions=INSTRUCTIONS,
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
