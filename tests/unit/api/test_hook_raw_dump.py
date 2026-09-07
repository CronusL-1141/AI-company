"""A10 录制开关：AITEAM_HOOK_RAW_DUMP 置位才录，录了也不许改接收端的行为。

这个开关是 CC 零漂移差分测试的取样口——服务端录，既不用改用户的 settings.json，
也不用给 hook 挂 wrapper，录下来的就是 hook 真正发出的那份 body。正因为它是标尺，
它自己一旦改了 payload、改了返回、或者把落盘异常漏出去，采来的语料就什么都证明
不了。故这里钉四条：不置位时录制函数一次都不进；置位时原样追加一行且返回不变；
一次请求一行、按请求序追加；落盘失败也只影响录制，不影响接收端。

translator 换成只记账的替身：payload 仍走真的 HookEventPayload 校验（替身只替下游，
不比生产宽松），但返回值恒定，"响应不变"这条断言才不受库状态影响。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from aiteam.api import deps
from aiteam.api.app import create_app
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import hooks as hooks_route
from aiteam.memory.store import MemoryStore
from aiteam.orchestrator.team_manager import TeamManager
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

SENTINEL = {"status": "stub-ok", "marker": "unchanged"}

# 带中文的 tool_input：既验"原样"（落盘不转义），也验非 ASCII 不炸编码。
PAYLOAD = {
    "hook_event_name": "PostToolUse",
    "session_id": "80d0cc5e-186a-4948-9e99-39ecfcf17730",
    "agent_id": "agent-1",
    "tool_name": "Bash",
    "tool_input": {"command": "echo 你好"},
    "cwd": "/tmp/os",
}


class _RecordingTranslator:
    """只记账的 translator 替身：收到什么原样存下，返回恒定值。"""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def handle_event(self, payload: dict) -> dict:
        self.seen.append(payload)
        return dict(SENTINEL)


class _Harness:
    def __init__(self, app, client: TestClient, stub: _RecordingTranslator) -> None:
        self.app = app
        self.client = client
        self.stub = stub

    def post(self, payload: dict):
        return self.client.post("/api/hooks/event", json=payload)


@pytest.fixture()
def hooks_api(monkeypatch):
    """接收端 + 内存库 + 记账替身；装配整套换，避免一半写真库一半写内存库。"""
    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    asyncio.get_event_loop().run_until_complete(repo.init_db())
    memory = MemoryStore(repository=repo)
    event_bus = EventBus(repo=repo)
    deps._repository = repo
    deps._memory_store = memory
    deps._event_bus = event_bus
    deps._manager = TeamManager(repository=repo, memory=memory)
    deps._hook_translator = HookTranslator(repo=repo, event_bus=event_bus)

    app = create_app()

    @asynccontextmanager
    async def _no_lifespan(_app):
        yield

    app.router.lifespan_context = _no_lifespan

    stub = _RecordingTranslator()
    app.dependency_overrides[deps.get_hook_translator] = lambda: stub
    monkeypatch.delenv(hooks_route.HOOK_RAW_DUMP_ENV, raising=False)

    with TestClient(app) as client:
        yield _Harness(app, client, stub)

    asyncio.get_event_loop().run_until_complete(close_db())
    deps._repository = None
    deps._memory_store = None
    deps._event_bus = None
    deps._manager = None
    deps._hook_translator = None


def _expected_dump(payload: dict) -> dict:
    """接收端交给下游的那份 dict（默认字段补齐后的形态）。"""
    return hooks_route.HookEventPayload(**payload).model_dump()


def test_switch_off_never_enters_the_recorder(hooks_api, monkeypatch, tmp_path):
    """不置位 = 零行为差：录制函数一次都不进，落盘目录里什么都不长。"""

    def _explode(*args, **kwargs):
        raise AssertionError("recorder ran while the switch was off")

    monkeypatch.setattr(hooks_route, "_dump_raw_hook_payload", _explode)

    resp = hooks_api.post(PAYLOAD)

    assert resp.status_code == 200
    assert resp.json() == SENTINEL
    assert hooks_api.stub.seen == [_expected_dump(PAYLOAD)]
    assert list(tmp_path.iterdir()) == []


def test_switch_on_appends_the_payload_verbatim(hooks_api, monkeypatch, tmp_path):
    """置位：追加一行，内容就是发给下游的那份，返回一字不改。"""
    dump = tmp_path / "capture" / "raw.jsonl"  # 父目录故意不存在，录制端自己建
    monkeypatch.setenv(hooks_route.HOOK_RAW_DUMP_ENV, str(dump))

    resp = hooks_api.post(PAYLOAD)

    assert resp.status_code == 200
    assert resp.json() == SENTINEL

    text = dump.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == 1
    assert text.endswith("\n")
    assert json.loads(lines[0]) == hooks_api.stub.seen[0]
    assert hooks_api.stub.seen[0] == _expected_dump(PAYLOAD)
    assert "你好" in text  # 原样落盘，不转成 \uXXXX


def test_switch_on_appends_one_line_per_request(hooks_api, monkeypatch, tmp_path):
    """一次请求一行，按请求序追加（不是覆盖，也不是攒批）。"""
    dump = tmp_path / "raw.jsonl"
    monkeypatch.setenv(hooks_route.HOOK_RAW_DUMP_ENV, str(dump))

    hooks_api.post({**PAYLOAD, "tool_name": "Read"})
    hooks_api.post({**PAYLOAD, "tool_name": "Write"})

    lines = dump.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["tool_name"] for line in lines] == ["Read", "Write"]


def test_recorder_failure_never_reaches_the_caller(hooks_api, monkeypatch, tmp_path):
    """落盘失败只是少一条语料，接收端照常处理、照常返回。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(hooks_route.HOOK_RAW_DUMP_ENV, str(blocker / "raw.jsonl"))

    resp = hooks_api.post(PAYLOAD)

    assert resp.status_code == 200
    assert resp.json() == SENTINEL
    assert hooks_api.stub.seen == [_expected_dump(PAYLOAD)]


def test_real_translator_response_is_identical_on_and_off(hooks_api, monkeypatch, tmp_path):
    """换回真 translator：同一条事件，开关开与关的响应逐字节相同。"""
    hooks_api.app.dependency_overrides.pop(deps.get_hook_translator)
    event = {**PAYLOAD, "hook_event_name": "UnhandledForTest"}

    off = hooks_api.post(event)

    dump = tmp_path / "raw.jsonl"
    monkeypatch.setenv(hooks_route.HOOK_RAW_DUMP_ENV, str(dump))
    on = hooks_api.post(event)

    assert off.status_code == on.status_code == 200
    assert off.json() == on.json()
    assert len(dump.read_text(encoding="utf-8").splitlines()) == 1
