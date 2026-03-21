"""AI Team OS — WebSocket endpoints."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from aiteam.api.ws.manager import ws_manager
from aiteam.api.ws.protocol import WSAck, WSError

router = APIRouter(tags=["websocket"])

HEARTBEAT_INTERVAL = 30  # seconds


@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    """Main event stream WebSocket endpoint.

    Supported client messages:
    - {"type": "subscribe", "channel": "team.*"}
    - {"type": "unsubscribe", "channel": "team.*"}
    - {"type": "pong"}  (heartbeat response)
    """
    conn_id = str(uuid4())
    await ws_manager.connect(conn_id, websocket)

    # Start heartbeat task
    heartbeat_task = asyncio.create_task(_heartbeat(conn_id, websocket))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(
                    WSError(message="无效的JSON格式").model_dump_json()
                )
                continue

            msg_type = msg.get("type", "")

            if msg_type == "subscribe":
                channel = msg.get("channel", "")
                if channel:
                    ws_manager.subscribe(conn_id, channel)
                    await websocket.send_text(
                        WSAck(action="subscribe", detail=channel).model_dump_json()
                    )

            elif msg_type == "unsubscribe":
                channel = msg.get("channel", "")
                if channel:
                    ws_manager.unsubscribe(conn_id, channel)
                    await websocket.send_text(
                        WSAck(action="unsubscribe", detail=channel).model_dump_json()
                    )

            elif msg_type == "pong":
                # Client heartbeat response, no processing needed
                pass

            else:
                await websocket.send_text(
                    WSError(message=f"未知消息类型: {msg_type}").model_dump_json()
                )

    except WebSocketDisconnect:
        pass
    finally:
        heartbeat_task.cancel()
        ws_manager.disconnect(conn_id)


async def _heartbeat(conn_id: str, websocket: WebSocket) -> None:
    """Periodically send heartbeat ping."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await websocket.send_text(json.dumps({"type": "ping"}))
            except Exception:
                break
    except asyncio.CancelledError:
        pass
