"""AI Team OS — token 用量归因只读端点（docs/token-attribution-v1-design.md 阶段 2）。

两个端点、一个 MCP 工具、``os_health_check`` 的摘要行，全部走
``StorageRepository.aggregate_token_attribution`` / ``usage_coverage_report``
这同一对方法。分母一旦有第二个实现，两处就会在某次改动之后悄悄给出不同的覆盖率，
而且没有任何东西会报错。

**只读**：本模块不写任何表，也不触发任何采集。按需触发、零新增守护（P3）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api.deps import get_repository
from aiteam.clock import utc_now
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    AttributionScope,
    DispatchPopulation,
    TokenAttribution,
    TokenMetric,
    UsageCoverageReport,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/usage", tags=["usage"])


def _window(days: int) -> tuple[object, object]:
    """把 days 折成 (since, until)；0 = 不设窗（全部历史）。

    窗口落在 ``created_at`` 上（见 repository._apply_created_window）—— 按测量时间
    落窗会让没测过的行从分母里消失，覆盖率恒等于 100%。
    """
    if days <= 0:
        return None, None
    now = utc_now()
    return now - timedelta(days=days), now


@router.get("/coverage")
async def get_usage_coverage(
    days: int = Query(0, ge=0, le=3650, description="回看天数；0 = 全部历史"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """覆盖率向量 —— 矩阵各行 + 各跳 C_hop。零 token 数值，只有分子分母。

    刻意**不吃隐式项目作用域**：agents 表是跨项目共享的，而覆盖率要回答的是"这套
    观测链健不健康"，按当前 cwd 悄悄裁一刀会让同一个问题在不同目录下得到不同答案。
    要按项目看，走 ``/attribution?scope=project&scope_id=...``。
    """
    since, until = _window(days)
    report: UsageCoverageReport = await repo.usage_coverage_report(since=since, until=until)
    return {"success": True, "data": report.model_dump(mode="json")}


@router.get("/attribution")
async def get_token_attribution(
    metric: str = Query(
        TokenMetric.USAGE_SUM.value,
        description="token 口径；当前只支持 usage_sum（ctx_last 无四层分解）",
    ),
    scope: str = Query(
        AttributionScope.PROJECT.value,
        description="归因层级：project / session / workflow_run / agent / task",
    ),
    scope_id: str = Query("", description="该层级的 id；留空 = 不按此维度过滤（全库）"),
    population: str = Query(
        DispatchPopulation.SUBAGENT.value,
        description="派工路径：subagent / leader_session（两者量级差数量级，必须分列）",
    ),
    days: int = Query(0, ge=0, le=3650, description="回看天数；0 = 全部历史"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """按 scope 聚合四层用量，与分子、分母、未归因分类同层返回。

    响应体里**没有合计字段**，任何参数组合下都拿不到孤立总量：四层实测 95.6% 是
    cache_read，只报总量等于只报缓存读取量（§1.2 / §2.5）。
    """
    try:
        parsed_metric = TokenMetric(metric)
        parsed_scope = AttributionScope(scope)
        parsed_population = DispatchPopulation(population)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    since, until = _window(days)
    try:
        result: TokenAttribution = await repo.aggregate_token_attribution(
            metric=parsed_metric,
            scope=parsed_scope,
            scope_id=scope_id,
            population=parsed_population,
            since=since,
            until=until,
        )
    except ValueError as exc:
        # ctx_last / workflow_self_report 这类"结构上进不来"的请求在这里被挡下，
        # 错误信息本身带着原因 —— 把知识放在拒绝路径上，比放在注释里更难被绕过。
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"success": True, "data": result.model_dump(mode="json")}
