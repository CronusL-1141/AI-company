"""标准重启路径（POST /api/system/shutdown）退出前必须等完 hook 后台作业。

os_restart_api 只走这个端点：``_delayed_exit`` 0.5s 后 ``os._exit``，lifespan 与
``deps.cleanup_dependencies`` 都不执行。hook 用量记账挪到后台之后，SessionEnd 回执
deferred 时终测还没落库；这条退出路径不 drain，Leader 行的终值与子 agent 的四层用量
就随进程一起没了（审查实测：真实 493MB transcript，SessionEnd 65ms 回执、0.1s 后
shutdown，Leader 行四层全 NULL；HEAD 同步落库不丢）。

整条生产 HTTP 栈 + 临时文件库 + 真 HookTranslator。解析被门闩卡住，门闩在 no-drain
实现早已 ``os._exit`` 之后才打开；``os._exit`` 换成"当场直读 SQLite 文件拍快照"，断言
的是进程真正退出那一刻库里有什么。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from aiteam.api import app as app_module
from aiteam.api import debug_log, deps
from aiteam.api import event_bus as event_bus_module
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.api.routes import hooks, system
from aiteam.api.ws.manager import ConnectionManager
from aiteam.services import token_attribution
from aiteam.storage.connection import get_engine
from aiteam.storage.repository import StorageRepository

TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")
# 门闩打开的时刻：晚于 _delayed_exit 的 0.5s 让出，早于 drain 上限。
GATE_OPENS_AFTER_SECONDS = 1.0


def _assistant(req: str, *, inp: int, out: int, cache_r: int) -> dict:
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


class _GatedParses:
    """Hold every transcript parse at a gate the test opens; the real parse then runs unchanged."""

    def __init__(self, monkeypatch) -> None:
        self.gate = threading.Event()
        real_advance = token_attribution.TranscriptUsageCursor.advance
        real_full = token_attribution.parse_transcript_usage

        def gated_advance(cursor, path, *, final=False):
            self.gate.wait(timeout=30)
            return real_advance(cursor, path, final=final)

        def gated_full(path):
            self.gate.wait(timeout=30)
            return real_full(path)

        monkeypatch.setattr(token_attribution.TranscriptUsageCursor, "advance", gated_advance)
        monkeypatch.setattr(token_attribution, "parse_transcript_usage", gated_full)


class _HardExit:
    """Stand-in for os._exit: photograph the database file at the moment the process would die."""

    def __init__(self, database: Path, translator: HookTranslator) -> None:
        self._database = database
        self._translator = translator
        self.called = asyncio.Event()
        self.code: int | None = None
        self.rows: dict[str, dict] = {}
        # (退出序列的步骤, 该步骤开始时仍在飞的后台作业数)
        self.steps: list[tuple[str, int]] = []

    def _step(self, name: str) -> None:
        self.steps.append((name, self._translator._background.in_flight))  # noqa: SLF001

    def checkpoint(self) -> None:
        # 真 checkpoint 会连 DEFAULT_DB_URL（生产库）；这里只记下它排在退出序列的哪一步。
        self._step("checkpoint")

    def __call__(self, code: int) -> None:
        self.code = code
        self._step("exit")
        con = sqlite3.connect(self._database)
        try:
            cols = ", ".join((*TOKEN_COLUMNS, "tokens_measured_at"))
            for agent_id, *values in con.execute(f"SELECT id, {cols} FROM agents"):
                self.rows[agent_id] = dict(zip((*TOKEN_COLUMNS, "tokens_measured_at"), values, strict=True))
        finally:
            con.close()
        self.called.set()


@pytest_asyncio.fixture
async def shutdown_stack(tmp_path, monkeypatch):
    """Production HTTP stack over a temporary SQLite file; the hard exit and WAL checkpoint are stubbed."""
    monkeypatch.setattr(debug_log, "setup_debug_log", lambda: None)
    monkeypatch.setattr(app_module, "_get_mcp_http_app", lambda: None)
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "0")
    app = app_module.create_app()
    database = tmp_path / "shutdown.sqlite"
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
    parses = _GatedParses(monkeypatch)
    hard_exit = _HardExit(database, translator)
    monkeypatch.setattr(system, "_wal_checkpoint_best_effort", hard_exit.checkpoint)
    monkeypatch.setattr(system.os, "_exit", hard_exit)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        yield client, repo, translator, parses, hard_exit, database
    finally:
        parses.gate.set()
        await translator.drain(timeout=10)
        await client.aclose()
        app.dependency_overrides.clear()
        await get_engine(database_url).dispose()


@pytest.mark.asyncio
async def test_http_shutdown_lands_deferred_usage_before_the_hard_exit(shutdown_stack, tmp_path: Path):
    client, repo, translator, parses, hard_exit, _database = shutdown_stack
    session_id = "sess-http-shutdown"
    leader = await repo.create_agent(
        team_id="team-leader", name="Leader", role="leader", source="hook", session_id=session_id,
    )
    await repo.update_agent(leader.id, status="busy")
    worker = await repo.create_agent(
        team_id="team-x", name="researcher", role="researcher", source="hook",
        session_id=session_id, cc_tool_use_id="cc-agent-shutdown-1",
    )
    main_transcript = _write_transcript(tmp_path / f"{session_id}.jsonl", requests=30)
    sub_transcript = _write_transcript(tmp_path / "agent-shutdown.jsonl", requests=12)

    sub_ack = await client.post("/api/hooks/event", json={
        "hook_event_name": "SubagentStop", "session_id": session_id,
        "agent_id": "cc-agent-shutdown-1", "agent_type": "researcher",
        "agent_transcript_path": str(sub_transcript),
    })
    end_ack = await client.post("/api/hooks/event", json={
        "hook_event_name": "SessionEnd", "session_id": session_id,
        "transcript_path": str(main_transcript), "cwd": str(tmp_path), "reason": "exit",
    })
    assert sub_ack.status_code == 200 and end_ack.status_code == 200
    assert end_ack.json()["leader_usage_skip"] == "deferred"
    assert translator._background.in_flight == 2  # noqa: SLF001 - 两笔记账都还在路上

    shutdown = await client.post("/api/system/shutdown")
    assert shutdown.status_code == 200 and shutdown.json()["success"] is True
    # 没有 drain 的实现在 ~0.5s 就硬退了；门闩开在那之后，解析才可能完成。
    asyncio.get_running_loop().call_later(GATE_OPENS_AFTER_SECONDS, parses.gate.set)
    await asyncio.wait_for(hard_exit.called.wait(), timeout=15)

    assert hard_exit.code == 0
    # Leader 终测与子 agent 记账，都要在进程死掉那一刻已经在库里。
    for agent_id, transcript in ((leader.id, main_transcript), (worker.id, sub_transcript)):
        expected = token_attribution.parse_transcript_usage(transcript)
        assert expected is not None
        stored = hard_exit.rows[agent_id]
        assert {name: stored[name] for name in TOKEN_COLUMNS} == {name: expected[name] for name in TOKEN_COLUMNS}
        assert stored["tokens_measured_at"] is not None
    assert hard_exit.steps == [("checkpoint", 0), ("exit", 0)], "WAL checkpoint / 硬退时还有后台作业在飞"
