"""Release checks distinguish imported support modules from hook handlers."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
CODEX = ROOT / "plugin/harness/codex"
SUPPORT = ("codex_observation.py", "codex_completion_delivery.py")


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"support_check_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def packaging_tree(tmp_path):
    for relative in ("scripts", "plugin/hooks", "src/aiteam/hooks", "plugin/harness/codex/hooks"):
        (tmp_path / relative).mkdir(parents=True)
    shutil.copyfile(CODEX / "surface.py", tmp_path / "plugin/harness/codex/surface.py")
    for relative in ("plugin/hooks", "src/aiteam/hooks", "plugin/harness/codex/hooks"):
        (tmp_path / relative / "hook_core.py").write_text("CORE = True\n", encoding="utf-8")
    surface = _load(CODEX / "surface.py")
    for name in (*surface.CODEX_HOOK_SCRIPTS, *SUPPORT):
        (tmp_path / "plugin/harness/codex/hooks" / name).write_text("VALUE = True\n", encoding="utf-8")
    # Execute the real I1 block, not a second implementation of its predicate.
    script = (ROOT / "scripts/check_invariants.sh").read_text(encoding="utf-8")
    i1 = script.split("# ── I1b:", 1)[0] + '\nexit "$FAIL"\n'
    (tmp_path / "scripts/check_invariants.sh").write_text(i1, encoding="utf-8")
    return tmp_path


def _i1(tree: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
           "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        ["bash", str(tree / "scripts/check_invariants.sh")], cwd=tree, env=env,
        capture_output=True, text=True, timeout=20, check=False,
    )


def test_support_files_pass_i1_without_becoming_handlers(packaging_tree):
    result = _i1(packaging_tree)
    assert result.returncode == 0, result.stdout + result.stderr
    surface = _load(CODEX / "surface.py")
    assert surface.CODEX_SUPPORT_MODULES == SUPPORT
    registered = {script for _, _, _, entries in surface.CODEX_HOOK_SURFACE
                  for script, _, _, _ in entries}
    assert not registered.intersection(SUPPORT)
    assert set(surface.CODEX_HOOK_SCRIPTS) == registered
    assert (CODEX / "hooks.json").read_text() == surface.render()
    manifest = json.loads((CODEX / "hooks.json").read_text())
    assert (CODEX / "hook-trust.lock").read_text() == surface.render_trust_lock(manifest)


@pytest.mark.parametrize("fault", ["unknown", "missing", "plugin_collision", "src_collision", "both_collision"])
def test_i1_still_rejects_incomplete_or_cross_host_inventory(packaging_tree, fault):
    hooks = packaging_tree / "plugin/harness/codex/hooks"
    if fault == "unknown":
        (hooks / "undeclared_support.py").write_text("VALUE = True\n")
    elif fault == "missing":
        (hooks / SUPPORT[0]).unlink()
    else:
        targets = ("plugin/hooks", "src/aiteam/hooks") if fault == "both_collision" else (
            "plugin/hooks" if fault == "plugin_collision" else "src/aiteam/hooks",
        )
        for target in targets:
            shutil.copyfile(hooks / SUPPORT[0], packaging_tree / target / SUPPORT[0])
    result = _i1(packaging_tree)
    assert result.returncode != 0, result.stdout + result.stderr
    assert ("undeclared_support.py" if fault == "unknown" else SUPPORT[0]) in result.stdout
    if fault.endswith("collision"):
        assert "适配器私有文件名污染了 CC hook 目录" in result.stdout


@pytest.fixture
def isolation(packaging_tree, monkeypatch):
    checker = _load(ROOT / "scripts/check_codex_isolation.py")
    monkeypatch.setattr(checker, "CODEX_DIR", packaging_tree / "plugin/harness/codex")
    monkeypatch.setattr(checker, "CC_HOOKS_DIR", packaging_tree / "plugin/hooks")
    monkeypatch.setattr(checker, "CC_SOURCE_HOOKS_DIR", packaging_tree / "src/aiteam/hooks")
    surface = _load(CODEX / "surface.py")
    return checker, surface, packaging_tree


def test_i20_accepts_exact_support_inventory(isolation):
    checker, surface, _ = isolation
    errors = []
    checker.check_support_modules(surface, errors)
    checker.check_same_name_ban(surface, errors)
    assert errors == []


@pytest.mark.parametrize("declaration", [
    "helper.py", ("../outside.py",), ("nested/helper.py",), ("bad.txt",),
    (None,), (SUPPORT[0], SUPPORT[0]), ("send_event_codex.py",),
])
def test_i20_rejects_invalid_support_declaration(isolation, declaration):
    checker, surface, _ = isolation
    candidate = SimpleNamespace(CODEX_HOOK_SCRIPTS=surface.CODEX_HOOK_SCRIPTS,
                                CODEX_SUPPORT_MODULES=declaration)
    errors = []
    checker.check_support_modules(candidate, errors)
    assert errors


@pytest.mark.parametrize("side", ["plugin/hooks", "src/aiteam/hooks"])
def test_i20_support_names_must_not_pollute_either_cc_copy(isolation, side):
    checker, surface, tree = isolation
    (tree / side / SUPPORT[0]).write_text("VALUE = True\n")
    errors = []
    checker.check_same_name_ban(surface, errors)
    assert any(SUPPORT[0] in error for error in errors)


def test_i20_rejects_a_missing_support_file(isolation):
    checker, surface, tree = isolation
    (tree / "plugin/harness/codex/hooks" / SUPPORT[0]).unlink()
    errors = []
    checker.check_support_modules(surface, errors)
    assert any(SUPPORT[0] in error for error in errors)
