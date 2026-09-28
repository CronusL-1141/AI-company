"""hook_delivery：hook 事件 POST 失败的分类与本机账。

以前 send_event 把 HTTPError（URLError 的子类）和拒连都写成 "API unreachable"，读超时又
落进通用 error，丢事件率拆不开分母。这里钉三件事：

1. 分类器对每类失败给出唯一的类名，判据是 urllib 真实抛出的异常（桩服务器造，不手搓异常）；
2. 失败落一行本机账，只记标识与耗时，不记载荷；并发写入逐行完整、一行不少；
3. hook_core 里给 Codex 用的那份分类器与本模块逐字相同，post_event 的返回值与 stderr
   文案不变（Codex 入口按 stderr 后缀记账）。

端到端（真 send_event 子进程 + 真 uvicorn）在 test_hook_delivery_e2e.py。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from testlib import serve_in_background

ROOT = Path(__file__).resolve().parents[3]
HOOKS_DIR = ROOT / "plugin" / "hooks"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_under_test_{name}", HOOKS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook_delivery = _load("hook_delivery")
hook_core = _load("hook_core")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _ledger(home: Path) -> list[dict]:
    path = home / ".claude" / "data" / "ai-team-os" / "hook-delivery" / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class _Stub(BaseHTTPRequestHandler):
    mode = "ok"

    def do_POST(self) -> None:  # noqa: N802 - fixed by BaseHTTPRequestHandler
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.mode == "sleep":
            time.sleep(1.0)
        if self.mode == "drop":
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        code = {"ok": 200, "sleep": 200, "4xx": 404, "5xx": 503}[self.mode]
        body = b"{}"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def stub():
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    serve_in_background(server)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ------------------------------------------------------------------ 分类


@pytest.mark.parametrize(
    ("mode", "expected", "status"),
    [
        ("ok", "posted", None),
        ("4xx", "http_4xx", 404),
        ("5xx", "http_5xx", 503),
        ("sleep", "timeout_after_send", None),
        ("drop", "reset_after_send", None),
    ],
)
def test_each_failure_gets_its_own_class(stub, home, capsys, mode, expected, status):
    _Stub.mode = mode
    url = f"http://127.0.0.1:{stub.server_address[1]}"
    result = hook_delivery.post_body(b"{}", url, "PreToolUse", session_id="s-1",
                                     tool_use_id="toolu_1", timeout=0.4)
    assert result == expected
    ledger = _ledger(home)
    if expected == "posted":
        assert ledger == []
        assert capsys.readouterr().err == ""
        return
    assert len(ledger) == 1
    entry = ledger[0]
    assert entry["cls"] == expected and entry["status"] == status
    assert entry["ev"] == "PreToolUse" and entry["sid"] == "s-1" and entry["keyed"] is True
    assert f"post_failed cls={expected}" in capsys.readouterr().err


def test_refused_is_not_mistaken_for_a_timeout(home):
    result = hook_delivery.post_body(b"{}", f"http://127.0.0.1:{_closed_port()}", "Stop")
    assert result == "refused"
    assert [e["cls"] for e in _ledger(home)] == ["refused"]
    assert _ledger(home)[0]["keyed"] is False


def test_connect_phase_failure_other_than_refusal(home):
    """A connect that times out because the accept queue is full: URLError, not a refusal.

    listen(1) and never accept; once the queue holds what it can, the kernel drops
    further SYNs and the next connect times out (the overloaded-server shape).
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    held = []
    try:
        for _ in range(16):  # fill the queue; stop at the first connect the kernel ignores
            client = socket.socket()
            client.settimeout(0.3)
            try:
                client.connect(("127.0.0.1", port))
            except TimeoutError:
                client.close()
                break
            held.append(client)
        else:
            pytest.skip("accept queue never filled on this platform")
        result = hook_delivery.post_body(b"{}", f"http://127.0.0.1:{port}", "Stop", timeout=0.5)
    finally:
        for client in held:
            client.close()
        server.close()
    assert result == "connect_failed"
    assert [e["cls"] for e in _ledger(home)] == ["connect_failed"]


def test_classes_are_the_documented_seven():
    assert hook_delivery.FAILURE_CLASSES == (
        "http_4xx", "http_5xx", "refused", "connect_failed",
        "timeout_after_send", "reset_after_send", "other",
    )


def test_ledger_never_carries_payload_content(stub, home):
    _Stub.mode = "5xx"
    secret = b'{"tool_input": {"command": "echo very-private-marker"}}'
    hook_delivery.post_body(secret, f"http://127.0.0.1:{stub.server_address[1]}", "PreToolUse")
    raw = (home / ".claude/data/ai-team-os/hook-delivery/ledger.jsonl").read_text(encoding="utf-8")
    assert "very-private-marker" not in raw
    assert set(json.loads(raw)) == {"t", "ev", "cls", "status", "ms", "sid", "keyed", "spool"}


def test_ledger_rotates_one_generation(home, monkeypatch):
    monkeypatch.setattr(hook_delivery, "LEDGER_MAX_BYTES", 300)
    for _ in range(10):
        hook_delivery._record({"t": "x", "ev": "Stop", "cls": "refused"})
    directory = home / ".claude/data/ai-team-os/hook-delivery"
    assert (directory / "ledger.1.jsonl").exists()
    assert (directory / "ledger.jsonl").stat().st_size <= 300 + 100
    assert sorted(p.name for p in directory.iterdir()) == [".rotate.lock", "ledger.1.jsonl", "ledger.jsonl"]


