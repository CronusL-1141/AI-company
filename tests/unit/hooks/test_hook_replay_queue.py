"""hook_delivery 的补投队列：入队规则、补投标记、预算、上限、TTL、尝试次数、孤儿、并发认领。

桩服务器记录每个收到的 body（真 HTTP、真 urllib）；并发用例用 48 个真子进程同时 drain。
端到端（真 send_event + 真 uvicorn）在 tests/unit/api/test_hook_replay_e2e.py。
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from testlib import serve_in_background

ROOT = Path(__file__).resolve().parents[3]
HOOKS_DIR = ROOT / "plugin" / "hooks"


def _load():
    spec = importlib.util.spec_from_file_location("_hd_under_test", HOOKS_DIR / "hook_delivery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hd = _load()


class _Recorder(BaseHTTPRequestHandler):
    bodies: list[dict] = []
    lock = threading.Lock()
    status = 200
    delay = 0.0
    answer: dict = {"status": "recorded"}
    answer_by_id: dict = {}  # tool_use_id -> answer for its redeliveries

    def do_POST(self) -> None:  # noqa: N802 - fixed by BaseHTTPRequestHandler
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        with self.lock:
            type(self).bodies.append(body)
        if self.delay:
            time.sleep(self.delay)
        answer = self.answer_by_id.get(body.get("tool_use_id"), self.answer)
        raw = json.dumps(answer).encode()
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    return tmp_path


@pytest.fixture()
def server():
    _Recorder.bodies = []
    _Recorder.status, _Recorder.delay, _Recorder.answer = 200, 0.0, {"status": "recorded"}
    _Recorder.answer_by_id = {}
    ThreadingHTTPServer.request_queue_size = 256  # a real API listens with a deep backlog
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    srv.daemon_threads = True
    serve_in_background(srv)
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _closed_url() -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def _body(tool_use_id: str, event: str = "PreToolUse") -> bytes:
    return json.dumps({"hook_event_name": event, "session_id": "s-q", "tool_name": "Bash",
                       "tool_input": {"command": "ls"}, "tool_use_id": tool_use_id}).encode()


def _queue(home: Path, box: str = "new") -> list[str]:
    path = home / ".claude/data/ai-team-os/hook-delivery/spool" / box
    return sorted(os.listdir(path)) if path.exists() else []


def _ledger(home: Path) -> list[dict]:
    path = home / ".claude/data/ai-team-os/hook-delivery/ledger.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _fail_one(home: Path, tool_use_id: str, event: str = "PreToolUse") -> str:
    return hd.post_body(_body(tool_use_id, event), _closed_url(), event,
                        session_id="s-q", tool_use_id=tool_use_id)


def _replays(bodies: list[dict]) -> list[dict]:
    return [b for b in bodies if "_hook_replay" in b]


# ------------------------------------------------------------------ 入队规则


def test_a_failed_keyed_tool_event_is_queued_and_redelivered_with_the_marker(home, server):
    before = datetime.now(UTC)
    assert _fail_one(home, "toolu_a") == "refused"
    after = datetime.now(UTC)
    assert len(_queue(home)) == 1
    assert _ledger(home)[-1]["spool"] == "queued"
    assert hd.post_body(_body("toolu_live", "Stop"), server, "Stop") == "posted"
    replays = _replays(_Recorder.bodies)
    assert [r["tool_use_id"] for r in replays] == ["toolu_a"]
    marker = replays[0]["_hook_replay"]
    assert marker["attempt"] == 1
    assert before <= datetime.fromisoformat(marker["origin_at"]) <= after
    assert _queue(home) == [] and _queue(home, "cur") == []
    assert _ledger(home)[-1]["replay"] == "delivered"


@pytest.mark.parametrize(
    ("event", "tool_use_id", "status", "reason"),
    [
        ("Stop", "", None, "unkeyed"),
        ("PreToolUse", "", None, "unkeyed"),
        ("PermissionDenied", "toolu_d", None, "not_replayable_event"),
        ("SubagentStart", "toolu_s", None, "not_replayable_event"),
        ("PreToolUse", "toolu_4", 404, "not_replayable_class"),
    ],
    ids=["stop", "tool-without-id", "permission-denied", "lifecycle", "http-4xx"],
)
def test_only_keyed_tool_events_enter_the_queue(home, server, event, tool_use_id, status, reason):
    url = _closed_url()
    if status:
        _Recorder.status, url = status, server
    hd.post_body(_body(tool_use_id, event), url, event, session_id="s-q", tool_use_id=tool_use_id)
    assert _queue(home) == []
    assert _ledger(home)[-1]["spool"] == reason


def test_a_5xx_and_a_timeout_are_queued(home, server):
    _Recorder.status = 503
    assert hd.post_body(_body("toolu_5"), server, "PreToolUse", session_id="s-q",
                        tool_use_id="toolu_5") == "http_5xx"
    _Recorder.status, _Recorder.delay = 200, 0.5
    assert hd.post_body(_body("toolu_t"), server, "PostToolUse", session_id="s-q",
                        tool_use_id="toolu_t", timeout=0.2) == "timeout_after_send"
    assert len(_queue(home)) == 2


# ------------------------------------------------------------------ 补投结果


def test_a_duplicate_answer_is_recorded_as_such(home, server):
    _fail_one(home, "toolu_dup")
    _Recorder.answer = {"status": "recorded", "duplicate": True}
    hd.post_body(_body("toolu_live", "Stop"), server, "Stop")
    assert _ledger(home)[-1]["replay"] == "duplicate"
    assert _queue(home) == []


def test_a_failed_redelivery_is_requeued_and_stops_the_drain(home, server):
    for i in range(3):
        _fail_one(home, f"toolu_{i}")
    _Recorder.status = 503
    assert hd.drain(server) == 1
    queued = _queue(home)
    assert len(queued) == 3 and _queue(home, "cur") == []
    record = json.loads((home / ".claude/data/ai-team-os/hook-delivery/spool/new" / queued[0]).read_text())
    assert record["attempts"] == 1
    assert _ledger(home)[-1]["replay"] == "failed"


def test_a_record_is_dropped_after_its_last_attempt(home, server):
    _fail_one(home, "toolu_x")
    _Recorder.status = 503
    for _ in range(hd.MAX_ATTEMPTS):
        hd.drain(server)
    assert _queue(home) == []
    assert [e.get("replay") for e in _ledger(home) if "replay" in e] == (
        ["failed"] * (hd.MAX_ATTEMPTS - 1) + ["exhausted"])


def test_a_4xx_on_redelivery_drops_the_record(home, server):
    _fail_one(home, "toolu_r")
    _Recorder.status = 422
    hd.drain(server)
    assert _queue(home) == []
    assert _ledger(home)[-1]["replay"] == "rejected"


def test_expired_records_are_dropped_unsent(home, server, monkeypatch):
    _fail_one(home, "toolu_old")
    monkeypatch.setattr(hd, "RECORD_TTL_S", 0)
    time.sleep(0.01)
    hd.drain(server)
    assert _replays(_Recorder.bodies) == [] and _queue(home) == []
    assert _ledger(home)[-1]["replay"] == "expired"


def test_a_full_queue_drops_the_new_event_after_pruning_expired_ones(home, monkeypatch):
    monkeypatch.setattr(hd, "MAX_PENDING", 3)
    for i in range(3):
        _fail_one(home, f"toolu_{i}")
    _fail_one(home, "toolu_overflow")
    assert len(_queue(home)) == 3
    assert _ledger(home)[-1]["spool"] == "spool_full"
    monkeypatch.setattr(hd, "RECORD_TTL_S", 0)
    time.sleep(0.01)
    _fail_one(home, "toolu_fresh")  # the full queue is pruned of expired records first
    assert len(_queue(home)) == 1
    assert [e["replay"] for e in _ledger(home) if "replay" in e] == ["expired"] * 3


def test_claimed_records_count_toward_the_cap_like_queue_state_says(home, monkeypatch):
    monkeypatch.setattr(hd, "MAX_PENDING", 3)
    for i in range(3):
        _fail_one(home, f"toolu_{i}")
    tmp, new, cur = hd._dirs()
    hd._claim(new, cur, _queue(home)[0])  # one is in flight in another hook
    assert hd.queue_state()["pending"] == 3
    _fail_one(home, "toolu_more")
    assert _ledger(home)[-1]["spool"] == "spool_full"


def test_an_orphaned_claim_is_recovered_and_delivered(home, server, monkeypatch):
    _fail_one(home, "toolu_orphan")
    spool = home / ".claude/data/ai-team-os/hook-delivery/spool"
    name = _queue(home)[0]
    os.rename(spool / "new" / name, spool / "cur" / name)  # a hook died mid-drain
    old = time.time() - hd.ORPHAN_AFTER_S - 5
    os.utime(spool / "cur" / name, (old, old))
    hd.drain(server)
    assert [r["tool_use_id"] for r in _replays(_Recorder.bodies)] == ["toolu_orphan"]
    assert [e["replay"] for e in _ledger(home) if "replay" in e] == ["orphan_recovered", "delivered"]


def test_a_fresh_claim_is_left_alone(home, server):
    _fail_one(home, "toolu_busy")
    spool = home / ".claude/data/ai-team-os/hook-delivery/spool"
    name = _queue(home)[0]
    os.rename(spool / "new" / name, spool / "cur" / name)
    os.utime(spool / "cur" / name)  # another hook is posting it right now
    hd.drain(server)
    assert _replays(_Recorder.bodies) == [] and _queue(home, "cur") == [name]


def test_a_corrupt_record_is_dropped(home, server):
    _fail_one(home, "toolu_c")
    spool = home / ".claude/data/ai-team-os/hook-delivery/spool"
    (spool / "new" / _queue(home)[0]).write_text("{not json")
    hd.drain(server)
    assert _queue(home) == [] and _ledger(home)[-1]["replay"] == "corrupt"


def test_in_flight_answer_keeps_the_record_and_stops_the_drain(home, server):
    """The first delivery is still being handled: it can still fail, so nothing is deleted."""
    _fail_one(home, "toolu_busy1")
    _fail_one(home, "toolu_busy2")
    _Recorder.answer = {"status": "duplicate", "reason": "in_flight", "duplicate": True}
    assert hd.drain(server) == 1
    queued = _queue(home)
    assert len(queued) == 2 and _queue(home, "cur") == []
    spool = home / ".claude/data/ai-team-os/hook-delivery/spool/new"
    assert json.loads((spool / queued[0]).read_text())["attempts"] == 0  # not a spent attempt
    last = _ledger(home)[-1]
    assert (last["replay"], last["reason"]) == ("failed", "in_flight")
    _Recorder.answer = {"status": "recorded", "duplicate": True}  # the first delivery finished
    hd.drain(server)
    assert _queue(home) == []
    assert [e["replay"] for e in _ledger(home) if "replay" in e][-2:] == ["duplicate", "duplicate"]


def test_a_stalled_holder_cannot_revive_a_record_someone_else_delivered(home, server):
    """A hook stalls past ORPHAN_AFTER_S holding a claim; the record is recovered and
    delivered by another hook; the stalled one then fails its POST and tries to put
    the record back. It must find its claim gone and leave the queue empty."""
    _fail_one(home, "toolu_stall")
    tmp, new, cur = hd._dirs()
    name = _queue(home)[0]
    claimed = hd._claim(new, cur, name)
    record = json.loads(Path(claimed).read_text())
    old = time.time() - hd.ORPHAN_AFTER_S - 5
    os.utime(claimed, (old, old))  # the holder stalled
    hd.drain(server)  # another hook recovers the orphan and delivers it
    assert [r["tool_use_id"] for r in _replays(_Recorder.bodies)] == ["toolu_stall"]
    record["attempts"] = 1
    assert hd._requeue(tmp, new, claimed, name, record) is False
    assert _queue(home) == [] and _queue(home, "cur") == [] and os.listdir(tmp) == []
    hd.drain(server)
    assert len(_replays(_Recorder.bodies)) == 1  # not delivered a second time


def test_a_huge_tool_input_is_shrunk_before_it_is_stored(home, server):
    content = "x" * 1_000_000
    body = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s-q", "tool_name": "Write",
                       "tool_input": {"file_path": "/tmp/big.txt", "content": content},
                       "tool_use_id": "toolu_big"}).encode()
    hd.post_body(body, _closed_url(), "PreToolUse", session_id="s-q", tool_use_id="toolu_big")
    last = _ledger(home)[-1]
    assert (last["spool"], last["shrunk_from"]) == ("queued", len(body))
    stored = home / ".claude/data/ai-team-os/hook-delivery/spool/new" / _queue(home)[0]
    assert stored.stat().st_size <= hd.MAX_RECORD_BYTES + 1024
    hd.drain(server)
    replayed = _replays(_Recorder.bodies)[0]
    assert replayed["tool_use_id"] == "toolu_big" and replayed["tool_name"] == "Write"
    assert replayed["tool_input"]["file_path"] == "/tmp/big.txt"
    assert replayed["tool_input"]["content"].endswith("...(truncated)")
    assert len(replayed["tool_input"]["content"]) < 2000


def test_a_body_that_cannot_be_shrunk_is_not_stored(home):
    """Many short strings: nothing to cut, still too big - too_large, and nothing on disk."""
    many = {f"k{i}": "v" * 50 for i in range(3000)}
    body = json.dumps({"hook_event_name": "PreToolUse", "session_id": "s-q", "tool_name": "X",
                       "tool_input": many, "tool_use_id": "toolu_huge"}).encode()
    hd.post_body(body, _closed_url(), "PreToolUse", session_id="s-q", tool_use_id="toolu_huge")
    last = _ledger(home)[-1]
    assert (last["spool"], last["bytes"]) == ("too_large", len(body))
    assert _queue(home) == []


def test_leftovers_of_a_hook_that_died_while_writing_are_recovered(home, server):
    """A requeue that died after writing the updated record into tmp/, and a stale
    leftover whose record is already queued: the first is delivered, the second deleted."""
    _fail_one(home, "toolu_q")
    tmp, new, cur = hd._dirs()
    queued_name = _queue(home)[0]
    lost = f"{time.time_ns():020d}-0123456789abcdef.json"
    record = {"v": 1, "ev": "PreToolUse", "sid": "s-q", "tool_use_id": "toolu_lost",
              "origin_at": datetime.now(UTC).isoformat(), "origin_cls": "refused",
              "attempts": 1, "body": _body("toolu_lost").decode()}
    old = time.time() - hd.ORPHAN_AFTER_S - 5
    for filename, content in ((f"{lost}.1-aa.new", json.dumps(record)),
                              (f"{queued_name}.2-bb.old", "{}")):
        path = Path(tmp) / filename
        path.write_text(content)
        os.utime(path, (old, old))
    hd.drain(server)
    assert os.listdir(tmp) == []
    assert sorted(r["tool_use_id"] for r in _replays(_Recorder.bodies)) == ["toolu_lost", "toolu_q"]


def test_a_hand_deleted_cur_dir_does_not_stop_the_queue(home, server):
    _fail_one(home, "toolu_rm")
    os.rmdir(Path(hd.spool_dir()) / "cur")
    hd.drain(server)
    assert [r["tool_use_id"] for r in _replays(_Recorder.bodies)] == ["toolu_rm"]


def test_outcomes_carry_the_first_failure_class(home, server):
    _Recorder.status, _Recorder.delay = 200, 0.5
    hd.post_body(_body("toolu_slow"), server, "PreToolUse", session_id="s-q",
                 tool_use_id="toolu_slow", timeout=0.2)  # timeout_after_send
    _Recorder.delay = 0.0
    _fail_one(home, "toolu_down")  # refused
    _Recorder.answer_by_id = {"toolu_slow": {"status": "recorded", "duplicate": True}}
    hd.drain(server)
    replays = {e["origin_cls"]: e["replay"] for e in _ledger(home) if "replay" in e}
    assert replays == {"timeout_after_send": "duplicate", "refused": "delivered"}


# ------------------------------------------------------------------ 热路径预算


def test_a_slow_post_does_not_drain(home, server):
    _fail_one(home, "toolu_wait")
    _Recorder.delay = hd.DRAIN_IF_POSTED_WITHIN_S + 0.05
    hd.post_body(_body("toolu_live", "Stop"), server, "Stop")
    assert _replays(_Recorder.bodies) == [] and len(_queue(home)) == 1


def test_a_drain_makes_at_most_five_posts(home, server):
    for i in range(20):
        _fail_one(home, f"toolu_{i:02d}")
    hd.post_body(_body("toolu_live", "Stop"), server, "Stop")
    replays = _replays(_Recorder.bodies)
    assert [r["tool_use_id"] for r in replays] == [f"toolu_{i:02d}" for i in range(5)]  # oldest first
    assert len(_queue(home)) == 15


def test_the_drain_stays_within_its_time_budget(home, server):
    for i in range(20):
        _fail_one(home, f"toolu_{i:02d}")
    _Recorder.delay = 0.12
    started = time.monotonic()
    assert hd.post_body(_body("toolu_live", "Stop"), server, "Stop") == "posted"
    elapsed = time.monotonic() - started
    # The count is the precise check; the wall clock only guards against a runaway
    # (a busy CI runner can add hundreds of milliseconds on its own).
    assert elapsed < 0.12 + hd.DRAIN_BUDGET_S + 1.0, elapsed
    assert len(_replays(_Recorder.bodies)) <= 2


# ------------------------------------------------------------------ 并发认领

WRITERS = 48  # enough processes to crush a dev machine, per the repo's concurrency rule
RECORDS = 150

_DRAINER = """
import importlib.util, os, sys, time
spec = importlib.util.spec_from_file_location("hook_delivery", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
gate, url = sys.argv[2], sys.argv[3]
deadline = time.monotonic() + 10  # never outlive a parent that died before opening the gate
while not os.path.exists(gate):
    if time.monotonic() > deadline:
        sys.exit(3)
    time.sleep(0.001)
new = os.path.join(module.spool_dir(), "new")
stop = time.monotonic() + 20
while os.listdir(new) and time.monotonic() < stop:
    module.drain(url)
"""


def test_48_processes_draining_at_once_deliver_each_record_exactly_once(home, server, tmp_path):
    for i in range(RECORDS):
        hd._enqueue(_body(f"toolu_{i:03d}"), "PreToolUse", "s-q", f"toolu_{i:03d}", time.time_ns(),
                    "refused")
    assert len(_queue(home)) == RECORDS
    gate = tmp_path / "go"
    env = {**os.environ, "HOME": str(home)}
    procs: list[subprocess.Popen] = []
    try:
        for _ in range(WRITERS):
            procs.append(subprocess.Popen(
                [sys.executable, "-c", _DRAINER, str(HOOKS_DIR / "hook_delivery.py"), str(gate), server],
                env=env,
            ))
        time.sleep(1.5)  # let every drainer import and park on the gate
        gate.touch()
        codes = [proc.wait(60) for proc in procs]
    finally:
        for proc in procs:  # reap on every path, including a failure before the gate opened
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    assert codes == [0] * WRITERS
    # Under this load a redelivery can time out and be retried: the same record then
    # arrives again with the next attempt number (the server dedupes it). What must
    # never happen is two processes posting the same attempt of the same record.
    posted = Counter((r["tool_use_id"], r["_hook_replay"]["attempt"])
                     for r in _replays(_Recorder.bodies))
    assert max(posted.values()) == 1, [k for k, v in posted.items() if v > 1][:5]
    assert {tool_use_id for tool_use_id, _ in posted} == {f"toolu_{i:03d}" for i in range(RECORDS)}
    assert _queue(home) == [] and _queue(home, "cur") == []
    outcomes = Counter(e["replay"] for e in _ledger(home) if "replay" in e)
    assert outcomes["delivered"] == RECORDS
    assert set(outcomes) <= {"delivered", "failed"}  # retried, never dropped
