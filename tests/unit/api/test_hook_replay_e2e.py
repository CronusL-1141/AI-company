"""补投队列端到端：真 send_event 子进程、真 uvicorn（httptools）、临时 SQLite 文件。

三种现场，事件都走真实的失败再补投：
* 超时但已落库：外部连接持写锁，hook 1.5s 放弃并入队；锁放开后事件照常落库。下一次
  成功的 hook 补投它，服务端按回执答 duplicate，库里仍只有一条。
* 拒连：API 不在时 hook 入队；API 起来后下一次成功的 hook 补投，事件落库一次，时间取原始时刻。
* 5xx：处理器第一次抛错（回执随之释放），hook 入队；补投时正常处理，落库一次。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

from aiteam.api import middleware as middleware_module
from aiteam.api.hook_translator import HookTranslator
from tests.unit.api.test_hook_tool_pairing_e2e import (  # noqa: F401 - api is a pytest fixture
    SEND_EVENT,
    _db_time,
    _marker,
    _start_session,
    _tool,
    api,
)


def _hook(api, event: str, payload: dict, url: str | None = None) -> subprocess.CompletedProcess:  # noqa: F811
    env = {**os.environ, "AITEAM_API_URL": url or f"http://127.0.0.1:{api.port}",
           "HOME": str(api.home)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    proc = subprocess.run(
        [sys.executable, str(SEND_EVENT), event], input=json.dumps(payload),
        capture_output=True, text=True, timeout=30, env=env, cwd=str(api.home),
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def _ledger(api) -> list[dict]:  # noqa: F811
    path = api.home / ".claude/data/ai-team-os/hook-delivery/ledger.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _queued(api) -> list[str]:  # noqa: F811
    root = api.home / ".claude/data/ai-team-os/hook-delivery/spool"
    return [n for box in ("new", "cur") if (root / box).exists() for n in os.listdir(root / box)]


def _tool_uses(api, marker: str) -> list[tuple]:  # noqa: F811
    return api.rows("SELECT timestamp, data FROM events WHERE type = 'cc.tool_use' AND data LIKE ?",
                    f"%{marker}%")


def _stop_until_drained(api, hooks: int = 10) -> int:  # noqa: F811
    """Send Stop hooks until one of them has drained the queue; returns how many it took.

    A hook drains only when its own POST came back within DRAIN_IF_POSTED_WITHIN_S
    (0.3s): a slow API is not handed more work. On a busy machine the Stop right
    after the API came up can take longer than that and rightly leaves the queue
    alone, so the next quick hook is the one that redelivers. What each redelivery
    did is still checked in full: the ledger must hold exactly one replay line.
    """
    for sent in range(1, hooks + 1):
        _hook(api, "Stop", {"session_id": "00000000-0000-4000-8000-00000000b001",
                            "cwd": str(api.home), "stop_hook_active": False})
        if not _queued(api):
            print(f"queue drained by Stop hook #{sent}")
            return sent
    raise AssertionError(f"{hooks} Stop hooks left the queue holding {_queued(api)}")


def _wait_for(check, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.1)


def test_timed_out_event_that_landed_is_answered_as_duplicate_on_replay(api):  # noqa: F811
    stats = middleware_module.hook_ingest_stats
    before = dict(stats)
    with api.running():
        _start_session(api)
        m = _marker()
        locker = sqlite3.connect(api.database, isolation_level=None, timeout=5)
        locker.execute("BEGIN IMMEDIATE")
        try:
            proc = _hook(api, "PreToolUse", _tool(api, f"toolu_{m}", f"echo {m}"))
            time.sleep(1.0)
        finally:
            locker.execute("COMMIT")
            locker.close()
        assert "post_failed cls=timeout_after_send" in proc.stderr
        assert [e["spool"] for e in _ledger(api) if "cls" in e and "replay" not in e] == ["queued"]
        _wait_for(lambda: len(_tool_uses(api, m)) == 1)  # it landed; only the receipt was lost

        _stop_until_drained(api)
        assert [e["replay"] for e in _ledger(api) if "replay" in e] == ["duplicate"]
        assert len(_tool_uses(api, m)) == 1
        assert _queued(api) == []
    assert stats["replay_duplicate"] - before["replay_duplicate"] == 1


def test_refused_event_is_replayed_once_the_api_is_back(api):  # noqa: F811
    m = _marker()
    with api.running():
        _start_session(api)
        down_url = f"http://127.0.0.1:{api.port}"
    origin_floor = datetime.now(UTC)
    proc = _hook(api, "PreToolUse", _tool(api, f"toolu_{m}", f"echo {m}"), url=down_url)
    origin_ceiling = datetime.now(UTC)
    assert "post_failed cls=refused" in proc.stderr
    assert len(_queued(api)) == 1
    time.sleep(1.2)  # so that "dated at the origin" is distinguishable from "now"
    with api.running():
        assert _tool_uses(api, m) == []
        _stop_until_drained(api)
        rows = _tool_uses(api, m)
    assert len(rows) == 1
    at, data = rows[0]
    assert origin_floor - timedelta(milliseconds=5) <= _db_time(at) <= origin_ceiling
    assert json.loads(data)["hook_replay"]["attempt"] == 1
    assert [e["replay"] for e in _ledger(api) if "replay" in e] == ["delivered"]
    assert _queued(api) == []


def test_event_rejected_with_5xx_is_replayed_and_lands_once(api, monkeypatch):  # noqa: F811
    m = _marker()
    real = HookTranslator.handle_event
    failed: list[bool] = []

    async def flaky(self, payload):
        if m in json.dumps(payload) and not failed:
            failed.append(True)
            raise RuntimeError("handler failure injected by the test")
        return await real(self, payload)

    monkeypatch.setattr(HookTranslator, "handle_event", flaky)
    with api.running():
        _start_session(api)
        proc = _hook(api, "PreToolUse", _tool(api, f"toolu_{m}", f"echo {m}"))
        assert "post_failed cls=http_5xx status=500" in proc.stderr
        assert _tool_uses(api, m) == []
        _stop_until_drained(api)
        assert len(_tool_uses(api, m)) == 1
    assert [e["replay"] for e in _ledger(api) if "replay" in e] == ["delivered"]
    assert _queued(api) == []
