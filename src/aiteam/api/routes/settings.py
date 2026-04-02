"""AI Team OS — Settings routes (wake config, etc.)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Store wake config in a simple JSON file alongside the database
_CONFIG_PATH = Path.home() / ".claude" / "data" / "ai-team-os" / "wake_config.json"

_DEFAULT_WAKE_CONFIG = {
    "interval": "30m",
    "prompt_template": "你好，请检查当前项目状态，查看任务墙上是否有待处理的任务，并继续推进工作。",
    "autonomy_level": "consult",
}


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(_DEFAULT_WAKE_CONFIG)


def _save_config(config: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


class WakeConfig(BaseModel):
    interval: str  # "10m" | "30m" | "1h" | "off"
    prompt_template: str
    autonomy_level: str  # "full" | "consult" | "readonly"


@router.get("/wake-config")
async def get_wake_config() -> dict:
    """Get current wake schedule configuration."""
    return _load_config()


@router.put("/wake-config")
async def put_wake_config(body: WakeConfig) -> dict:
    """Update wake schedule configuration."""
    config = body.model_dump()
    _save_config(config)
    return {"ok": True, "config": config}
