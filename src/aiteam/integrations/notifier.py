"""Notification service — sends alerts to Slack/webhook on key events."""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

# Urgency level to Slack color mapping
_URGENCY_COLORS: dict[str, str] = {
    "low": "#36a64f",
    "medium": "#ffa500",
    "high": "#ff0000",
}


async def send_webhook(url: str, message: str, metadata: dict | None = None) -> bool:
    """Send a Slack-compatible webhook message.

    Args:
        url: Slack incoming webhook URL.
        message: Plain-text fallback message.
        metadata: Optional extra fields (urgency, event_type, source, data).

    Returns:
        True on success, False on failure.
    """
    payload: dict = {"text": message}

    if metadata:
        urgency = metadata.get("urgency", "medium")
        color = _URGENCY_COLORS.get(urgency, _URGENCY_COLORS["medium"])
        fields = []
        if metadata.get("event_type"):
            fields.append({"title": "Event", "value": metadata["event_type"], "short": True})
        if metadata.get("source"):
            fields.append({"title": "Source", "value": metadata["source"], "short": True})
        if metadata.get("urgency"):
            fields.append({"title": "Urgency", "value": urgency, "short": True})
        attachment: dict = {
            "color": color,
            "text": message,
            "fields": fields,
            "mrkdwn_in": ["text"],
        }
        payload["attachments"] = [attachment]
        # text at top level is the fallback for clients that don't render attachments
        payload["text"] = ""

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True
    except Exception as exc:
        logger.warning("Webhook delivery failed: %s", exc)
        return False
