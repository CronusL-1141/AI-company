"""AI Team OS — 工具渐进式加载 P1 路由。

GET /api/tools/always-load：会话启动期由 OS 的 MCP server 调用，重算近期高频
MCP 工具的 alwaysLoad 白名单（一条 SQL + 迟滞防抖），返回裸工具名列表。MCP server
据此给命中工具挂 ``_meta {"anthropic/alwaysLoad": true}`` 豁免 defer。

设计规格见 docs/tool-loading-design.md 的 P1 节。功能纯增益：任何一步失败都返回
空名单 200，一切照旧走 ToolSearch，绝不抛 5xx。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from aiteam.api.always_load import (
    ROTATION_EVENT_TYPE,
    build_candidates,
    compute_rotation,
    parse_registered_param,
)
from aiteam.api.deps import get_scoped_repository
from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("/always-load")
async def get_always_load(
    registered: str = Query(
        "",
        description="当前 MCP server 实际注册的裸工具名（逗号分隔）；用于过滤已删工具。"
        "留空则不做注册过滤（如手动调试）。",
    ),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """重算 alwaysLoad 轮换名单并落审计事件。

    - 取数：近 7 天 agent_activities 中 ``mcp__ai-team-os%`` 工具，跨天数≥2，频次降序；
    - 归一化：去前缀得裸名，按 ``registered`` 过滤当前实际注册工具；
    - 迟滞：读最近一条轮换事件的 ``data.tools`` 作在位者，挑战者需 >在位者×1.2 才换入；
    - 硬顶 5 目标 3，数据不足不凑数；
    - 计算结果落一条 events 审计行（同时是下期迟滞基线）；
    - 任何一步失败 → 返回空名单 200。
    """
    try:
        rows = await repo.alwaysload_tool_frequencies()
        registered_set = parse_registered_param(registered)
        candidates = build_candidates(rows, registered_set)

        prev = await repo.list_events(event_type=ROTATION_EVENT_TYPE, limit=1)
        incumbents: list[str] = []
        if prev:
            tools_data = prev[0].data.get("tools") or []
            incumbents = [
                t["name"]
                for t in tools_data
                if isinstance(t, dict) and isinstance(t.get("name"), str)
            ]

        result = compute_rotation(candidates, incumbents)

        # 计算结果落台账一行——审计 + 下期迟滞基线合一，不建新表不写 config。
        await repo.create_event(
            event_type=ROTATION_EVENT_TYPE,
            source="api.tools.always_load",
            data={
                "tools": [{"name": c.name, "count": c.count} for c in result.tools],
                "added": result.added,
                "removed": result.removed,
            },
        )

        return {
            "tools": result.names,
            "detail": [{"name": c.name, "count": c.count, "days": c.days} for c in result.tools],
            "added": result.added,
            "removed": result.removed,
        }
    except Exception:  # noqa: BLE001 — 功能纯增益，坏了无损，静默降级为全 defer。
        logger.exception("alwaysLoad rotation failed; returning empty whitelist")
        return {"tools": [], "detail": [], "added": [], "removed": []}
