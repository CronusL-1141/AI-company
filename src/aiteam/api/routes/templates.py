"""AI Team OS — 团队模板路由（只读）."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/config", tags=["team-templates"])

# 模板配置文件路径
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "plugin" / "config"
TEMPLATES_FILE = CONFIG_DIR / "team-templates.json"


def _read_templates() -> list[dict[str, Any]]:
    """读取模板配置文件."""
    if not TEMPLATES_FILE.exists():
        return []
    text = TEMPLATES_FILE.read_text(encoding="utf-8")
    data = json.loads(text)
    return data.get("templates", [])


@router.get("/team-templates")
async def list_templates() -> dict[str, Any]:
    """列出所有团队模板."""
    templates = _read_templates()
    return {"success": True, "data": templates}


@router.get("/team-templates/{template_id}")
async def get_template(template_id: str) -> dict[str, Any]:
    """获取单个模板详情."""
    templates = _read_templates()
    for t in templates:
        if t["id"] == template_id:
            return {"success": True, "data": t}
    raise HTTPException(status_code=404, detail=f"模板 '{template_id}' 不存在")
