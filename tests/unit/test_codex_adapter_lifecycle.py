"""Codex adapter install/update/uninstall stays inside the Codex write set."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "codex_adapter.py"


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

    assert module.status(ROOT, codex_home, Path("/usr/bin/python3")) == 1
    assert module.install(ROOT, codex_home, Path("/usr/bin/python3"), dry_run=True) == 0
    assert not (codex_home / "hooks" / module.OBSERVER_DIRNAME).exists()

    assert module.install(ROOT, codex_home, Path("/usr/bin/python3"), dry_run=False) == 0
    observer = codex_home / "hooks" / module.OBSERVER_DIRNAME
    assert all((observer / name).is_file() for name in module._owned_names(module._surface(ROOT)))
    assert module.status(ROOT, codex_home, Path("/usr/bin/python3")) == 0

    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert installed["custom"] == {"keep": True}
    assert installed["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert any("ai-team-os-observer" in json.dumps(group) for group in installed["hooks"]["PreToolUse"])

    assert module.uninstall(ROOT, codex_home, Path("/usr/bin/python3"), dry_run=True) == 0
    assert observer.is_dir()
    assert module.uninstall(ROOT, codex_home, Path("/usr/bin/python3"), dry_run=False) == 0
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
        (install_dir / name).write_text("old", encoding="utf-8")

    assert module.install(ROOT, codex_home, Path("/usr/bin/python3"), dry_run=False) == 0
    updated = json.loads(hooks_path.read_text(encoding="utf-8"))
    pre = updated["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == "Bash"
    assert "ai-team-os-observer" in json.dumps(pre[1])
    assert len(pre) == 2
