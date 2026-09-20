"""Explicit Codex HTTP startup runs against an isolated home and database."""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/codex_adapter.py"


def _module():
    spec = importlib.util.spec_from_file_location("codex_start", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_start_requires_local_address_and_does_not_guess_port(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.delenv("AITEAM_API_URL", raising=False)
    with pytest.raises(RuntimeError, match="缺少"):
        module.start(ROOT, tmp_path, Path(sys.executable), None)
    with pytest.raises(RuntimeError, match="本机"):
        module.start(ROOT, tmp_path, Path(sys.executable), "https://remote.example/mcp/")


def test_start_refuses_occupied_incompatible_service(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_ready", lambda url: (False, "incompatible"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(RuntimeError, match="端口已被占用"):
            module.start(ROOT, tmp_path, Path(sys.executable), f"http://127.0.0.1:{port}")
        assert listener.fileno() != -1


def test_start_rebinds_while_prior_accepted_socket_is_in_time_wait(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_ready", lambda url: (False, "stopped"))
    with socket.socket() as old:
        old.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        old.bind(("127.0.0.1", 0))
        old.listen()
        port = old.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as peer:
            connection, _ = old.accept()
            connection.close()  # Active close leaves TIME_WAIT on the server side.
            assert peer.recv(1) == b""
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))

    def verify_bound_socket(command, **kwargs):
        with socket.fromfd(kwargs["pass_fds"][0], socket.AF_INET, socket.SOCK_STREAM) as bound:
            assert bound.getsockname() == ("127.0.0.1", port)
            assert bound.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        with socket.socket() as competing:
            competing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                competing.bind(("127.0.0.1", port))
                competing.listen()
        return 0

    monkeypatch.setattr(module.subprocess, "call", verify_bound_socket)
    assert module.start(ROOT, tmp_path, Path(sys.executable), f"http://127.0.0.1:{port}") == 0


@pytest.mark.parametrize("concurrent", [False, True], ids=["single", "concurrent"])
def test_real_initialize_repeated_start_and_helper(tmp_path, concurrent):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = home / ".codex"
    codex_home.mkdir()
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    helper = ROOT / "src/aiteam/mcp/http_headers.py"
    config = f'[mcp_servers.ai-team-os]\nurl = "{url}/mcp/"\n'
    (codex_home / "config.toml").write_text(config)
    env = dict(os.environ, HOME=str(home), CODEX_HOME=str(codex_home),
               AITEAM_DB_PATH=str(tmp_path / "isolated.db"), PYTHONPATH=str(ROOT / "src"),
               FASTMCP_CHECK_FOR_UPDATES="off")
    env.pop("AITEAM_API_URL", None)
    command = [sys.executable, str(SCRIPT), "start"]
    log_path = tmp_path / "service.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=log, start_new_session=True)
    processes = [process]
    race_log = tmp_path / "competing-service.log"
    if concurrent:
        with race_log.open("w") as log:
            processes.append(subprocess.Popen(command, env=env, stdout=log, stderr=log, start_new_session=True))
    try:
        with httpx.Client(base_url=url, trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                assert any(item.poll() is None for item in processes), log_path.read_text()
                try:
                    if client.get("/api/mcp/http-readiness").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail(log_path.read_text()[-5000:])
            assert (tmp_path / "isolated.db").is_file()
            duplicate = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
            assert duplicate.returncode == 0, duplicate.stderr
            assert "复用已运行" in duplicate.stdout
            assert any(item.poll() is None for item in processes)
            if concurrent:
                deadline = time.monotonic() + 5
                while sum(item.poll() is None for item in processes) > 1 and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert sum(item.poll() is None for item in processes) == 1
                combined = log_path.read_text() + race_log.read_text()
                assert combined.count("Application startup complete.") == 1, combined
            header_run = subprocess.run([sys.executable, str(helper), "--api-url", url],
                                        env=env, capture_output=True, text=True, timeout=10)
            assert header_run.returncode == 0, header_run.stderr
            headers = json.loads(header_run.stdout)
            headers["Accept"] = "application/json, text/event-stream"
            response = client.post("/mcp/", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "codex-start-test", "version": "1"}},
            })
            response.raise_for_status()
            if response.headers.get("content-type", "").startswith("application/json"):
                result = response.json()
            else:
                result = json.loads(next(line[5:] for line in response.text.splitlines() if line.startswith("data:")))
            assert result["result"]["serverInfo"]["name"] == "ai-team-os"
            session = response.headers["mcp-session-id"]
            client.delete("/mcp/", headers={**headers, "Mcp-Session-Id": session})
    finally:
        for item in processes:
            if item.poll() is None:
                os.killpg(item.pid, signal.SIGINT)
                item.wait(timeout=15)
