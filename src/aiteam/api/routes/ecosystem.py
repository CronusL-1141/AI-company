"""AI Team OS — Claude 生态仓档案 API 路由。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from aiteam.api.deps import get_scoped_repository
from aiteam.services.ecosystem_deep_reviewer import EcosystemDeepReviewer
from aiteam.services.ecosystem_scanner import (
    EcosystemScanner,
    FilterConfig,
    default_gh_search,
)
from aiteam.services.ecosystem_summarizer import (
    TOP_N_SORT_OPTIONS,
    EcosystemSummarizer,
)
from aiteam.services.ecosystem_tagger import EcosystemTagger
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    DataSourceKind,
    EcosystemDeepReview,
    EcosystemIndexDiff,
    EcosystemRepoEvent,
    EcosystemRepoProfile,
    EcosystemScanRun,
    EcosystemScanStrategy,
    EcosystemStatusChange,
    EcosystemTag,
    EcosystemTagCategory,
)

# Default ScanProfile template — used when no profile has been configured.
# v1.6.0-P1.A: stars is admission gate only; once admitted repos are permanent.
# Removed: active_definition / inactive_signals / archive_signals (no inactivity eviction).
_DEFAULT_SCAN_PROFILE: dict = {
    "popularity_floor": {
        "github": 1000,
        "huggingface": 200,
        "npm": 1000,
        "pypi": 1000,
    },
    "alert_thresholds": {
        "max_new_per_scan": 50,
    },
}

router = APIRouter(prefix="/api/ecosystem", tags=["ecosystem"])


class EcosystemProfileCreate(BaseModel):
    repo_full_name: str
    name: str
    owner: str
    description: str | None = None
    stars: int = 0
    language: str | None = None
    topics: list[str] = []
    homepage: str | None = None
    last_commit_at: str | None = None
    needs_deep_review: bool = False
    relevance_category: str | None = None
    relevance_score: int = 0
    one_line_summary: str | None = None
    last_scanned_at: str | None = None
    pushed_at: str | None = None
    is_archived: bool = False
    scan_run_id: str | None = None
    description_excerpt: str = ""


def _make_summary_excerpt(text: str | None, max_chars: int = 150) -> str:
    """Truncate long text to excerpt for list endpoints (P1.B layered return)."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_profile(data: EcosystemProfileCreate) -> EcosystemRepoProfile:
    """Convert API payload to Pydantic model."""
    last_commit_at = _parse_dt(data.last_commit_at)
    pushed_at = _parse_dt(data.pushed_at)

    now = datetime.now(tz=UTC)
    last_scanned_at = _parse_dt(data.last_scanned_at) or now

    return EcosystemRepoProfile(
        repo_full_name=data.repo_full_name,
        name=data.name,
        owner=data.owner,
        description=data.description,
        stars=data.stars,
        language=data.language,
        topics=data.topics,
        homepage=data.homepage,
        last_commit_at=last_commit_at,
        needs_deep_review=data.needs_deep_review,
        relevance_category=data.relevance_category,
        relevance_score=data.relevance_score,
        one_line_summary=data.one_line_summary,
        last_scanned_at=last_scanned_at,
        first_seen_at=now,
        pushed_at=pushed_at,
        is_archived=data.is_archived,
        scan_run_id=data.scan_run_id,
        description_excerpt=data.description_excerpt or "",
    )


def _profile_to_dict(
    p: EcosystemRepoProfile,
    stage_status: str | None = None,
    research_count: int = 0,
) -> dict[str, Any]:
    """序列化 profile。

    v1.5.1：透出 stage_status（取自 latest deep_review；无 review 时调用方传 "queued"）。
    research_count = 该 repo 关联的 deep_review 行数（前端用于 RepoCard 研究次数徽章）。
    """
    return {
        "id": p.id,
        "repo_full_name": p.repo_full_name,
        "name": p.name,
        "owner": p.owner,
        "description": p.description,
        "stars": p.stars,
        "language": p.language,
        "topics": p.topics,
        "homepage": p.homepage,
        "last_commit_at": p.last_commit_at.isoformat() if p.last_commit_at else None,
        "needs_deep_review": p.needs_deep_review,
        # v1.6.1: relevance_category/relevance_score 弃用（硬编码启发式无参考价值），不再透出
        "one_line_summary": p.one_line_summary,
        "last_scanned_at": p.last_scanned_at.isoformat(),
        "first_seen_at": p.first_seen_at.isoformat(),
        "pushed_at": p.pushed_at.isoformat() if p.pushed_at else None,
        "is_archived": p.is_archived,
        "scan_run_id": p.scan_run_id,
        "description_excerpt": _make_summary_excerpt(p.description_excerpt, 150) if p.description_excerpt else None,
        # v1.5.0-A 新增字段（前端 stage 徽章 / 失败提示 / 活跃集 tab 依赖）
        "shallow_summary": p.shallow_summary,
        "last_shallow_refreshed_at": (
            p.last_shallow_refreshed_at.isoformat()
            if p.last_shallow_refreshed_at
            else None
        ),
        "is_deleted": p.is_deleted,
        "is_private_now": p.is_private_now,
        "last_fetch_error": p.last_fetch_error,
        "fetch_failure_count": p.fetch_failure_count,
        "is_active": p.is_active,
        "active_rank": p.active_rank,
        # v1.5.1 新增：透出渐进漏斗 stage 状态 + 研究次数（让前端徽章渲染 + 总数统计准确）
        "stage_status": stage_status or "queued",
        "research_count": research_count,
        # v1.6.0-P1.A manual status fields
        "last_active_status": p.last_active_status,
        "manual_status": p.manual_status,
        # v1.6.1 multi-source
        "sources": p.sources or [],
        "primary_source": p.primary_source or "github",
        "canonical_id": p.canonical_id,
        "source_kind": p.source_kind,
    }


def _profile_to_list_dict(
    p: EcosystemRepoProfile,
    stage_status: str | None = None,
    research_count: int = 0,
) -> dict[str, Any]:
    """P1.B layered return: summary-only fields for list endpoints.

    Omits shallow_summary (long text) and other large fields.
    Use _profile_to_dict for full detail.
    """
    return {
        "id": p.id,
        "repo_full_name": p.repo_full_name,
        "name": p.name,
        "owner": p.owner,
        "stars": p.stars,
        "language": p.language,
        "topics": p.topics,
        # v1.6.1: relevance_category/relevance_score 弃用（硬编码启发式无参考价值），不再透出
        "one_line_summary": p.one_line_summary,
        "description_excerpt": _make_summary_excerpt(p.description, 150),
        "is_archived": p.is_archived,
        "last_active_status": p.last_active_status,
        "manual_status": p.manual_status,
        "canonical_id": p.canonical_id,
        "source_kind": p.source_kind,
        # v1.6.1 multi-source: 前端卡片用 sources 列表渲染多源徽章
        "sources": p.sources or [],
        "primary_source": p.primary_source or "github",
        "last_commit_at": p.last_commit_at.isoformat() if p.last_commit_at else None,
        "last_scanned_at": p.last_scanned_at.isoformat(),
        "pushed_at": p.pushed_at.isoformat() if p.pushed_at else None,
        "stage_status": stage_status or "queued",
        "research_count": research_count,
        "needs_deep_review": p.needs_deep_review,
    }


async def _build_stage_map(
    repo: StorageRepository,
    profile_ids: list[str],
    shallow_summary_map: dict[str, str | None] | None = None,
) -> tuple[dict[str, str], dict[str, int]]:
    """批量获取每个 profile 的 latest deep_review.stage_status + 研究次数。

    返回 (stage_map, count_map)：
    - stage_map[repo_id] = "queued" / "shallow_done" / ...（latest deep_review，无则按
      v1.6.0 SST 规则：有 shallow_summary -> "shallow_done"；否则 "queued"）
    - count_map[repo_id] = 该 repo 真正被研究过的次数（v1.5.2: 只数 architecture_done+ 的行，
      浅扫完成不计入 — 浅扫是预筛动作，不属于"被研究")

    shallow_summary_map: {repo_id: shallow_summary} — 用于无 deep_review 时的 fallback。
    一次性查询所有 reviews（按 created_at desc），Python 端聚合，避免路由层 N+1。
    """
    # v1.5.2: "已被研究"的语义边界 — 进入 architecture stage 后才算
    _researched_stages = {"architecture_done", "debated", "referenced", "integrated"}

    stage_map: dict[str, str] = {}
    count_map: dict[str, int] = {}
    if not profile_ids:
        return stage_map, count_map
    id_set = set(profile_ids)
    # list_deep_reviews 不支持 IN 多 id，但限定 limit 后 Python 过滤已足够（当前 < 数千行）
    reviews = await repo.list_deep_reviews(limit=10000)
    for r in reviews:
        if r.repo_id not in id_set:
            continue
        # 只数真正被研究过的行（不含浅扫 shallow_done / queued / *_failed）
        stage_value = (
            r.stage_status.value
            if hasattr(r.stage_status, "value")
            else (r.stage_status or "")
        )
        if stage_value in _researched_stages:
            count_map[r.repo_id] = count_map.get(r.repo_id, 0) + 1
        if r.repo_id not in stage_map:
            # list_deep_reviews 已按 created_at desc，第一条即 latest
            stage_map[r.repo_id] = stage_value or "queued"
    # v1.6.0 SST fallback: no deep_review → check shallow_summary before defaulting to "queued"
    for pid in profile_ids:
        if pid not in stage_map:
            summary = (shallow_summary_map or {}).get(pid)
            if summary and summary.strip():
                stage_map[pid] = "shallow_done"
            else:
                stage_map[pid] = "queued"
        count_map.setdefault(pid, 0)
    return stage_map, count_map


