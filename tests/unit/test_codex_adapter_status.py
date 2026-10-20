"""Status distinguishes file presence from content equality without writing."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def installed_tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("AITEAM_API_URL", raising=False)
    spec = importlib.util.spec_from_file_location("codex_adapter_status", ROOT / "scripts/codex_adapter.py")
    assert spec is not None and spec.loader is not None
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)

    repo = tmp_path / "source"
    surface_dir = repo / "plugin/harness/codex"
    surface_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "plugin/harness/codex/surface.py", surface_dir / "surface.py")
    shutil.copytree(ROOT / "plugin/harness/codex/hooks", surface_dir / "hooks",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    surface = adapter._surface(repo)
    names = adapter._owned_names(surface)
    observer = codex_home / "hooks" / adapter.OBSERVER_DIRNAME
    observer.mkdir(parents=True)
    for name in names:
        shutil.copy2(surface_dir / "hooks" / name, observer / name)
    manifest = {"hooks": adapter._installed_manifest(surface, observer, Path(sys.executable))}
    (codex_home / "hooks.json").write_text(json.dumps(manifest), encoding="utf-8")
    (codex_home / "config.toml").write_text(
        'model = "keep-local-model"\n[mcp_servers.third-party]\ncommand = "keep-me"\n',
        encoding="utf-8",
    )
    receipt = {
        "schema": 1, "repo_root": str(repo), "python": sys.executable, "hooks_only": True,
        "files": list(names), "sha256": {name: adapter._sha256(observer / name) for name in names},
    }
    (observer / adapter.META_NAME).write_text(json.dumps(receipt), encoding="utf-8")
    return adapter, repo, codex_home, observer, names


def _snapshot(root):
    """Include directories to catch creation/removal, and every file's mtime and bytes."""
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else None)
        for path in (root, *root.rglob("*"))
    }


def _status_twice(installed_tree, capsys, expected_code):
    adapter, repo, codex_home, _, _ = installed_tree
    before = _snapshot(repo.parent)
    command = ["status", "--repo-root", str(repo), "--codex-home", str(codex_home),
               "--python", sys.executable, "--hooks-only"]
    outputs = []
    for _ in range(2):
        assert adapter.main(command) == expected_code
        outputs.append(capsys.readouterr().out)
        assert _snapshot(repo.parent) == before
    assert outputs[0] == outputs[1]
    return outputs[0]


@pytest.mark.parametrize("state", ["equal", "drifted", "missing", "mixed", "no-install-dir"],
                         ids=["all-equal", "all-drifted", "all-missing", "mixed", "no-install-dir"])
def test_status_counts_content_and_presence_without_writing(installed_tree, capsys, state):
    _, _, _, observer, names = installed_tree
    total = len(names)
    assert total >= 3
    drifted = names if state == "drifted" else names[:1] if state == "mixed" else ()
    missing = names if state == "missing" else names[1:2] if state == "mixed" else ()
    for name in drifted:
        path = observer / name
        path.write_bytes(path.read_bytes() + b"\n# installed local drift\n")
    for name in missing:
        (observer / name).unlink()
    if state == "no-install-dir":
        shutil.rmtree(observer)
        missing = names

    present = total - len(missing)
    equal = present - len(drifted)
    out = _status_twice(installed_tree, capsys, 0 if state == "equal" else 1)
    assert f"文件: 存在 {present}/{total}, 哈希一致 {equal}/{total}, 漂移 {len(drifted)}, 缺失 {len(missing)}" in out
    assert " 同步" not in out
    assert f"安装目录: {'缺失' if state == 'no-install-dir' else '存在'}" in out


@pytest.mark.parametrize("registration", ["custom", "absent", "partial", "broken-interpreter"],
                         ids=["custom-preserved", "unregistered", "missing-handler", "broken-command"])
def test_status_keeps_registration_checks_and_exit_codes(installed_tree, capsys, registration):
    adapter, _, codex_home, _, names = installed_tree
    hooks_path = codex_home / "hooks.json"
    document = json.loads(hooks_path.read_text(encoding="utf-8"))
    groups = document["hooks"]["UserPromptSubmit"]
    if registration == "custom":
        groups[0]["matcher"] = ""
        handler = groups[0]["hooks"][0]
        handler["command"] = handler["command"].replace("leader-codex", "custom-reader") + " explicit-project-id"
    elif registration == "absent":
        document["hooks"] = {}
    elif registration == "partial":
        document["hooks"].pop("UserPromptSubmit")
    else:
        groups[0]["hooks"][0]["command"] = groups[0]["hooks"][0]["command"].replace(
            sys.executable, str(codex_home / "missing-python"), 1,
        )
    hooks_path.write_text(json.dumps(document), encoding="utf-8")

    out = _status_twice(installed_tree, capsys, 0 if registration == "custom" else 1)
    total = len(names)
    assert f"文件: 存在 {total}/{total}, 哈希一致 {total}/{total}, 漂移 0, 缺失 0" in out
    assert f"注册: {'已登记（保留定制声明）' if registration == 'custom' else '需要安装或更新'}" in out
    if registration == "broken-interpreter":
        assert f"Hook 命令路径不存在: {codex_home / 'missing-python'}" in out
