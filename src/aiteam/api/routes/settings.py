"""AI Team OS — Settings routes (wake config, webhook config, etc.)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Body
from pydantic import BaseModel

import aiteam.config.settings as cfg_module
from aiteam.api.language import resolve_language

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Store wake config in a simple JSON file alongside the database
_CONFIG_PATH = Path.home() / ".claude" / "data" / "ai-team-os" / "wake_config.json"
_CONFIG_LOCK = threading.RLock()

_DEFAULT_WAKE_CONFIG = {
    "interval": "30m",
    "prompt_template": "你好，请检查当前项目状态，查看任务墙上是否有待处理的任务，并继续推进工作。",
    "autonomy_level": "consult",
}


def _load_config() -> dict:
    with _CONFIG_LOCK:
        try:
            config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(config, dict):
                return config
        except (OSError, UnicodeError, ValueError):
            pass
    return dict(_DEFAULT_WAKE_CONFIG)


def _save_config(config: dict) -> None:
    with _CONFIG_LOCK:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=_CONFIG_PATH.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(config, ensure_ascii=False, indent=2))
            os.replace(temporary, _CONFIG_PATH)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _update_config(values: dict) -> None:
    # Wake settings and language share this file: serialize their read/modify/write.
    with _CONFIG_LOCK:
        config = _load_config()
        config.update(values)
        _save_config(config)


@router.get("/language")
async def get_language(
    cwd: str | None = None,
    host: Literal["cc", "codex", "system"] = "system",
    fallback_language: str | None = None,
) -> dict:
    """Read the shared language mode; an unscoped Dashboard follows the system."""
    return await resolve_language(cwd=cwd, host=host, fallback_language=fallback_language)


@router.put("/language")
async def put_language(
    mode: Annotated[Literal["follow", "zh", "en"], Body(embed=True)],
    cwd: str | None = None,
    host: Literal["cc", "codex", "system"] = "system",
    fallback_language: str | None = None,
) -> dict:
    """Persist a global override, or restore host/system preference with follow."""
    await asyncio.to_thread(_update_config, {"language_mode": mode})
    return await resolve_language(cwd=cwd, host=host, fallback_language=fallback_language)


class WakeConfig(BaseModel):
    interval: str  # "10m" | "30m" | "1h" | "off"
    prompt_template: str
    autonomy_level: str  # "full" | "consult" | "readonly"


@router.get("/wake-config")
async def get_wake_config() -> dict:
    """Get current wake schedule configuration."""
    return await asyncio.to_thread(_load_config)


@router.put("/wake-config")
async def put_wake_config(body: WakeConfig) -> dict:
    """Update wake schedule configuration."""
    config = body.model_dump()
    await asyncio.to_thread(_update_config, config)
    return {"ok": True, "config": config}


# ============================================================
# Webhook / Slack notification config
# ============================================================

_WEBHOOK_CONFIG_PATH = Path.home() / ".claude" / "data" / "ai-team-os" / "webhook_config.json"

_DEFAULT_WEBHOOK_CONFIG: dict = {
    "slack_webhook_url": "",
    "notification_events": ["task.completed", "task.failed", "briefing.added"],
}


def _load_webhook_config() -> dict:
    if _WEBHOOK_CONFIG_PATH.exists():
        try:
            return json.loads(_WEBHOOK_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(_DEFAULT_WEBHOOK_CONFIG)


def _save_webhook_config(config: dict) -> None:
    _WEBHOOK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WEBHOOK_CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Sync into the live settings module so EventBus picks up changes without restart
    cfg_module.SLACK_WEBHOOK_URL = config.get("slack_webhook_url", "")
    cfg_module.NOTIFICATION_EVENTS = config.get(
        "notification_events", list(_DEFAULT_WEBHOOK_CONFIG["notification_events"])
    )


class WebhookConfig(BaseModel):
    slack_webhook_url: str
    notification_events: list[str] = ["task.completed", "task.failed", "briefing.added"]


@router.get("/webhook")
async def get_webhook_config() -> dict:
    """Get current Slack/webhook notification configuration."""
    config = _load_webhook_config()
    # Mask URL for display — only show if non-empty
    display = dict(config)
    if display.get("slack_webhook_url"):
        display["slack_webhook_url_configured"] = True
    else:
        display["slack_webhook_url_configured"] = False
    return display


@router.put("/webhook")
async def put_webhook_config(body: WebhookConfig) -> dict:
    """Update Slack/webhook notification configuration.

    Set slack_webhook_url to an empty string to disable notifications.
    """
    config = body.model_dump()
    _save_webhook_config(config)
    return {"ok": True, "notification_events": config["notification_events"]}


@router.delete("/webhook")
async def delete_webhook_config() -> dict:
    """Disable Slack/webhook notifications by clearing the webhook URL."""
    config = _load_webhook_config()
    config["slack_webhook_url"] = ""
    _save_webhook_config(config)
    return {"ok": True, "message": "Webhook URL cleared — notifications disabled."}


class SendNotificationRequest(BaseModel):
    message: str
    urgency: str = "medium"


@router.post("/webhook/send")
async def post_send_notification(body: SendNotificationRequest) -> dict:
    """Manually send a notification message to the configured Slack webhook.

    Returns an error if no webhook URL is configured.
    """
    from aiteam.integrations.notifier import send_webhook

    url = cfg_module.SLACK_WEBHOOK_URL
    if not url:
        # Try loading persisted config in case module default was never overridden
        persisted = _load_webhook_config()
        url = persisted.get("slack_webhook_url", "")
        if url:
            cfg_module.SLACK_WEBHOOK_URL = url

    if not url:
        return {
            "ok": False,
            "error": "No webhook URL configured. Use PUT /api/settings/webhook first.",
        }

    ok = await send_webhook(url, body.message, metadata={"urgency": body.urgency})
    return {"ok": ok, "message": body.message if ok else "Delivery failed — check server logs."}
