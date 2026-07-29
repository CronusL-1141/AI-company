"""AI Team OS — token 用量归因只读端点（docs/token-attribution-v1-design.md 阶段 2 / 5）。

覆盖率与归因两个端点、一个 MCP 工具、``os_health_check`` 的摘要行，全部走
``StorageRepository.aggregate_token_attribution`` / ``usage_coverage_report``
这同一对方法。分母一旦有第二个实现，两处就会在某次改动之后悄悄给出不同的覆盖率，
而且没有任何东西会报错。

阶段 5 追加两条，都是 `/usage` 页的结构性需要，不是顺手加的便利接口：

* ``/scopes`` —— 下钻候选枚举（§5.2 ③）。只回派工数，**一个 token 数都不回**：
  列表带上用量就成了一张没有分母的排行榜。
* ``/probe`` —— 单次实测卡（§5.2 ④）的唯一取数口。它现场解析一份 transcript，
  **不读 agents 表的 token 五列** —— 那张卡豁免于覆盖率闸，代价就是数据只能来自
  单次解析；从聚合视图取数会让豁免变成绕过闸门的后门。

**只读**：本模块不写任何表，也不触发任何采集。按需触发、零新增守护（P3）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from aiteam.api import usage_probe
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


@router.get("/scopes")
async def list_usage_scopes(
    scope: str = Query(
        AttributionScope.PROJECT.value,
        description="要枚举的层级：project / session / workflow_run / agent / task",
    ),
    parent_scope: str = Query("", description="父层级；与 parent_id 成对使用，留空 = 不限父级"),
    parent_id: str = Query("", description="父层级的 id"),
    population: str = Query(
        DispatchPopulation.SUBAGENT.value,
        description="派工路径：subagent / leader_session（量级差数量级，必须分列）",
    ),
    days: int = Query(0, ge=0, le=3650, description="回看天数；0 = 全部历史"),
    limit: int = Query(30, ge=1, le=200, description="最多返回多少个候选"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """下钻候选清单 —— 每项只有 id、名字、派工数、已测数，**没有任何 token 数值**。

    四层用量必须对选中的那一个 scope_id 走 ``/attribution`` 单独取，这是结构上的分工
    而不是省事：一张能排序的列表一旦带上用量，就会被当成排行榜读，而排行榜里没有
    分母（§2.5）。
    """
    try:
        parsed_scope = AttributionScope(scope)
        parsed_parent = AttributionScope(parent_scope) if parent_scope else None
        parsed_population = DispatchPopulation(population)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    since, until = _window(days)
    try:
        rows = await repo.list_attribution_scopes(
            scope=parsed_scope,
            parent_scope=parsed_parent,
            parent_id=parent_id,
            population=parsed_population,
            since=since,
            until=until,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "data": rows}


@router.get("/unattributed")
async def sample_unattributed_dispatches(
    population: str = Query(
        DispatchPopulation.SUBAGENT.value,
        description="派工路径：subagent / leader_session",
    ),
    days: int = Query(0, ge=0, le=3650, description="回看天数；0 = 全部历史"),
    scan_limit: int = Query(400, ge=1, le=2000, description="最多扫描多少行未归因派工来取样"),
    per_reason: int = Query(5, ge=1, le=50, description="每个原因码最多返回几行样例"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """未归因抽屉的下钻样例 —— 每个原因码几行真实的行，用来判断"救不救得回"。

    这里的行数**不是分母**：只扫最近 ``scan_limit`` 行。各类的计数以 ``/coverage``
    的全量扫描为准，响应里的 ``scanned`` 就是给页面标注这个边界用的。
    """
    try:
        parsed_population = DispatchPopulation(population)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    since, until = _window(days)
    try:
        data = await repo.sample_unattributed(
            population=parsed_population,
            since=since,
            until=until,
            scan_limit=scan_limit,
            per_reason=per_reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "data": data}


@router.get("/probe")
async def probe_single_dispatch(
    agent_id: str = Query(..., description="要实测的那一次派工（agents.id）"),
    repo: StorageRepository = Depends(get_repository),
) -> dict:
    """单次实测 —— 现场解析这一个 agent 的 transcript，返回四层用量与首尾摘要。

    **不读 agents 表的 token 五列**：那是台账值（回采或采集时写进去的），而本端点
    要兑现的承诺是"我刚刚亲自数了一遍"。§5.2 ④ 给这张卡的覆盖率闸豁免，代价正是
    这一条 —— 数据只能来自单次 transcript 解析。

    响应里没有合计字段，也不会透传解析器内部的 ``total_tokens``（§1.2）。
    """
    agent = await repo.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} 不存在")
    if not agent.transcript_path:
        # 这不是错误，是 §3.4 的一个正式原因码。把它说清楚，看的人才知道该找谁救。
        raise HTTPException(
            status_code=404,
            detail=f"agent {agent_id} 未登记 transcript 路径（no_transcript_path）—— 无法单次实测",
        )

    payload = usage_probe.probe_dispatch(agent.transcript_path)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"transcript 已不在磁盘或不含可解析的用量行（transcript_gone）"
                f"：{agent.transcript_path}"
            ),
        )
    payload["agent_id"] = agent.id
    payload["agent_name"] = agent.name
    return {"success": True, "data": payload}
