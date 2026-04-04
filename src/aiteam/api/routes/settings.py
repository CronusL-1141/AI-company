"""AI Team OS — Settings routes (wake config, webhook config, etc.)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

import aiteam.config.settings as cfg_module

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


# ============================================================
# Budget configuration
# ============================================================

_BUDGET_CONFIG_PATH = Path.home() / ".claude" / "data" / "ai-team-os" / "budget_config.json"

_DEFAULT_BUDGET_CONFIG: dict = {
    "weekly_usd": 50.0,
    "alert_threshold": 0.8,
}


def _load_budget_config() -> dict:
    if _BUDGET_CONFIG_PATH.exists():
        try:
            return json.loads(_BUDGET_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(_DEFAULT_BUDGET_CONFIG)


def _save_budget_config(config: dict) -> None:
    _BUDGET_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _BUDGET_CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Sync into live settings module so budget checks pick up changes without restart
    cfg_module.COST_BUDGET_WEEKLY_USD = config.get("weekly_usd", 50.0)
    cfg_module.COST_ALERT_THRESHOLD = config.get("alert_threshold", 0.8)


class BudgetConfig(BaseModel):
    weekly_usd: float = 50.0
    alert_threshold: float = 0.8  # 0.0–1.0 fraction


@router.get("/budget")
async def get_budget_config() -> dict:
    """Get current cost budget configuration."""
    return _load_budget_config()


@router.put("/budget")
async def put_budget_config(body: BudgetConfig) -> dict:
    """Update cost budget configuration.

    Sets the weekly USD spend limit and the alert threshold fraction
    (e.g. 0.8 = alert at 80% utilization).
    """
    config = body.model_dump()
    _save_budget_config(config)
    return {"ok": True, "config": config}


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