@router.post("/profiles")
async def upsert_profile(
    data: EcosystemProfileCreate,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Upsert 一个生态仓档案（按 repo_full_name 唯一键）。"""
    existing = await repo.get_ecosystem_profile(data.repo_full_name)
    is_new = existing is None

    profile = _parse_profile(data)
    await repo.upsert_ecosystem_profile(profile)
    result = _profile_to_dict(profile)
    result["created"] = is_new
    return result


@router.get("/profiles")
async def search_profiles(
    keyword: str = Query(default=""),
    topic: str = Query(default=""),
    min_stars: int = Query(default=0),
    max_stars: int = Query(default=0),
    category: str = Query(default=""),
    language: str = Query(default=""),
    pushed_after: str = Query(default=""),
    is_archived: bool | None = Query(default=None),
    needs_deep_review: bool | None = Query(default=None),
    tags: str = Query(default=""),
    tag_match_mode: str = Query(default="all"),
    sort: str = Query(default="stars"),
    limit: int = Query(default=20, le=100),
    offset: int = Query(default=0, ge=0),
    facet_counts: bool = Query(default=False),
    # v1.5.0-E 前端 tab 切换 / failed 筛选依赖（service 层若不识别会忽略）
    is_active: bool | None = Query(default=None),
    is_deleted: bool | None = Query(default=None),
    stage_status: str = Query(default=""),
    # P1.B: detail=true returns full fields (shallow_summary etc); default is summary-only
    detail: bool = Query(default=False),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """检索生态仓档案列表（Stage E 扩展版）。

    - tags: 逗号分隔的标签名列表，配合 tag_match_mode (all/any) 实现 AND/OR 过滤
    - sort: stars / recency (pushed_at desc)  [v1.6.1: relevance 排序已弃用]
    - facet_counts: True 时附带 category/language/archived 聚合分布
    - is_active / is_deleted / stage_status: v1.5.0-E 前端筛选（活跃/全量 tab、failed 筛选）
    - detail=false (default): 返回摘要字段（P1.B 分层返回，不含 shallow_summary 长文本）
    - detail=true: 返回完整字段（含 shallow_summary）
    """
    pushed_after_dt = _parse_dt(pushed_after) if pushed_after else None
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # v1.5.1: stage_status filter 不依赖 stars limit — 先全 DB 算 stage_map 找 wanted_ids
    id_filter: list[str] | None = None
    id_exclude: list[str] | None = None
    if stage_status:
        wanted_stages = {s.strip() for s in stage_status.split(",") if s.strip()}
        if wanted_stages:
            full_reviews = await repo.list_deep_reviews(limit=10000)
            full_stage_map: dict[str, str] = {}
            for r in full_reviews:
                if r.repo_id in full_stage_map:
                    continue  # latest_only（list_deep_reviews 已 created_at desc）
                stg = (
                    r.stage_status.value
                    if hasattr(r.stage_status, "value")
                    else r.stage_status
                ) or "queued"
                full_stage_map[r.repo_id] = stg

            other_wanted = wanted_stages - {"queued"}
            wants_queued = "queued" in wanted_stages

            if wants_queued and not other_wanted:
                # 纯 queued: 排除所有有 review 的仓（无论 stage）
                id_exclude = list(full_stage_map.keys())
            elif other_wanted and not wants_queued:
                # 纯非-queued: 限定到匹配 stages 的仓
                id_filter = [
                    rid for rid, stg in full_stage_map.items() if stg in other_wanted
                ]
            elif wants_queued and other_wanted:
                # queued + 其他: 排除"有 review 但 stage 不匹配"的仓
                id_exclude = [
                    rid for rid, stg in full_stage_map.items() if stg not in other_wanted
                ]

    profiles, total = await repo.search_ecosystem_profiles_extended(
        keyword=keyword,
        topic=topic,
        min_stars=min_stars,
        max_stars=max_stars if max_stars > 0 else None,
        needs_deep_review=needs_deep_review,
        category=category,
        language=language,
        pushed_after=pushed_after_dt,
        is_archived=is_archived,
        tags=tag_list or None,
        tag_match_mode=tag_match_mode,
        sort=sort,
        limit=limit,
        offset=offset,
        id_filter=id_filter,
        id_exclude=id_exclude,
    )

    # v1.5.0-E 客户端二次过滤：is_active / is_deleted
    # DEPRECATED v1.6.0 P1.A: is_active 字段已废弃，请用 last_active_status。
    # 此参数保留仅作向后兼容，内部 fallback 到 last_active_status='active'。
    if is_active is not None:
        def _is_active(p):
            status = getattr(p, 'last_active_status', None)
            if status is not None:
                return status == 'active'
            return bool(getattr(p, 'is_active', True))
        profiles = [p for p in profiles if _is_active(p) == is_active]
    if is_deleted is not None:
        profiles = [p for p in profiles if p.is_deleted == is_deleted]

    # v1.5.1: 一次性批量获取 stage_map + count_map（消除原 N+1 查询）
    # v1.6.0 SST: pass shallow_summary so repos with summary but no deep_review → "shallow_done"
    _shallow_map = {p.id: p.shallow_summary for p in profiles}
    stage_map, count_map = await _build_stage_map(repo, [p.id for p in profiles], _shallow_map)

    # stage_status 已在 SQL 层用 id_filter / id_exclude 完成；路由层不再后过滤
    if is_active is not None or is_deleted is not None:
        total = len(profiles)

    # P1.B: use summary-only serializer by default; full fields only when detail=True
    _serialize_fn = _profile_to_dict if detail else _profile_to_list_dict
    payload: dict[str, Any] = {
        "profiles": [
            _serialize_fn(
                p,
                stage_status=stage_map.get(p.id, "queued"),
                research_count=count_map.get(p.id, 0),
            )
            for p in profiles
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
    }

    if facet_counts:
        facets = await repo.compute_ecosystem_facet_counts(
            keyword=keyword,
            min_stars=min_stars,
            max_stars=max_stars if max_stars > 0 else None,
            category=category,
            language=language,
            is_archived=is_archived,
        )
        payload["facet_counts"] = facets

    return payload


@router.get("/profiles/{repo_full_name:path}/full")
async def get_profile_full(
    repo_full_name: str,
    relations_limit: int = Query(default=50, le=200),
    deep_reviews_limit: int = Query(default=20, le=100),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """获取仓的全息详情：profile + tags + deep_reviews + relations + scan_run。

    repo_full_name 形如 "owner/repo"。也支持直接传 repo id（id 形式无 "/"）。
    """
    if "/" in repo_full_name:
        result = await repo.get_ecosystem_profile_full(
            repo_full_name=repo_full_name,
            relations_limit=relations_limit,
            deep_reviews_limit=deep_reviews_limit,
        )
    else:
        # 当作主键 id 处理
        result = await repo.get_ecosystem_profile_full(
            repo_id=repo_full_name,
            relations_limit=relations_limit,
            deep_reviews_limit=deep_reviews_limit,
        )

    if result is None:
        raise HTTPException(status_code=404, detail="ecosystem repo not found")

    return _serialize_full(result)


class ManualStatusBody(BaseModel):
    """Request body for setting manual status on a repo."""

    status: str | None = None  # 'no_value' to mark; null/None to clear
    reason: str = ""
    set_by: str = "user"


@router.post("/repos/{repo_id}/manual_status")
async def set_repo_manual_status(
    repo_id: str,
    body: ManualStatusBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Set or clear manual status on a repo (e.g. mark as no_value after deep review).

    POST body: {status: 'no_value', reason: 'low quality content', set_by: 'user'}
    To clear: {status: null, reason: ''}

    When status='no_value', the repo's last_active_status will reflect 'manual_archived'
    on the next index_update run.
    """
    # Validate status value — allowed: 'no_value', 'pinned', null
    allowed = {None, "no_value", "pinned"}
    if body.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: no_value, pinned, null. Got: {body.status!r}",
        )

    found = await repo.update_repo_manual_status(
        repo_id=repo_id,
        manual_status=body.status,
        reason=body.reason,
        set_by=body.set_by,
    )
    if not found:
        raise HTTPException(status_code=404, detail=f"Repo {repo_id} not found")

    # Update last_active_status based on new manual_status
    if body.status == "no_value":
        await repo.update_repo_active_status(repo_id, new_status="manual_archived")
    elif body.status == "pinned":
        # pinned repos stay active — ensure last_active_status reflects this
        await repo.update_repo_active_status(repo_id, new_status="active")
    elif body.status is None:
        # Clearing manual status — restore to 'active' unless github archived
        profile = await repo.get_ecosystem_profile_by_id(repo_id)
        if profile and not profile.is_archived:
            await repo.update_repo_active_status(repo_id, new_status="active")

    return {
        "success": True,
        "repo_id": repo_id,
        "manual_status": body.status,
        "reason": body.reason,
        "set_by": body.set_by,
        "message": (
            f"Marked as no_value: {body.reason}" if body.status == "no_value"
            else f"Pinned: {body.reason}" if body.status == "pinned"
            else "Manual status cleared"
        ),
    }


