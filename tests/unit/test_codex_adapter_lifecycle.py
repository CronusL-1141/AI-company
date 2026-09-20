"""Codex adapter install/update/uninstall stays inside the Codex write set."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "codex_adapter.py"


@pytest.fixture(autouse=True)
def explicit_local_api(monkeypatch):
    monkeypatch.setenv("AITEAM_API_URL", "http://127.0.0.1:49152")


def _module():
    spec = importlib.util.spec_from_file_location("codex_adapter_lifecycle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _foreign_hooks() -> dict:
    return {"hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{
            "type": "command", "command": "/custom/guard.sh", "timeout": 2,
        }]}],
        "SessionStart": [{"hooks": [{
            "type": "command", "command": "/custom/start.sh", "timeout": 2,
        }]}],
    }, "custom": {"keep": True}}


def test_install_update_status_and_uninstall_preserve_foreign_hooks(tmp_path, capsys):
    module = _module()
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    hooks_path = codex_home / "hooks.json"
    hooks_path.write_text(json.dumps(_foreign_hooks()), encoding="utf-8")

    assert module.status(ROOT, codex_home, Path(sys.executable)) == 1
    assert module.install(ROOT, codex_home, Path(sys.executable), dry_run=True) == 0
    assert not (codex_home / "hooks" / module.OBSERVER_DIRNAME).exists()

    assert module.install(ROOT, codex_home, Path(sys.executable), dry_run=False) == 0
    observer = codex_home / "hooks" / module.OBSERVER_DIRNAME
    assert all((observer / name).is_file() for name in module._owned_names(module._surface(ROOT)))
    assert module.status(ROOT, codex_home, Path(sys.executable)) == 0

    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert installed["custom"] == {"keep": True}
    assert installed["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert any("ai-team-os-observer" in json.dumps(group) for group in installed["hooks"]["PreToolUse"])

    assert module.uninstall(ROOT, codex_home, Path(sys.executable), dry_run=True) == 0
    assert observer.is_dir()
    assert module.uninstall(ROOT, codex_home, Path(sys.executable), dry_run=False) == 0
    removed = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert removed == _foreign_hooks()
    assert not observer.exists()
    assert ".claude" not in capsys.readouterr().out


def test_update_keeps_existing_observer_slot_and_does_not_reorder_foreign_group(tmp_path):
    module = _module()
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    hooks_path = codex_home / "hooks.json"
    install_dir = codex_home / "hooks" / module.OBSERVER_DIRNAME
    old = _foreign_hooks()
    old["hooks"]["PreToolUse"].append({"hooks": [{
        "type": "command", "command": f'"/usr/bin/python3" "{install_dir}/send_event_codex.py" PreToolUse',
        "timeout": 5,
    }]})
    hooks_path.write_text(json.dumps(old), encoding="utf-8")
    install_dir.mkdir(parents=True)
    for name in module._owned_names(module._surface(ROOT)):
        shutil.copy2(ROOT / "plugin/harness/codex/hooks" / name, install_dir / name)

    assert module.install(ROOT, codex_home, Path(sys.executable), dry_run=False) == 0
    updated = json.loads(hooks_path.read_text(encoding="utf-8"))
    pre = updated["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == "Bash"
    assert "ai-team-os-observer" in json.dumps(pre[1])
    assert len(pre) == 2


def test_mixed_group_custom_arguments_and_foreign_indices_are_unchanged(tmp_path):
    module = _module()
    home = tmp_path / "codex"
    module.install(ROOT, home, Path(sys.executable), dry_run=False)
    path = home / "hooks.json"
    document = json.loads(path.read_text())
    group = document["hooks"]["UserPromptSubmit"][0]
    group["hooks"][0]["command"] += " actual-project-id"
    group["hooks"][0]["command"] = group["hooks"][0]["command"].replace("leader-codex", "custom-reader")
    group["hooks"].append({"type": "command", "command": "/custom/guard.sh"})
    document["hooks"]["UserPromptSubmit"].append({"hooks": [{"type": "command", "command": "/custom/last.sh"}]})
    path.write_text(json.dumps(document))
    module.install(ROOT, home, Path(sys.executable), dry_run=False)
    assert json.loads(path.read_text()) == document
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="中间 handler"):
        module.uninstall(ROOT, home, Path(sys.executable), dry_run=False)
    assert path.read_bytes() == before
    assert (home / "hooks" / module.OBSERVER_DIRNAME / "hook_core.py").exists()


def test_uninstall_retains_modified_and_user_files(tmp_path):
    module = _module()
    home = tmp_path / "codex"
    module.install(ROOT, home, Path(sys.executable), dry_run=False)
    observer = home / "hooks" / module.OBSERVER_DIRNAME
    modified = observer / "channel_unread_codex.py"
    modified.write_text("# user customization")
    extra = observer / "user.txt"
    extra.write_text("keep")
    with pytest.raises(RuntimeError, match="用户修改"):
        module.install(ROOT, home, Path(sys.executable), dry_run=False)
    module.uninstall(ROOT, home, Path(sys.executable), dry_run=False)
    assert modified.read_text() == "# user customization"
    assert extra.read_text() == "keep"


def test_failed_install_rolls_back_files_and_registration(tmp_path, monkeypatch):
    module = _module()
    home = tmp_path / "codex"
    home.mkdir()
    hooks = home / "hooks.json"
    hooks.write_text(json.dumps(_foreign_hooks()))
    before = hooks.read_bytes()
    def fail(*args):
        raise OSError("injected write failure")
    monkeypatch.setattr(module, "_atomic_json", fail)
    with pytest.raises(OSError, match="injected"):
        module.install(ROOT, home, Path(sys.executable), dry_run=False)
    assert hooks.read_bytes() == before
    assert not list((home / "hooks" / module.OBSERVER_DIRNAME).iterdir())
    assert not (home / "config.toml").exists()
    assert not (home / "bin/aiteam-http-headers.py").exists()


def test_codex_home_environment_and_complete_bootstrap(tmp_path, monkeypatch):
    import os
    import subprocess
    module = _module()
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert module.main(["install"]) == 0
    observer = home / "hooks" / module.OBSERVER_DIRNAME
    assert (observer / "hook_core.py").is_file()
    run = subprocess.run([sys.executable, str(observer / "session_bootstrap_codex.py")],
                         input=json.dumps({"cwd": "/tmp/中文项目"}), text=True, capture_output=True,
                         env=dict(os.environ, AITEAM_API_URL="http://127.0.0.1:1"))
    assert run.returncode == 0
    assert json.loads(run.stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "ModuleNotFoundError" not in run.stderr


def test_symlink_install_is_refused(tmp_path):
    module = _module()
    real = tmp_path / "shared"
    real.mkdir()
    home = tmp_path / "codex"
    home.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeError, match="符号链接"):
        module.main(["install", "--codex-home", str(home)])
    assert list(real.iterdir()) == []


def test_fresh_command_quotes_apostrophe_and_space_in_install_path(tmp_path):
    import shlex
    module = _module()
    home = tmp_path / "Codex user's directory"
    module.install(ROOT, home, Path(sys.executable), dry_run=False)
    document = json.loads((home / "hooks.json").read_text())
    for groups in document["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                tokens = shlex.split(handler["command"])
                assert tokens[0] == sys.executable
                assert Path(tokens[1]).is_file()


def test_status_reports_stale_helper_without_showing_credentials(tmp_path, monkeypatch, capsys):
    module = _module()
    (tmp_path / "config.toml").write_text(
        '[mcp_servers.ai-team-os]\nurl = "http://127.0.0.1:8123/mcp/"\n'
        'http_headers_helper = "/missing/python /missing/helper.py"\n'
        'bearer_token = "secret-not-for-diagnostics"\n'
    )
    monkeypatch.delenv("AITEAM_API_URL", raising=False)
    monkeypatch.setattr(module, "_ready", lambda url: (False, "offline"))
    assert module.connection_status(tmp_path) == 1
    out = capsys.readouterr().out
    assert "/missing/helper.py" in out
    assert "8123" in out
    assert "secret-not-for-diagnostics" not in out


def test_start_preflight_failure_redacts_child_diagnostics(tmp_path, monkeypatch, capsys):
    import socket
    from types import SimpleNamespace
    module = _module()
    monkeypatch.setattr(module, "_ready", lambda url: (False, "offline"))
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stderr="credential-secret-do-not-show", stdout=""))
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    assert module.start(ROOT, tmp_path, Path(sys.executable), f"http://127.0.0.1:{port}") == 1
    captured = capsys.readouterr()
    assert "credential-secret" not in captured.err
    assert "预检失败" in captured.err


def test_complete_install_preserves_config_and_restores_only_owned_fields(tmp_path):
    import tomllib
    module = _module()
    config = tmp_path / "config.toml"
    original = ('model = "custom-model"\n[mcp_servers.third-party]\ncommand = "keep-me"\n'
                '[mcp_servers.ai-team-os]\nurl = "http://127.0.0.1:49152/mcp/"\n'
                'startup_timeout_sec = 12\ntool_timeout_sec = 99\nbearer_token_env_var = "TEST_TOKEN"\n')
    config.write_text(original)
    auth = tmp_path / "auth.json"
    auth.write_text('{"sentinel":"never-change"}')
    module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    first = config.read_bytes()
    module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    assert config.read_bytes() == first
    installed = tomllib.loads(first.decode())
    assert installed["mcp_servers"]["ai-team-os"]["startup_timeout_sec"] == 12
    assert "--runtime-script" in installed["mcp_servers"]["ai-team-os"]["http_headers_helper"]
    module.uninstall(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    assert tomllib.loads(config.read_text()) == tomllib.loads(original)
    assert auth.read_text() == '{"sentinel":"never-change"}'
    assert not (tmp_path / "bin/aiteam-http-headers.py").exists()


def test_fresh_install_requires_address_before_writing(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.delenv("AITEAM_API_URL", raising=False)
    with pytest.raises(RuntimeError, match="缺少"):
        module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    assert not list(tmp_path.iterdir())


def test_mcp_failure_rolls_back_config_helper_and_hook_bytes(tmp_path, monkeypatch):
    module = _module()
    module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    paths = [path for path in tmp_path.rglob("*") if path.is_file() and ".bak-" not in path.name]
    before = {path: path.read_bytes() for path in paths}
    def fail(*args):
        raise OSError("injected final receipt failure")
    monkeypatch.setattr(module, "_atomic_json", fail)
    with pytest.raises(OSError, match="injected"):
        module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False,
                       runtime_dir=tmp_path / "new-runtime")
    assert {path: path.read_bytes() for path in paths} == before


def test_user_changed_helper_command_and_foreign_url_are_protected(tmp_path):
    module = _module()
    module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    config = tmp_path / "config.toml"
    config.write_text(config.read_text().replace("--api-url", "--custom --api-url"))
    before = config.read_bytes()
    with pytest.raises(RuntimeError, match="用户修改"):
        module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    assert config.read_bytes() == before
    with pytest.raises(RuntimeError, match="URL 与目标不同"):
        module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False, api_url="http://127.0.0.1:49153")
    assert config.read_bytes() == before


def test_uninstall_keeps_user_added_mcp_fields_and_usable_helper(tmp_path):
    module = _module()
    module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    config = tmp_path / "config.toml"
    config.write_text(config.read_text() + "tool_timeout_sec = 99\n")
    before = config.read_bytes()
    module.uninstall(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    assert config.read_bytes() == before
    assert (tmp_path / "bin/aiteam-http-headers.py").exists()


def test_non_posix_install_refuses_before_any_files_are_written(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = _module()
    monkeypatch.setattr(module, "os", SimpleNamespace(name="nt"))
    with pytest.raises(RuntimeError, match="仅支持 POSIX"):
        module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    assert not list(tmp_path.iterdir())


def test_uninstall_write_failure_restores_config_and_all_installed_files(tmp_path, monkeypatch):
    module = _module()
    module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    def fail(*args):
        raise OSError("injected config failure")
    monkeypatch.setattr(module, "_write_bytes", fail)
    with pytest.raises(OSError, match="injected"):
        module.uninstall(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    assert {path: path.read_bytes() for path in before} == before


def test_legacy_unknown_file_is_not_accepted_by_release_hash_manifest(tmp_path):
    module = _module()
    observer = tmp_path / "hooks" / module.OBSERVER_DIRNAME
    observer.mkdir(parents=True)
    script = observer / "session_bootstrap_codex.py"
    script.write_text("# unrecognized user hook")
    with pytest.raises(RuntimeError, match="来源未知"):
        module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    assert script.read_text() == "# unrecognized user hook"
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize(("restart_required", "expected", "message"),
                         [(True, 1, "需要显式 stop"), (None, 0, "源码版本未知")],
                         ids=["old-running-code", "untracked-runtime"])
def test_connection_status_distinguishes_stale_and_unknown_runtime(
    tmp_path, monkeypatch, capsys, restart_required, expected, message,
):
    from types import SimpleNamespace
    module = _module()
    module.install(ROOT, tmp_path, Path(sys.executable), dry_run=False)
    (tmp_path / "ai-team-os/runtime").mkdir(parents=True)
    monkeypatch.setattr(module, "_ready", lambda url: (True, "ready"))
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps({"api_ready": True, "restart_required": restart_required})))
    assert module.connection_status(tmp_path) == expected
    assert message in capsys.readouterr().out
