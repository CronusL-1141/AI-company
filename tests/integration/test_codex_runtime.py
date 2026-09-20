"""Cold-start lifecycle and crash recovery, never using the developer's DB."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "scripts/codex_runtime.py"
HELPER = ROOT / "src/aiteam/mcp/http_headers.py"


@pytest.fixture
def isolated(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / ".codex"),
               AITEAM_DB_PATH=str(tmp_path / "isolated.db"), FASTMCP_CHECK_FOR_UPDATES="off")
    env.pop("AITEAM_API_URL", None)
    base = [sys.executable, str(RUNTIME)]
    options = ["--api-url", url, "--runtime-dir", str(runtime)]
    helper = [sys.executable, str(HELPER), "--api-url", url, "--runtime-script", str(RUNTIME),
              "--runtime-dir", str(runtime)]
    info = dict(tmp=tmp_path, home=home, runtime=runtime, url=url, env=env, base=base,
                options=options, helper=helper)
    yield info
    result = subprocess.run(base + ["stop"] + options, env=env, capture_output=True, text=True, timeout=15)
    if (runtime / "api.json").exists():
        pytest.fail(f"owned runtime cleanup failed: {result.stderr}")


def run(info, command="ensure", **kwargs):
    return subprocess.run(info["base"] + [command] + info["options"], env=info["env"],
                          capture_output=True, text=True, timeout=40, **kwargs)


def api_record(info):
    return json.loads((info["runtime"] / "api.json").read_text())


def initialize(info, headers):
    headers = dict(headers, Accept="application/json, text/event-stream")
    with httpx.Client(trust_env=False, timeout=5) as client:
        response = client.post(info["url"] + "/mcp/", headers=headers,
                               json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                                   "protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "restart-test", "version": "1"}}})
        response.raise_for_status()
        assert "ai-team-os" in response.text


def test_cold_helper_exit_reconnect_and_stop_restart(isolated):
    info = isolated
    first = subprocess.run(info["helper"], env=info["env"], cwd=info["tmp"],
                           capture_output=True, text=True, timeout=40)
    assert first.returncode == 0, first.stderr
    headers = json.loads(first.stdout)  # stdout contains only the connection headers.
    record = api_record(info)
    assert psutil.pid_exists(record["pid"])
    assert os.getsid(record["pid"]) == record["pid"]
    initialize(info, headers)
    second = subprocess.run(info["helper"], env=info["env"], cwd=info["tmp"],
                            capture_output=True, text=True, timeout=40)
    assert second.returncode == 0, second.stderr
    assert api_record(info) == record
    initialize(info, json.loads(second.stdout))
    assert run(info, "stop").returncode == 0
    assert not psutil.pid_exists(record["pid"])
    third = subprocess.run(info["helper"], env=info["env"], cwd=info["tmp"],
                           capture_output=True, text=True, timeout=40)
    assert third.returncode == 0, third.stderr
    assert api_record(info)["pid"] != record["pid"]
    initialize(info, json.loads(third.stdout))


def test_48_competing_cold_helpers_launch_one_api(isolated):
    info = isolated

    async def exercise():
        async def one():
            child = await asyncio.create_subprocess_exec(*info["helper"], env=info["env"], cwd=info["tmp"],
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(child.communicate(), 45)
            assert child.returncode == 0, err.decode()
            return json.loads(out), api_record(info)["pid"]
        return await asyncio.gather(*(one() for _ in range(48)))

    results = asyncio.run(exercise())
    assert len({pid for _, pid in results}) == 1
    assert len({json.dumps(headers) for headers, _ in results}) == 1
    initialize(info, results[0][0])


@pytest.mark.parametrize("signum", [signal.SIGKILL, signal.SIGTERM, signal.SIGINT],
                         ids=["kill", "terminate-group", "interrupt-group"])
def test_interrupted_launcher_leaves_registered_service_recoverable(isolated, signum):
    info = isolated
    child = subprocess.Popen(info["base"] + ["ensure"] + info["options"], env=info["env"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not (info["runtime"] / "api.json").exists():
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.005)
        record = api_record(info)
        os.killpg(child.pid, signum)
        child.wait(timeout=5)
        resumed = run(info)
        assert resumed.returncode == 0, resumed.stderr
        assert api_record(info) == record
        initialize(info, {})
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_wrong_identity_never_signals_existing_service(isolated):
    info = isolated
    assert run(info).returncode == 0
    path = info["runtime"] / "api.json"
    original = path.read_text()
    bad = json.loads(original)
    bad["created"] -= 100
    path.write_text(json.dumps(bad))
    try:
        stopped = run(info, "stop")
        assert stopped.returncode != 0
        assert psutil.pid_exists(bad["pid"])
        initialize(info, {})
    finally:
        path.write_text(original)


def test_foreign_port_is_not_terminated_or_claimed(isolated):
    info = isolated
    port = int(info["url"].rsplit(":", 1)[1])
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", port))
        foreign.listen()
        result = run(info)
        assert result.returncode != 0
        assert "占用" in result.stderr
        assert foreign.fileno() != -1
        assert not (info["runtime"] / "api.json").exists()


def test_another_runtime_reuses_but_cannot_stop_foreign_healthy_api(isolated):
    info = isolated
    assert run(info).returncode == 0
    record = api_record(info)
    other = dict(info, options=["--api-url", info["url"], "--runtime-dir", str(info["tmp"] / "other")])
    result = run(other)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["api"] == "reused"
    assert run(other, "stop").returncode == 0
    assert psutil.pid_exists(record["pid"])
    initialize(info, {})


def test_stale_pid_reuse_does_not_block_start_or_signal_unrelated_process(isolated):
    info = isolated
    info["runtime"].mkdir()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process = psutil.Process(other.pid)
        stale = {"pid": other.pid, "created": process.create_time() - 100, "uid": os.getuid(),
                 "command": ["old-api"], "target": info["url"]}
        (info["runtime"] / "api.json").write_text(json.dumps(stale))
        result = run(info)
        assert result.returncode == 0, result.stderr
        assert other.poll() is None
        assert api_record(info)["pid"] != other.pid
        initialize(info, {})
    finally:
        other.terminate()
        other.wait(timeout=5)


def test_api_recovers_after_owned_process_is_killed(isolated):
    info = isolated
    assert run(info).returncode == 0
    api = api_record(info)
    process = psutil.Process(api["pid"])
    process.kill()
    process.wait(timeout=10)
    recovered = run(info)
    assert recovered.returncode == 0 and json.loads(recovered.stdout)["api"] == "started", recovered.stderr
    assert api_record(info)["pid"] != api["pid"]
    initialize(info, {})


def test_updated_source_requires_restart_and_new_process_serves_new_version(isolated):
    info = isolated
    checkout = info["tmp"] / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(RUNTIME, checkout / "scripts/codex_runtime.py")
    shutil.copytree(ROOT / "src/aiteam", checkout / "src/aiteam",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    updated = dict(info, base=[sys.executable, str(checkout / "scripts/codex_runtime.py")])
    assert run(updated).returncode == 0
    first = api_record(info)
    initial = json.loads(run(updated, "status").stdout)
    assert initial["restart_required"] is False
    with httpx.Client(trust_env=False, timeout=5) as client:
        old_version = client.get(info["url"] + "/api/health").json()["version"]
        version_file = checkout / "src/aiteam/__init__.py"
        version_file.write_text(version_file.read_text() + '\n__version__ = "0.0.0-acceptance"\n')
        changed = json.loads(run(updated, "status").stdout)
        assert changed["source_changed"] is True and changed["restart_required"] is True
        assert client.get(info["url"] + "/api/health").json()["version"] == old_version
        assert api_record(info)["pid"] == first["pid"]  # Inspection must never restart a shared API.
        assert run(updated, "stop").returncode == 0
        assert run(updated).returncode == 0
        assert api_record(info)["pid"] != first["pid"]
        assert client.get(info["url"] + "/api/health").json()["version"] == "0.0.0-acceptance"
        assert json.loads(run(updated, "status").stdout)["restart_required"] is False


def test_legacy_process_record_without_source_fingerprint_is_unknown(isolated):
    info = isolated
    assert run(info).returncode == 0
    record = api_record(info)
    record.pop("source_fingerprint")
    (info["runtime"] / "api.json").write_text(json.dumps(record))
    state = json.loads(run(info, "status").stdout)
    assert state["api_owned"] is True and state["api_ready"] is True
    assert state["source_changed"] is None and state["restart_required"] is None


@pytest.mark.skipif(not os.environ.get("AITEAM_TEST_CODEX_BINARY"), reason="native Codex binary opt-in required")
def test_native_codex_cold_start_and_reopen_without_model_turn(isolated):
    info = isolated
    home = Path(info["env"]["CODEX_HOME"])
    home.mkdir()
    helper = shlex.join(info["helper"])
    (home / "config.toml").write_text(
        '[mcp_servers.ai-team-os]\n'
        f'url = "{info["url"]}/mcp/"\nhttp_headers_helper = {json.dumps(helper)}\nstartup_timeout_sec = 45\n'
    )

    async def connect_once():
        process = await asyncio.create_subprocess_exec(
            os.environ["AITEAM_TEST_CODEX_BINARY"], "app-server", "--stdio", env=info["env"],
            cwd=info["tmp"], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024,
        )

        async def request(number, method, params):
            process.stdin.write((json.dumps({"id": number, "method": method, "params": params}) + "\n").encode())
            await process.stdin.drain()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 45)
                assert line, "native app-server exited"
                data = json.loads(line)
                if data.get("id") == number:
                    assert "error" not in data, data.get("error")
                    return data["result"]

        try:
            await request(1, "initialize", {"clientInfo": {"name": "os-cold-start-test", "version": "1"},
                                            "capabilities": {"experimentalApi": True}})
            process.stdin.write(b'{"method":"initialized"}\n')
            await process.stdin.drain()
            thread = await request(2, "thread/start", {"cwd": str(info["tmp"]), "ephemeral": True,
                                                       "model": "codex-model-test", "approvalPolicy": "never",
                                                       "sandbox": "read-only"})
            thread_id = thread["thread"]["id"]
            status = await request(3, "mcpServerStatus/list", {"threadId": thread_id, "detail": "toolsAndAuthOnly"})
            server = next(row for row in status["data"] if row["name"] == "ai-team-os")
            assert "os_health_check" in server["tools"]
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                projects = (await client.get(info["url"] + "/api/projects")).json()["data"]
                if not projects:
                    created = await client.post(info["url"] + "/api/projects",
                                                json={"name": "Native startup", "root_path": str(info["tmp"])})
                    created.raise_for_status()
            tool = await request(4, "mcpServer/tool/call", {"threadId": thread_id, "server": "ai-team-os",
                                                           "tool": "os_health_check", "arguments": {}})
            assert tool.get("isError") is not True
            assert tool["structuredContent"]["status"] == "healthy"
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.terminate()
                await process.wait()

    asyncio.run(connect_once())
    original = api_record(info)
    assert psutil.pid_exists(original["pid"])
    asyncio.run(connect_once())
    assert api_record(info) == original
