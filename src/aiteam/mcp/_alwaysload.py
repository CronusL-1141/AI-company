"""会话启动期给命中工具挂 alwaysLoad meta（工具渐进式加载 P1，MCP server 侧）。

register_all(mcp) 注册完全部工具后调用 ``apply_always_load_meta(mcp)``：向本地 API
查询 GET /api/tools/always-load 拿到近期高频白名单，给对应已注册工具的组件挂
``meta["anthropic/alwaysLoad"] = True``。FastMCP 的 ``Tool.to_mcp_tool()`` 会把该
组件的 ``meta`` 经 ``get_meta()`` 序列化进 tools/list 的 ``_meta`` 字段，CC 据此豁免 defer。

全程 best-effort：API 不在 / 超时 / 解析失败一律静默，所有工具照旧走 ToolSearch。
静默降级有个副作用——从外面看不出"挂了 0 个"是因为本来就没候选，还是因为超时；
所以挂完会补发一条 ``tool.alwaysload.applied`` 事件，把结果和原因都记上。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from aiteam.mcp._base import _get_api_url

logger = logging.getLogger(__name__)

# CC 识别的常驻豁免键；值为 True 即免检索直达。
ALWAYSLOAD_META_KEY = "anthropic/alwaysLoad"

# 取名单的超时。启动路径上不能久等，但也不能短于服务端的实际应答时间：端点冷启动
# 实测 1.6~4.6s，旧值 2.0 会在冷启动时稳定超时并静默降级为零常驻（服务端照记轮换
# 事件，从台账上看不出失败）。服务端加 TTL 缓存后热路径 ~0.1s，这里放宽到 3.5s 是
# 给冷启动留的余量，仍在 CC 等工具列表的 5s 上限之内。
_TIMEOUT_S = 3.5
# 覆盖超时的环境变量（秒）。非数字或非正数一律忽略、回落默认值。调大要自己承担
# 风险：超过 CC 等工具列表的时限，整个 MCP server 都会被判超时。
_TIMEOUT_ENV = "AITEAM_ALWAYSLOAD_TIMEOUT"

# 回报落地结果的超时。这条 POST 纯属记账，不值得为它多等。
_REPORT_TIMEOUT_S = 1.0

# 没拿到名单的原因，闭集与服务端 ``aiteam.api.always_load.AppliedReason`` 对齐
# （两侧隔着一条 HTTP，不做运行时耦合，一致性由单测钉死）。
_REASON_OK = ""
_REASON_TIMEOUT = "timeout"
_REASON_HTTP_ERROR = "http_error"
_REASON_NO_API = "no_api"


def _fetch_timeout_s() -> float:
    """取名单的超时秒数；环境变量优先，非法值回落默认。"""
    raw = os.environ.get(_TIMEOUT_ENV, "").strip()
    if not raw:
        return _TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.debug("alwaysLoad: %s=%r 不是数字，用默认超时", _TIMEOUT_ENV, raw)
        return _TIMEOUT_S
    if value <= 0:
        logger.debug("alwaysLoad: %s=%r 非正数，用默认超时", _TIMEOUT_ENV, raw)
        return _TIMEOUT_S
    return value


def _tool_components(mcp) -> list[object]:
    """当前已注册的工具组件列表；任何失败返回空列表。

    走公开的 ``mcp.list_tools()``。它是协程，但本函数只在 stdio 启动路径上
    （``mcp.run()`` 之前、尚无运行中的事件循环）被调用，所以 ``asyncio.run``
    是安全的；万一在已有事件循环里被调用，静默降级为空（全 defer）。

    两个已实测的前提（fastmcp 3.4.3 / 3.4.5 均成立，升 4.0 时须重验）：
      * ``list_tools()`` 返回的是组件对象本身而非副本，故对其 ``meta`` 原地
        赋值会经 ``to_mcp_tool()`` 进入 ``tools/list`` 的 ``_meta``；
      * 返回值只含工具，不含资源/提示词，无需再按类型过滤。

    旧实现读 ``mcp.local_provider._components`` 私有组件表并从
    ``fastmcp.tools.base`` 导入 Tool 做类型过滤——两者都是 fastmcp 4.0
    移除 3.x 兼容 shim 时的必炸点，已换成公开面。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # 无运行中的事件循环，可以 asyncio.run
    else:
        logger.debug("alwaysLoad: 已在事件循环内，跳过工具枚举")
        return []
    try:
        return list(asyncio.run(mcp.list_tools()))
    except Exception:
        logger.debug("alwaysLoad: 工具枚举失败", exc_info=True)
        return []