def test_ledger_failure_never_raises(home, monkeypatch):
    monkeypatch.setattr(hook_delivery, "ledger_dir", lambda: "/dev/null/not-a-dir")
    assert hook_delivery.post_body(b"{}", f"http://127.0.0.1:{_closed_port()}", "Stop") == "refused"


# ------------------------------------------------------------------ 并发写账

WRITERS = 48  # enough processes to crush a dev machine, per the repo's concurrency rule
LINES_PER_WRITER = 200

_WRITER = """
import importlib.util, os, sys, time
spec = importlib.util.spec_from_file_location("hook_delivery", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
gate, index, count = sys.argv[2], sys.argv[3], int(sys.argv[4])
deadline = time.monotonic() + 10  # never outlive a parent that died before opening the gate
while not os.path.exists(gate):
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.001)
for n in range(count):
    module._record({"t": "2026-01-01T00:00:00+00:00", "ev": "PreToolUse", "cls": "refused",
                    "status": None, "ms": n, "sid": "writer-" + index, "keyed": True})
"""


def test_concurrent_writers_lose_and_garble_no_line(home, tmp_path):
    """48 processes append 200 lines each at once: every line arrives whole, none lost."""
    import subprocess

    gate = tmp_path / "go"
    env = {**os.environ, "HOME": str(home)}
    procs: list[subprocess.Popen] = []
    try:
        for i in range(WRITERS):
            procs.append(subprocess.Popen(
                [sys.executable, "-c", _WRITER, str(HOOKS_DIR / "hook_delivery.py"), str(gate),
                 str(i), str(LINES_PER_WRITER)],
                env=env,
            ))
        time.sleep(1.5)  # let every writer import and park on the gate
        gate.touch()
        codes = [proc.wait(60) for proc in procs]
    finally:
        for proc in procs:  # reap on every path, including a failure before the gate opened
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    assert codes == [0] * WRITERS
    # ~1.3 MB in total, so the ledger rotates once mid-burst: count both generations.
    directory = home / ".claude/data/ai-team-os/hook-delivery"
    lines = [line for name in ("ledger.1.jsonl", "ledger.jsonl")
             for line in (directory / name).read_text(encoding="utf-8").splitlines()]
    parsed = [json.loads(line) for line in lines]  # a torn line fails here
    assert len(parsed) == WRITERS * LINES_PER_WRITER
    per_writer: dict[str, set[int]] = {}
    for entry in parsed:
        per_writer.setdefault(entry["sid"], set()).add(entry["ms"])
    assert len(per_writer) == WRITERS
    assert all(v == set(range(LINES_PER_WRITER)) for v in per_writer.values())


# ------------------------------------------------------------------ hook_core 对钉


def test_classifier_is_verbatim_in_hook_core():
    assert inspect.getsource(hook_core.classify_post_failure) == inspect.getsource(
        hook_delivery.classify_post_failure
    )


@pytest.mark.parametrize(
    ("mode", "state", "cls", "stderr_tail"),
    [
        ("4xx", "post_unreachable", "http_4xx", "API unreachable - HTTP Error 404: Not Found"),
        ("5xx", "post_unreachable", "http_5xx", "API unreachable - HTTP Error 503: Service Unavailable"),
        ("sleep", "error", "timeout_after_send", "error - timed out"),
    ],
)
def test_hook_core_state_and_stderr_unchanged_class_added(stub, capsys, monkeypatch,
                                                          mode, state, cls, stderr_tail):
    """Codex 入口解析的正是这些 stderr 后缀；状态值也照旧，细类另走 post_event_detailed。"""
    _Stub.mode = mode
    real_urlopen = hook_core.urllib.request.urlopen
    monkeypatch.setattr(hook_core.urllib.request, "urlopen",
                        lambda req, timeout: real_urlopen(req, timeout=0.4))
    url = f"http://127.0.0.1:{stub.server_address[1]}"
    assert hook_core.post_event({"hook_event_name": "Stop"}, url) == state
    assert capsys.readouterr().err.rstrip().endswith(f"[aiteam-hook] Stop: {stderr_tail}")
    assert hook_core.post_event_detailed({"hook_event_name": "Stop"}, url) == (state, cls)


def test_hook_core_refused_detail():
    url = f"http://127.0.0.1:{_closed_port()}"
    assert hook_core.post_event_detailed({"hook_event_name": "Stop"}, url) == (
        hook_core.HookPostState.POST_UNREACHABLE, "refused",
    )


def test_delivery_module_is_frozen_with_the_entry():
    frozen = json.loads((ROOT / "scripts" / "hook_entry_freeze.json").read_text(encoding="utf-8"))
    assert "plugin/hooks/hook_delivery.py" in frozen


def test_delivery_copies_are_byte_identical():
    assert (HOOKS_DIR / "hook_delivery.py").read_bytes() == (
        ROOT / "src" / "aiteam" / "hooks" / "hook_delivery.py"
    ).read_bytes()
