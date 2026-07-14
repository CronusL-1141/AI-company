"""模型治理路由 — 可用模型自动拉取 + 默认启动模型读写。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from aiteam.api import model_discovery
from aiteam.api.deps import get_scoped_repository
from aiteam.storage.repository import StorageRepository

router = APIRouter(prefix="/api/models", tags=["model-governance"])


class DefaultModelBody(BaseModel):
    model: str = ""  # 空串 = 移除键，恢复 CC 自身默认


@router.get("/available")
async def available_models(force: bool = Query(False)) -> dict:
    """可用模型清单（文件真相源：本机 transcript 实际出现过的模型）。"""
    return {"success": True, "data": model_discovery.scan_available_models(force=force)}


@router.get("/usage")
async def model_usage(
    days: int = Query(7, ge=1, le=90),
    repo: StorageRepository = Depends(get_scoped_repository),
) -> dict:
    """近 N 天各模型档位的 workflow agent 用量聚合（编排宪章观测口径）。"""
    usage = await repo.aggregate_model_usage(days)
    return {"success": True, "data": {"days": days, "usage": usage}}


@router.get("/default")
async def get_default_model() -> dict:
    """当前默认启动模型（~/.claude/settings.json 的 model 键）。"""
    return {"success": True, "data": {"model": model_discovery.read_default_model()}}


@router.put("/default")
async def put_default_model(body: DefaultModelBody) -> dict:
    """设置默认启动模型（写 settings.json，新开 CC 会话生效）。"""
    result = model_discovery.set_default_model(body.model)
    return {"success": bool(result.get("ok")), "data": result}
