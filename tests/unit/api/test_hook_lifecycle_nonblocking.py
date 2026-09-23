"""Lifecycle hooks must not wait for transcript parsing, and parsing must not block the loop.

事故实录（2026-09 诊断批）：Stop（300s 节流到期）/ SessionStart / PostCompact / SessionEnd
在回 hook 之前同步全量解析主会话 transcript，而且就在事件循环上逐行 ``json.loads``。
490MB 的会话单次约 2s —— hook 客户端 1.5s 超时只丢了 ack，真正丢数据的是阻塞窗里
**别的会话**排队的请求：uvicorn 在客户端断开时直接判 disconnect，路由根本没跑。

这份测试钉的是三件事，全部经真实 ASGI 栈（生产中间件 + 路由 + 临时文件库）：

1. hook 响应不等解析：注入 ≥1.2s 的慢解析，Stop（解析到期）与 SessionStart 的响应仍 <500ms；
2. 解析不在事件循环上：同一窗口里 5ms ticker 的最大滞后 <300ms（给争 CPU 的 CI 留余量）；
3. 结果照样落库且不重不漏：drain 之后跨持久化边界（直连 SQLite 文件）读 Leader 行，
   等于整份解析的结果；同一会话并发触发 3 次，解析恰好 2 次（1 次在飞 + 1 次补跑）。

慢解析用**占 CPU 的忙循环**而不是 ``time.sleep``：sleep 会释放 GIL，放进线程后事件循环
毫无压力，比生产宽松；生产解析是纯 Python 字节码 + ``json.loads``，忙循环才是它的形状。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import aiosqlite
import httpx
import pytest
import pytest_asyncio

from aiteam.api import app as app_module
from aiteam.api import debug_log, deps, workflow_ingest
from aiteam.api import event_bus as event_bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import hooks
from aiteam.api.ws.manager import ConnectionManager
from aiteam.services import token_attribution
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

SLOW_PARSE_SECONDS = 1.2
RESPONSE_BUDGET_SECONDS = 0.5
LOOP_LAG_BUDGET_SECONDS = 0.3
TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")


def _burn_cpu(seconds: float) -> None:
    """Hold the interpreter busy like a real parse does (bytecode, not a GIL-free sleep)."""
    deadline = time.perf_counter() + seconds
    spins = 0
    while time.perf_counter() < deadline:
        spins += 1


def _assistant(req: str, *, inp: int, out: int, cache_r: int = 0) -> dict:
    return {
        "type": "assistant",
        "requestId": req,
        "message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": cache_r,
                "output_tokens": out,
            },
        },
    }


def _write_transcript(path: Path, requests: int) -> Path:
    rows = [{"type": "user", "message": {"role": "user", "content": "go"}}]
    for i in range(requests):
        rows.append(_assistant(f"req_{i}", inp=i + 1, out=3, cache_r=100 * i))
        rows.append(_assistant(f"req_{i}", inp=i + 1, out=50 + i, cache_r=100 * i))
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


class _SlowParses:
    """Count every transcript parse and make each one cost ``SLOW_PARSE_SECONDS`` of CPU.

    先真读、再烧 CPU：一轮解析读到的就是它开始那一刻文件里的字节，``read_done`` 在第一次
    真读完成后置位。读完之后才追加的内容，这一轮不可能看见，只有补跑读得到。
    """

    def __init__(self, monkeypatch) -> None:
        self.calls = 0
        self.read_done = threading.Event()
        real_advance = token_attribution.TranscriptUsageCursor.advance

        def unpatched_full(path):
            # 整份解析内部也走游标，得绕开被打桩的 advance，否则一次解析会被算两次。
            return real_advance(token_attribution.TranscriptUsageCursor(), path, final=True)

        def slow_advance(cursor, path, *, final=False):
            self.calls += 1
            result = real_advance(cursor, path, final=final)
            self.read_done.set()
            _burn_cpu(SLOW_PARSE_SECONDS)
            return result

        def slow_full(path):
            self.calls += 1
            result = unpatched_full(path)
            self.read_done.set()
            _burn_cpu(SLOW_PARSE_SECONDS)
            return result

        self.unpatched_full = unpatched_full

        monkeypatch.setattr(token_attribution.TranscriptUsageCursor, "advance", slow_advance)
        monkeypatch.setattr(token_attribution, "parse_transcript_usage", slow_full)


class _LoopLag:
    """5ms ticker: how late does the event loop wake up while a request is in flight?"""

    INTERVAL = 0.005

    def __init__(self) -> None:
        self.max_lag = 0.0
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _tick(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            started = loop.time()
            await asyncio.sleep(self.INTERVAL)
            self.max_lag = max(self.max_lag, loop.time() - started - self.INTERVAL)

    async def __aenter__(self) -> _LoopLag:
        self._task = asyncio.create_task(self._tick())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        assert self._task is not None
        await self._task


@pytest_asyncio.fixture
async def hook_stack(tmp_path, monkeypatch):
    """Production HTTP stack over a temporary SQLite file (no MCP, no shared log file)."""
    monkeypatch.setattr(debug_log, "setup_debug_log", lambda: None)
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    app = app_module.create_app()
    database = tmp_path / "lifecycle.sqlite"
    database_url = f"sqlite+aiosqlite:///{database}"
    repo = StorageRepository(db_url=database_url)
    await repo.init_db()
    bus = EventBus(repo=repo)
    translator = HookTranslator(repo=repo, event_bus=bus)
    app.dependency_overrides.update({
        deps.get_repository: lambda: repo,
        deps.get_event_bus: lambda: bus,
        deps.get_hook_translator: lambda: translator,
    })
    monkeypatch.setattr(deps, "_repository", repo)
    monkeypatch.setattr(deps, "_event_bus", bus)
    monkeypatch.setattr(deps, "_hook_translator", translator)
    monkeypatch.setattr(event_bus_module, "ws_manager", ConnectionManager())
    monkeypatch.setattr(event_bus_module.cfg, "SLACK_WEBHOOK_URL", "")
    monkeypatch.delenv(hooks.HOOK_RAW_DUMP_ENV, raising=False)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        yield client, repo, translator, database
    finally:
        await translator.drain(timeout=10)
        await client.aclose()
        app.dependency_overrides.clear()
        await get_engine(database_url).dispose()


async def _make_leader(repo: StorageRepository, session_id: str):
    agent = await repo.create_agent(
        team_id="team-leader", name="Leader", role="leader", source="hook", session_id=session_id,
    )
    await repo.update_agent(agent.id, status="busy")
    return agent


async def _read_tokens(database: Path, agent_id: str) -> dict:
    """Read the row straight from the SQLite file: nothing in-process can fake it."""
    async with aiosqlite.connect(database) as db:
        cols = ", ".join((*TOKEN_COLUMNS, "tokens_measured_at", "transcript_path"))
        async with db.execute(f"SELECT {cols} FROM agents WHERE id = ?", (agent_id,)) as cursor:
            row = await cursor.fetchone()
    assert row is not None
    return dict(zip((*TOKEN_COLUMNS, "tokens_measured_at", "transcript_path"), row, strict=True))


def _expected(transcript: Path, parse=None) -> dict:
    full = (parse or token_attribution.parse_transcript_usage)(transcript)
    assert full is not None
    return {name: full[name] for name in TOKEN_COLUMNS}


async def _timed_post(client: httpx.AsyncClient, payload: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    response = await client.post("/api/hooks/event", json=payload)
    elapsed = time.perf_counter() - started
    print(f"{payload['hook_event_name']} acknowledged in {elapsed * 1000:.0f}ms")
    assert response.status_code == 200, response.text
    return response.json(), elapsed


# ============================================================
# 1 + 2 + 3：响应不等解析、循环不被占、结果照样落库
# ============================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["Stop", "SessionStart"])
async def test_due_parse_does_not_hold_the_response_or_the_loop(
    hook_stack, tmp_path: Path, monkeypatch, event: str
):
    client, repo, translator, database = hook_stack
    session_id = f"sess-nonblock-{event.lower()}"
    leader = await _make_leader(repo, session_id)
    transcript = _write_transcript(tmp_path / f"{session_id}.jsonl", requests=40)
    expected = _expected(transcript)
    parses = _SlowParses(monkeypatch)

    # 首个 Stop 必然到期（本会话还没测过）；SessionStart 是强制定格。
    payload = {
        "hook_event_name": event,
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
    }
    async with _LoopLag() as lag:
        body, elapsed = await _timed_post(client, payload)
        # 响应已回，解析正在线程里烧 CPU：这段时间循环仍要能照常转。
        await asyncio.sleep(0.3)

    print(f"{event}: max loop lag {lag.max_lag * 1000:.0f}ms")
    assert elapsed < RESPONSE_BUDGET_SECONDS, f"{event} waited {elapsed:.3f}s for the parse"
    assert lag.max_lag < LOOP_LAG_BUDGET_SECONDS, f"event loop stalled {lag.max_lag:.3f}s"
    assert body["leader_usage_skip"] == "deferred"
    assert body["leader_usage"] is None

    assert await translator.drain(timeout=10)
    assert parses.calls == 1
    stored = await _read_tokens(database, leader.id)
    assert {name: stored[name] for name in TOKEN_COLUMNS} == expected
    assert stored["tokens_measured_at"] is not None
    assert stored["transcript_path"] == str(transcript)


@pytest.mark.asyncio
async def test_concurrent_triggers_coalesce_and_the_rerun_sees_new_bytes(
    hook_stack, tmp_path: Path, monkeypatch
):
    """同一会话并发触发 3 次：1 次在飞 + 恰好 1 次补跑，补跑读到在飞期间追加的内容。

    恰好 2 次而不是"至多 2 次"：少于 2 说明脏标记被吞了（在飞那一轮开始后才追加的
    字节就永远没人读），多于 2 说明 single-flight 没挡住并发。
    """
    client, repo, translator, database = hook_stack
    session_id = "sess-coalesce"
    leader = await _make_leader(repo, session_id)
    transcript = _write_transcript(tmp_path / "coalesce.jsonl", requests=10)
    parses = _SlowParses(monkeypatch)
    base = {"session_id": session_id, "transcript_path": str(transcript), "trigger": "manual"}

    first, _ = await _timed_post(client, {**base, "hook_event_name": "PostCompact"})
    assert first["leader_usage_skip"] == "deferred"
    # 等在飞那一轮真正读完文件（此后它在烧 CPU）再追加：追加的字节只有补跑读得到，
    # 下面的数值断言因此能独立鉴别补跑有没有发生，不只靠 calls 计数兜底。
    assert await asyncio.to_thread(parses.read_done.wait, 10)
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant("req_late", inp=1000, out=2000, cache_r=3000)) + "\n")
    rest = await asyncio.gather(
        _timed_post(client, {**base, "hook_event_name": "PostCompact"}),
        _timed_post(client, {**base, "hook_event_name": "PostCompact"}),
    )
    for body, elapsed in rest:
        assert body["leader_usage_skip"] == "deferred"
        # 这两发与解析线程、彼此都在争 GIL：纯字节码忙循环不释放 GIL，aiosqlite 每次
        # 线程往返都要等满 5ms 切换间隔，两发并发实测 0.6-0.8s（真实解析读文件时会
        # 让出 GIL，同样 30 次 DB 往返只慢到约 35ms）。这里只钉"没有等解析"：比一次
        # 解析的耗时短。单发响应预算由上面的用例钉。
        assert elapsed < SLOW_PARSE_SECONDS

    assert await translator.drain(timeout=15)
    assert parses.calls == 2
    stored = await _read_tokens(database, leader.id)
    assert {name: stored[name] for name in TOKEN_COLUMNS} == _expected(transcript, parses.unpatched_full)
    assert stored["input_tokens"] >= 1000  # 在飞那一轮之后追加的请求确实被补跑读到


@pytest.mark.asyncio
async def test_subagent_stop_does_not_wait_for_the_subagent_transcript(
    hook_stack, tmp_path: Path, monkeypatch
):
    client, repo, translator, database = hook_stack
    worker = await repo.create_agent(
        team_id="team-x", name="researcher", role="researcher", source="hook",
        session_id="sess-sub", cc_tool_use_id="cc-agent-sub-1",
    )
    transcript = _write_transcript(tmp_path / "agent-sub.jsonl", requests=12)
    expected = _expected(transcript)
    parses = _SlowParses(monkeypatch)

    async with _LoopLag() as lag:
        body, elapsed = await _timed_post(client, {
            "hook_event_name": "SubagentStop",
            "session_id": "sess-sub",
            "agent_id": "cc-agent-sub-1",
            "agent_type": "researcher",
            "agent_transcript_path": str(transcript),
        })
        await asyncio.sleep(0.3)

    print(f"SubagentStop: max loop lag {lag.max_lag * 1000:.0f}ms")
    assert elapsed < RESPONSE_BUDGET_SECONDS, f"SubagentStop waited {elapsed:.3f}s"
    assert lag.max_lag < LOOP_LAG_BUDGET_SECONDS, f"event loop stalled {lag.max_lag:.3f}s"
    assert body["agents_waiting"] == [worker.id]
    assert await translator.drain(timeout=10)
    assert parses.calls == 1
    stored = await _read_tokens(database, worker.id)
    assert {name: stored[name] for name in TOKEN_COLUMNS} == expected


@pytest.mark.asyncio
async def test_session_start_does_not_wait_for_workflow_reconcile(
    hook_stack, tmp_path: Path, monkeypatch
):
    """对账是 DB 往返为主的 async 活，慢桩用 await 形态（与生产同形）。"""
    client, repo, translator, _database = hook_stack
    calls: list[str | None] = []

    async def slow_reconcile(_repo, _bus, project_dir=None, session_id=None):
        await asyncio.sleep(SLOW_PARSE_SECONDS)
        calls.append(project_dir)
        return {"ingested": 0, "updated": 0, "errors": 0, "scanned": 0}

    monkeypatch.setattr(workflow_ingest, "reconcile", slow_reconcile)
    cwd = str(tmp_path / "proj")
    body, elapsed = await _timed_post(client, {
        "hook_event_name": "SessionStart", "session_id": "sess-reconcile", "cwd": cwd,
    })
    assert body["status"] == "recorded"
    assert elapsed < RESPONSE_BUDGET_SECONDS, f"SessionStart waited {elapsed:.3f}s for reconcile"
    assert calls == []
    assert await translator.drain(timeout=10)
    assert calls == [cwd]


@pytest.mark.asyncio
async def test_deferred_capture_is_not_reported_as_a_forced_miss(
    hook_stack, tmp_path: Path, caplog
):
    """``deferred`` 是稳态结局：强制定格走后台时一个 WARNING 都不该有。"""
    client, repo, translator, _database = hook_stack
    await _make_leader(repo, "sess-quiet-defer")
    transcript = _write_transcript(tmp_path / "quiet.jsonl", requests=3)
    with caplog.at_level(logging.WARNING, logger="aiteam.api.hook_translator"):
        body, _ = await _timed_post(client, {
            "hook_event_name": "PostCompact",
            "session_id": "sess-quiet-defer",
            "transcript_path": str(transcript),
        })
        assert await translator.drain(timeout=10)
    assert body["leader_usage_skip"] == "deferred"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
