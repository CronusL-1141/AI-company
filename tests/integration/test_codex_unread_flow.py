"""Exercise the Codex entry against the committed core over real local HTTP."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "plugin/harness/codex/hooks/channel_unread_codex.py"


def _additional_context(stdout: str) -> str:
    document = json.loads(stdout)
    assert set(document) == {"hookSpecificOutput"}
    output = document["hookSpecificOutput"]
    assert set(output) == {"hookEventName", "additionalContext"}
    assert output["hookEventName"] == "UserPromptSubmit"
    assert isinstance(output["additionalContext"], str)
    assert output["additionalContext"]
    return output["additionalContext"]


@pytest.fixture
def live_core(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AITEAM_DB_PATH": str(tmp_path / "core.db"),
        "AITEAM_API_URL": base,
        "AITEAM_UNREAD_AUDIT_PATH": str(tmp_path / "unread-audit.jsonl"),
        "TMPDIR": str(tmp_path),
        "NO_PROXY": "127.0.0.1,localhost",
    }
    env.pop("AITEAM_HOOK_RAW_DUMP", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("Isolated core exited before health")
                try:
                    if client.get("/api/health").json().get("status") == "ok":
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Isolated core health timeout")
            yield client, env
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _tool(env, name, arguments):
    code = """
import asyncio, json, sys
from fastmcp import Client
from aiteam.mcp.server import mcp
async def run():
    name, arguments = json.load(sys.stdin)
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
        print(json.dumps(result.data))
asyncio.run(run())
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=ROOT,
        input=json.dumps([name, arguments]), text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_notice_read_and_ack_preserve_project_and_reader_boundaries(live_core, tmp_path):
    client, env = live_core
    reader = "leader-codex"
    channel = "team:codex-unread-e2e"
    roots = [tmp_path / "project-a", tmp_path / "project-b"]
    projects = []
    for index, root in enumerate(roots):
        root.mkdir()
        response = client.post("/api/projects", json={
            "name": f"unread-test-{index}", "root_path": str(root),
        })
        response.raise_for_status()
        projects.append(response.json()["data"]["id"])

    def send(project, mention, content):
        response = client.post(f"/api/channels/{channel}/messages", json={
            "sender": "leader-cc", "content": content, "mentions": [mention],
            "project_id": project,
        })
        response.raise_for_status()
        return response.json()["data"]

    first = send(projects[0], reader, "first-local-message")
    second = send(projects[0], "@" + reader, "latest-local-message")
    send(projects[0], reader + "-other", "wrong-reader-message")
    send(projects[1], reader, "wrong-project-message")
    send(None, reader, "unscoped-message")

    def unread(project):
        return _tool(env, "channel_unread", {"reader": reader, "project_id": project})["data"]["total"]

    def run_hook(project=None, cwd=roots[0]):
        args = [sys.executable, str(HOOK), reader]
        if project is not None:
            args.append(project)
        result = subprocess.run(
            args, input=json.dumps({"cwd": str(cwd)}), text=True,
            capture_output=True, env=env, cwd=ROOT, timeout=4,
        )
        assert result.returncode == 0, result.stderr
        return result

    assert unread(projects[0]) == unread(projects[0]) == 2
    assert unread(projects[1]) == 1
    notice = _additional_context(run_hook(projects[0], roots[1]).stdout)
    for value in (channel, reader, projects[0], "latest-local-message", "channel_read_ack"):
        assert value in notice
    for value in ("wrong-reader-message", "wrong-project-message", "unscoped-message"):
        assert value not in notice
    assert _additional_context(run_hook().stdout) == notice
    assert unread(projects[0]) == 2

    page = _tool(env, "channel_read", {"channel": channel, "limit": 1})["data"]
    assert page[0]["id"] == first["id"]
    assert unread(projects[0]) == 2
    ack = {"channel": channel, "reader": reader, "project_id": projects[0],
           "last_read_at": page[-1]["created_at"]}
    assert _tool(env, "channel_read_ack", ack)["data"]["advanced"] is True
    assert unread(projects[0]) == 1
    assert "信道未读 1 条" in _additional_context(run_hook(projects[0]).stdout)
    assert _tool(env, "channel_read_ack", ack)["data"]["advanced"] is False

    remaining = _tool(env, "channel_read", {
        "channel": channel, "since": first["created_at"], "limit": 1,
    })["data"]
    assert remaining[0]["id"] == second["id"]
    ack["last_read_at"] = remaining[-1]["created_at"]
    assert _tool(env, "channel_read_ack", ack)["data"]["advanced"] is True
    assert unread(projects[0]) == 0
    cleared = run_hook(projects[0])
    assert cleared.stdout == cleared.stderr == ""
    assert unread(projects[1]) == 1
    missing = run_hook(cwd=tmp_path / "unbound-worktree")
    assert missing.stdout == ""
    assert "missing_project_binding" in missing.stderr