def _registered_tool_names(mcp) -> list[str]:
    """当前实际注册的裸工具名列表。"""
    names: list[str] = []
    for comp in _tool_components(mcp):
        name = getattr(comp, "name", None)
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _fetch_always_load(registered: list[str]) -> tuple[list[str], str]:
    """调 GET /api/tools/always-load。

    Returns:
        ``(裸工具名列表, 原因)``。取到名单时原因为空串（名单本身可能是空的——数据
        不足不凑数）；取不到时名单为空且原因说明是超时、HTTP 错误还是 API 不在。
    """
    try:
        query = urllib.parse.urlencode({"registered": ",".join(registered)})
        url = f"{_get_api_url()}/api/tools/always-load?{query}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_fetch_timeout_s()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        tools = payload.get("tools", [])
        return [t for t in tools if isinstance(t, str)], _REASON_OK
    except urllib.error.HTTPError:
        # 先于 URLError：HTTPError 是它的子类，服务端答了但状态码不是 2xx。
        logger.debug("alwaysLoad: 端点返回错误状态", exc_info=True)
        return [], _REASON_HTTP_ERROR
    except urllib.error.URLError as exc:
        # 连接阶段超时被包成 URLError(TimeoutError)，与"连不上"必须分开记。
        if isinstance(exc.reason, TimeoutError):
            logger.debug("alwaysLoad: 取名单超时", exc_info=True)
            return [], _REASON_TIMEOUT
        logger.debug("alwaysLoad: API 不可达", exc_info=True)
        return [], _REASON_NO_API
    except TimeoutError:
        # 读阶段超时直接抛裸 TimeoutError。
        logger.debug("alwaysLoad: 取名单超时", exc_info=True)
        return [], _REASON_TIMEOUT
    except Exception:
        # 答了但读不懂（JSON 坏了等）——归入 http_error，与"没答"区分开。
        logger.debug("alwaysLoad: 名单解析失败", exc_info=True)
        return [], _REASON_HTTP_ERROR


def _post_applied_event(tools: list[str], elapsed_ms: int, reason: str) -> None:
    """把落地结果回报给 API，best-effort，失败静默。

    不为 ``no_api`` 单开短路：本地回环上的连接拒绝是立即返回的，多这一次尝试的代价
    可以忽略，少一条分支反而更不容易错。
    """
    try:
        body = json.dumps(
            {"tools": tools, "elapsed_ms": elapsed_ms, "reason": reason}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{_get_api_url()}/api/tools/always-load/applied",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_REPORT_TIMEOUT_S) as resp:
            resp.read()
    except Exception:
        logger.debug("alwaysLoad: 落地结果回报失败", exc_info=True)


def apply_always_load_meta(mcp) -> list[str]:
    """给命中的已注册工具挂 alwaysLoad meta。静默失败，返回实际挂上的工具名列表。"""
    started = time.monotonic()
    reason = _REASON_OK
    tagged: list[str] = []
    try:
        components = _tool_components(mcp)
        if components:
            registered = [
                n
                for n in (getattr(c, "name", None) for c in components)
                if isinstance(n, str) and n
            ]
            names, reason = _fetch_always_load(registered)
            winners = set(names)
            for comp in components:
                name = getattr(comp, "name", None)
                if name in winners:
                    existing = getattr(comp, "meta", None)
                    meta = dict(existing) if existing else {}
                    meta[ALWAYSLOAD_META_KEY] = True
                    comp.meta = meta  # type: ignore[attr-defined]
                    tagged.append(name)
            if tagged:
                logger.info("alwaysLoad meta applied to %d tools: %s", len(tagged), tagged)
    except Exception:
        logger.debug("alwaysLoad meta application skipped", exc_info=True)

    _post_applied_event(tagged, int((time.monotonic() - started) * 1000), reason)
    return tagged
