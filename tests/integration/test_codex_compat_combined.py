"""Combine project events, unread hooks and diagnostics in one real API process."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
READER = "combined-reader"
SCRIPTS = {
    "cc": ROOT / "src/aiteam/hooks/channel_unread.py",
    "codex": ROOT / "plugin/harness/codex/hooks/channel_unread_codex.py",
}
SERVER = """
import asyncio
import socket
import sys
from pathlib import Path

import uvicorn
from aiteam.api.app import create_app
from aiteam.api.routes import team_config

team_config.CONFIG_DIR = Path.cwd() / "config"
team_config.CONFIG_FILE = team_config.CONFIG_DIR / "team-defaults.json"
listener = socket.socket(fileno=int(sys.argv[1]))
server = uvicorn.Server(uvicorn.Config(
    create_app(), host="127.0.0.1", port=listener.getsockname()[1],
    log_level="info", lifespan="on", loop="asyncio",
))
asyncio.run(server.serve(sockets=[listener]))
"""


def _assert_no_diagnostic_text(value: Any, *forbidden: str) -> None:
    """Inspect decoded strings so JSON escaping cannot conceal quoted text."""
    if isinstance(value, str):
        for fragment in forbidden:
            assert fragment not in value
    elif isinstance(value, dict):
        for key, child in value.items():
            _assert_no_diagnostic_text(key, *forbidden)
            _assert_no_diagnostic_text(child, *forbidden)
    elif isinstance(value, list):
        for child in value:
            _assert_no_diagnostic_text(child, *forbidden)


@pytest.mark.parametrize("text", [
    'Editor "Alpha"', r'Task "combined-task": notes\daily.txt.',
])
def test_diagnostic_text_check_rejects_quoted_values_and_keys(text: str) -> None:
    for record in (
        {"event": "http.server.response", "metadata": {"note": [f"Received: {text}"]}},
        {"event": "http.server.response", "metadata": {text: "ordinary value"}},
    ):
        decoded = json.loads(json.dumps([record]))
        with pytest.raises(AssertionError):
            _assert_no_diagnostic_text(decoded, text)


@contextmanager
def _isolated_api(
    directory: Path,
) -> Iterator[tuple[str, subprocess.Popen[str], dict[str, str]]]:
    # Inherit only the runtime path, not host proxy, session or credential settings.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(directory), "TMPDIR": str(directory),
            "TMP": str(directory), "TEMP": str(directory),
            "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
            "AITEAM_API_URL": url, "AITEAM_DB_PATH": str(directory / "aiteam.db"),
            "AITEAM_DIAGNOSTICS_ENABLED": "1",
            "AITEAM_DIAGNOSTICS_DIR": str(directory / "diagnostics"),
            "XDG_STATE_HOME": str(directory / "state"), "NO_PROXY": "*",
        }
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", SERVER, str(listener.fileno())],
            pass_fds=(listener.fileno(),), env=environment, cwd=directory,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                pytest.fail(f"Isolated API exited before readiness: {stdout}\n{stderr}")
            try:
                with opener.open(f"{url}/api/projects", timeout=0.2) as response:
                    assert response.status == 200
                    assert json.load(response)["data"] == []
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("Isolated API did not become ready")
        yield url, process, environment
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"Isolated API did not stop: {stdout}\n{stderr}")
        assert process.poll() is not None
        assert process.returncode in (0, -int(signal.SIGTERM)), (stdout, stderr)


def _hook_context(
    adapter: str, project_id: str, directory: Path, environment: dict[str, str],
) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS[adapter]), READER, project_id],
        input=json.dumps({"cwd": str(directory)}), text=True, capture_output=True,
        timeout=5, cwd=directory, env={
            **environment,
            "AITEAM_DIAGNOSTICS_ENABLED": "0",
            "AITEAM_UNREAD_AUDIT_PATH": str(directory / f"{adapter}-unread.jsonl"),
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    if adapter == "cc" or not result.stdout:
        return result.stdout.removesuffix("\n")
    assert len(result.stdout.splitlines()) == 1
    document = json.loads(result.stdout)
    assert set(document) == {"hookSpecificOutput"}
    output = document["hookSpecificOutput"]
    assert set(output) == {"hookEventName", "additionalContext"}
    assert output["hookEventName"] == "UserPromptSubmit"
    return output["additionalContext"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited socket and signal fixture")
def test_project_events_and_unread_hooks_share_correlated_api_lifecycle(tmp_path, monkeypatch):
    from aiteam.diagnostics import flush_diagnostics
    from aiteam.mcp import _base

    diagnostics = tmp_path / "diagnostics"
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_DIR", str(diagnostics))
    monkeypatch.setenv("AITEAM_DIAGNOSTICS_ENABLED", "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setattr(_base, "PROJECT_DIR", "")
    monkeypatch.setattr(_base, "_session_project_id", "")
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
    ))

    def call(method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        result = _base._api_call(method, path, data)
        assert result["success"] is True, result
        return result

    with _isolated_api(tmp_path) as (url, process, environment):
        monkeypatch.setenv("AITEAM_API_URL", url)
        projects, tasks = {}, {}
        for name in ("owner", "other"):
            project_dir = tmp_path / name
            project_dir.mkdir()
            projects[name] = call("POST", "/api/projects", {
                "name": name, "root_path": str(project_dir),
            })["data"]["id"]
            tasks[name] = call("POST", f"/api/projects/{projects[name]}/tasks", {
                "title": f'{name} task "alpha"',
            })["data"]["id"]
            updated = call("PUT", f"/api/tasks/{tasks[name]}", {"status": "running"})["data"]
            assert updated["team_id"] is None
            persisted = call("GET", f"/api/tasks/{tasks[name]}")["data"]
            assert persisted["status"] == "running"
            assert persisted["project_id"] == projects[name]

        owner, other = projects["owner"], projects["other"]
        feeds = {}
        for name, project_id in projects.items():
            feed = call("GET", f"/api/events?project_id={project_id}")
            assert feed["total"] == len(feed["data"]) == 2
            assert {event["type"] for event in feed["data"]} == {
                "task.updated", "task.status_changed",
            }
            assert all(
                (event["entity_id"] or event["data"]["task_id"]) == tasks[name]
                for event in feed["data"]
            )
            feeds[name] = {event["id"] for event in feed["data"]}
        assert feeds["owner"].isdisjoint(feeds["other"])
        excluded = call("GET", f"/api/events?project_id={other}&entity_id={tasks['owner']}")
        assert excluded["data"] == [] and excluded["total"] == 0

        channel = "team:" + "daily-review-" * 8
        sender = 'Editor "Alpha"'
        content = f'Task "{tasks["owner"]}": notes\\daily.txt.'
        message = call("POST", f"/api/channels/{channel}/messages", {
            "sender": sender, "content": content, "mentions": [READER], "project_id": owner,
        })["data"]
        call("POST", f"/api/channels/{channel}/messages", {
            "sender": "Other editor", "content": "Other project notes.",
            "mentions": [READER], "project_id": other,
        })
        unread_path = f"/api/channels/unread?reader={READER}&project_id={owner}"
        unread = call("GET", unread_path)["data"]
        assert unread["total"] == 1 and unread["truncated"] is False
        assert unread["channels"][0]["channel"] == channel
        assert unread["channels"][0]["latest_sender"] == sender
        assert unread["channels"][0]["latest_excerpt"] == content

        binding = {"channel": channel, "reader": READER, "project_id": owner}
        for adapter in SCRIPTS:
            context = _hook_context(adapter, owner, tmp_path, environment)
            assert json.dumps(sender) in context
            assert json.dumps(content) in context
            assert "Other project notes." not in context
            if adapter == "cc":
                assert f"channel_read(channel={json.dumps(channel)})" in context
                arguments = ", ".join(f"{key}={json.dumps(value)}" for key, value in binding.items())
                assert f"channel_read_ack({arguments}, last_read_at=" in context
            else:
                assert context.count("参数=") == 1
                actual, _ = json.JSONDecoder().raw_decode(context.partition("参数=")[2])
                assert actual == binding

        # A hook notification is not an ACK; read the persisted message before advancing.
        assert call("GET", unread_path)["data"]["total"] == 1
        messages = call("GET", f"/api/channels/{channel}/messages")["data"]
        received = next(item for item in messages if item["id"] == message["id"])
        assert received["project_id"] == owner and received["content"] == content
        ack = {"reader": READER, "project_id": owner, "last_read_at": received["created_at"]}
        cursor_path = f"/api/channels/{channel}/read-cursor"
        assert call("POST", cursor_path, ack)["data"]["advanced"] is True
        assert call("POST", cursor_path, ack)["data"]["advanced"] is False
        assert call("GET", unread_path)["data"]["total"] == 0
        assert call("GET", f"/api/channels/unread?reader={READER}&project_id={other}")["data"]["total"] == 1
        for adapter in SCRIPTS:
            assert _hook_context(adapter, owner, tmp_path, environment) == ""

    assert flush_diagnostics(timeout=2)
    records = [
        json.loads(line) for path in sorted(diagnostics.glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    started = [record for record in records if record["event"] == "http.client.started"]
    assert started
    assert len({record["request_id"] for record in started}) == len(started)
    for request in started:
        correlated = [record for record in records if record.get("request_id") == request["request_id"]]
        assert {record["event"] for record in correlated} == {
            "http.client.started", "http.client.completed", "http.server.received", "http.server.response",
        }
        assert len(correlated) == 4
        client = next(record for record in correlated if record["event"] == "http.client.completed")
        server = next(record for record in correlated if record["event"] == "http.server.response")
        assert client["pid"] == os.getpid() and server["pid"] == process.pid
        assert client["status"] == server["status"] and 200 <= server["status"] < 300
        assert client["peer"] == {"host": "127.0.0.1", "port": int(url.rsplit(":", 1)[1])}
    assert any(
        record["method"] == "PUT" and record["url"] == f"{url}/api/tasks/{tasks['owner']}"
        for record in started
    )
    lifecycle = [record for record in records if record["event"].startswith("api.")]
    assert [record["event"] for record in lifecycle] == [
        "api.startup.begin", "api.startup.complete", "api.signal.received",
        "api.shutdown.begin", "api.shutdown.complete",
    ]
    assert all(record["pid"] == process.pid for record in lifecycle)
    assert lifecycle[2]["signal"] == "SIGTERM"
    assert lifecycle[2]["sender_pid"] == "unknown"
    _assert_no_diagnostic_text(records, sender, content)
