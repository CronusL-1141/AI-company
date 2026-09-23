"""LangGraph 编排路径：回复形状与请求型号的回归用例.

真 ChatAnthropic 打到本地 Messages API 桩上，桩按真 API 的规矩拒收：不认识的
型号回 404，thinking 常开的型号收到 disabled/budget_tokens 回 400。回复先给
thinking 块再给 text 块（thinking 常开型号的形状），流式与非流式都支持。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

pytest.importorskip("langchain_core", reason="requires ai-team-os[langgraph] extra")
pytest.importorskip("langgraph", reason="requires ai-team-os[langgraph] extra")
pytest.importorskip("langchain_anthropic", reason="requires ai-team-os[langgraph] extra")

from langchain_core.messages import AIMessage

from aiteam.orchestrator.nodes import (
    DEFAULT_LLM_MODEL,
    LLM_MAX_TOKENS,
    api_model_id,
    response_text,
)
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.repository import StorageRepository
from aiteam.types import TaskStatus

KNOWN_MODELS = {
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-fable-5-1",
    "claude-haiku-4-5-20251001",
}
ALWAYS_THINKING = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}


class _MessagesStub:
    """Local POST /v1/messages that records every request body."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._count = 0
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _json(self, code: int, obj: dict) -> None:
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _error(self, code: int, kind: str, message: str) -> None:
                self._json(code, {"type": "error", "error": {"type": kind, "message": message}})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                with stub._lock:
                    stub.requests.append(body)
                    stub._count += 1
                    n = stub._count
                model = body.get("model")
                if model not in KNOWN_MODELS:
                    self._error(404, "not_found_error", f"model: {model}")
                    return
                thinking = body.get("thinking") or {}
                if model in ALWAYS_THINKING and thinking.get("type") in ("disabled", "enabled"):
                    self._error(400, "invalid_request_error", "thinking is always on")
                    return
                blocks: list[dict[str, Any]] = []
                if model in ALWAYS_THINKING:
                    blocks.append({"type": "thinking", "thinking": "", "signature": f"sig{n}"})
                blocks.append({"type": "text", "text": f"正文{n}"})
                if not body.get("stream"):
                    self._json(
                        200,
                        {
                            "id": f"msg_{n}",
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": blocks,
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 20},
                        },
                    )
                    return
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()

                def event(name: str, data: dict) -> None:
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())

                event(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": f"msg_{n}",
                            "type": "message",
                            "role": "assistant",
                            "model": model,
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 1},
                        },
                    },
                )
                for i, block in enumerate(blocks):
                    if block["type"] == "thinking":
                        event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": i,
                                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                            },
                        )
                        event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": i,
                                "delta": {"type": "signature_delta", "signature": block["signature"]},
                            },
                        )
                    else:
                        event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": i,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                        for piece in (block["text"][:1], block["text"][1:]):
                            event(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": i,
                                    "delta": {"type": "text_delta", "text": piece},
                                },
                            )
                    event("content_block_stop", {"type": "content_block_stop", "index": i})
                event(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 20},
                    },
                )
                event("message_stop", {"type": "message_stop"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture()
def messages_stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[_MessagesStub]:
    stub = _MessagesStub()
    thread = threading.Thread(target=stub.server.serve_forever, daemon=True)
    thread.start()
    for key in ("ANTHROPIC_API_URL", "ANTHROPIC_BASE_URL"):
        monkeypatch.setenv(key, stub.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, "127.0.0.1,localhost")
    try:
        yield stub
    finally:
        stub.server.shutdown()
        stub.server.server_close()


@pytest.fixture()
def manager(db_repository: StorageRepository) -> TeamManager:
    return TeamManager(repository=db_repository, memory=None)


async def _team(manager: TeamManager, mode: str, *models: str) -> None:
    await manager.create_team("t", mode=mode)
    for i, model in enumerate(models):
        await manager.add_agent("t", name=f"a{i}", role="工程师", model=model)


# ================================================================
# thinking 常开时回复是块列表，state 只收正文
# ================================================================


@pytest.mark.parametrize("mode", ["coordinate", "broadcast"])
async def test_run_task_keeps_text_when_reply_opens_with_thinking(
    manager: TeamManager,
    db_repository: StorageRepository,
    messages_stub: _MessagesStub,
    mode: str,
) -> None:
    await _team(manager, mode, "claude-opus-5-5")

    result = await manager.run_task("t", "写一段简介")

    assert result.status == TaskStatus.COMPLETED, result.result
    assert result.result.startswith("正文")
    assert all(v.startswith("正文") for v in result.agent_outputs.values())
    stored = await db_repository.get_task(result.task_id)
    assert stored is not None
    assert stored.result == result.result
    # Every node call streams and leaves room for thinking.
    assert messages_stub.requests
    for body in messages_stub.requests:
        assert body.get("stream") is True
        assert body["max_tokens"] == LLM_MAX_TOKENS


def test_response_text_keeps_text_blocks_only() -> None:
    blocks = AIMessage(
        content=[
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "甲", "index": 1},
            "乙",
            {"type": "tool_use", "id": "x", "name": "n", "input": {}},
        ]
    )
    assert response_text(blocks) == "甲乙"
    assert response_text(AIMessage(content="纯文本")) == "纯文本"


# ================================================================
# 观测回填的 agents[0].model 须是可用 API 型号才用
# ================================================================


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ("", DEFAULT_LLM_MODEL),
        ("opus", DEFAULT_LLM_MODEL),
        ("inherit", DEFAULT_LLM_MODEL),
        ("gpt-6-astra", DEFAULT_LLM_MODEL),
        ("claude-opus-5-5[1m]", "claude-opus-5-5"),
        ("claude-opus-4-8", "claude-opus-4-8"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    ],
)
async def test_run_task_uses_first_agent_model_only_when_it_is_an_api_id(
    manager: TeamManager,
    messages_stub: _MessagesStub,
    observed: str,
    expected: str,
) -> None:
    await _team(manager, "coordinate", observed)

    result = await manager.run_task("t", "写一段简介")

    assert result.status == TaskStatus.COMPLETED, result.result
    assert {body["model"] for body in messages_stub.requests} == {expected}


async def test_run_task_explicit_model_wins_over_agent_model(
    manager: TeamManager,
    messages_stub: _MessagesStub,
) -> None:
    await _team(manager, "coordinate", "claude-opus-4-8")

    result = await manager.run_task("t", "写一段简介", model="claude-fable-5-1")

    assert result.status == TaskStatus.COMPLETED, result.result
    assert {body["model"] for body in messages_stub.requests} == {"claude-fable-5-1"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("opus", None),
        ("claude", None),
        ("gpt-5.6-sol", None),
        ("anthropic.claude-opus-5", None),
        ("claude-opus-5-5[1m]", "claude-opus-5-5"),
        (" claude-sonnet-5 ", "claude-sonnet-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    ],
)
def test_api_model_id(value: str | None, expected: str | None) -> None:
    assert api_model_id(value) == expected
