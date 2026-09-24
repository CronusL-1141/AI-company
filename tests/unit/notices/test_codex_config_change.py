"""The Codex bridge keeps the shared authorisation gate and adapter transaction."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP

from aiteam.mcp.tools import infra
from aiteam.services import codex_config_change as bridge
from aiteam.services import config_change
from aiteam.services.config_change import ConfigChangeError
from aiteam.services.notices.detectors.codex_copies import codex_home

ROOT = Path(__file__).resolve().parents[3]
CHANGE = "update_codex_adapter"


def make_installation(tmp_path, monkeypatch, *, hooks_only=True):
    """All source, installed files and process state live below tmp_path."""
    home, codex, source = tmp_path / "user-home", tmp_path / "custom-codex", tmp_path / "source"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    subprocess.run(["git", "init", "-q", "-b", "master", str(source)], check=True, capture_output=True)
    shutil.copytree(ROOT / "plugin/harness/codex", source / "plugin/harness/codex",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (source / "scripts").mkdir()
    for name in ("codex_adapter.py", "codex_runtime.py"):
        shutil.copy2(ROOT / "scripts" / name, source / "scripts" / name)
    (source / "src/aiteam/mcp").mkdir(parents=True)
    shutil.copy2(ROOT / "src/aiteam/mcp/http_headers.py", source / "src/aiteam/mcp/http_headers.py")
    (source / "pyproject.toml").write_text('[project]\nname = "ai-team-os"\nversion = "8.7.6"\n')
    command = [sys.executable, "-I", "-B", str(source / "scripts/codex_adapter.py"), "install",
               "--repo-root", str(source), "--codex-home", str(codex), "--python", sys.executable,
               "--api-url", "http://127.0.0.1:49153"]
    if hooks_only:
        command.append("--hooks-only")
    subprocess.run(command, cwd=tmp_path, env=bridge._environment(codex),
                   capture_output=True, text=True, timeout=30, check=True)
    script = source / "plugin/harness/codex/hooks/channel_unread_codex.py"
    installed = codex / "hooks/ai-team-os-observer" / script.name
    script.write_bytes(script.read_bytes() + b"\n")  # a real one-byte source update
    return SimpleNamespace(home=home, codex=codex, source=source, script=script,
                           installed=installed, receipt=installed.parent / bridge.RECEIPT_NAME)


@pytest.fixture()
def installation(tmp_path, monkeypatch):
    return make_installation(tmp_path, monkeypatch)


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_preview_uses_receipt_source_and_shows_exact_installation(installation, tmp_path):
    before = snapshot(tmp_path)
    shown = config_change.preview(CHANGE, now=1000)
    assert snapshot(tmp_path) == before
    assert shown["baseline"]["root"] == str(installation.source)
    assert shown["baseline"]["version"] == "8.7.6"
    assert shown["baseline"]["codex_home"] == str(installation.codex)
    assert shown["baseline"]["hooks_only"] == "True"
    assert shown["baseline"]["python"] == sys.executable
    assert str(installation.codex) in shown["summary"] and "hooks-only" in shown["summary"]
    assert shown["notice_key"] == ""  # the detector, not a successful write, confirms recovery
    assert bridge.plan().payload == bridge._invoke(installation.source, installation.codex, Path(sys.executable))
    assert not list(installation.source.rglob("__pycache__"))


def test_codex_home_defaults_match_detectors(installation, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "")
    assert codex_home() == installation.home / ".codex"
    with pytest.raises(ConfigChangeError, match="No valid Codex install receipt"):
        config_change.preview(CHANGE)


@pytest.mark.parametrize("configured", ["   ", "~/missing-codex"], ids=["blank-literal", "tilde-literal"])
def test_invalid_explicit_codex_home_is_a_controlled_refusal(installation, monkeypatch, configured):
    monkeypatch.setenv("CODEX_HOME", configured)
    with pytest.raises(ConfigChangeError, match="No valid Codex install receipt"):
        config_change.preview(CHANGE)


@pytest.mark.parametrize("damage", ["missing", "invalid", "relative-root", "missing-source"],
                         ids=["missing", "invalid", "relative-root", "missing-source"])
def test_no_receipt_or_source_never_falls_back_to_mcp_tree(installation, tmp_path, damage):
    if damage == "missing":
        installation.receipt.unlink()
    elif damage == "invalid":
        installation.receipt.write_text("{}")
    else:
        receipt = json.loads(installation.receipt.read_text())
        receipt["repo_root"] = "relative" if damage == "relative-root" else str(tmp_path / "absent")
        installation.receipt.write_text(json.dumps(receipt))
    before = snapshot(tmp_path)
    with pytest.raises(ConfigChangeError):
        config_change.preview(CHANGE)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("damage", ["not-git", "nested-root", "wrong-project", "project-list", "relative-python"],
                         ids=str)
def test_repository_and_interpreter_are_verified_before_execution(installation, tmp_path, damage):
    receipt = json.loads(installation.receipt.read_text())
    if damage == "not-git":
        shutil.rmtree(installation.source / ".git")
    elif damage == "nested-root":
        nested = installation.source / "nested"
        nested.mkdir()
        receipt["repo_root"] = str(nested)
    elif damage == "wrong-project":
        (installation.source / "pyproject.toml").write_text('[project]\nname = "other-project"\n')
    elif damage == "project-list":
        (installation.source / "pyproject.toml").write_text("project = []\n")
    else:
        receipt["python"] = "python3"
    installation.receipt.write_text(json.dumps(receipt))
    before = snapshot(tmp_path)
    with pytest.raises(ConfigChangeError, match="Cannot verify"):
        config_change.preview(CHANGE)
    assert snapshot(tmp_path) == before


def test_receipt_adapter_cannot_access_parent_memory_secrets_or_working_directory(installation, tmp_path, monkeypatch):
    parent_key = b"synthetic-parent-HMAC-secret-do-not-inherit"
    monkeypatch.setattr(config_change, "_KEY", parent_key)
    marker = SimpleNamespace(touched=False)
    monkeypatch.setitem(sys.modules, "codex_parent_probe", marker)
    monkeypatch.setenv("MCP_PRIVATE_TEST_SECRET", "synthetic-secret-in-parent-only")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai-secret")
    monkeypatch.setenv("PYTHONPATH", "parent-import-path-must-not-be-inherited")
    parent_cwd = tmp_path / "mcp-working-directory"
    parent_cwd.mkdir()
    monkeypatch.chdir(parent_cwd)
    trace = installation.home / "child-probe.jsonl"
    adapter = installation.source / "scripts/codex_adapter.py"
    probe = textwrap.dedent(f'''
        import io as _probe_io
        from aiteam.services import config_change as _probe_config
        _probe_key = _probe_config._KEY.hex()
        _probe_config._KEY = b"child-attempted-parent-replacement"
        _probe_marker = sys.modules.get("codex_parent_probe")
        if _probe_marker is not None:
            _probe_marker.touched = True
        sys.modules["codex_child_probe"] = "child-only"
        Path("relative-probe.txt").write_text("child attempted relative write")
        _probe_input = sys.stdin.read() if __name__ == "__main__" else ""
        if _probe_input:
            sys.stdin = _probe_io.StringIO(_probe_input)
        with open({str(trace)!r}, "a", encoding="utf-8") as _probe_file:
            _probe_file.write(json.dumps({{
                "pid": os.getpid(), "cwd": os.getcwd(), "key": _probe_key,
                "parent_marker": _probe_marker is not None,
                "secret": os.environ.get("MCP_PRIVATE_TEST_SECRET"),
                "api_key": os.environ.get("OPENAI_API_KEY"),
                "pythonpath": os.environ.get("PYTHONPATH"),
                "api_url": os.environ.get("AITEAM_API_URL"),
                "argv": sys.argv, "stdin": _probe_input,
            }}) + "\\n")
    ''')
    marker_text = '\nif __name__ == "__main__":\n    try:'
    assert marker_text in adapter.read_text()
    adapter.write_text(adapter.read_text().replace(marker_text, probe + marker_text))
    shown = config_change.preview(CHANGE)
    quote = "用户批准原话-仅留在父MCP"
    result = config_change.apply(CHANGE, shown["confirm_token"], quote)
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    assert config_change._KEY == parent_key and not marker.touched
    assert len(rows) == 3 and all(row["pid"] != os.getpid() for row in rows)
    assert "codex_child_probe" not in sys.modules
    assert not (parent_cwd / "relative-probe.txt").exists()
    assert all(row["cwd"] != str(parent_cwd) and not Path(row["cwd"]).exists() for row in rows)
    assert all(not row["parent_marker"] and row["key"] != parent_key.hex() for row in rows)
    assert all(row[name] is None for row in rows for name in ("secret", "api_key", "pythonpath", "api_url"))
    raw = trace.read_text()
    assert parent_key.hex() not in raw and shown["confirm_token"] not in raw and quote not in raw
    [applied] = [row for row in rows if row["stdin"]]
    assert json.loads(applied["stdin"])["change"] == CHANGE
    assert result["targets"] and installation.installed.read_bytes() == installation.script.read_bytes()


@pytest.mark.parametrize("damage", ["schema", "target", "hash", "options", "extra", "duplicate"], ids=str)
def test_subprocess_json_is_validated_before_issuing_a_token(installation, damage):
    document = bridge.plan().payload
    if damage == "schema":
        document["schema"] = True
    elif damage == "target":
        document["targets"][0]["path"] = str(installation.home / "unapproved.txt")
    elif damage == "hash":
        document["targets"][0]["after_sha256"] = "not-a-hash"
    elif damage == "options":
        document["options"]["hooks_only"] = "false"
    elif damage == "extra":
        document["unknown"] = "not part of the contract"
    output = json.dumps(document)
    if damage == "duplicate":
        output = '{"schema": 1,' + output[1:]
    (installation.source / "scripts/codex_adapter.py").write_text(f"print({output!r})\n")
    before = snapshot(installation.codex)
    with pytest.raises(ConfigChangeError, match="Cannot preview"):
        config_change.preview(CHANGE)
    assert snapshot(installation.codex) == before


@pytest.mark.parametrize("damage", ["timeout", "output-limit", "exit", "invalid-json"], ids=str)
def test_subprocess_failures_are_bounded_and_reported(installation, monkeypatch, damage):
    script, message = {
        "timeout": ("import time; time.sleep(5)", "timed out"),
        "output-limit": ("print('x' * 2000)", "size limit"),
        "exit": ("import sys; sys.stderr.write('explicit child failure'); sys.exit(7)", "exited 7"),
        "invalid-json": ("print('not json')", "Cannot preview"),
    }[damage]
    if damage == "timeout":
        monkeypatch.setattr(bridge, "_CLI_TIMEOUT_S", 0.05)
    if damage == "output-limit":
        monkeypatch.setattr(bridge, "_MAX_OUTPUT_BYTES", 1024)
    (installation.source / "scripts/codex_adapter.py").write_text(script)
    with pytest.raises(ConfigChangeError, match=message):
        config_change.preview(CHANGE)


@pytest.mark.parametrize("recorded_python", [True, False], ids=["receipt-python", "sys-executable-fallback"])
def test_full_install_cli_preserves_recorded_connection_and_options(tmp_path, monkeypatch, recorded_python):
    install = make_installation(tmp_path, monkeypatch, hooks_only=False)
    if not recorded_python:
        receipt = json.loads(install.receipt.read_text())
        receipt.pop("python")
        install.receipt.write_text(json.dumps(receipt))
    config = install.codex / "config.toml"
    before = config.read_bytes()
    shown = config_change.preview(CHANGE)
    assert shown["baseline"]["hooks_only"] == "False"
    assert shown["baseline"]["api_url"] == "http://127.0.0.1:49153"
    assert shown["baseline"]["python"] == sys.executable
    result = config_change.apply(CHANGE, shown["confirm_token"], "保留连接并更新")
    assert result["targets"] and config.read_bytes() == before


@pytest.mark.parametrize("changed", ["source", "installed", "manifest", "receipt", "adapter"],
                         ids=["source", "installed", "manifest", "receipt", "adapter"])
def test_drift_refuses_without_writes(installation, tmp_path, changed):
    token = config_change.preview(CHANGE, now=1000)["confirm_token"]
    path = {"source": installation.script, "installed": installation.installed,
            "manifest": installation.codex / "hooks.json", "receipt": installation.receipt,
            "adapter": installation.source / "scripts/codex_adapter.py"}[changed]
    path.write_bytes(path.read_bytes() + b"\n")
    before = snapshot(tmp_path)
    with pytest.raises(ConfigChangeError, match="changed since the preview"):
        config_change.apply(CHANGE, token, "确认更新 Codex 适配器", now=1010)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(("quote", "delay", "token_kind", "message"), [
    ("", 10, "valid", "user_quote"), ("   ", 10, "valid", "user_quote"),
    ("同意", 601, "valid", "expired"), ("同意", 10, "forged", "changed since the preview"),
], ids=["empty-quote", "blank-quote", "expired", "forged"])
def test_invalid_authorisation_never_writes(installation, tmp_path, quote, delay, token_kind, message):
    token = config_change.preview(CHANGE, now=1000)["confirm_token"]
    if token_kind == "forged":
        token = "1000." + "0" * 32
    before = snapshot(tmp_path)
    with pytest.raises(ConfigChangeError, match=message):
        config_change.apply(CHANGE, token, quote, now=1000 + delay)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("legacy", [False, True], ids=["explicit-mode", "legacy-mode"])
def test_apply_passes_exact_preview_backs_up_and_preserves_hooks_only(installation, monkeypatch, legacy):
    if legacy:
        receipt = json.loads(installation.receipt.read_text())
        receipt.pop("hooks_only")
        installation.receipt.write_text(json.dumps(receipt))
    config = installation.codex / "config.toml"
    config.write_text('[mcp_servers.ai-team-os]\ncommand = "keep-this-stdio"\n')
    manifest = installation.codex / "hooks.json"
    kept = config.read_bytes(), manifest.read_bytes()
    original = bridge._run_process
    expected = []

    def observed(command, *args, **kwargs):
        if "--expected-preview" in command:
            assert command[command.index("--expected-preview") + 1] == "-"
            expected.append(json.loads(kwargs["payload"]))
            assert "按预览更新" not in kwargs["payload"].decode()
        return original(command, *args, **kwargs)

    monkeypatch.setattr(bridge, "_run_process", observed)
    planned = bridge.plan()
    shown = config_change.preview(CHANGE, now=1000)
    originals = {item["path"]: Path(item["path"]).read_bytes() for item in shown["targets"]}
    result = config_change.apply(CHANGE, shown["confirm_token"], "按预览更新", now=1010)
    assert expected == [planned.payload]
    assert set(result) == {"targets", "registration_changed", "change", "notice_key", "baseline", "user_quote"}
    assert result["user_quote"] == "按预览更新"
    for item in result["targets"]:
        assert Path(item["backup"]).read_bytes() == originals[item["path"]]
        assert config_change.sha256_file(Path(item["path"])) == item["after_sha256"]
    assert (config.read_bytes(), manifest.read_bytes()) == kept
    assert not (installation.codex / "bin").exists()
    assert config_change.preview(CHANGE)["nothing_to_do"]


@pytest.mark.parametrize("budget", [900, 768, 512], ids=["900-bytes", "768-bytes", "512-bytes"])
def test_offline_compaction_uses_bytes_preserves_identity_and_does_not_mutate(budget):
    data = {"change": CHANGE, "host": "codex", "session_id": "00000000-1111-2222-3333-444444444444",
            "tool": "os_config_change", "user_quote": "确认按预览更新。" * 1000,
            "targets": [{"path": "/长目录" * 500, "backup": "/长备份" * 500}],
            "baseline": {"root": "/中文来源" * 500}, "warnings": ["保留退役文件。" * 1000]}
    original = copy.deepcopy(data)
    compact = config_change.compact_for_local_record(data, limit=budget)
    assert len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode()) <= budget
    for field in ("change", "host", "session_id", "tool"):
        assert compact[field] == data[field]
    assert compact["user_quote"] and data["user_quote"].startswith(compact["user_quote"])
    assert compact["target_count"] == 1
    assert compact["targets_sha256"] == hashlib.sha256(json.dumps(data["targets"], sort_keys=True).encode()).hexdigest()
    assert data == original


def test_oversized_local_envelope_reports_failure_without_appending(installation, monkeypatch):
    import uuid

    monkeypatch.setattr(infra, "_api_call", lambda *args, **kwargs: {"success": False})
    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex="超长异常标识" * 1000))
    result = infra._record_config_write({"change": CHANGE, "host": "codex", "tool": "os_config_change",
                                        "session_id": "session", "user_quote": "同意", "targets": []})
    assert result["recorded"] == "none" and "budget cannot hold" in result["note"]
    assert not infra._local_record_path("codex").exists()


async def test_real_mcp_preview_and_apply_keep_event_loop_serving(installation, monkeypatch):
    original = bridge._run_process
    event_loop_thread = threading.get_ident()

    def slow_child(*args, **kwargs):
        assert threading.get_ident() != event_loop_thread
        time.sleep(0.05)
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge, "_run_process", slow_child)
    monkeypatch.setattr(infra, "_api_call", lambda *args, **kwargs: {"success": False})
    server = FastMCP("codex-config-change-test")
    infra.register(server)
    beats = 0

    async def heartbeat():
        nonlocal beats
        while True:
            beats += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(heartbeat())
    try:
        async with Client(server) as client:
            shown = (await client.call_tool("os_config_change", {"change": CHANGE})).data
            during_preview = beats
            applied = (await client.call_tool("os_config_change", {
                "change": CHANGE, "confirm_token": shown["confirm_token"], "user_quote": "更新",
            })).data
            assert applied["success"]
            assert during_preview > 50 and beats > during_preview + 50
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
