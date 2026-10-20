"""Installer import checks (aiteam and every declared dependency) match a newly launched interpreter."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "install.py"


@pytest.fixture()
def install_mod():
    spec = importlib.util.spec_from_file_location("install_verification_under_test", INSTALLER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_importable_sees_editable_path_added_after_installer_start(tmp_path):
    """Reproduce pip adding a .pth after the installer has already loaded site."""
    env = dict(os.environ, PYTHONUSERBASE=str(tmp_path / "userbase"))
    env.pop("PYTHONNOUSERSITE", None)
    env.pop("PYTHONPATH", None)
    probe = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
        env=env, capture_output=True, text=True, check=True, timeout=15,
    )
    user_site = Path(probe.stdout.strip())
    user_site.mkdir(parents=True)
    source = tmp_path / "editable-src"
    source.mkdir()
    module = "installer_fresh_pth_probe"
    (source / f"{module}.py").write_text("VALUE = 42\n", encoding="utf-8")
    driver = textwrap.dedent("""
        import importlib.util, json, pathlib, site, sys
        spec = importlib.util.spec_from_file_location("installer", sys.argv[1])
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        module, source = sys.argv[2:]
        assert site.ENABLE_USER_SITE
        before = importlib.util.find_spec(module) is not None
        (pathlib.Path(site.getusersitepackages()) / "editable-probe.pth").write_text(source + "\\n")
        after = importlib.util.find_spec(module) is not None
        print(json.dumps({"before": before, "after": after, "fresh": installer._importable(module)}))
    """)
    result = subprocess.run(
        [sys.executable, "-c", driver, str(INSTALLER), module, str(source)],
        env=env, cwd=tmp_path, capture_output=True, text=True, check=True, timeout=30,
    )
    assert json.loads(result.stdout) == {"before": False, "after": False, "fresh": True}


@pytest.mark.parametrize("body, expected", [
    ("VALUE = 42\n", True),
    ("raise RuntimeError('broken installation')\n", False),
], ids=["importable", "import-raises"])
def test_importable_really_imports(install_mod, tmp_path, monkeypatch, body, expected):
    module = "installer_import_probe"
    (tmp_path / f"{module}.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert importlib.util.find_spec(module) is not None
    assert install_mod._importable(module) is expected


def test_importable_missing_module(install_mod):
    assert install_mod._importable("installer_definitely_missing_4372c7b2") is False


@pytest.mark.parametrize("error", [
    OSError("cannot start interpreter"),
    subprocess.TimeoutExpired("python", 15),
], ids=["spawn-error", "timeout"])
def test_importable_subprocess_failure(install_mod, monkeypatch, error):
    def fail(cmd, **kwargs):
        assert cmd[0] == sys.executable
        assert 0 < kwargs["timeout"] <= 15
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert install_mod._importable("aiteam") is False


def test_dependency_check_uses_the_same_fresh_import(install_mod, tmp_path, monkeypatch):
    """A declared dependency that is findable but raises on import is reported missing."""
    module = "installer_dependency_probe"
    (tmp_path / f"{module}.py").write_text("raise ImportError('half installed')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(install_mod, "declared_dependencies", lambda root: [(module, "probe>=1")])
    monkeypatch.setattr(install_mod, "_pip_install_specs", lambda specs, root: False)
    assert importlib.util.find_spec(module) is not None
    assert install_mod.verify_runtime_dependencies(tmp_path) == ["probe>=1"]


def test_next_steps_direct_users_to_mcp_autostart():
    """Inspect printed instructions without executing any installation steps."""
    main = next(
        node for node in ast.parse(INSTALLER.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    instructions = [
        node.args[0].value for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "print" and node.args
        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
    ]
    next_steps = "\n".join(instructions[instructions.index("Next steps:"):])
    assert "Restart Claude Code" in next_steps
    assert "MCP server starts the local API automatically" in next_steps
    assert "uvicorn" not in next_steps
    assert "aiteam up" not in next_steps
    assert "/os-up" not in next_steps, "/os-up starts services; it does not create a team"
