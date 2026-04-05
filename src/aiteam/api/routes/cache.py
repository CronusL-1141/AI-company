"""AI Team OS — Semantic cache management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from aiteam.api.schemas import APIResponse
from aiteam.api.semantic_cache import cache_clear, cache_stats

router = APIRouter(prefix="/api/cache", tags=["cache"])


class CacheStatsResponse(BaseModel):
    hits: int
    misses: int
    total_requests: int
    hit_rate: float
    active_entries: int
    total_entries: int
    bm25_enabled: bool


@router.get("/stats", response_model=APIResponse[CacheStatsResponse])
async def get_cache_stats() -> APIResponse[Any]:
    """Get semantic cache statistics — hit rate, entry count, BM25 status."""
    stats = cache_stats()
    return APIResponse(data=stats)


@router.post("/clear", response_model=APIResponse[dict])
async def clear_cache() -> APIResponse[Any]:
    """Clear all semantic cache entries."""
    removed = cache_clear()
    return APIResponse(data={"removed": removed}, message=f"Cleared {removed} cache entries")