@router.get("/search/by_capability")
async def search_by_capability(
    tags: str = Query(default=""),
    match_mode: str = Query(default="all"),
    min_stars: int = Query(default=0),
    max_stars: int = Query(default=0),
    sort: str = Query(default="stars"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """通过能力标签反向检索仓库。

    tags 逗号分隔。match_mode: all (AND) / any (OR)。
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if not tag_list:
        return {"profiles": [], "total": 0, "limit": limit, "offset": offset}

    profiles, total = await repo.search_ecosystem_profiles_extended(
        min_stars=min_stars,
        max_stars=max_stars if max_stars > 0 else None,
        tags=tag_list,
        tag_match_mode=match_mode,
        sort=sort,
        limit=limit,
        offset=offset,
    )

    return {
        "profiles": [_profile_to_dict(p) for p in profiles],
        "total": total,
        "limit": limit,
        "offset": offset,
        "matched_tags": tag_list,
        "match_mode": match_mode,
    }


# ============================================================
# Deep review endpoints (Stage F)
# ============================================================


class DeepReviewRequestBody(BaseModel):
    """请求一份新的深扫报告（按 repo_id）。"""

    repo_id: str
    priority: str = "medium"
    timeout_minutes: int = Field(default=45, ge=5, le=180)
    agent_id: str | None = None


class DeepReviewLinkBody(BaseModel):
    """Hook 用的 link 入口：把已存在的 report_id 写到 deep_review。

    K5: hook 解析 5 段式 markdown 后，附带结构化字段一起回填到 row。
    若任一字段为 None，则保留 row 原值（向后兼容旧 hook）。
    """

    report_id: str
    summary_md: str | None = None
    architecture_md: str | None = None
    risks_md: str | None = None
    learnings_md: str | None = None
    integration_md: str | None = None
    demo_result: str | None = None
    demo_log_excerpt: str | None = None
    integration_recommendation: str | None = None


def _deep_review_to_dict(dr: EcosystemDeepReview) -> dict[str, Any]:
    """Serialize a deep-review row for the public API."""
    return {
        "id": dr.id,
        "repo_id": dr.repo_id,
        "status": dr.status.value if hasattr(dr.status, "value") else dr.status,
        "agent_id": dr.agent_id,
        "summary_md": dr.summary_md,
        "architecture_md": dr.architecture_md,
        "demo_result": (
            dr.demo_result.value
            if dr.demo_result is not None and hasattr(dr.demo_result, "value")
            else dr.demo_result
        ),
        "demo_log_excerpt": dr.demo_log_excerpt,
        "risks_md": dr.risks_md,
        "learnings_md": dr.learnings_md,
        "integration_recommendation": (
            dr.integration_recommendation.value
            if dr.integration_recommendation is not None
            and hasattr(dr.integration_recommendation, "value")
            else dr.integration_recommendation
        ),
        "report_id": dr.report_id,
        "dispatch_prompt": getattr(dr, "dispatch_prompt", "") or "",
        "started_at": dr.started_at.isoformat() if dr.started_at else None,
        "completed_at": dr.completed_at.isoformat() if dr.completed_at else None,
        "duration_seconds": dr.duration_seconds,
        "created_at": dr.created_at.isoformat() if dr.created_at else None,
        # v1.5.0-A 新增字段（前端研究历程 timeline 依赖）
        "stage_status": (
            dr.stage_status.value
            if hasattr(dr.stage_status, "value")
            else dr.stage_status
        ),
        "integration_md": getattr(dr, "integration_md", "") or "",
        "shallow_completed_at": (
            dr.shallow_completed_at.isoformat() if dr.shallow_completed_at else None
        ),
        "architecture_completed_at": (
            dr.architecture_completed_at.isoformat()
            if dr.architecture_completed_at
            else None
        ),
        "debated_at": dr.debated_at.isoformat() if dr.debated_at else None,
        "stage3_completed_at": (
            dr.stage3_completed_at.isoformat() if dr.stage3_completed_at else None
        ),
        "debate_meeting_id": dr.debate_meeting_id,
        "integration_task_id": dr.integration_task_id,
    }


def _get_reviewer(repo: StorageRepository) -> EcosystemDeepReviewer:
    """Build an EcosystemDeepReviewer bound to the request's repository."""
    return EcosystemDeepReviewer(repo=repo)


@router.post("/deep_reviews")
async def request_deep_review(
    body: DeepReviewRequestBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """入队一份深扫报告，返回 stage_status=queued 行 + 派遣 prompt。

    D5 收敛：status 列为 stage_status 的派生只读视图，新行 status='queued'
    （历史行为返回 'running'，v1.6.2 起停写）。
    """
    reviewer = _get_reviewer(repo)
    try:
        review = await reviewer.request(
            repo_id=body.repo_id,
            priority=body.priority,
            timeout_minutes=body.timeout_minutes,
            agent_id=body.agent_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _deep_review_to_dict(review)


@router.get("/deep_reviews")
async def list_deep_reviews(
    status: str = Query(default=""),
    limit: int = Query(default=20, le=100),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """按 status 过滤列出深扫报告（newest-first）。"""
    reviewer = _get_reviewer(repo)
    rows = await reviewer.list_reviews(status=status, limit=limit)
    return {
        "reviews": [_deep_review_to_dict(r) for r in rows],
        "total": len(rows),
    }


@router.get("/deep_reviews/by_repo/{repo_id}")
async def deep_review_status(
    repo_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """获取指定仓的最新一份深扫报告状态；找不到返回 404。"""
    reviewer = _get_reviewer(repo)
    review = await reviewer.status(repo_id=repo_id)
    if review is None:
        raise HTTPException(status_code=404, detail="no deep review for repo_id")
    return _deep_review_to_dict(review)


@router.post("/deep_reviews/{deep_review_id}/cancel")
async def cancel_deep_review(
    deep_review_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """取消一份排队 / 进行中的深扫报告。"""
    reviewer = _get_reviewer(repo)
    review = await reviewer.cancel(deep_review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="deep review not found")
    return _deep_review_to_dict(review)


@router.post("/deep_reviews/{deep_review_id}/backfill")
async def backfill_deep_review(
    deep_review_id: str,
    body: DeepReviewLinkBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Backfill 5-section fields on an already-linked deep_review row.

    Same payload as link_report but bypasses the "report_id already set"
    short-circuit. Used by scripts/backfill_deep_review_sections.py to
    populate rows created before the K5 hook parser shipped.
    """
    review = await repo.get_deep_review(deep_review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="deep review not found")

    update_fields: dict[str, Any] = {"report_id": body.report_id}
    if body.summary_md is not None:
        update_fields["summary_md"] = body.summary_md
    if body.architecture_md is not None:
        update_fields["architecture_md"] = body.architecture_md
    if body.risks_md is not None:
        update_fields["risks_md"] = body.risks_md
    if body.learnings_md is not None or body.integration_md is not None:
        learnings = body.learnings_md or ""
        if body.integration_md is not None:
            sep = "\n\n" if learnings else ""
            learnings = (
                f"{learnings}{sep}## 5. 集成建议\n{body.integration_md.strip()}"
            )
        update_fields["learnings_md"] = learnings
    if body.demo_log_excerpt is not None:
        update_fields["demo_log_excerpt"] = body.demo_log_excerpt
    if body.demo_result is not None:
        update_fields["demo_result"] = body.demo_result
    if body.integration_recommendation is not None:
        update_fields["integration_recommendation"] = body.integration_recommendation

    updated = await repo.update_deep_review(deep_review_id, **update_fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="deep review not found")
    return _deep_review_to_dict(updated)


@router.post("/deep_reviews/{deep_review_id}/link_report")
async def link_deep_review_report(
    deep_review_id: str,
    body: DeepReviewLinkBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """把 report_id 关联到 deep_review_id，并将状态推进到 completed。

    供 PostToolUse hook 使用。重复调用幂等：若已绑定 report_id 直接返回。
    Hook 同时回填 5 段式解析后的字段（summary_md / architecture_md / ...）。
    """
    reviewer = _get_reviewer(repo)
    review = await reviewer.link_report(
        deep_review_id,
        body.report_id,
        summary_md=body.summary_md,
        architecture_md=body.architecture_md,
        risks_md=body.risks_md,
        learnings_md=body.learnings_md,
        integration_md=body.integration_md,
        demo_result=body.demo_result,
        demo_log_excerpt=body.demo_log_excerpt,
        integration_recommendation=body.integration_recommendation,
    )
    if review is None:
        raise HTTPException(status_code=404, detail="deep review not found")
    return _deep_review_to_dict(review)


def _serialize_full(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert dict containing Pydantic models into JSON-friendly dict."""
    profile = payload["profile"]
    deep_reviews = payload.get("deep_reviews") or []
    scan_run = payload.get("scan_run")

    # v1.6.0 SST: derive stage_status using same logic as list endpoint (_build_stage_map)
    # 优先级: deep_review.stage_status > shallow_summary 非空 → 'shallow_done' > 'queued'
    stage_status = "queued"
    latest_dr = deep_reviews[0] if deep_reviews else None  # repository 返回应按时间倒序
    if latest_dr and getattr(latest_dr, "stage_status", None):
        stage_status = latest_dr.stage_status
    elif profile.shallow_summary and profile.shallow_summary.strip():
        stage_status = "shallow_done"

    serialized: dict[str, Any] = {
        "profile": _profile_to_dict(profile, stage_status=stage_status, research_count=len(deep_reviews)),
        "tags": payload.get("tags") or [],
        "deep_reviews": [_deep_review_to_dict(dr) for dr in deep_reviews],
        "relations_from": payload.get("relations_from") or [],
        "relations_to": payload.get("relations_to") or [],
        "scan_run": (
            {
                "id": scan_run.id,
                "strategy": (
                    scan_run.strategy.value
                    if hasattr(scan_run.strategy, "value")
                    else scan_run.strategy
                ),
                "started_at": (
                    scan_run.started_at.isoformat()
                    if getattr(scan_run, "started_at", None)
                    else None
                ),
                "completed_at": (
                    scan_run.completed_at.isoformat()
                    if getattr(scan_run, "completed_at", None)
                    else None
                ),
                "duration_seconds": scan_run.duration_seconds,
                "repos_added": scan_run.repos_added,
                "repos_updated": scan_run.repos_updated,
                "repos_skipped": scan_run.repos_skipped,
                "metadata_changed_count": getattr(scan_run, "metadata_changed_count", 0),
                "errors": scan_run.errors,
                "notes": scan_run.notes,
                "triggered_by": scan_run.triggered_by,
                "agent_id": scan_run.agent_id,
            }
            if scan_run is not None
            else None
        ),
    }
    return serialized


# ============================================================
# Stage C: Scan Run endpoints
# ============================================================


def _scan_run_to_dict(run: EcosystemScanRun) -> dict[str, Any]:
    """Serialise an EcosystemScanRun into a JSON-friendly dict."""
    return {
        "id": run.id,
        "strategy": run.strategy.value if hasattr(run.strategy, "value") else run.strategy,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "duration_seconds": run.duration_seconds,
        "repos_added": run.repos_added,
        "repos_updated": run.repos_updated,
        "repos_skipped": run.repos_skipped,
        # v1.6.1 Phase 2: real metadata change count
        "metadata_changed_count": getattr(run, "metadata_changed_count", 0),
        "errors": run.errors,
        "notes": run.notes,
        "triggered_by": run.triggered_by,
        "agent_id": run.agent_id,
    }


class ScanRunCreate(BaseModel):
    strategy: str = "incremental"
    triggered_by: str = "manual"
    notes: str = ""
    agent_id: str | None = None


class ScanRunComplete(BaseModel):
    duration_seconds: float = 0.0
    repos_added: int = 0
    repos_updated: int = 0
    repos_skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    completed_at: str | None = None
    notes: str | None = None


class ScanRunExecute(BaseModel):
    strategy: str = "incremental"
    min_stars: int = 1000
    triggered_by: str = "manual"
    notes: str = ""
    agent_id: str | None = None


def _coerce_strategy(value: str) -> EcosystemScanStrategy:
    """Map a strategy string to the enum, defaulting to INCREMENTAL on bad input."""
    try:
        return EcosystemScanStrategy(value)
    except ValueError:
        return EcosystemScanStrategy.INCREMENTAL


@router.post("/scan-runs")
async def create_scan_run(
    body: ScanRunCreate,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Create a new EcosystemScanRun row in OPEN state.

    Used by the legacy ecosystem_scan tool to bracket each invocation.
    """
    run = EcosystemScanRun(
        strategy=_coerce_strategy(body.strategy),
        triggered_by=body.triggered_by or "manual",
        notes=body.notes or "",
        agent_id=body.agent_id,
    )
    await repo.create_scan_run(run)
    return _scan_run_to_dict(run)


@router.get("/scan-runs")
async def list_scan_runs(
    strategy: str = Query(default=""),
    limit: int = Query(default=10, le=100, ge=1),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List recent scan runs, ordered by started_at DESC."""
    rows = await repo.list_scan_runs(strategy=strategy, limit=limit)
    return {
        "runs": [_scan_run_to_dict(r) for r in rows],
        "total": len(rows),
    }


@router.get("/scan-runs/{run_id}")
async def get_scan_run(
    run_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Get a single scan run by id."""
    row = await repo.get_scan_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="scan run not found")
    return _scan_run_to_dict(row)


@router.post("/scan-runs/{run_id}/complete")
async def complete_scan_run(
    run_id: str,
    body: ScanRunComplete,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Update a scan run with completion stats."""
    completed_at = _parse_dt(body.completed_at) or datetime.now(tz=UTC)
    fields: dict[str, Any] = {
        "completed_at": completed_at,
        "duration_seconds": body.duration_seconds,
        "repos_added": body.repos_added,
        "repos_updated": body.repos_updated,
        "repos_skipped": body.repos_skipped,
        "errors": body.errors,
    }
    if body.notes is not None:
        fields["notes"] = body.notes
    updated = await repo.update_scan_run(run_id, **fields)
    if updated is None:
        raise HTTPException(status_code=404, detail="scan run not found")
    return _scan_run_to_dict(updated)


@router.post("/scan-runs/execute")
async def execute_scan_run(
    body: ScanRunExecute,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Trigger a scanner run synchronously and return the resulting summary.

    This drives the EcosystemScanner service end-to-end: open a ScanRun,
    fan out gh queries, apply secondary filters, upsert profiles, close
    the ScanRun. Errors are collected (graceful degradation).
    """
    config = FilterConfig.from_env()
    config.min_stars = body.min_stars or config.min_stars
    scanner = EcosystemScanner(repo=repo, gh_search=default_gh_search, config=config)
    strategy = _coerce_strategy(body.strategy)
    result = await scanner.scan(
        strategy=strategy,
        triggered_by=body.triggered_by or "manual",
        agent_id=body.agent_id,
        notes=body.notes or "",
    )
    return {
        "run_id": result.run_id,
        "strategy": result.strategy,
        "scanned": result.scanned,
        "new_profiles": result.new_profiles,
        "updated_profiles": result.updated_profiles,
        "skipped": result.skipped,
        "archived_marked": result.archived_marked,
        "errors": result.errors,
        "duration_seconds": result.duration_seconds,
        "per_query_stats": result.per_query_stats,
        "category_distribution": result.category_distribution,
    }


class EcosystemRefreshBody(BaseModel):
    """按需增量刷新请求体。"""

    notes: str = ""
    triggered_by: str = "manual"


async def _default_repo_fetcher(full_name: str) -> dict[str, Any]:
    """gh CLI 单仓探测，产出 EcosystemRefresher 约定的 GhFetcher 响应。

    返回 {http_status, pushed_at, stars, is_archived[, error_message,
    rate_limit_remaining]}；gh 不可用/超时归为 http_status=0（transient）。
    """
    import asyncio
    import json as _json
    import subprocess

    def _run() -> dict[str, Any]:
        try:
            proc = subprocess.run(  # noqa: S603, S607 — 固定命令，参数仅仓名
                ["gh", "api", f"repos/{full_name}"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"http_status": 0, "error_message": str(exc)[:200]}
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            if "404" in err or "Not Found" in err:
                return {"http_status": 404, "error_message": err[:200]}
            if "rate limit" in err.lower():
                return {
                    "http_status": 403,
                    "rate_limit_remaining": 0,
                    "error_message": err[:200],
                }
            if "403" in err:
                return {"http_status": 403, "error_message": err[:200]}
            return {"http_status": 0, "error_message": err[:200]}
        try:
            d = _json.loads(proc.stdout)
        except ValueError as exc:
            return {"http_status": 0, "error_message": f"bad json: {exc}"}
        return {
            "http_status": 200,
            "pushed_at": d.get("pushed_at"),
            "stars": int(d.get("stargazers_count") or 0),
            "is_archived": bool(d.get("archived")),
        }

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


@router.post("/refresh")
async def run_shallow_refresh(
    body: EcosystemRefreshBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """按需增量刷新活跃集（2026-07-10 用户裁定：周期 cron 退役，改手动触发）。

    对项目活跃集（top_n by stars）逐仓 gh 探测：写状态快照；
    pushed_at 有更新的重排 Stage 0 浅扫；404/403 标记 deleted/private。
    原 `_ensure_ecosystem_weekly_cron` 注册的每周 emit_event 无消费者，
    刷新能力此前实际不可达——本端点是它的第一个真实入口。
    """
    from aiteam.services.ecosystem_refresher import EcosystemRefresher
    from aiteam.services.ecosystem_shallow_queue import EcosystemShallowQueueWorker

    worker = EcosystemShallowQueueWorker(repo=repo)
    refresher = EcosystemRefresher(repo, worker, gh_fetcher=_default_repo_fetcher)
    r = await refresher.shallow_refresh(
        triggered_by=body.triggered_by or "manual", notes=body.notes
    )
    resp: dict[str, Any] = {
        "success": True,
        "project_id": r.project_id,
        "active_total": r.active_total,
        "refreshed": r.refreshed,
        "skipped_no_diff": r.skipped_no_diff,
        "snapshots_written": r.snapshots_written,
        "marked_deleted": r.marked_deleted,
        "marked_private": r.marked_private,
        "transient_errors": r.transient_errors,
        "errors": r.errors[:20],
        "scan_run_id": r.scan_run_id,
    }
    if r.refreshed:
        resp["hint"] = (
            f"{r.refreshed} 个仓有新推送已重排浅扫队列。"
            "浅扫执行请开启 ultracode 用 Workflow 编排，"
            "产物回写 ecosystem_apply_shallow_summary（模板见 skill /os-workflow）。"
        )
    return resp


# ============================================================
# Stage D — Tag dictionary + tagger endpoints
# ============================================================


def _tag_to_dict(t: EcosystemTag) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "aliases": t.aliases,
        "category": t.category.value if hasattr(t.category, "value") else t.category,
        "description": t.description,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


class TagApplyBatchRequest(BaseModel):
    repo_ids: list[str] = Field(default_factory=list, description="留空则对全库 profile 打标")
    repo_full_names: list[str] = Field(default_factory=list)
    agent_id: str | None = "ecosystem-tagger"
    limit: int = Field(default=200, ge=1, le=1000)
    replace_auto: bool = Field(
        default=False,
        description=(
            "True 时先删除每仓 source∈{github_topic,auto_rule} 的旧标签再插入新计算结果。"
            "保留 manual/auto_llm 不动。用于规则升级后清理陈旧假阳性。默认 False (追加模式) 保持向后兼容。"
        ),
    )


class TagApplyLLMResultRequest(BaseModel):
    repo_id: str
    tags: list[dict[str, Any]] = Field(
        default_factory=list,
        description='[{"name": str, "confidence": float}, ...]',
    )
    agent_id: str | None = None


class ManualTagRequest(BaseModel):
    repo_id: str
    tag_name: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    agent_id: str | None = None


class LLMDispatchPlanRequest(BaseModel):
    repo_ids: list[str] = Field(default_factory=list)
    team_name: str = "ecosystem-platform"
    agent_template: str = "researcher"
    max_concurrency: int = Field(default=20, ge=1, le=50)


@router.get("/tags")
async def list_tags(
    category: str = Query(default=""),
    limit: int = Query(default=200, le=500),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """列出全部标签字典；可按 category 过滤。"""
    tags = await repo.list_tags(category=category, limit=limit)
    return {"tags": [_tag_to_dict(t) for t in tags], "total": len(tags)}


@router.post("/tags")
async def upsert_tag(
    payload: dict[str, Any],
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """新增或更新标签（按 name 唯一键）。"""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    category_raw = (payload.get("category") or "").strip()
    try:
        category = EcosystemTagCategory(category_raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid category '{category_raw}'. "
                f"Allowed: {[c.value for c in EcosystemTagCategory]}"
            ),
        ) from exc

    existing = await repo.get_tag_by_name(name)
    is_new = existing is None
    tag = EcosystemTag(
        name=name,
        aliases=payload.get("aliases") or [],
        category=category,
        description=payload.get("description") or "",
    )
    if existing is not None:
        tag.id = existing.id
    await repo.upsert_tag(tag)
    saved = await repo.get_tag_by_name(name)
    if saved is None:
        raise HTTPException(status_code=500, detail="tag not persisted")
    out = _tag_to_dict(saved)
    out["created"] = is_new
    return out


@router.get("/repos/{repo_id}/tags")
async def list_repo_tags(
    repo_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """列出某仓的所有标签关联，附带 tag 详情。"""
    rows = await repo.list_repo_tags(repo_id=repo_id, limit=200)
    out: list[dict[str, Any]] = []
    for r in rows:
        tag = await repo.get_tag(r.tag_id)
        out.append(
            {
                "repo_tag_id": r.id,
                "repo_id": r.repo_id,
                "tag_id": r.tag_id,
                "tag_name": tag.name if tag else None,
                "tag_category": (
                    tag.category.value
                    if tag and hasattr(tag.category, "value")
                    else None
                ),
                "confidence": r.confidence,
                "source": r.source.value if hasattr(r.source, "value") else r.source,
                "agent_id": r.agent_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return {"repo_id": repo_id, "tags": out, "total": len(out)}


async def _resolve_profiles_for_batch(
    repo: StorageRepository,
    repo_ids: list[str],
    repo_full_names: list[str],
    limit: int,
) -> list[EcosystemRepoProfile]:
    profiles: list[EcosystemRepoProfile] = []
    if repo_ids:
        for rid in repo_ids:
            p = await repo.get_ecosystem_profile_by_id(rid)
            if p is not None:
                profiles.append(p)
    elif repo_full_names:
        for name in repo_full_names:
            p = await repo.get_ecosystem_profile(name)
            if p is not None:
                profiles.append(p)
    else:
        rows, _ = await repo.search_ecosystem_profiles_extended(limit=limit)
        profiles = list(rows)
    return profiles


@router.post("/tags/apply")
async def apply_tags_batch(
    req: TagApplyBatchRequest,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """对一批仓执行 Layer 1+2 自动打标 (GitHub topics + 规则)。

    - 若 repo_ids 与 repo_full_names 均为空，处理全库前 limit 条 profile。
    - 返回每仓命中分布 + 哪些仓需要 Layer 3 LLM 兜底。
    """
    profiles = await _resolve_profiles_for_batch(
        repo, req.repo_ids, req.repo_full_names, req.limit
    )

    repo_dicts = [
        {
            "id": p.id,
            "repo_full_name": p.repo_full_name,
            "name": p.name,
            "owner": p.owner,
            "description": p.description,
            "topics": p.topics,
            "language": p.language,
        }
        for p in profiles
    ]

    tagger = EcosystemTagger(repo)
    stats = await tagger.tag_repos_batch(
        repo_dicts,
        agent_id=req.agent_id,
        replace_auto=req.replace_auto,
    )
    return stats.to_dict()


@router.post("/tags/llm/dispatch_plan")
async def build_llm_dispatch_plan(
    req: LLMDispatchPlanRequest,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """为指定仓集生成 Layer 3 LLM 子 agent 派遣计划。"""
    profiles: list[EcosystemRepoProfile] = []
    for rid in req.repo_ids:
        p = await repo.get_ecosystem_profile_by_id(rid)
        if p is not None:
            profiles.append(p)
    if not profiles:
        return {
            "team_name": req.team_name,
            "agent_template": req.agent_template,
            "max_concurrency": req.max_concurrency,
            "total_requested": 0,
            "dispatched": 0,
            "skipped_due_to_limit": 0,
            "dispatch": [],
            "instructions": "no repos to dispatch",
        }

    repo_dicts = [
        {
            "id": p.id,
            "repo_full_name": p.repo_full_name,
            "description": p.description,
            "topics": p.topics,
            "language": p.language,
        }
        for p in profiles
    ]

    tagger = EcosystemTagger(repo)
    plan = await tagger.build_llm_dispatch_plan(
        repo_dicts,
        team_name=req.team_name,
        agent_template=req.agent_template,
        max_concurrency=req.max_concurrency,
    )
    return plan


@router.post("/tags/llm/result")
async def apply_llm_tag_result(
    req: TagApplyLLMResultRequest,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """sub-agent 提交 Layer 3 LLM 打标结果。"""
    profile = await repo.get_ecosystem_profile_by_id(req.repo_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="repo not found")

    tagger = EcosystemTagger(repo)
    result = await tagger.apply_llm_tags(
        repo_id=req.repo_id,
        repo_full_name=profile.repo_full_name,
        llm_output_tags=req.tags,
        agent_id=req.agent_id,
    )
    return result.to_dict()


@router.post("/tags/manual")
async def manual_tag(
    req: ManualTagRequest,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """手动给某仓打标。"""
    profile = await repo.get_ecosystem_profile_by_id(req.repo_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="repo not found")

    tagger = EcosystemTagger(repo)
    rt = await tagger.manual_tag(
        repo_id=req.repo_id,
        tag_name=req.tag_name,
        confidence=req.confidence,
        agent_id=req.agent_id,
    )
    if rt is None:
        raise HTTPException(
            status_code=404, detail=f"tag '{req.tag_name}' not registered"
        )
    return {
        "repo_id": rt.repo_id,
        "tag_id": rt.tag_id,
        "tag_name": req.tag_name,
        "confidence": rt.confidence,
        "source": rt.source.value if hasattr(rt.source, "value") else rt.source,
    }


@router.delete("/repos/{repo_id}/tags/{tag_name}")
async def remove_tag(
    repo_id: str,
    tag_name: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """移除某仓的某标签关联。"""
    tagger = EcosystemTagger(repo)
    removed = await tagger.remove_tag(repo_id, tag_name)
    if not removed:
        raise HTTPException(status_code=404, detail="tag not associated with repo")
    return {"removed": True, "repo_id": repo_id, "tag_name": tag_name}


# ============================================================
# Stage G — summarizer endpoints
# ============================================================


def _get_summarizer(repo: StorageRepository) -> EcosystemSummarizer:
    return EcosystemSummarizer(repo=repo)


@router.get("/summary/weekly")
async def summary_weekly(
    window_days: int = Query(default=7, ge=1, le=90),
    top_movers_limit: int = Query(default=5, ge=1, le=20),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """生成过去 N 天的生态简报 markdown。

    返回 {markdown: str, window_days, generated_at}。MCP 层负责 report_save。
    """
    summarizer = _get_summarizer(repo)
    md = await summarizer.weekly_summary(
        window_days=window_days,
        top_movers_limit=top_movers_limit,
    )
    return {
        "markdown": md,
        "window_days": window_days,
        "top_movers_limit": top_movers_limit,
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }


@router.get("/summary/by_tag")
async def summary_by_tag(
    tag: str = Query(..., min_length=1),
    include_archived: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=500),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """按 tag 列出该方向所有仓 markdown。"""
    summarizer = _get_summarizer(repo)
    md = await summarizer.by_tag_summary(
        tag,
        include_archived=include_archived,
        limit=limit,
    )
    return {
        "markdown": md,
        "tag": tag,
        "include_archived": include_archived,
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }


@router.get("/summary/top_n")
async def summary_top_n(
    category: str = Query(default=""),
    n: int = Query(default=10, ge=1, le=100),
    sort: str = Query(default="stars"),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Top N 排行 markdown 表格。sort: stars / pushed_at / scan_freshness。"""
    if sort not in TOP_N_SORT_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"sort must be one of {list(TOP_N_SORT_OPTIONS)}",
        )
    summarizer = _get_summarizer(repo)
    md = await summarizer.top_n_summary(category=category, n=n, sort=sort)
    return {
        "markdown": md,
        "category": category,
        "n": n,
        "sort": sort,
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }


@router.get("/summary/health")
async def summary_health(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """平台自检 markdown：仓数 / 扫描 / 深扫 / 标签覆盖 / 失活率。"""
    summarizer = _get_summarizer(repo)
    md = await summarizer.health_summary()
    return {
        "markdown": md,
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }


# ============================================================
# v1.5.0-B Stage 0 shallow-scan queue endpoints
# ============================================================


class ApplyShallowSummaryBody(BaseModel):
    """Payload from a Stage 0 ai-engineer sub-agent reporting its summary."""

    repo_id: str
    shallow_summary: str = ""
    deep_review_id: str | None = None
    error_kind: str = ""
    error_message: str = ""
    http_status: int | None = None
    rate_limit_remaining: int | None = None


def _get_shallow_worker(
    repo: StorageRepository,
):
    """Construct an EcosystemShallowQueueWorker bound to repo's project."""
    from aiteam.services.ecosystem_shallow_queue import EcosystemShallowQueueWorker

    return EcosystemShallowQueueWorker(repo=repo)


@router.post("/shallow_queue/apply_summary")
async def apply_shallow_summary(
    body: ApplyShallowSummaryBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Apply a Stage 0 shallow summary or report a failure.

    On success path:
      - update_profile_shallow_summary
      - update_deep_review_stage to SHALLOW_DONE
    On failure path:
      - delegate to worker.report_failure (handles classification, profile
        flag updates, and self-learning bookkeeping).
    """
    from aiteam.services.ecosystem_shallow_queue import EcosystemShallowQueueWorker
    from aiteam.types import EcosystemStageStatus

    worker = EcosystemShallowQueueWorker(repo=repo)

    if body.error_kind:
        decision = await worker.report_failure(
            body.repo_id,
            error_kind=body.error_kind,
            http_status=body.http_status,
            error_message=body.error_message,
            rate_limit_remaining=body.rate_limit_remaining,
            deep_review_id=body.deep_review_id,
        )
        return {
            "success": False,
            "failure_class": decision.failure_class,
            "immediate_retry": decision.immediate_retry,
            "retry_delay_seconds": decision.retry_delay_seconds,
            "marked_deleted": decision.mark_deleted,
            "marked_private": decision.mark_private,
        }

    summary = (body.shallow_summary or "").strip()
    if not summary:
        raise HTTPException(
            status_code=400,
            detail="shallow_summary required when error_kind not set",
        )

    profile = await repo.update_profile_shallow_summary(
        body.repo_id, shallow_summary=summary
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="repo profile not found")

    review = None
    if body.deep_review_id:
        # D5 收敛：stage_status 是唯一权威轴；update_deep_review_stage 的
        # choke point 会派生 status=completed、补 completed_at 并释放认领，
        # v1.6.1 的 status 补写补丁块已随之删除。
        review = await repo.update_deep_review_stage(
            body.deep_review_id,
            EcosystemStageStatus.SHALLOW_DONE,
        )

    return {
        "success": True,
        "repo_id": profile.id,
        "shallow_summary_length": len(profile.shallow_summary),
        "stage_status": review.stage_status.value if review else None,
    }


@router.get("/shallow_queue/status")
async def shallow_queue_status(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return Stage 0 queue metrics for the active project."""
    worker = _get_shallow_worker(repo)
    return await worker.queue_status()


@router.post("/shallow_queue/tick")
async def shallow_queue_tick(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Run one Stage 0 worker tick on demand (Leader-driven)."""
    worker = _get_shallow_worker(repo)
    result = await worker.tick()
    return {
        "queued": result.queued,
        "dispatched": result.dispatched,
        "skipped_inflight": result.skipped_inflight,
        "errors": result.errors,
        "intents": [
            {
                "repo_id": i.repo_id,
                "repo_full_name": i.repo_full_name,
                "deep_review_id": i.deep_review_id,
                "prompt": i.prompt,
                "timeout_seconds": i.timeout_seconds,
            }
            for i in result.intents
        ],
    }


# ============================================================
# v1.5.0-C — Stage 1 / 2 / 3 lifecycle trigger routes
# ============================================================


class DeepReviewRequestBatchBody(BaseModel):
    """Stage 1 batch request payload (POST /api/ecosystem/lifecycle/request_batch)."""

    tags: list[str] = Field(default_factory=list)
    min_stars: int | None = None
    limit: int = 20
    research_goal: str = ""


class ApplyArchitectureMdBody(BaseModel):
    """Stage 1 writeback payload."""

    deep_review_id: str
    architecture_md: str = ""
    agent_id: str | None = None
    error_message: str = ""  # 非空时进入 architecture_failed 路径


class TriggerDebateBody(BaseModel):
    """Stage 2 debate trigger payload."""

    repo_ids: list[str] = Field(default_factory=list)
    research_goal: str = ""
    suggested_advocate: str = "backend-architect"
    suggested_critic: str = "code-reviewer"
    suggested_judge: str = "team-lead"


class LinkDebateMeetingBody(BaseModel):
    """Stage 2 link meeting payload (called after debate_start)."""

    review_ids: list[str] = Field(default_factory=list)
    meeting_id: str


class ApplyDebateResultBody(BaseModel):
    """Stage 2 result writeback payload."""

    deep_review_id: str
    risks_md: str = ""
    learnings_md: str = ""
    integration_md: str = ""
    integration_recommendation: str = ""
    agent_id: str | None = None


class MarkAsReferenceBody(BaseModel):
    """Stage 3 reference path payload."""

    deep_review_id: str
    agent_id: str | None = None
    confidence: float = 1.0


class StartIntegrationBody(BaseModel):
    """Stage 3 integrate path — request task payload."""

    deep_review_id: str
    title: str = ""
    description: str = ""
    priority: str = "high"
    horizon: str = "mid"
    extra_tags: list[str] = Field(default_factory=list)


class LinkIntegrationTaskBody(BaseModel):
    """Stage 3 link task payload (called after task_create)."""

    deep_review_id: str
    task_id: str


def _get_lifecycle_service(repo: StorageRepository):
    """Construct an EcosystemLifecycleService bound to repo's project scope."""
    from aiteam.services.ecosystem_lifecycle import EcosystemLifecycleService

    return EcosystemLifecycleService(repo)


@router.post("/lifecycle/request_batch")
async def lifecycle_request_batch(
    body: DeepReviewRequestBatchBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 1: queue a batch of deep_review rows for tag-filtered candidates.

    Returns dispatch intents for backend-architect sub-agents. Leader is
    responsible for actually spawning each agent via the Agent tool.
    """
    service = _get_lifecycle_service(repo)
    try:
        intents = await service.request_deep_review_batch(
            tags=body.tags,
            min_stars=body.min_stars,
            limit=body.limit,
            research_goal=body.research_goal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "dispatched": len(intents),
        "intents": [
            {
                "repo_id": i.repo_id,
                "repo_full_name": i.repo_full_name,
                "deep_review_id": i.deep_review_id,
                "prompt": i.prompt,
                "timeout_seconds": i.timeout_seconds,
                "project_id": i.project_id,
            }
            for i in intents
        ],
    }


@router.post("/lifecycle/apply_architecture_md")
async def lifecycle_apply_architecture_md(
    body: ApplyArchitectureMdBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 1 writeback: agent submits architecture_md or reports failure."""
    from aiteam.types import EcosystemStageStatus

    service = _get_lifecycle_service(repo)

    # Failure path — empty architecture_md + non-empty error_message
    if not body.architecture_md.strip() and body.error_message:
        review = await repo.get_deep_review(body.deep_review_id)
        if review is None:
            raise HTTPException(status_code=404, detail="deep_review not found")
        # D5: 只追加 risks_md 注记；status=failed / completed_at 由随后的
        # ARCHITECTURE_FAILED stage 推进派生补齐。
        await repo.update_deep_review(
            body.deep_review_id,
            risks_md=(review.risks_md or "")
            + f"\n\n[architecture failed] {body.error_message}",
        )
        await repo.update_deep_review_stage(
            body.deep_review_id,
            EcosystemStageStatus.ARCHITECTURE_FAILED,
        )
        return {
            "success": False,
            "stage_status": EcosystemStageStatus.ARCHITECTURE_FAILED.value,
            "error": body.error_message,
        }

    try:
        review = await service.apply_architecture_md(
            body.deep_review_id,
            architecture_md=body.architecture_md,
            agent_id=body.agent_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "deep_review_id": review.id,
        "stage_status": review.stage_status.value,
        "architecture_md_length": len(review.architecture_md),
    }


@router.post("/lifecycle/trigger_debate")
async def lifecycle_trigger_debate(
    body: TriggerDebateBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 2: build a debate dispatch intent (caller must invoke debate_start)."""
    service = _get_lifecycle_service(repo)
    try:
        intent = await service.trigger_debate(
            repo_ids=body.repo_ids,
            research_goal=body.research_goal,
            suggested_advocate=body.suggested_advocate,
            suggested_critic=body.suggested_critic,
            suggested_judge=body.suggested_judge,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "review_ids": intent.review_ids,
        "repo_full_names": intent.repo_full_names,
        "research_goal": intent.research_goal,
        "suggested_topic": intent.suggested_topic,
        "suggested_advocate": intent.suggested_advocate,
        "suggested_critic": intent.suggested_critic,
        "suggested_judge": intent.suggested_judge,
        "project_id": intent.project_id,
        "next_action": (
            "调 debate_start(topic=suggested_topic, advocate=..., critic=..., "
            "judge=...) 创建会议，然后再调 /lifecycle/link_debate_meeting "
            "把 meeting.id 写回 review_ids。"
        ),
    }


# ============================================================
# v1.7.0: 浅扫批次管理 endpoints
# ============================================================


class CreateShallowBatchBody(BaseModel):
    triggered_by: str = "user"  # 'cron' / 'manual' / 'user'
    trigger_reason: str | None = None


class ApproveBatchBody(BaseModel):
    approved_by: str = "user"


@router.post("/shallow_batches")
async def create_shallow_batch(
    body: CreateShallowBatchBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """创建新浅扫批次 — 发现候选仓并固化快照，状态=pending_approval，等待人工审批。

    候选仓逻辑：活跃仓（is_deleted=False, last_active_status != 'archived'）中
    shallow_summary 为空或过期（> 30 天未刷新）的仓。
    """
    import json
    import uuid
    from datetime import timedelta

    from aiteam.types import EcosystemShallowBatch

    # Clock convention: profile timestamps are stored naive-local (same as the
    # rest of the DB) — an aware UTC cutoff here raised TypeError on compare.
    now = datetime.now()
    cutoff = now - timedelta(days=30)

    # 发现候选仓：读所有活跃档案，筛选需要浅扫的仓
    all_profiles = await repo.search_ecosystem_profiles(limit=1000)

    candidates = []
    for p in all_profiles:
        # 排除已删除 / 私有 / 归档
        if p.is_deleted or p.is_private_now:
            continue
        if p.last_active_status in ("archived", "manual_archived"):
            continue
        # 候选条件：无浅扫摘要 或 摘要超过 30 天
        ts = p.last_shallow_refreshed_at
        if ts is not None and ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        needs_refresh = not p.shallow_summary or ts is None or ts < cutoff
        if needs_refresh:
            candidates.append(p.id)

    batch = EcosystemShallowBatch(
        id=str(uuid.uuid4()),
        project_id=repo._project_scope or None,
        triggered_by=body.triggered_by,
        trigger_reason=body.trigger_reason,
        candidates_count=len(candidates),
        candidates_snapshot_json=json.dumps(candidates),
        status="pending_approval",
        created_at=now,
        updated_at=now,
    )
    created = await repo.create_shallow_batch(batch)
    return {
        "success": True,
        "batch_id": created.id,
        "candidates_count": created.candidates_count,
        "status": created.status,
        "message": f"批次已创建，发现 {created.candidates_count} 个候选仓，等待审批",
    }


@router.get("/shallow_batches")
async def list_shallow_batches(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """列出浅扫批次（按 created_at 倒序）。"""
    batches, total = await repo.list_shallow_batches(limit=limit, offset=offset)
    return {
        "batches": [_batch_to_dict(b) for b in batches],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/shallow_batches/{batch_id}")
async def get_shallow_batch(
    batch_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """查询单个浅扫批次详情。"""
    batch = await repo.get_shallow_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return {"batch": _batch_to_dict(batch)}


@router.get("/shallow_batches/{batch_id}/items")
async def get_shallow_batch_items(
    batch_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """查询批次内候选仓清单。"""
    batch = await repo.get_shallow_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    items = await repo.get_batch_items(batch_id)
    return {
        "batch_id": batch_id,
        "items": items,
        "total": len(items),
    }


@router.post("/shallow_batches/{batch_id}/approve")
async def approve_shallow_batch(
    batch_id: str,
    body: ApproveBatchBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """审批通过批次 — 创建 DR 行并触发 tick 开始运行。"""
    import json

    from aiteam.types import EcosystemDeepReview, EcosystemStageStatus

    batch = await repo.get_shallow_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if batch.status != "pending_approval":
        raise HTTPException(
            status_code=400,
            detail=f"batch status is '{batch.status}', only pending_approval can be approved",
        )

    now = datetime.now()  # naive-local, matches DB-wide clock convention

    # 读候选快照，为每个 repo 创建 DR 行（若已有未完成行则跳过）
    candidate_ids: list[str] = []
    if batch.candidates_snapshot_json:
        try:
            candidate_ids = json.loads(batch.candidates_snapshot_json)
        except Exception:
            candidate_ids = []

    created_count = 0
    for repo_id in candidate_ids:
        # D5: status 由 create_deep_review 按 stage_status 派生，不再传入。
        dr = EcosystemDeepReview(
            project_id=batch.project_id,
            repo_id=repo_id,
            stage_status=EcosystemStageStatus.QUEUED,
            batch_id=batch_id,
            created_at=now,
        )
        try:
            await repo.create_deep_review(dr)
            created_count += 1
        except Exception:
            # 已存在同 repo 未完成 DR 时忽略重复
            pass

    # 更新批次状态
    updated = await repo.update_shallow_batch(
        batch_id,
        status="running",
        approved_by=body.approved_by,
        approved_at=now,
    )
    if updated is None:
        raise HTTPException(status_code=500, detail="failed to update batch status")

    # 触发一次 tick 让 worker 开始认领
    try:
        worker = _get_shallow_worker(repo)
        await worker.tick()
    except Exception:
        pass  # tick 失败不阻断审批

    return {
        "success": True,
        "batch_id": batch_id,
        "status": "running",
        "dr_created": created_count,
        "message": f"批次已批准，创建 {created_count} 个浅扫任务，worker 已触发",
    }


@router.post("/shallow_batches/{batch_id}/cancel")
async def cancel_shallow_batch(
    batch_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """取消批次 — 将 DR 行（如果有）标记为 cancelled。"""
    batch = await repo.get_shallow_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if batch.status in ("completed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"batch is already '{batch.status}', cannot cancel",
        )

    # 更新批次状态
    updated = await repo.update_shallow_batch(batch_id, status="cancelled")
    if updated is None:
        raise HTTPException(status_code=500, detail="failed to cancel batch")

    return {
        "success": True,
        "batch_id": batch_id,
        "status": "cancelled",
        "message": "批次已取消",
    }


@router.post("/shallow_batches/{batch_id}/rollback")
async def rollback_shallow_batch(
    batch_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """回滚批次 — approve 的逆操作。

    删除该批次中尚未开工的浅扫任务行（stage_status='queued' 且未被认领），
    已认领/已完成的行保留；批次置 cancelled。被移除的仓在下次创建批次时
    会被重新发现（候选按"30 天未刷新"规则实时计算）。
    """
    batch = await repo.get_shallow_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    deleted = await repo.delete_unstarted_batch_reviews(batch_id)
    await repo.update_shallow_batch(batch_id, status="cancelled")
    return {
        "success": True,
        "batch_id": batch_id,
        "deleted_queued": deleted,
        "status": "cancelled",
        "message": f"批次已回滚，移除 {deleted} 个未开工任务",
    }


def _batch_to_dict(b: Any) -> dict:
    """将 EcosystemShallowBatch 转为 JSON-safe dict。"""
    return {
        "id": b.id,
        "project_id": b.project_id,
        "triggered_by": b.triggered_by,
        "trigger_reason": b.trigger_reason,
        "candidates_count": b.candidates_count,
        "status": b.status,
        "approved_by": b.approved_by,
        "approved_at": b.approved_at.isoformat() if b.approved_at else None,
        "completed_at": b.completed_at.isoformat() if b.completed_at else None,
        "new_repos_count": b.new_repos_count,
        "updated_repos_count": b.updated_repos_count,
        "metadata_changed_count": b.metadata_changed_count,
        "failed_count": b.failed_count,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


# ============================================================
# Worker pool claim endpoints (v1.5.3)
# ============================================================


class ShallowQueueClaimBody(BaseModel):
    worker_id: str


class ReviewQueueClaimBody(BaseModel):
    worker_id: str
    min_stars: int = 0


class ApplyQualityReviewBody(BaseModel):
    dr_id: str
    quality_score: int = Field(..., ge=0, le=100)
    quality_notes: str = ""
    recommendation: str = ""


class ReleaseClaimBody(BaseModel):
    dr_id: str
    reason: str = ""


@router.post("/shallow_queue/claim")
async def shallow_queue_claim(
    body: ShallowQueueClaimBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """认领下一个待浅扫仓（stage_status='queued'，claimed_by IS NULL）。

    原子操作：SELECT + UPDATE WHERE claimed_by IS NULL，并发安全。
    v1.7.0: 补充 repo_full_name/topics/description/owner/stars/last_commit_at，
            省去 worker 每仓一次 ecosystem_repo_get 调用。
    Returns claimed deep_review row or {"claimed": false} when queue is empty.
    """
    review = await repo.claim_next_shallow_repo(worker_id=body.worker_id)
    if review is None:
        return {"claimed": False}

    # 拉 repo profile 基础字段
    profile = await repo.get_ecosystem_profile_by_id(review.repo_id)
    extra: dict[str, Any] = {}
    if profile is not None:
        extra = {
            "repo_full_name": profile.repo_full_name,
            "topics": profile.topics,
            "description": profile.description,
            "owner": profile.owner,
            "stars": profile.stars,
            "last_commit_at": profile.last_commit_at.isoformat() if profile.last_commit_at else None,
        }

    return {
        "claimed": True,
        "dr_id": review.id,
        "repo_id": review.repo_id,
        "claimed_by": review.claimed_by,
        "claimed_at": review.claimed_at.isoformat() if review.claimed_at else None,
        "stage_status": review.stage_status.value
        if hasattr(review.stage_status, "value")
        else str(review.stage_status),
        **extra,
    }


@router.post("/review_queue/claim")
async def review_queue_claim(
    body: ReviewQueueClaimBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """认领下一个待质量审查行（stage_status='shallow_done'，quality_score IS NULL，claimed_by IS NULL）。

    同时返回关联 repo profile 的 shallow_summary 供审查者评估。
    Returns claimed review + profile or {"claimed": false} when queue is empty.
    """
    result = await repo.claim_next_review_repo(
        worker_id=body.worker_id,
        min_stars=body.min_stars,
    )
    if result is None:
        return {"claimed": False}
    review, profile = result
    return {
        "claimed": True,
        "dr_id": review.id,
        "repo_id": review.repo_id,
        "claimed_by": review.claimed_by,
        "claimed_at": review.claimed_at.isoformat() if review.claimed_at else None,
        "stage_status": review.stage_status.value
        if hasattr(review.stage_status, "value")
        else str(review.stage_status),
        "repo_full_name": profile.repo_full_name,
        "stars": profile.stars,
        "shallow_summary": profile.shallow_summary,
    }


@router.post("/review_queue/apply")
async def review_queue_apply(
    body: ApplyQualityReviewBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """写入质量审查结果并释放认领锁。

    写入 quality_score / quality_notes / reviewed_by / reviewed_at，
    清空 claimed_by 释放认领锁。
    """
    review = await repo.apply_quality_review(
        dr_id=body.dr_id,
        quality_score=body.quality_score,
        quality_notes=body.quality_notes,
        recommendation=body.recommendation,
    )
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "dr_id": review.id,
        "quality_score": review.quality_score,
        "quality_notes": review.quality_notes,
        "reviewed_by": review.reviewed_by,
        "reviewed_at": review.reviewed_at.isoformat() if review.reviewed_at else None,
        "claimed_by": review.claimed_by,
    }


@router.post("/claims/release")
async def claims_release(
    body: ReleaseClaimBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """释放认领锁（worker 放弃），清空 claimed_by 让其他 worker 可重新认领。

    reason 记录到 quality_notes 便于追踪。
    """
    review = await repo.release_claim(dr_id=body.dr_id, reason=body.reason)
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "dr_id": review.id,
        "claimed_by": review.claimed_by,
        "quality_notes": review.quality_notes,
    }


@router.post("/lifecycle/link_debate_meeting")
async def lifecycle_link_debate_meeting(
    body: LinkDebateMeetingBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 2 helper: link meeting_id to review rows after debate_start."""
    service = _get_lifecycle_service(repo)
    updated = await service.link_debate_meeting(
        review_ids=body.review_ids,
        meeting_id=body.meeting_id,
    )
    return {
        "success": True,
        "linked": updated,
        "meeting_id": body.meeting_id,
    }


@router.post("/lifecycle/apply_debate_result")
async def lifecycle_apply_debate_result(
    body: ApplyDebateResultBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 2 writeback: agent submits debate conclusion → debated."""
    service = _get_lifecycle_service(repo)
    try:
        review = await service.apply_debate_result(
            body.deep_review_id,
            risks_md=body.risks_md,
            learnings_md=body.learnings_md,
            integration_md=body.integration_md,
            integration_recommendation=body.integration_recommendation,
            agent_id=body.agent_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "deep_review_id": review.id,
        "stage_status": review.stage_status.value,
        "debated_at": review.debated_at.isoformat() if review.debated_at else None,
    }


@router.post("/lifecycle/mark_as_reference")
async def lifecycle_mark_as_reference(
    body: MarkAsReferenceBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 3 reference: 加 lifecycle:reference tag + 推进 referenced。"""
    service = _get_lifecycle_service(repo)
    review = await service.mark_as_reference(
        body.deep_review_id,
        agent_id=body.agent_id,
        confidence=body.confidence,
    )
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "deep_review_id": review.id,
        "stage_status": review.stage_status.value,
        "stage3_completed_at": (
            review.stage3_completed_at.isoformat()
            if review.stage3_completed_at
            else None
        ),
    }


@router.post("/lifecycle/start_integration")
async def lifecycle_start_integration(
    body: StartIntegrationBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 3 integrate: build a task dispatch intent."""
    service = _get_lifecycle_service(repo)
    try:
        intent = await service.start_integration(
            body.deep_review_id,
            title=body.title,
            description=body.description,
            priority=body.priority,
            horizon=body.horizon,
            extra_tags=body.extra_tags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "review_id": intent.review_id,
        "repo_id": intent.repo_id,
        "repo_full_name": intent.repo_full_name,
        "task_payload": {
            "title": intent.title,
            "description": intent.description,
            "priority": intent.priority,
            "horizon": intent.horizon,
            "tags": intent.tags,
        },
        "project_id": intent.project_id,
        "next_action": (
            "调 POST /api/projects/{project_id}/tasks 创建任务，然后再调 "
            "/lifecycle/link_integration_task 把 task.id 写回 review。"
        ),
    }


@router.post("/lifecycle/link_integration_task")
async def lifecycle_link_integration_task(
    body: LinkIntegrationTaskBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Stage 3 helper: link task_id to review row after task_create."""
    service = _get_lifecycle_service(repo)
    review = await service.link_integration_task(
        deep_review_id=body.deep_review_id,
        task_id=body.task_id,
    )
    if review is None:
        raise HTTPException(status_code=404, detail="deep_review not found")
    return {
        "success": True,
        "deep_review_id": review.id,
        "integration_task_id": review.integration_task_id,
        "stage_status": review.stage_status.value,
    }


# ============================================================
# Project Settings (v1.5.0-E 决策 12.1: 项目详情页 Ecosystem 设置 tab)
# ============================================================


class ProjectSettingsBody(BaseModel):
    """项目级 ecosystem 配置 (PUT 入参)。"""

    min_stars: int = Field(default=1000, ge=0)
    top_n: int = Field(default=200, ge=1, le=1000)
    refresh_interval_days: int = Field(default=7, ge=1, le=90)
    auto_shallow_on_archive: bool = True
    focus_topics: list[str] = Field(default_factory=list)
    focus_languages: list[str] = Field(default_factory=list)
    shallow_concurrency: int = Field(default=5, ge=1, le=20)
    deep_concurrency: int = Field(default=3, ge=1, le=10)
    # v1.6.1 Phase 2: migrated from scan_profile.alert_thresholds
    alert_max_new_per_scan: int = Field(default=50, ge=1, le=10000)


def _settings_to_dict(s: Any) -> dict[str, Any]:
    """Serialize EcosystemProjectSettings for API."""
    return {
        "project_id": s.project_id,
        "min_stars": s.min_stars,
        "top_n": s.top_n,
        "refresh_interval_days": s.refresh_interval_days,
        "auto_shallow_on_archive": s.auto_shallow_on_archive,
        "focus_topics": s.focus_topics,
        "focus_languages": s.focus_languages,
        "shallow_concurrency": s.shallow_concurrency,
        "deep_concurrency": s.deep_concurrency,
        "alert_max_new_per_scan": getattr(s, "alert_max_new_per_scan", 50),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("/projects/{project_id}/settings")
async def get_project_settings(
    project_id: str,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """获取项目的 ecosystem 配置（不存在则按默认值创建）。"""
    settings = await repo.ensure_ecosystem_project_settings(project_id)
    return _settings_to_dict(settings)


@router.put("/projects/{project_id}/settings")
async def update_project_settings(
    project_id: str,
    body: ProjectSettingsBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """更新项目的 ecosystem 配置。"""
    from aiteam.types import EcosystemProjectSettings as _Settings

    payload = _Settings(
        project_id=project_id,
        min_stars=body.min_stars,
        top_n=body.top_n,
        refresh_interval_days=body.refresh_interval_days,
        auto_shallow_on_archive=body.auto_shallow_on_archive,
        focus_topics=body.focus_topics,
        focus_languages=body.focus_languages,
        shallow_concurrency=body.shallow_concurrency,
        deep_concurrency=body.deep_concurrency,
        alert_max_new_per_scan=body.alert_max_new_per_scan,
    )
    saved = await repo.upsert_ecosystem_project_settings(payload)
    return _settings_to_dict(saved)


# ============================================================
# Failed repo retry (v1.5.0-E 决策: 失败仓立即重试按钮)
# ============================================================


class RetryFailedRepoBody(BaseModel):
    """重置仓的失败状态以触发下一轮 Stage 0 浅扫。"""

    reason: str = "manual_retry"


@router.post("/profiles/{repo_id}/retry")
async def retry_failed_repo(
    repo_id: str,
    body: RetryFailedRepoBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """重置 fetch_failure_count + last_fetch_error，让仓重新进入浅扫队列。

    用于前端 "立即重试" 按钮。
    """
    profile = await repo.get_ecosystem_profile_by_id(repo_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="ecosystem repo not found")

    # 清失败计数 + 错误消息（不动 is_deleted/is_private_now，由 scanner 重新判定）
    profile.fetch_failure_count = 0
    profile.last_fetch_error = ""
    await repo.upsert_ecosystem_profile(profile)
    return {
        "success": True,
        "repo_id": repo_id,
        "repo_full_name": profile.repo_full_name,
        "reason": body.reason,
        "next_action": "Stage 0 浅扫队列下次 tick 会重新派遣 agent",
    }


# ============================================================
# v1.6.0 P0.2: DataSource + ScanProfile + QuickSetup endpoints
# ============================================================


def _get_project_id_from_repo(repo: StorageRepository, request: Any = None) -> str:
    """Extract project_id from repository scope."""
    return repo._project_scope or ""


class DataSourceCreateBody(BaseModel):
    kind: str
    name: str
    config: dict = Field(default_factory=dict)


class DataSourceUpdateBody(BaseModel):
    name: str | None = None
    config: dict | None = None
    enabled: bool | None = None


class ScanProfileUpdateBody(BaseModel):
    profile: dict


class QuickSetupBody(BaseModel):
    sources: list[str] = Field(default=["github"])
    queries: list[str] = Field(default_factory=list)
    use_defaults: bool = True
    custom_profile: dict | None = None


class IndexUpdateBody(BaseModel):
    dry_run: bool = True


@router.post("/data_sources")
async def create_data_source(
    body: DataSourceCreateBody,
    request: Any = Depends(lambda: None),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Create a new data source configuration for the current project."""
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    try:
        kind = DataSourceKind(body.kind)
    except ValueError:
        valid = [k.value for k in DataSourceKind]
        raise HTTPException(
            status_code=422, detail=f"Invalid kind '{body.kind}'. Valid: {valid}"
        )

    ds = await repo.create_data_source(
        project_id=project_id,
        kind=kind,
        name=body.name,
        config=body.config,
    )
    return {
        "success": True,
        "data_source": {
            "id": ds.id,
            "project_id": ds.project_id,
            "kind": ds.kind.value,
            "name": ds.name,
            "config": ds.config,
            "enabled": ds.enabled,
            "version": ds.version,
            "created_at": ds.created_at.isoformat(),
        },
    }


@router.get("/data_sources")
async def list_data_sources(
    limit: int = Query(default=50, le=100),
    offset: int = Query(default=0, ge=0),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List all data sources for the current project (P1.B: paginated, config truncated).

    config field returns only top-level keys (not full JSON) to prevent token explosion.
    Use GET /data_sources/{id} for full config.
    """
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    sources = await repo.list_data_sources(project_id)
    paged = sources[offset: offset + limit]
    return {
        "success": True,
        "data_sources": [
            {
                "id": ds.id,
                "project_id": ds.project_id,
                "kind": ds.kind.value,
                "name": ds.name,
                # P1.B: return only config key names, not full JSON
                "config_keys": list((ds.config or {}).keys()),
                "queries_count": len((ds.config or {}).get("queries", [])),
                "enabled": ds.enabled,
                "version": ds.version,
                "created_at": ds.created_at.isoformat(),
            }
            for ds in paged
        ],
        "total": len(sources),
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < len(sources),
    }


@router.put("/data_sources/{ds_id}")
async def update_data_source(
    ds_id: str,
    body: DataSourceUpdateBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Update a data source configuration."""
    from aiteam.api.exceptions import NotFoundError

    try:
        ds = await repo.update_data_source(
            ds_id,
            name=body.name,
            config=body.config,
            enabled=body.enabled,
        )
    except NotFoundError:
        raise HTTPException(status_code=404, detail=f"DataSource {ds_id} not found")

    return {
        "success": True,
        "data_source": {
            "id": ds.id,
            "project_id": ds.project_id,
            "kind": ds.kind.value,
            "name": ds.name,
            "config": ds.config,
            "enabled": ds.enabled,
            "version": ds.version,
            "updated_at": ds.updated_at.isoformat(),
        },
    }


@router.get("/scan_profile")
async def get_scan_profile(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Get the active scan profile for the current project.

    Returns the default profile with is_default=True when no profile is configured.
    """
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    profile = await repo.get_active_scan_profile(project_id)
    if profile is None:
        return {
            "success": True,
            "is_default": True,
            "scan_profile": {
                "id": None,
                "project_id": project_id,
                "version": 0,
                "profile": _DEFAULT_SCAN_PROFILE,
                "is_active": True,
                "created_at": None,
            },
        }

    return {
        "success": True,
        "is_default": False,
        "scan_profile": {
            "id": profile.id,
            "project_id": profile.project_id,
            "version": profile.version,
            "profile": profile.profile,
            "is_active": profile.is_active,
            "created_at": profile.created_at.isoformat(),
        },
    }


@router.put("/scan_profile")
async def update_scan_profile(
    body: ScanProfileUpdateBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Create a new scan profile version (old version is deactivated)."""
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    profile = await repo.create_or_update_scan_profile(project_id, body.profile)
    return {
        "success": True,
        "scan_profile": {
            "id": profile.id,
            "project_id": profile.project_id,
            "version": profile.version,
            "profile": profile.profile,
            "is_active": profile.is_active,
            "created_at": profile.created_at.isoformat(),
        },
    }


@router.post("/quick_setup")
async def quick_setup(
    body: QuickSetupBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Minimal 3-question setup wizard — creates data sources + scan profile in one call."""
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    created_sources = []
    for source_kind_str in body.sources:
        try:
            kind = DataSourceKind(source_kind_str)
        except ValueError:
            raise HTTPException(
                status_code=422, detail=f"Unknown source kind: {source_kind_str}"
            )
        ds_config: dict = {}
        if body.queries:
            ds_config["queries"] = body.queries
        ds = await repo.create_data_source(
            project_id=project_id,
            kind=kind,
            name=f"{kind.value} default",
            config=ds_config,
        )
        created_sources.append(ds.id)

    # Build profile: use defaults or merge with custom overrides
    profile_dict = dict(_DEFAULT_SCAN_PROFILE)
    if not body.use_defaults and body.custom_profile:
        profile_dict.update(body.custom_profile)

    profile = await repo.create_or_update_scan_profile(project_id, profile_dict)

    return {
        "success": True,
        "data_source_ids": created_sources,
        "scan_profile_id": profile.id,
        "scan_profile_version": profile.version,
        "next_step": "Call POST /api/ecosystem/index_update?dry_run=true to preview changes",
    }


@router.post("/index_update")
async def index_update(
    body: IndexUpdateBody,
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Trigger a real ecosystem index update scan with diff calculation.

    Flow:
      1. Verify scan_profile + data_sources are configured.
      2. Check gh CLI auth status — return missing_setup when not logged in.
      3. For each enabled github data_source, run EcosystemScanner with queries
         from data_source.config['queries'].
      4. Compute NormalizedSignal for each fresh repo (rank / percentile / activity_score).
      5. Classify each repo against scan_profile.active_definition.
      6. Diff against DB: new / reactivated / deactivated / stale / archived.
      7. Check alert_thresholds — stop + return alert when exceeded.
      8. dry_run=False: upsert profiles + write index_diffs + write status_changes.
    """
    import subprocess

    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    # ── Step 1: verify configuration ──────────────────────────────────────────
    missing: list[str] = []
    sources = await repo.list_data_sources(project_id)
    active_sources = [s for s in sources if s.enabled]
    if not active_sources:
        missing.append("data_source")

    profile = await repo.get_active_scan_profile(project_id)
    if profile is None:
        missing.append("scan_profile")

    if missing:
        return {
            "success": False,
            "dry_run": body.dry_run,
            "missing_setup": missing,
            "message": (
                "Setup incomplete. Call POST /api/ecosystem/quick_setup first, "
                "or configure data_sources and scan_profile individually."
            ),
        }

    assert profile is not None  # narrowing for type checker

    # ── Step 2: check gh CLI auth ─────────────────────────────────────────────
    try:
        auth_check = await asyncio.to_thread(
            subprocess.run,
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if auth_check.returncode != 0:
            return {
                "success": False,
                "dry_run": body.dry_run,
                "missing_setup": ["gh_auth"],
                "message": (
                    "GitHub CLI is not authenticated. "
                    "Run `gh auth login` then retry."
                ),
            }
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {
            "success": False,
            "dry_run": body.dry_run,
            "missing_setup": ["gh_cli"],
            "message": "GitHub CLI (gh) not found. Install from https://cli.github.com/ and run `gh auth login`.",
        }

    # ── Step 3a: capture pre-scan state for diff calculation ──────────────────
    # Must be done BEFORE scanner upserts new repos, so we can identify truly new ones.
    pre_scan_profiles, _ = await repo.search_ecosystem_profiles_extended(
        limit=5000,
        offset=0,
    )
    pre_scan_map: dict[str, EcosystemRepoProfile] = {
        p.repo_full_name: p for p in pre_scan_profiles
    }

    # ── Step 3b: run scanner for each github data_source ─────────────────────
    profile_def = profile.profile
    # v1.6.0-P1.A: simplified profile — stars is gate only, no inactivity signals.
    # Support both old-style (active_definition.min_popularity_floor) and
    # new-style (popularity_floor) for backwards compatibility.
    _old_active_def = profile_def.get("active_definition", {})
    _old_min_floor = _old_active_def.get("min_popularity_floor", {})
    _new_floor = profile_def.get("popularity_floor", {})
    # New style takes precedence; fall back to old style for legacy profiles
    min_stars_by_source: dict = _new_floor if _new_floor else _old_min_floor

    # v1.6.1 Phase 2: read alert threshold from settings (migrated from scan_profile)
    # Fall back to scan_profile.alert_thresholds for backward compat with old profiles.
    _settings_obj = await repo.ensure_ecosystem_project_settings(project_id)
    _profile_alert = profile_def.get("alert_thresholds", {}).get("max_new_per_scan", 50)
    max_new_per_scan = getattr(_settings_obj, "alert_max_new_per_scan", None) or _profile_alert

    # Collect all fresh repo dicts from all github sources (deduplicated by repo_full_name)
    all_fresh: dict[str, dict[str, Any]] = {}  # repo_full_name → repo_dict from scanner

    for ds in active_sources:
        if ds.kind.value != DataSourceKind.GITHUB.value:
            continue  # only github fetcher implemented in P0
        ds_config = ds.config or {}
        raw_queries = ds_config.get("queries", [])
        min_stars_gh = min_stars_by_source.get("github", 1000)

        # Build EcosystemScanner query tuples from the data_source config.
        # raw_queries contains strings like "topic:claude-code" or plain keywords.
        scanner_queries: list[tuple[str, tuple[str, ...]]] = []
        for q in raw_queries:
            if q.startswith("topic:"):
                topic = q[len("topic:"):]
                scanner_queries.append(("", (topic,)))
            else:
                scanner_queries.append((q, ()))

        if not scanner_queries:
            # Fall back to DEFAULT_QUERIES when no queries are configured
            from aiteam.services.ecosystem_scanner import DEFAULT_QUERIES
            scanner_queries = list(DEFAULT_QUERIES)

        if not body.dry_run:
            # Real run: let scanner upsert into ecosystem_repo_profiles
            filter_cfg = FilterConfig(min_stars=min_stars_gh)
            scanner = EcosystemScanner(
                repo=repo,
                gh_search=default_gh_search,
                config=filter_cfg,
                project_id=project_id,
            )
            await scanner.scan(
                strategy=EcosystemScanStrategy.FULL,
                queries=tuple(scanner_queries),
                triggered_by="index_update",
            )

        # Collect fresh repo list via gh_search (for both dry_run and real run).
        # dry_run: this is the ONLY data collection path — scanner.scan is skipped.
        # real run: scanner already upserted; we re-fetch to build all_fresh for diff.
        for keyword, topics in scanner_queries:
            try:
                items = await default_gh_search(keyword, min_stars_gh, list(topics))
                for item in items:
                    fn = item.get("repo_full_name", "")
                    if fn and fn not in all_fresh:
                        all_fresh[fn] = item
            except Exception:
                pass

    # ── Step 4: classify active status (P1.A simplified) ──────────────────────
    # Stars-only gate: repos in all_fresh already passed min_stars filter.
    # Status logic: github_archived=True → archived; manual_status='no_value' → manual_archived
    # Everything else → active (permanent; no rank eviction, no pushed_at stale).
    total = len(all_fresh)
    min_stars_gh = min_stars_by_source.get("github", 5000)

    # repo_full_name → computed_status
    fresh_statuses: dict[str, str] = {}
    for fn, repo_data in all_fresh.items():
        stars = repo_data.get("stars", 0)
        if stars < min_stars_gh:
            # fetcher should have pre-filtered, but guard here
            fresh_statuses[fn] = "not_collected"
            continue
        if repo_data.get("is_archived", False):
            fresh_statuses[fn] = "archived"
        else:
            fresh_statuses[fn] = "active"

    # ── Step 5: diff against DB ───────────────────────────────────────────────
    existing_map = pre_scan_map

    new_repos: list[str] = []
    updated_repos: list[str] = []
    github_archived_changed: list[str] = []
    removed_from_query: list[str] = []  # in DB but not in this scan — preserve, just log
    status_changes: list[EcosystemStatusChange] = []

    for fn, computed_status in fresh_statuses.items():
        if computed_status == "not_collected":
            continue
        existing = existing_map.get(fn)

        if existing is None:
            # Truly new repo
            new_repos.append(fn)
            if not body.dry_run:
                upserted = await repo.get_ecosystem_profile(fn, project_id=project_id)
                if upserted:
                    # Determine effective status: manual_status takes priority
                    if upserted.manual_status == "no_value":
                        effective_status = "manual_archived"
                    elif upserted.manual_status == "pinned":
                        effective_status = "active"
                    else:
                        effective_status = computed_status
                    await repo.update_repo_active_status(upserted.id, new_status=effective_status)
                    status_changes.append(EcosystemStatusChange(
                        repo_id=upserted.id,
                        project_id=project_id,
                        from_status=None,
                        to_status=effective_status,
                        reason="new_repo",
                    ))
        else:
            updated_repos.append(fn)
            prev_status = existing.last_active_status

            # Determine effective status: manual_status takes priority
            if existing.manual_status == "no_value":
                effective_status = "manual_archived"
            elif existing.manual_status == "pinned":
                # Pinned repos stay active regardless of scan result
                effective_status = "active"
            else:
                effective_status = computed_status

            # Track GitHub archived status changes
            if effective_status == "archived" and prev_status != "archived":
                github_archived_changed.append(fn)
            elif effective_status != "archived" and prev_status == "archived":
                github_archived_changed.append(fn)  # unarchived

            if effective_status != prev_status:
                if not body.dry_run:
                    await repo.update_repo_active_status(existing.id, new_status=effective_status)
                    status_changes.append(EcosystemStatusChange(
                        repo_id=existing.id,
                        project_id=project_id,
                        from_status=prev_status,
                        to_status=effective_status,
                        reason="github_archived_change" if "archived" in (effective_status, prev_status or "")
                               else "status_change",
                    ))

    # Repos in DB but absent from this scan → preserve status permanently (P1.A)
    # P1.C-2: pinned repos are excluded from removed_from_query count
    fresh_fn_set = set(fn for fn, s in fresh_statuses.items() if s != "not_collected")
    for fn, existing in existing_map.items():
        if fn not in fresh_fn_set:
            if existing.manual_status == "pinned":
                # Pinned repos never count as removed — they stay active regardless
                pass
            else:
                removed_from_query.append(fn)
            # No status change — repos are permanent once admitted

    # ── Step 6: check alert thresholds ────────────────────────────────────────
    alerted = False
    alert_message = ""
    if len(new_repos) > max_new_per_scan:
        alerted = True
        alert_message = (
            f"Alert: {len(new_repos)} new repos exceeds max_new_per_scan={max_new_per_scan}. "
            "No changes committed. Adjust scan_profile or confirm via dry_run=False with raised threshold."
        )

    if alerted:
        diff = EcosystemIndexDiff(
            project_id=project_id,
            diff_type="incremental",
            new_count=len(new_repos),
            reactivated_count=0,
            deactivated_count=0,
            stale_count=0,
            archived_count=0,
            github_archived_changed_count=len(github_archived_changed),
            removed_from_query_count=len(removed_from_query),
            details_json={
                "new": new_repos[:50],
                "github_archived_changed": github_archived_changed[:50],
                "removed_from_query": removed_from_query[:50],
            },
            markdown_summary=alert_message,
            alerted=True,
        )
        if not body.dry_run:
            await repo.create_index_diff(diff)
        return {
            "success": True,
            "dry_run": body.dry_run,
            "alerted": True,
            "message": alert_message,
            "diff": {
                "new_count": diff.new_count,
                "updated_count": len(updated_repos),
                "github_archived_changed_count": diff.github_archived_changed_count,
                "removed_from_query_count": diff.removed_from_query_count,
            },
        }

    # ── Step 7: persist diff + status changes (dry_run=False only) ────────────
    markdown_summary = _build_diff_markdown_p1(
        new_repos=new_repos,
        github_archived_changed=github_archived_changed,
        removed_from_query=removed_from_query,
        dry_run=body.dry_run,
        total_scanned=total,
        scan_profile_version=profile.version,
    )

    diff = EcosystemIndexDiff(
        project_id=project_id,
        diff_type="initial" if not existing_map else "incremental",
        new_count=len(new_repos),
        reactivated_count=0,
        deactivated_count=0,
        stale_count=0,
        archived_count=0,
        github_archived_changed_count=len(github_archived_changed),
        removed_from_query_count=len(removed_from_query),
        details_json={
            "new": new_repos[:200],
            "github_archived_changed": github_archived_changed[:200],
            "removed_from_query": removed_from_query[:200],
        },
        markdown_summary=markdown_summary,
        alerted=False,
    )

    if not body.dry_run:
        await repo.create_index_diff(diff)
        if status_changes:
            await repo.bulk_create_status_changes(status_changes)
            # v1.6.0: also write status_changed events to ecosystem_repo_events
            status_events = [
                EcosystemRepoEvent(
                    repo_id=sc.repo_id,
                    project_id=sc.project_id,
                    event_type="status_changed",
                    payload_json={"from": sc.from_status, "to": sc.to_status},
                    source="api",
                    from_status=sc.from_status,
                    to_status=sc.to_status,
                    reason=sc.reason or None,
                )
                for sc in status_changes
            ]
            if status_events:
                await repo.bulk_create_repo_events(status_events)

    diff_summary = (
        f"{diff.new_count} new, {len(updated_repos)} updated, "
        f"{diff.github_archived_changed_count} archive changes"
    )
    return {
        "success": True,
        "dry_run": body.dry_run,
        "alerted": False,
        "scan_profile_version": profile.version,
        "total_scanned": total,
        "diff": {
            "id": diff.id,
            "new_count": diff.new_count,
            "updated_count": len(updated_repos),
            "github_archived_changed_count": diff.github_archived_changed_count,
            "removed_from_query_count": diff.removed_from_query_count,
            "markdown_summary": diff.markdown_summary,
            "summary": diff_summary,
        },
        "message": (
            "Dry-run complete — no changes written." if body.dry_run
            else f"Index updated: {diff.new_count} new, {len(updated_repos)} updated metadata, "
                 f"{diff.archived_count} github_archived changes."
        ),
    }


def _build_diff_markdown_p1(
    new_repos: list[str],
    github_archived_changed: list[str],
    removed_from_query: list[str],
    dry_run: bool,
    total_scanned: int,
    scan_profile_version: int,
) -> str:
    """Build human-readable markdown summary for P1.A simplified diff."""
    mode = "Dry-run preview" if dry_run else "Index update"
    lines = [
        f"## {mode} — scan_profile v{scan_profile_version}",
        "",
        f"Total repos in fresh scan: **{total_scanned}**",
        "",
        "| Category | Count |",
        "|----------|-------|",
        f"| New | {len(new_repos)} |",
        f"| GitHub archived status changed | {len(github_archived_changed)} |",
        f"| Absent from this query (preserved) | {len(removed_from_query)} |",
    ]
    if new_repos:
        lines += ["", "### New repos (first 10)"]
        for fn in new_repos[:10]:
            lines.append(f"- {fn}")
    if github_archived_changed:
        lines += ["", "### GitHub archived status changed (first 10)"]
        for fn in github_archived_changed[:10]:
            lines.append(f"- {fn}")
    return "\n".join(lines)


@router.get("/index_diffs/latest")
async def get_latest_index_diff(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return the most recent index diff for the current project."""
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    diff = await repo.get_latest_index_diff(project_id)
    if diff is None:
        return {"success": True, "diff": None, "message": "No index diffs found yet."}

    return {
        "success": True,
        "diff": {
            "id": diff.id,
            "diff_type": diff.diff_type,
            "new_count": diff.new_count,
            "reactivated_count": diff.reactivated_count,
            "deactivated_count": diff.deactivated_count,
            "stale_count": diff.stale_count,
            "archived_count": diff.archived_count,
            # new fields (P1 hotfix): read new columns, fallback to old for pre-hotfix rows
            "github_archived_changed_count": diff.github_archived_changed_count or diff.archived_count,
            "removed_from_query_count": diff.removed_from_query_count,
            "markdown_summary": diff.markdown_summary,
            "alerted": diff.alerted,
            "generated_at": diff.generated_at.isoformat(),
        },
    }


@router.get("/index_diffs/history")
async def get_index_diff_history(
    limit: int = Query(default=10, ge=1, le=20),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return recent index diffs for the current project (P1.B: details truncated to first 5).

    Full details_json available in /index_diffs/latest.
    """
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    diffs = await repo.list_index_diffs(project_id, limit=limit)

    def _truncate_details(details: dict | None) -> dict:
        if not details:
            return {}
        return {k: v[:5] if isinstance(v, list) else v for k, v in details.items()}

    return {
        "success": True,
        "diffs": [
            {
                "id": d.id,
                "diff_type": d.diff_type,
                "new_count": d.new_count,
                "reactivated_count": d.reactivated_count,
                "deactivated_count": d.deactivated_count,
                "stale_count": d.stale_count,
                "archived_count": d.archived_count,
                "alerted": d.alerted,
                "generated_at": d.generated_at.isoformat(),
                # P1.B: only first 5 items per category to prevent token explosion
                "details_summary": _truncate_details(d.details_json),
            }
            for d in diffs
        ],
        "total": len(diffs),
    }


@router.get("/repos/{repo_id}/events")
async def get_repo_events(
    repo_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return event history for a single repo, newest first (v1.6.0 event sourcing).

    Returns:
        {success: True, repo_id, events: [{id, event_type, payload_json, source, triggered_at, ...}], total}
    """
    events = await repo.list_repo_events(repo_id, limit=limit)
    return {
        "success": True,
        "repo_id": repo_id,
        "events": [
            {
                "id": ev.id,
                "event_type": ev.event_type,
                "payload_json": ev.payload_json,
                "source": ev.source,
                "scan_run_id": ev.scan_run_id,
                "from_status": ev.from_status,
                "to_status": ev.to_status,
                "reason": ev.reason,
                "triggered_at": ev.triggered_at.isoformat(),
            }
            for ev in events
        ],
        "total": len(events),
    }


def _event_to_summary(ev: EcosystemRepoEvent) -> str:
    """Translate event_type + payload into a single human-readable sentence."""
    t = ev.event_type
    p = ev.payload_json or {}
    if t == "discovered":
        return "首次入档浅扫"
    if t == "rescanned":
        return "浅扫无变化"
    if t == "topics_changed":
        before = p.get("before", [])
        after = p.get("after", [])
        return f"topics 变化: {len(before)} → {len(after)}"
    if t == "stars_jumped":
        before = p.get("before", p.get("old_stars", "?"))
        after = p.get("after", p.get("new_stars", "?"))
        pct = p.get("pct", "")
        pct_str = f" (+{pct}%)" if pct else ""
        return f"stars: {before} → {after}{pct_str}"
    if t == "status_changed":
        from_s = p.get("from", ev.from_status or "?")
        to_s = p.get("to", ev.to_status or "?")
        return f"{from_s} → {to_s}"
    if t == "manual_pinned":
        return "人工锁定"
    if t == "manual_unpinned":
        return "取消人工锁定"
    if t == "archived":
        return "已归档"
    if t == "removed_from_query":
        return "已从查询结果中移除"
    return t


@router.get("/repos/{repo_id}/scan_history")
async def get_repo_scan_history(
    repo_id: str,
    limit: int = Query(default=50, le=200),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """合并 events + deep_reviews 按时间倒序，返回扫描研究历程 timeline。

    Returns:
        {
          success, repo_id, total,
          entries: [
            {kind: 'event'|'deep_review', timestamp, type, summary, payload, expandable_md}
          ]
        }
    """
    events = await repo.list_repo_events(repo_id, limit=limit * 2)
    deep_reviews = await repo.list_deep_reviews(repo_id=repo_id, limit=limit * 2)

    entries: list[dict[str, Any]] = []

    for ev in events:
        entries.append({
            "kind": "event",
            "type": ev.event_type,
            "timestamp": ev.triggered_at.isoformat(),
            "source": ev.source,
            "scan_run_id": ev.scan_run_id,
            "payload": ev.payload_json,
            "summary": _event_to_summary(ev),
        })

    for dr in deep_reviews:
        # v1.6.0 修：跳过 v1.5 stage tracking 空占位 row（无任何实际内容仅记 stage='shallow_done'）
        # 这些 row 是 v1.5 浅扫完成时自动创建的 stage 标记，非真深扫。265 老仓都有这种 row。
        has_real_content = bool(
            (dr.summary_md or "").strip()
            or (dr.architecture_md or "").strip()
            or (dr.risks_md or "").strip()
            or (dr.learnings_md or "").strip()
            or (dr.integration_md or "").strip()
        )
        if not has_real_content:
            continue

        ts = (dr.completed_at or dr.created_at)
        stage_val = (
            dr.stage_status.value if hasattr(dr.stage_status, "value") else (dr.stage_status or "unknown")
        )
        # v1.6.0 修: 根据 stage 区分浅扫/深扫语义，不再都叫"深扫"
        # shallow_done 阶段（仅 summary_md 有内容）→ 浅扫记录
        # architecture_done 及之后阶段（有 architecture_md 等）→ 深扫记录
        is_deep = bool((dr.architecture_md or "").strip())
        kind_label = "深扫" if is_deep else "浅扫"
        entry_kind = "deep_review" if is_deep else "shallow_scan"

        entries.append({
            "kind": entry_kind,
            "type": f"{entry_kind}_{stage_val}",
            "timestamp": ts.isoformat() if ts else dr.created_at.isoformat(),
            "stage_status": stage_val,
            "review_id": dr.id,
            "summary": f"{kind_label} ({stage_val})",
            "expandable_md": {
                "summary": dr.summary_md or "",
                "architecture": dr.architecture_md or "",
                "risks": dr.risks_md or "",
                "learnings": dr.learnings_md or "",
                "integration": dr.integration_md or "",
            },
            "integration_recommendation": (
                dr.integration_recommendation.value
                if hasattr(dr.integration_recommendation, "value")
                else dr.integration_recommendation
            ),
        })

    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    trimmed = entries[:limit]

    return {
        "success": True,
        "repo_id": repo_id,
        "total": len(trimmed),
        "entries": trimmed,
    }


@router.get("/diff")
async def get_diff_period(
    from_date: str = Query(..., alias="from", description="ISO date YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="ISO date YYYY-MM-DD"),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return a time-period diff computed dynamically from the events table (v1.6.0 event sourcing).

    Groups events by event_type to produce new / topics_changed / stars_jumped / status_changed counts.

    Returns:
        {success, from, to, summary: {new, topics_changed, stars_jumped, status_changed, ...},
         events_count, events_by_type: {event_type: count}}
    """
    from datetime import datetime

    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    try:
        from_dt = datetime.fromisoformat(from_date).replace(tzinfo=UTC)
        to_dt = datetime.fromisoformat(to_date).replace(hour=23, minute=59, second=59, tzinfo=UTC)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")

    if from_dt > to_dt:
        raise HTTPException(status_code=400, detail="from must be <= to")

    events = await repo.query_events_in_period(project_id, from_dt, to_dt)

    by_type: dict[str, int] = {}
    for ev in events:
        by_type[ev.event_type] = by_type.get(ev.event_type, 0) + 1

    return {
        "success": True,
        "from": from_date,
        "to": to_date,
        "summary": {
            "new": by_type.get("discovered", 0),
            "topics_changed": by_type.get("topics_changed", 0),
            "stars_jumped": by_type.get("stars_jumped", 0),
            "status_changed": by_type.get("status_changed", 0),
            "archived": by_type.get("archived", 0),
            "manual_pinned": by_type.get("manual_pinned", 0),
            "manual_unpinned": by_type.get("manual_unpinned", 0),
        },
        "events_count": len(events),
        "events_by_type": by_type,
    }


@router.get("/queries_recap")
async def get_queries_recap(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """Return a recap of all search queries that have discovered repos in this project.

    Scans discovered_via_queries across all profiles and aggregates counts.

    Returns:
        {queries: list[str], by_query: {query: count}, total_repos: N}
    """

    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    profiles, total_repos = await repo.search_ecosystem_profiles_extended(
        limit=5000,
        offset=0,
    )

    by_query: dict[str, int] = {}
    for p in profiles:
        for q in p.discovered_via_queries:
            by_query[q] = by_query.get(q, 0) + 1

    queries_sorted = sorted(by_query.keys(), key=lambda q: by_query[q], reverse=True)

    return {
        "success": True,
        "queries": queries_sorted,
        "by_query": by_query,
        "total_repos": total_repos,
        "repos_with_query_data": sum(1 for p in profiles if p.discovered_via_queries),
    }


@router.get("/repos/pinned")
async def get_pinned_repos(
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict[str, Any]:
    """List all repos with manual_status='pinned' for the current project.

    Pinned repos are permanently active regardless of scan results.
    """
    project_id = repo._project_scope
    if not project_id:
        raise HTTPException(status_code=400, detail="X-Project-Id header required")

    profiles, total = await repo.list_pinned_repos()

    return {
        "success": True,
        "pinned_repos": [_profile_to_list_dict(p) for p in profiles],
        "total": total,
    }
