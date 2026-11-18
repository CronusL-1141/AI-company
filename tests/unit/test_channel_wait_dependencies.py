"""Keep runtime dependencies present in all supported installation paths."""

import ast
import importlib.util
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {"httpx": ">=0.28.1", "websockets": ">=15.0", "psutil": ">=5.9.8"}


def test_channel_wait_dependency_declarations():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    plugin = [
        line for line in (ROOT / "plugin/requirements.txt").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    for declarations in [pyproject["project"]["dependencies"], plugin]:
        specs = {item.name: str(item.specifier) for item in map(Requirement, declarations)}
        for name, version in REQUIRED.items():
            assert specs[name] == version


def test_installer_takes_dependencies_from_pyproject_only():
    """install.py repairs missing dependencies from pyproject, with no hand-copied list to fall behind.

    Its old fallback list had drifted (four declared dependencies missing) before it was removed.
    """
    spec = importlib.util.spec_from_file_location("install_dependency_source", ROOT / "install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    declared = {Requirement(spec).name: str(Requirement(spec).specifier)
                for _, spec in installer.declared_dependencies(ROOT)}
    for name, version in REQUIRED.items():
        assert declared[name] == version
    constants = {node.value for node in ast.walk(ast.parse((ROOT / "install.py").read_text()))
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    pinned = {name + version for name, version in REQUIRED.items()}
    assert not pinned & constants, "install.py re-grew a dependency list"
