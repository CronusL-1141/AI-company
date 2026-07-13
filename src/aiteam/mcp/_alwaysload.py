"""会话启动期给命中工具挂 alwaysLoad meta（工具渐进式加载 P1，MCP server 侧）。

register_all(mcp) 注册完全部工具后调用 ``apply_always_load_meta(mcp)``：向本地 API
查询 GET /api/tools/always-load 拿到近期高频白名单，给对应已注册工具的组件挂
``meta["anthropic/alwaysLoad"] = True``。FastMCP 的 ``Tool.to_mcp_tool()`` 会把该
组件的 ``meta`` 经 ``get_meta()`` 序列化进 tools/list 的 ``_meta`` 字段，CC 据此豁免 defer。

全程 best-effort：API 不在 / 超时 / 解析失败一律静默，所有工具照旧走 ToolSearch。
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from aiteam.mcp._base import _get_api_url

logger = logging.getLogger(__name__)

# CC 识别的常驻豁免键；值为 True 即免检索直达。
ALWAYSLOAD_META_KEY = "anthropic/alwaysLoad"
# 启动路径上严禁久等——2 秒拿不到就当没有，全 defer。
_TIMEOUT_S = 2.0


def _tool_components(mcp) -> dict[str, object]:
    """取本地 provider 的工具组件字典（key -> Tool 组件）；失败返回空。"""
    try:
        from fastmcp.tools.base import Tool as FastMCPTool

        components = mcp.local_provider._components  # noqa: SLF001 — 无公开同步枚举 API
        return {
            key: comp
            for key, comp in components.items()
            if isinstance(comp, FastMCPTool)
        }
    except Exception:
        return {}


def _registered_tool_names(mcp) -> list[str]:
    """当前实际注册的裸工具名列表。"""
    names: list[str] = []
    for comp in _tool_components(mcp).values():
        name = getattr(comp, "name", None)
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _fetch_always_load(registered: list[str]) -> list[str]:
    """调 GET /api/tools/always-load，返回裸工具名列表；任何失败返回空列表。"""
    try:
        query = urllib.parse.urlencode({"registered": ",".join(registered)})
        url = f"{_get_api_url()}/api/tools/always-load?{query}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        tools = payload.get("tools", [])
        return [t for t in tools if isinstance(t, str)]
    except Exception:
        return []


def apply_always_load_meta(mcp) -> list[str]:
    """给命中的已注册工具挂 alwaysLoad meta。静默失败，返回实际挂上的工具名列表。"""
    try:
        components = _tool_components(mcp)
        if not components:
            return []
        registered = [
            n for n in (getattr(c, "name", None) for c in components.values()) if isinstance(n, str) and n
        ]
        winners = set(_fetch_always_load(registered))
        if not winners:
            return []

        tagged: list[str] = []
        for comp in components.values():
            name = getattr(comp, "name", None)
            if name in winners:
                existing = getattr(comp, "meta", None)
                meta = dict(existing) if existing else {}
                meta[ALWAYSLOAD_META_KEY] = True
                comp.meta = meta  # type: ignore[attr-defined]
                tagged.append(name)
        if tagged:
            logger.info("alwaysLoad meta applied to %d tools: %s", len(tagged), tagged)
        return tagged
    except Exception:
        logger.debug("alwaysLoad meta application skipped", exc_info=True)
        return []
