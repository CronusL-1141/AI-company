"""AI Team OS — 决策事件查询路由 (TOP2驾驶舱 Phase 1 & 2b)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from aiteam.api.deps import get_repository
from aiteam.api.schemas import APIListResponse
from aiteam.storage.repository import StorageRepository
from aiteam.types import Event

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


@router.get("", response_model=APIListResponse[Event])
async def list_decisions(
    team_id: str | None = Query(None, description="按来源team_id过滤（source前缀匹配）"),
    type: str | None = Query(
        None,
        description=(
            "事件类型前缀或精确类型，如 'decision.' 匹配所有决策事件，"
            "'decision.task_assigned' 精确匹配任务分配决策"
        ),
    ),
    limit: int = Query(50, ge=1, le=200, description="返回数量限制"),
    repo: StorageRepository = Depends(get_repository),
) -> APIListResponse[Event]:
    """查询决策事件列表，按时间倒序返回.

    支持按事件类型前缀过滤：
    - `decision.*` — 所有决策事件（任务分配、方案选择、Agent选择）
    - `knowledge.*` — 经验教训事件
    - `intent.*` — Agent意图事件
    """
    # 判断是否为前缀过滤（含通配符*或以.结尾）
    type_prefix: str | None = None
    exact_type: str | None = None

    if type is not None:
        if type.endswith("*") or type.endswith("."):
            # 前缀匹配：去掉尾部 * 或保留 . 前缀
            type_prefix = type.rstrip("*")
        elif "." in type and not any(
            type == f"{ns}.{sub}"
            for ns in ("decision", "knowledge", "intent", "agent", "task", "meeting", "cc", "file", "system", "memory")
            for sub in type.split(".", 1)[1:]
            if sub
        ):
            # 无子名称的命名空间（如 "decision"）当作前缀
            type_prefix = type + "."
        else:
            # 精确类型或带子名称的前缀
            if type.endswith("."):
                type_prefix = type
            else:
                # 尝试判断：如果type不含具体子名称中的点后内容，作为前缀
                # 简单策略：精确匹配优先，让repository处理
                exact_type = type
    else:
        # 无type参数时，默认只返回 decision.*/knowledge.*/intent.* 事件
        type_prefix = None  # 不限制，由下面的namespace逻辑处理

    # 构建查询
    if type is None:
        # 默认：返回所有决策相关事件（decision. + knowledge. + intent.）
        # 合并三类查询结果
        decision_events = await repo.list_events(type_prefix="decision.", limit=limit)
        knowledge_events = await repo.list_events(type_prefix="knowledge.", limit=limit)
        intent_events = await repo.list_events(type_prefix="intent.", limit=limit)

        all_events = decision_events + knowledge_events + intent_events
        # 按时间倒序合并，截取limit条
        all_events.sort(key=lambda e: e.timestamp, reverse=True)
        events = all_events[:limit]

        # 按team_id过滤（source格式为 "team:{team_id}"）
        if team_id:
            events = [
                e for e in events
                if e.source == f"team:{team_id}" or team_id in e.source
            ]
    else:
        events = await repo.list_events(
            event_type=exact_type,
            type_prefix=type_prefix,
            limit=limit,
        )
        if team_id:
            events = [
                e for e in events
                if e.source == f"team:{team_id}" or team_id in e.source
            ]

    return APIListResponse(data=events, total=len(events))
