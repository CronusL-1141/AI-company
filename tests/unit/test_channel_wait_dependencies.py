"""Keep channel-wait dependencies present in all supported installation paths."""

import ast
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {"httpx": ">=0.28.1", "websockets": ">=15.0"}


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


def test_channel_wait_dependencies_in_installer_fallback():
    tree = ast.parse((ROOT / "install.py").read_text())
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "run" and node.args and isinstance(node.args[0], ast.List)
        ):
            continue
        arguments = {item.value for item in node.args[0].elts if isinstance(item, ast.Constant)}
        if {"pip", "install", "fastmcp>=3.4.5,<4"} <= arguments:
            assert {name + version for name, version in REQUIRED.items()} <= arguments
            return
    raise AssertionError("The explicit pip dependency fallback was not found")
