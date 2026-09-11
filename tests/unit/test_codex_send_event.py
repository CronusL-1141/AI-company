"""Exercise the real Codex sender process against an isolated HTTP receiver."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "plugin/harness/codex/hooks/send_event_codex.py"


def _counts(path: Path) -> dict[str, int]:
    with sqlite3.connect(path / "hook-counts.sqlite3") as connection:
        return dict(connection.execute("SELECT state, count FROM counters"))


def _invocations(path: Path) -> list[dict]:
    with sqlite3.connect(path / "hook-counts.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM invocations")]


@pytest.fixture
def receiver():
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", bodies
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _env(path: Path, url: str) -> dict[str, str]:
    return {
        **os.environ,
        "AITEAM_CODEX_STATE_DIR": str(path),
        "AITEAM_API_URL": url,
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "127.0.0.1,localhost",
    }


def _run(path: Path, url: str, raw: str, event: str | None = "PostToolUse"):
    result = subprocess.run(
        [sys.executable, str(HOOK), *([event] if event is not None else [])],
        input=raw, text=True, capture_output=True, env=_env(path, url), timeout=8,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert "[aiteam-codex-hook] invoked\n" in result.stderr
    return result


@pytest.mark.parametrize("response", ["plain response", {"result": "structured"}])
def test_post_and_argv_override(tmp_path, receiver, response):
    url, bodies = receiver
    payload = {"hook_event_name": "ignored", "tool_name": "Bash", "tool_response": response}
    result = _run(tmp_path, url, json.dumps(payload))
    assert "[aiteam-codex-hook] posted\n" in result.stderr
    body, = bodies
    sampled_at = body.pop("source_observed_at")
    assert datetime.fromisoformat(sampled_at).tzinfo is not None
    assert body == {**payload, "hook_event_name": "PostToolUse", "harness": "codex"}
    assert _counts(tmp_path) == {
        "invoked": 1, "posted": 1, "inert_dropped": 0, "post_unreachable": 0, "error": 0,
    }


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse"])
@pytest.mark.parametrize("name", ["update_plan", "mcp__codex_app__list_threads"])
def test_inert_without_post(tmp_path, receiver, event, name):
    url, bodies = receiver
    result = _run(tmp_path, url, json.dumps({"tool_name": name}), event)
    assert "[aiteam-codex-hook] inert_dropped\n" in result.stderr
    assert bodies == []
    assert _counts(tmp_path)["inert_dropped"] == 1


@pytest.mark.parametrize("name", [
    "collaborationspawn_agent", "collaborationwait_agent", "collaborationsend_message",
    "collaborationfollowup_task", "collaborationlist_agents", "collaborationinterrupt_agent",
    "spawn_agent", "send_input", "close_agent", "resume_agent", "view_image",
    "list_mcp_resources", "read_mcp_resource", "mcp__ai_team_os__task_memo_read",
    "mcp__ai-team-os__task_memo_add", "unregistered_tool",
])
def test_keep_activity_tools(tmp_path, receiver, name):
    url, bodies = receiver
    _run(tmp_path, url, json.dumps({"tool_name": name}))
    assert bodies[0]["tool_name"] == name
    assert _counts(tmp_path)["posted"] == 1


def test_non_tool_event_and_payload_event(tmp_path, receiver):
    url, bodies = receiver
    _run(tmp_path, url, json.dumps({"hook_event_name": "Stop", "tool_name": "update_plan"}), None)
    assert bodies[0]["hook_event_name"] == "Stop"


def test_entry_declares_codex_and_preserves_exact_dispatch_when_oversized(tmp_path, receiver):
    url, bodies = receiver
    payload = {"session_id": "native-parent", "agent_id": "native-child",
               "agent_type": "default", "harness": "claude-code", "tool_name": "Bash",
               "timestamp": "2020-01-01T00:00:00Z", "source_observed_at": "untrusted-value",
               "parent_thread_id": "native-parent",
               "tool_input": {"large": list(range(10_000))}}
    _run(tmp_path, url, json.dumps(payload))
    body, = bodies
    assert body["_stripped"] is True
    assert body["harness"] == "codex"
    assert body["agent_id"] == "native-child"
    assert body["agent_type"] == "default"
    assert body["session_id"] == "native-parent"
    assert body["parent_thread_id"] == "native-parent"
    assert body["timestamp"] == "2020-01-01T00:00:00Z"
    assert datetime.fromisoformat(body["source_observed_at"]).tzinfo is not None


def test_source_observation_is_sampled_before_stdin_wait(tmp_path, receiver):
    import time

    url, bodies = receiver
    process = subprocess.Popen(
        [sys.executable, str(HOOK), "PreToolUse"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(tmp_path, url),
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if _counts(tmp_path)["invoked"] == 1:
                    break
            except sqlite3.Error:
                pass
            time.sleep(0.01)
        else:
            pytest.fail("No invocation marker before stdin")
        before_input = datetime.now(UTC)
        stdout, _ = process.communicate(json.dumps({"tool_name": "Bash"}), timeout=5)
        assert process.returncode == 0 and stdout == ""
        body, = bodies
        assert datetime.fromisoformat(body["source_observed_at"]) <= before_input
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)


@pytest.mark.parametrize("raw", ["", "{", "[]", "null", '"text"', "42"])
def test_bad_payload(tmp_path, receiver, raw):
    url, bodies = receiver
    result = _run(tmp_path, url, raw)
    assert "[aiteam-codex-hook] error\n" in result.stderr
    assert bodies == []
    assert _counts(tmp_path)["error"] == 1


def test_missing_event(tmp_path, receiver):
    url, bodies = receiver
    _run(tmp_path, url, "{}", None)
    assert _counts(tmp_path)["error"] == 1
    assert not bodies


def test_unreachable(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        result = _run(tmp_path, url, "{}")
    assert "[aiteam-codex-hook] post_unreachable\n" in result.stderr
    assert _counts(tmp_path)["post_unreachable"] == 1


def test_corrupt_counter_is_not_replaced(tmp_path, receiver):
    url, bodies = receiver
    path = tmp_path / "hook-counts.sqlite3"
    original = b"corrupt counter bytes"
    path.write_bytes(original)
    result = _run(tmp_path, url, "{}")
    assert result.stderr.count("counter_write_failed") == 2
    assert path.read_bytes() == original
    assert len(bodies) == 1


def test_counter_directory_failure_still_posts(tmp_path, receiver):
    url, bodies = receiver
    path = tmp_path / "not-a-directory"
    path.write_text("preserve")
    result = _run(path, url, "{}")
    assert "counter_write_failed" in result.stderr
    assert len(bodies) == 1


def test_invalid_counter_values_are_preserved(tmp_path, receiver):
    url, bodies = receiver
    path = tmp_path / "hook-counts.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE counters (state TEXT PRIMARY KEY, count)")
        connection.execute("INSERT INTO counters VALUES ('invoked', 'broken')")
    result = _run(tmp_path, url, "{}")
    assert result.stderr.count("counter_write_failed") == 2
    assert _counts(tmp_path) == {"invoked": "broken"}
    assert len(bodies) == 1


def test_concurrent_invocations(tmp_path, receiver):
    url, bodies = receiver
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _run(tmp_path, url, "{}"), range(32)))
    assert not any("counter_write_failed" in result.stderr for result in results)
    counts = _counts(tmp_path)
    assert counts["invoked"] == counts["posted"] == len(bodies) == 32
    rows = _invocations(tmp_path)
    assert len({row["call_id"] for row in rows}) == 32
    assert all(row["status"] == "posted" for row in rows)


def test_invoked_committed_before_stdin(tmp_path):
    process = subprocess.Popen(
        [sys.executable, str(HOOK), "Stop"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=_env(tmp_path, "http://127.0.0.1:1"),
    )
    try:
        # Acquiring the transaction after the invocation marker may race the
        # commit, so wait on the counter with a bounded monotonic deadline.
        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                counts = _counts(tmp_path)
                if counts.get("invoked") == 1:
                    break
            except sqlite3.Error:
                pass
            time.sleep(0.01)
        else:
            pytest.fail("invocation was not persisted before reading stdin")
        assert sum(counts.values()) == 1
    finally:
        process.kill()
        process.communicate(timeout=5)
    assert _counts(tmp_path)["invoked"] == 1
    row, = _invocations(tmp_path)
    assert row["status"] == "pending"
    assert row["event"] == "Stop"
    assert row["finished_at"] is None


def test_diagnostic_metadata_and_history(tmp_path, receiver):
    url, _ = receiver
    with sqlite3.connect(tmp_path / "hook-counts.sqlite3") as connection:
        connection.execute("CREATE TABLE counters (state TEXT PRIMARY KEY, count INTEGER)")
        connection.executemany("INSERT INTO counters VALUES (?, ?)", [
            ("invoked", 41), ("posted", 35), ("error", 6),
            ("inert_dropped", 0), ("post_unreachable", 0),
        ])
    payload = {"session_id": "session-1", "agent_id": "agent-2", "tool_name": "exec_command",
               "tool_input": {"cmd": "secret command"}, "tool_response": "secret response"}
    _run(tmp_path, url, json.dumps(payload))
    first, = _invocations(tmp_path)
    assert first["session_id"] == "session-1"
    assert first["agent_id"] == "agent-2"
    assert first["tool_name"] == "exec_command"
    assert first["reason_code"] == first["status"] == "posted"
    assert first["started_at"] <= first["finished_at"]
    assert first["elapsed_ms"] >= 0
    _run(tmp_path, url, "{}")
    assert _invocations(tmp_path)[0] == first
    assert _counts(tmp_path) == {"invoked": 43, "posted": 37, "error": 6,
                                 "inert_dropped": 0, "post_unreachable": 0}
    assert "secret" not in json.dumps(_invocations(tmp_path))


def test_diagnostics_reject_paths_and_arbitrary_text(tmp_path, receiver):
    url, _ = receiver
    secret = "/Users/private/secret"
    result = _run(tmp_path, url, json.dumps({
        "session_id": secret, "agent_id": {"secret": secret}, "tool_name": secret,
    }), "bad event\n" + secret)
    row, = _invocations(tmp_path)
    assert all(row[key] == "" for key in ("session_id", "agent_id", "tool_name", "event"))
    assert secret not in result.stderr + json.dumps(row)


@pytest.mark.parametrize("raw,event,reason,exception", [
    ("{", "Stop", "invalid_payload", "JSONDecodeError"),
    ("[]", "Stop", "invalid_payload", "ValueError"),
    ("{}", None, "invalid_event", "ValueError"),
    ('{"tool_name":"update_plan"}', "PostToolUse", "inert_tool", ""),
])
def test_diagnostic_reason(tmp_path, receiver, raw, event, reason, exception):
    url, _ = receiver
    _run(tmp_path, url, raw, event)
    row, = _invocations(tmp_path)
    assert row["reason_code"] == reason
    assert row["exception_class"] == exception


def test_real_http_timeout_classified(tmp_path):
    received = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            received.set()
            release.wait(timeout=5)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _run(tmp_path, f"http://127.0.0.1:{server.server_port}", "{}")
        assert received.is_set()
        row, = _invocations(tmp_path)
        assert row["status"] == "error"
        assert row["reason_code"] == "post_timeout"
        assert row["elapsed_ms"] >= 1400
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("status", [422, 500])
def test_real_http_rejection_classified(tmp_path, status):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status, "private error /Users/private/path")
            self.end_headers()

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run(tmp_path, f"http://127.0.0.1:{server.server_port}", "{}")
        row, = _invocations(tmp_path)
        assert row["status"] == "post_unreachable"
        assert _counts(tmp_path)["post_unreachable"] == 1
        assert row["reason_code"] == f"http_{status}"
        assert "private" not in result.stderr + json.dumps(row)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
