"""Release-to-candidate and native Codex acceptance, using no existing home or credentials."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "scripts/codex_adapter.py"
RUNTIME = ROOT / "scripts/codex_runtime.py"
RELEASE_REF = "refs/tags/v1.13.1"
RELEASE_SHA = "dd60e97eaad7e30f5e728ad9fc097f57f8a4bc88"


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "empty-home"
    home.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    codex = home / ".codex"
    runtime = codex / "ai-team-os/runtime"
    # Deliberately do not inherit login variables, provider keys, DB settings or proxy config.
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TMPDIR") if key in os.environ}
    env.update(HOME=str(home), CODEX_HOME=str(codex), AITEAM_DB_PATH=str(tmp_path / "isolated.db"),
               PYTHONPATH=str(ROOT / "src"), FASTMCP_CHECK_FOR_UPDATES="off")
    info = dict(tmp=tmp_path, home=home, codex=codex, runtime=runtime, url=url, env=env)
    yield info
    if runtime.exists():
        result = subprocess.run([sys.executable, str(RUNTIME), "stop", "--api-url", url,
                                 "--runtime-dir", str(runtime)], env=env, capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert not (runtime / "api.json").exists()


def adapter(info, command, *args):
    script = info.get("adapter", ADAPTER)
    return subprocess.run([sys.executable, str(script), command, "--api-url", info["url"], *args],
                          env=info["env"], cwd=info["tmp"], capture_output=True, text=True, timeout=40)


def install(info):
    result = adapter(info, "install")
    assert result.returncode == 0, result.stderr
    return tomllib.loads((info["codex"] / "config.toml").read_text())["mcp_servers"]["ai-team-os"]


def helper(info, server):
    result = subprocess.run(shlex.split(server["http_headers_helper"]), env=info["env"], cwd=info["tmp"],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def source_archive(info):
    """The source ZIP layout deliberately has no .git and no access to local refs."""
    archive = info["tmp"] / "candidate-archive"
    for relative in ("scripts", "src/aiteam", "plugin/harness/codex"):
        shutil.copytree(ROOT / relative, archive / relative,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    assert not (archive / ".git").exists()
    info["adapter"] = archive / "scripts/codex_adapter.py"
    return archive


@pytest.mark.parametrize("archive_install", [False, True], ids=["git-checkout", "source-zip"])
def test_empty_home_complete_install_http_call_update_and_uninstall(sandbox, archive_install):
    info = sandbox
    if archive_install:
        source_archive(info)
    assert not info["codex"].exists()
    server = install(info)
    assert not info["runtime"].exists(), "installation must not start a service"
    assert (info["codex"] / "hooks/ai-team-os-observer/hook_core.py").is_file()
    headers = helper(info, server)
    assert headers["X-Aiteam-Project-Dir"]
    with httpx.Client(base_url=info["url"], trust_env=False) as client:
        project = client.post("/api/projects", json={"name": "Persist across uninstall",
                                                    "root_path": str(info["tmp"])}).json()["data"]
        assert project["name"] == "Persist across uninstall"
        before = (info["runtime"] / "api.json").read_bytes()
        config = (info["codex"] / "config.toml").read_bytes()
        updated = subprocess.run([sys.executable, str(info.get("adapter", ADAPTER)), "update"],
                                 env=info["env"], cwd=info["tmp"], capture_output=True, text=True, timeout=40)
        assert updated.returncode == 0, updated.stderr  # No URL argument or inherited API environment.
        assert (info["runtime"] / "api.json").read_bytes() == before
        assert (info["codex"] / "config.toml").read_bytes() == config
        auth = info["codex"] / "auth.json"
        auth.write_text('{"sentinel":"not-real-credentials"}')
        extra = info["codex"] / "user-data.txt"
        extra.write_text("keep")
        hooks = info["codex"] / "hooks.json"
        document = json.loads(hooks.read_text())
        document["hooks"].setdefault("PreToolUse", []).insert(0, {
            "hooks": [{"type": "command", "command": "/third-party/guard"}]})
        hooks.write_text(json.dumps(document))
        assert adapter(info, "uninstall", "--apply").returncode == 0
        assert not (info["codex"] / "hooks/ai-team-os-observer").exists()
        assert not (info["codex"] / "bin/aiteam-http-headers.py").exists()
        assert "ai-team-os" not in tomllib.loads((info["codex"] / "config.toml").read_text()).get("mcp_servers", {})
        assert json.loads(hooks.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/third-party/guard"
        assert auth.read_text() == '{"sentinel":"not-real-credentials"}'
        assert extra.read_text() == "keep"
        assert any(row["id"] == project["id"] for row in client.get("/api/projects").json()["data"])
        assert (info["runtime"] / "api.json").read_bytes() == before


@pytest.mark.parametrize(("archive_install", "hooks_only"), [(False, False), (True, False), (True, True)],
                         ids=["git-checkout", "source-zip", "source-zip-existing-stdio"])
def test_real_published_no_receipt_upgrade_keeps_custom_registration(sandbox, archive_install, hooks_only):
    info = sandbox
    if archive_install:
        source_archive(info)
    revision = subprocess.run(["git", "rev-parse", f"{RELEASE_REF}^{{commit}}"], cwd=ROOT,
                              capture_output=True, text=True)
    if revision.returncode:
        pytest.skip("release migration requires the local v1.13.1 Git tag")
    assert revision.stdout.strip() == RELEASE_SHA
    manifest = json.loads((ROOT / "plugin/harness/codex/legacy-installed-hashes.json").read_text())
    release = next(item for item in manifest["releases"] if item["version"] == "v1.13.1")
    assert release["source_commit"] == RELEASE_SHA
    for name, digest in release["sha256"].items():
        content = subprocess.check_output(["git", "show", f"{RELEASE_SHA}:{name}"], cwd=ROOT)
        assert hashlib.sha256(content).hexdigest() == digest
    archive = subprocess.run(["git", "archive", RELEASE_SHA, "plugin/harness/codex"], cwd=ROOT,
                             capture_output=True, check=True).stdout
    old = info["tmp"] / "published-v1.13.1"
    old.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(old, filter="data")
    surface_path = old / "plugin/harness/codex/surface.py"
    spec = importlib.util.spec_from_file_location("published_surface", surface_path)
    surface = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(surface)
    observer = info["codex"] / "hooks/ai-team-os-observer"
    observer.mkdir(parents=True)
    for path in (surface_path.parent / "hooks").glob("*.py"):
        shutil.copy2(path, observer / path.name)
    assert not (observer / ".aiteam-codex-install.json").exists()
    manifest = surface.render_manifest()
    for groups in manifest["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                tokens = shlex.split(handler["command"])
                tokens = [token.replace(surface.PLACEHOLDER_PY, sys.executable)
                          .replace(surface.PLACEHOLDER_HOOKS_DIR, str(observer)) for token in tokens]
                handler["command"] = shlex.join(tokens)
                handler.pop("command_windows", None)
    custom = manifest["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    custom["command"] = custom["command"].replace("leader-codex", "release-reader") + " pinned-project"
    custom["timeout"] = 7
    hooks = info["codex"] / "hooks.json"
    hooks.write_text(json.dumps(manifest))
    # v1.13.1 documented manual HTTP integration and did not ship this installer/helper.
    config = info["codex"] / "config.toml"
    connection = ('command = "python3"\nargs = ["-m", "aiteam.mcp.server"]\n' if hooks_only
                  else f'url = "{info["url"]}/mcp/"\n')
    config.write_text('model = "user-model"\n[mcp_servers.third-party]\ncommand = "preserve"\n'
                      '[mcp_servers.ai-team-os]\n' + connection + 'startup_timeout_sec = 19\n')
    config_before = config.read_bytes()
    options = ["--hooks-only"] if hooks_only else []
    before_hook = (observer / "session_bootstrap_codex.py").read_bytes()
    assert before_hook != (ROOT / "plugin/harness/codex/hooks/session_bootstrap_codex.py").read_bytes()
    result = adapter(info, "update", *options)
    assert result.returncode == 0, result.stderr
    assert json.loads(hooks.read_text()) == manifest
    migrated = tomllib.loads(config.read_text())
    assert migrated["model"] == "user-model"
    assert migrated["mcp_servers"]["third-party"] == {"command": "preserve"}
    assert migrated["mcp_servers"]["ai-team-os"]["startup_timeout_sec"] == 19
    if hooks_only:
        assert config.read_bytes() == config_before
        assert not (info["codex"] / "bin/aiteam-http-headers.py").exists()
    receipt = json.loads((observer / ".aiteam-codex-install.json").read_text())
    assert (receipt["source_commit"] is None) == archive_install
    assert (observer / "session_bootstrap_codex.py").read_bytes() != before_hook
    # New hook process imports the full installed dependency set from the upgraded copy.
    run = subprocess.run([sys.executable, str(observer / "session_bootstrap_codex.py")],
                         input=json.dumps({"cwd": str(info["tmp"])}),
                         env={**info["env"], "AITEAM_API_URL": info["url"]},
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert adapter(info, "update", *options).returncode == 0
    assert json.loads(hooks.read_text()) == manifest


@pytest.mark.skipif(not os.environ.get("AITEAM_TEST_CODEX_BINARY"), reason="native Codex binary opt-in required")
def test_native_new_and_reopened_sessions_discover_116_and_call_persisted_project(sandbox):
    info = sandbox
    server = install(info)
    assert not info["runtime"].exists()

    async def connect(*, installed=True):
        process = await asyncio.create_subprocess_exec(
            os.environ["AITEAM_TEST_CODEX_BINARY"], "app-server", "--stdio", env=info["env"],
            cwd=info["tmp"], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024,
        )

        async def request(number, method, params):
            process.stdin.write((json.dumps({"id": number, "method": method, "params": params}) + "\n").encode())
            await process.stdin.drain()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 50)
                assert line, "native app-server exited"
                data = json.loads(line)
                if data.get("id") == number:
                    assert "error" not in data, data.get("error")
                    return data["result"]

        try:
            await request(1, "initialize", {"clientInfo": {"name": "os-release-acceptance", "version": "1"},
                                           "capabilities": {"experimentalApi": True}})
            process.stdin.write(b'{"method":"initialized"}\n')
            await process.stdin.drain()
            started = await request(2, "thread/start", {"cwd": str(info["tmp"]), "ephemeral": True,
                                                       "model": "codex-model-test", "approvalPolicy": "never",
                                                       "sandbox": "read-only"})
            thread_id = started["thread"]["id"]
            status = await request(3, "mcpServerStatus/list", {"threadId": thread_id, "detail": "toolsAndAuthOnly"})
            if not installed:
                assert not any(row["name"] == "ai-team-os" for row in status["data"])
                print(f"native release thread={thread_id}; uninstall=no OS MCP")
                return thread_id
            entry = next(row for row in status["data"] if row["name"] == "ai-team-os")
            assert len(entry["tools"]) == 116, list(entry["tools"])
            async with httpx.AsyncClient(base_url=info["url"], trust_env=False) as client:
                projects = (await client.get("/api/projects")).json()["data"]
                if not projects:
                    result = await client.post("/api/projects", json={
                        "name": "Native release", "root_path": str(info["tmp"])})
                    result.raise_for_status()
            tool = await request(4, "mcpServer/tool/call", {"threadId": thread_id, "server": "ai-team-os",
                                                           "tool": "os_health_check", "arguments": {}})
            assert tool.get("isError") is not True, tool
            assert tool["structuredContent"]["status"] == "healthy"
            readback = await request(5, "mcpServer/tool/call", {"threadId": thread_id, "server": "ai-team-os",
                                                               "tool": "project_list", "arguments": {}})
            assert readback.get("isError") is not True, readback
            assert any(row["name"] == "Native release" for row in readback["structuredContent"]["data"])
            print(f"native release thread={thread_id}; discovered={len(entry['tools'])}; os_health_check=healthy")
            return thread_id
        finally:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.terminate()
                await process.wait()

    first = asyncio.run(connect())
    record = (info["runtime"] / "api.json").read_bytes()
    assert adapter(info, "update").returncode == 0
    second = asyncio.run(connect())
    assert first != second
    assert (info["runtime"] / "api.json").read_bytes() == record
    assert helper(info, server)
    removed = adapter(info, "uninstall", "--apply")
    assert removed.returncode == 0, removed.stderr
    asyncio.run(connect(installed=False))
