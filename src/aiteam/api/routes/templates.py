"""AI Team OS — Team template routes (read-only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/config", tags=["team-templates"])

# Template configuration file path
CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "plugin" / "config"
TEMPLATES_FILE = CONFIG_DIR / "team-templates.json"


def _read_templates() -> list[dict[str, Any]]:
    """Read template configuration file."""
    if not TEMPLATES_FILE.exists():
        return []
    text = TEMPLATES_FILE.read_text(encoding="utf-8")
    data = json.loads(text)
    return data.get("templates", [])


@router.get("/team-templates")
async def list_templates() -> dict[str, Any]:
    """List all team templates."""
    templates = _read_templates()
    return {"success": True, "data": templates}


@router.get("/team-templates/{template_id}")
async def get_template(template_id: str) -> dict[str, Any]:
    """Get single template details."""
    templates = _read_templates()
    for t in templates:
        if t["id"] == template_id:
            return {"success": True, "data": t}
    raise HTTPException(status_code=404, detail=f"模板 '{template_id}' 不存在")
