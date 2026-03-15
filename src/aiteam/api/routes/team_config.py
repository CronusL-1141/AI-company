"""AI Team OS — 常驻成员配置路由."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/config", tags=["team-config"])

# 配置文件路径
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "plugin" / "config"
CONFIG_FILE = CONFIG_DIR / "team-defaults.json"

# 默认配置
_DEFAULT_CONFIG: dict[str, Any] = {
    "auto_create_team": True,
    "team_name_prefix": "auto",
    "permanent_members": [],
}


# ============================================================
# 请求模型
# ============================================================


class PermanentMember(BaseModel):
    """常驻成员定义."""

    name: str
    role: str
    model: str = "claude-sonnet-4-6"
    enabled: bool = True


class TeamDefaultsConfig(BaseModel):
    """常驻成员配置."""

    auto_create_team: bool = True
    team_name_prefix: str = "auto"
    permanent_members: list[PermanentMember] = Field(default_factory=list)


class MemberUpdate(BaseModel):
    """更新常驻成员请求（部分更新）."""

    role: str | None = None
    model: str | None = None
    enabled: bool | None = None


# ============================================================
# 辅助函数
# ============================================================


def _read_config() -> dict[str, Any]:
    """读取配置文件."""
    if not CONFIG_FILE.exists():
        return dict(_DEFAULT_CONFIG)
    text = CONFIG_FILE.read_text(encoding="utf-8")
    return json.loads(text)


def _write_config(data: dict[str, Any]) -> None:
    """写入配置文件."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ============================================================
# 路由
# ============================================================


@router.get("/team-defaults")
async def get_team_defaults() -> dict[str, Any]:
    """读取当前常驻成员配置."""
    config = _read_config()
    return {"success": True, "data": config}


@router.put("/team-defaults")
async def update_team_defaults(body: TeamDefaultsConfig) -> dict[str, Any]:
    """更新配置（整体替换）."""
    data = body.model_dump()
    _write_config(data)
    return {"success": True, "data": data, "message": "常驻成员配置已更新"}


@router.post("/team-defaults/members", status_code=201)
async def add_member(body: PermanentMember) -> dict[str, Any]:
    """添加一个常驻成员."""
    config = _read_config()
    members: list[dict[str, Any]] = config.get("permanent_members", [])

    # 检查名称是否已存在
    for m in members:
        if m["name"] == body.name:
            raise HTTPException(status_code=409, detail=f"成员 '{body.name}' 已存在")

    members.append(body.model_dump())
    config["permanent_members"] = members
    _write_config(config)
    return {"success": True, "data": body.model_dump(), "message": f"常驻成员 '{body.name}' 已添加"}


@router.delete("/team-defaults/members/{name}")
async def remove_member(name: str) -> dict[str, Any]:
    """删除一个常驻成员."""
    config = _read_config()
    members: list[dict[str, Any]] = config.get("permanent_members", [])

    original_len = len(members)
    members = [m for m in members if m["name"] != name]

    if len(members) == original_len:
        raise HTTPException(status_code=404, detail=f"成员 '{name}' 不存在")

    config["permanent_members"] = members
    _write_config(config)
    return {"success": True, "message": f"常驻成员 '{name}' 已删除"}


@router.patch("/team-defaults/members/{name}")
async def update_member(name: str, body: MemberUpdate) -> dict[str, Any]:
    """更新常驻成员（如启用/禁用、改model）."""
    config = _read_config()
    members: list[dict[str, Any]] = config.get("permanent_members", [])

    target = None
    for m in members:
        if m["name"] == name:
            target = m
            break

    if target is None:
        raise HTTPException(status_code=404, detail=f"成员 '{name}' 不存在")

    updates = body.model_dump(exclude_none=True)
    target.update(updates)
    _write_config(config)
    return {"success": True, "data": target, "message": f"常驻成员 '{name}' 已更新"}
