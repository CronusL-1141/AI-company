"""Request-scoped channel waiting; no daemon, acknowledgement, or model polling."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from aiteam.clock import ensure_utc

_CHANNEL = re.compile(r"(?:team:[a-zA-Z0-9_\-]+|project:[a-zA-Z0-9_\-]+|global)\Z")
_ROLE = re.compile(r"[a-zA-Z0-9_\-]{1,100}\Z")


async def wait_for_channel(
    *,
    api_url: str,
    channel: str,
    reader: str,
    sender: str,
    project_id: str,
    since: str = "",
    cursor: str = "",
    timeout_seconds: float = 45,
    limit: int = 50,
    io_timeout_seconds: float = 10,
) -> dict[str, Any]:
    """Subscribe before replay, then return the first persisted matching page.

    Errors never advance the caller's cursor. A timeout gets a final database
    read so a missed broadcast does not silently hide a committed message.
    Cancellation propagates through both client context managers.
    """
    from aiteam.types import ChannelInboxPage

    try:
        if not _CHANNEL.fullmatch(channel):
            raise ValueError("Invalid channel")
        if not _ROLE.fullmatch(reader) or not _ROLE.fullmatch(sender) or reader == sender:
            raise ValueError("reader and sender must be distinct role identifiers")
        if not project_id.strip():
            raise ValueError("project_id is required")
        if not 0 < timeout_seconds <= 300 or not 1 <= limit <= 200:
            raise ValueError("timeout_seconds must be in (0, 300]; limit must be in [1, 200]")
        if not 0 < io_timeout_seconds <= 60:
            raise ValueError("io_timeout_seconds must be in (0, 60]")
        if not since and not cursor:
            raise ValueError("An initial since timestamp or a continuation cursor is required")
        position = (
            ensure_utc(datetime.fromisoformat(since.replace("Z", "+00:00")))
            if not cursor else None
        )
        parsed = urlsplit(api_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("api_url must be an HTTP(S) base URL without query or fragment")
    except (ValueError, TypeError) as exc:
        return {"success": False, "error": str(exc), "_error_category": "invalid_input"}

    params = {
        "project_id": project_id,
        "reader": reader,
        "sender": sender,
        "cursor": cursor,
        "limit": limit,
    }
    if position is not None:
        params["since"] = position.isoformat()
    ws_url = urlunsplit((
        "wss" if parsed.scheme == "https" else "ws",
        parsed.netloc, parsed.path + "/ws/events", "", "",
    ))
    inbox_url = api_url.rstrip("/") + f"/api/channels/{quote(channel, safe='')}/inbox"

    async def read_page(client: httpx.AsyncClient) -> ChannelInboxPage:
        async with asyncio.timeout(io_timeout_seconds):
            response = await client.get(inbox_url, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ValueError("Inbox API did not return a successful response")
        page = ChannelInboxPage.model_validate(payload["data"])
        if len(page.messages) > limit or (page.has_more and not page.messages) or not page.next_cursor:
            raise ValueError("Inbox API returned an invalid page or cursor")
        # Validate persisted results too; never trust a WS frame's missing project.
        for message in page.messages:
            if (
                message.project_id != project_id or message.channel != channel
                or message.sender != sender
                or not {reader, "@" + reader}.intersection(message.mentions)
            ):
                raise ValueError("Inbox API returned a message outside the requested scope")
        return page

    def result(
        page: ChannelInboxPage, delivery_source: Literal["replay", "event", "timeout_read"],
    ) -> dict[str, Any]:
        return {
            "success": True,
            "data": {
                "status": "messages" if page.messages else "timeout",
                "delivery_source": delivery_source,
                **page.model_dump(mode="json"),
            },
        }

    try:
        async with httpx.AsyncClient(timeout=io_timeout_seconds, trust_env=False) as client:
            async with connect(ws_url, open_timeout=io_timeout_seconds, close_timeout=2, proxy=None) as ws:
                await ws.send(json.dumps({"type": "subscribe", "channel": "channel.message"}))
                async with asyncio.timeout(io_timeout_seconds):
                    while True:
                        ack = json.loads(await ws.recv())
                        if ack.get("type") == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                            continue
                        if ack != {"type": "ack", "action": "subscribe", "detail": "channel.message"}:
                            raise ValueError("Unexpected channel subscription response")
                        break
                page = await read_page(client)
                if page.messages:
                    return result(page, "replay")
                params["cursor"] = page.next_cursor
                params.pop("since", None)
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return result(await read_page(client), "timeout_read")
                    try:
                        event = json.loads(await asyncio.wait_for(ws.recv(), remaining))
                    except TimeoutError:
                        return result(await read_page(client), "timeout_read")
                    if event.get("type") == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                        continue
                    data = event.get("data", {})
                    if (
                        event.get("event_type") != "channel.message" or not isinstance(data, dict)
                        or data.get("channel") != channel or data.get("sender") != sender
                        or not isinstance(data.get("mentions"), list)
                        or not any(name in data["mentions"] for name in (reader, "@" + reader))
                    ):
                        continue
                    page = await read_page(client)
                    if page.messages:
                        return result(page, "event")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            return {
                "success": False, "error": "Channel cursor anchor is no longer available",
                "_error_category": "channel_wait_cursor_expired",
                "hint": "Explicitly replay from a prior since boundary and deduplicate by ID; do not reset to now.",
            }
        return {
            "success": False, "error": f"Inbox API returned HTTP {exc.response.status_code}",
            "_error_category": "channel_wait_api_error",
            "resume_cursor": params["cursor"],
        }
    except (OSError, TimeoutError, httpx.RequestError, WebSocketException) as exc:
        return {
            "success": False, "error": f"Channel wait transport failed: {type(exc).__name__}",
            "_error_category": "channel_wait_transport_error",
            "resume_cursor": params["cursor"],
            "hint": "Retry with resume_cursor if present; no messages were acknowledged by this wait.",
        }
    except (ValueError, KeyError, AttributeError, ValidationError) as exc:
        return {
            "success": False, "error": f"Invalid channel response: {type(exc).__name__}",
            "_error_category": "channel_wait_protocol_error",
            "resume_cursor": params["cursor"],
        }
