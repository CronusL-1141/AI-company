"""hook 投递失败分类的端到端：真 send_event.py 子进程对真 uvicorn（httptools）+ 临时库。

四种现场各跑一遍，断言落在本机账（HOME 指到临时目录）与服务端库两侧：

* 超时但已落库：外部连接持写锁 3s，hook 1.5s 超时放弃；账里记 timeout_after_send，
  锁放开后事件照样落库（服务端预读 body，只丢回执）。补投属于下一阶段，这里只验分类和记账。
* 拒连：端口上没有监听，记 refused。
* 5xx：translator 抛异常，记 http_5xx 与状态码。
* 4xx：API 前缀写错的装机，真服务对这条路径回 405，记 http_4xx。

PermissionDenied 的自有 POST 已改走同一出口（I25），拒连场景对它也跑一遍。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from aiteam.api.hook_translator import HookTranslator
from tests.unit.api.test_hook_ingest_preread import hook_server  # noqa: F401 - pytest fixture

ROOT = Path(__file__).resolve().parents[3]
SEND_EVENT = ROOT / "plugin" / "hooks" / "send_event.py"
PERMISSION_DENIED = ROOT / "plugin" / "hooks" / "permission_denied_recovery.py"


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _run_hook(script: Path, event: str, payload: dict, api_url: str, home: Path):
    env = {**os.environ, "AITEAM_API_URL": api_url, "HOME": str(home)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(script), event], input=json.dumps(payload),
        capture_output=True, text=True, timeout=30, env=env, cwd=str(home),
    )
    return proc, time.monotonic() - started


def _ledger(home: Path) -> list[dict]:
    path = home / ".claude" / "data" / "ai-team-os" / "hook-delivery" / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _pre_tool_use(marker: str) -> dict:
    return {
        "session_id": "synthetic-delivery-session",
        "cwd": "/workspace/cc-hook-cwd",
        "tool_name": "Bash",
        "tool_input": {"command": f"echo {marker}", "description": marker},
        "tool_use_id": f"toolu_{marker}",
    }


def _landed(database: Path, marker: str) -> int:
    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'cc.tool_use' AND data LIKE ?", (f"%{marker}%",),
        ).fetchone()[0]
    finally:
        con.close()


def test_timeout_after_send_is_classified_while_the_event_still_lands(hook_server, tmp_path):  # noqa: F811
    port, database, _ = hook_server
    marker = f"late-{uuid.uuid4().hex[:12]}"
    locker = sqlite3.connect(database, isolation_level=None, timeout=5)
    locker.execute("BEGIN IMMEDIATE")
    try:
        proc, elapsed = _run_hook(SEND_EVENT, "PreToolUse", _pre_tool_use(marker),
                                  f"http://127.0.0.1:{port}", tmp_path)
        time.sleep(1.0)  # hold the lock past the client's give-up
    finally:
        locker.execute("COMMIT")
        locker.close()
    assert proc.returncode == 0
    assert elapsed < 5, "the hook must give up at its own timeout, not wait for the lock"
    ledger = _ledger(tmp_path)
    assert [(e["cls"], e["ev"], e["keyed"]) for e in ledger] == [
        ("timeout_after_send", "PreToolUse", True),
    ]
    assert ledger[0]["sid"] == "synthetic-delivery-session"
    assert 1400 <= ledger[0]["ms"] < 5000
    assert "post_failed cls=timeout_after_send" in proc.stderr
    deadline = time.monotonic() + 15
    while _landed(database, marker) == 0:
        assert time.monotonic() < deadline, "the timed-out event never landed"
        time.sleep(0.1)
    assert _landed(database, marker) == 1


def test_refused_is_classified(tmp_path):
    proc, _ = _run_hook(SEND_EVENT, "Stop", {"session_id": "s-refused", "cwd": "/w"},
                        f"http://127.0.0.1:{_closed_port()}", tmp_path)
    assert proc.returncode == 0
    assert [(e["cls"], e["ev"], e["keyed"], e["status"]) for e in _ledger(tmp_path)] == [
        ("refused", "Stop", False, None),
    ]
    assert "API unreachable" not in proc.stderr


def test_server_error_is_classified_as_5xx(hook_server, tmp_path, monkeypatch):  # noqa: F811
    port, database, _ = hook_server
    marker = f"boom-{uuid.uuid4().hex[:12]}"

    async def explode(self, payload):
        raise RuntimeError("translator failure injected by the test")

    monkeypatch.setattr(HookTranslator, "handle_event", explode)
    proc, _ = _run_hook(SEND_EVENT, "PreToolUse", _pre_tool_use(marker),
                        f"http://127.0.0.1:{port}", tmp_path)
    assert proc.returncode == 0
    assert [(e["cls"], e["status"]) for e in _ledger(tmp_path)] == [("http_5xx", 500)]
    assert _landed(database, marker) == 0


def test_client_error_is_classified_as_4xx(hook_server, tmp_path):  # noqa: F811
    port, _, _ = hook_server
    proc, _ = _run_hook(SEND_EVENT, "Stop", {"session_id": "s-404", "cwd": "/w"},
                        f"http://127.0.0.1:{port}/wrong-prefix", tmp_path)
    assert proc.returncode == 0
    assert [(e["cls"], e["status"]) for e in _ledger(tmp_path)] == [("http_4xx", 405)]


def test_success_leaves_no_ledger_line(hook_server, tmp_path):  # noqa: F811
    port, database, _ = hook_server
    marker = f"ok-{uuid.uuid4().hex[:12]}"
    proc, _ = _run_hook(SEND_EVENT, "PreToolUse", _pre_tool_use(marker),
                        f"http://127.0.0.1:{port}", tmp_path)
    assert proc.returncode == 0 and proc.stderr == ""
    assert _ledger(tmp_path) == []
    assert _landed(database, marker) == 1


def test_permission_denied_event_goes_through_the_same_exit(tmp_path):
    payload = {"session_id": "s-denied", "tool_name": "Read", "tool_input": {"file_path": "/x"},
               "reason": "denied", "tool_use_id": "toolu_denied"}
    proc, _ = _run_hook(PERMISSION_DENIED, "PermissionDenied", payload,
                        f"http://127.0.0.1:{_closed_port()}", tmp_path)
    assert proc.returncode == 0
    assert [(e["cls"], e["ev"], e["keyed"]) for e in _ledger(tmp_path)] == [
        ("refused", "PermissionDenied", True),
    ]


def test_missing_delivery_module_falls_back_to_the_plain_post(tmp_path):
    """A partial install without hook_delivery.py still delivers (old unclassified path)."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "send_event.py").write_bytes(SEND_EVENT.read_bytes())
    proc, _ = _run_hook(hooks / "send_event.py", "Stop", {"session_id": "s", "cwd": "/w"},
                        f"http://127.0.0.1:{_closed_port()}", tmp_path)
    assert proc.returncode == 0
    assert "API unreachable" in proc.stderr
    assert "hook_delivery.py missing: plain POST, failures are not recorded" in proc.stderr
    assert _ledger(tmp_path) == []


def test_broken_delivery_module_says_why_and_falls_back(tmp_path):
    """A hook_delivery.py that cannot load (syntax error, wrong interpreter) is named on stderr."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "send_event.py").write_bytes(SEND_EVENT.read_bytes())
    (hooks / "hook_delivery.py").write_text("def broken(:\n", encoding="utf-8")
    proc, _ = _run_hook(hooks / "send_event.py", "Stop", {"session_id": "s", "cwd": "/w"},
                        f"http://127.0.0.1:{_closed_port()}", tmp_path)
    assert proc.returncode == 0
    assert "hook_delivery.py failed to load (SyntaxError" in proc.stderr
    assert "API unreachable" in proc.stderr  # still delivered through the fallback
    assert _ledger(tmp_path) == []


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch):
    # The hooks run with the developer's environment; loopback must never go via a proxy.
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
