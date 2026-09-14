"""AI Team OS — 工具渐进式加载 P1 路由。

GET /api/tools/always-load：会话启动期由 OS 的 MCP server 调用，返回近期高频 MCP
工具的 alwaysLoad 白名单（裸工具名）。MCP server 据此给命中工具挂
``_meta {"anthropic/alwaysLoad": true}`` 豁免 defer。名单在 TTL 内复用上次重算结果，
过期才走一条 SQL + 迟滞防抖重算；只有真正重算才落审计事件。

POST /api/tools/always-load/applied：MCP server 挂完 meta 后回报落地结果，与上面的
轮换事件成对——一条证明服务端算过，另一条证明客户端真收到了。

设计规格见 docs/tool-loading-design.md 的 P1 节。功能纯增益：任何一步失败都返回
空名单 200，一切照旧走 ToolSearch，绝不抛 5xx。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from aiteam.api.always_load import (
    ALWAYSLOAD_CACHE_TTL_S,
    APPLIED_EVENT_TYPE,
    ROTATION_EVENT_TYPE,
    AppliedReason,
    Candidate,
    build_candidates,
    compute_rotation,
    parse_registered_param,
)
from aiteam.api.deps import get_scoped_repository
from aiteam.clock import utc_now
from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tools", tags=["tools"])


def _monotonic() -> float:
    """TTL 判据的时钟。单独一层是为了单测可替换，且不随墙钟回拨而失效。"""
    return time.monotonic()


@dataclass(frozen=True)
class _RotationSnapshot:
    """一次真实重算的结果快照——TTL 内直接复用，不再查库也不再记事件。"""

    tools: tuple[Candidate, ...]
    computed_at: datetime
    monotonic_at: float


# 模块级缓存。本端点的取数（agent_activities 全表）与事件台账都不按项目过滤，
# 因此一份进程内快照即可，无需按 scope 分桶。
_cache: _RotationSnapshot | None = None


def reset_always_load_cache() -> None:
    """丢弃缓存快照，下次请求必定重算。测试夹具与手工排障用。"""
    global _cache
    _cache = None


def _empty_response() -> dict:
    """降级响应：名单为空，一切照旧走 ToolSearch。"""
    return {
        "tools": [],
        "detail": [],
        "added": [],
        "removed": [],
        "cached": False,
        "computed_at": None,
    }


def _render(
    snapshot: _RotationSnapshot,
    registered_set: set[str] | None,
    *,
    cached: bool,
    added: list[str],
    removed: list[str],
) -> dict:
    """把快照投影成响应体，并按调用方实际注册的工具过滤。

    重算路径上 ``build_candidates`` 已先按 ``registered`` 过滤过，这里过滤是为缓存
    命中准备的：快照可能是另一个 MCP server 实例算出来的，其在册工具集未必与本次
    调用方相同，多出来的名字必须在返回前去掉，否则客户端会挂到不存在的工具上。
    """
    tools = [
        c for c in snapshot.tools if registered_set is None or c.name in registered_set
    ]
    return {
        "tools": [c.name for c in tools],
        "detail": [{"name": c.name, "count": c.count, "days": c.days} for c in tools],
        "added": added,
        "removed": removed,
        "cached": cached,
        "computed_at": snapshot.computed_at.isoformat(),
    }


@router.get("/always-load")
async def get_always_load(
    registered: str = Query(
        "",
        description="当前 MCP server 实际注册的裸工具名（逗号分隔）；用于过滤已删工具。"
        "留空则不做注册过滤（如手动调试）。",
    ),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """返回 alwaysLoad 轮换名单；TTL 过期才重算并落审计事件。

    - 缓存命中：直接按 ``registered`` 过滤上次快照返回，``cached=true``，
      ``added``/``removed`` 为空（本次调用没有发生轮换）；
    - 缓存过期或缺失：
      - 取数：近 7 天 agent_activities 中 ``mcp__ai-team-os%`` 工具，跨天数≥2，频次降序；
      - 归一化：去前缀得裸名，按 ``registered`` 过滤当前实际注册工具；
      - 迟滞：读最近一条轮换事件的 ``data.tools`` 作在位者，挑战者需 >在位者×1.2 才换入；
      - 硬顶 5 目标 3，数据不足不凑数；
      - 计算结果落一条 events 审计行（同时是下期迟滞基线）；
    - 任何一步失败 → 返回空名单 200，且不写缓存（下次请求会重试）。
    """
    global _cache
    try:
        registered_set = parse_registered_param(registered)

        snapshot = _cache
        if snapshot is not None and _monotonic() - snapshot.monotonic_at < ALWAYSLOAD_CACHE_TTL_S:
            return _render(snapshot, registered_set, cached=True, added=[], removed=[])

        rows = await repo.alwaysload_tool_frequencies()
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
        # 只在这里发：缓存命中不落行，否则事件量只反映调用次数而不反映轮换次数。
        await repo.create_event(
            event_type=ROTATION_EVENT_TYPE,
            source="api.tools.always_load",
            data={
                "tools": [{"name": c.name, "count": c.count} for c in result.tools],
                "added": result.added,
                "removed": result.removed,
            },
        )

        snapshot = _RotationSnapshot(
            tools=tuple(result.tools),
            computed_at=utc_now(),
            monotonic_at=_monotonic(),
        )
        _cache = snapshot
        return _render(
            snapshot, registered_set, cached=False, added=result.added, removed=result.removed
        )
    except Exception:  # noqa: BLE001 — 功能纯增益，坏了无损，静默降级为全 defer。
        logger.exception("alwaysLoad rotation failed; returning empty whitelist")
        return _empty_response()


class AlwaysLoadAppliedRequest(BaseModel):
    """MCP server 侧的落地回报。"""

    tools: list[str] = Field(
        default_factory=list, description="实际挂上 alwaysLoad meta 的裸工具名列表。"
    )
    elapsed_ms: int = Field(
        0, ge=0, description="客户端从取名单到挂完 meta 的总耗时（毫秒）。"
    )
    reason: AppliedReason = Field(
        "", description='没拿到名单的原因："timeout" / "http_error" / "no_api"；空串=正常拿到。'
    )


@router.post("/always-load/applied")
async def post_always_load_applied(
    payload: AlwaysLoadAppliedRequest,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """记一条客户端落地事件。

    单独开这个入口而不复用 ``/api/hooks/event``：后者的请求体是 CC hook 形状，由
    HookTranslator 按 hook 事件名分发，塞一条非 hook 事件进去要么被当成未知 hook
    丢弃，要么污染 hook 的归因路径。

    与 GET 同样是纯增益：写失败也只返回 ``recorded=false``，不抛 5xx——客户端这条
    POST 在 MCP server 启动路径上，绝不能因为记账失败拖住会话启动。
    """
    try:
        await repo.create_event(
            event_type=APPLIED_EVENT_TYPE,
            source="mcp.alwaysload",
            data={
                "tools": payload.tools,
                "count": len(payload.tools),
                "elapsed_ms": payload.elapsed_ms,
                "reason": payload.reason,
            },
        )
        return {"recorded": True}
    except Exception:  # noqa: BLE001 — 记账失败不得影响会话启动。
        logger.exception("alwaysLoad applied event not recorded")
        return {"recorded": False}
