"""用真实 Hatchling 和轻量污染目录验证 sdist；不安装依赖、不访问用户环境。

只有两个构建用例需要 Hatchling（dev extras 才装，本机系统 Python 默认没有）：缺包时这两个
跳过，其余纯 tarfile 的门禁用例照跑。CI 设 AITEAM_REQUIRE_HATCHLING=1，缺包直接报错，不许
静默跳过。
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REQUIRE_HATCHLING = "AITEAM_REQUIRE_HATCHLING"


def sdist_builder():
    """Hatchling's SdistBuilder: skipped where it is not installed, an error where CI requires it."""
    if os.environ.get(REQUIRE_HATCHLING, "").strip() not in ("", "0"):
        from hatchling.builders.sdist import SdistBuilder  # a missing backend fails, never skips

        return SdistBuilder
    return pytest.importorskip(
        "hatchling.builders.sdist", reason="hatchling is a dev extra; CI sets AITEAM_REQUIRE_HATCHLING=1",
    ).SdistBuilder

SCRIPT = ROOT / "scripts" / "check_sdist_contents.py"
spec = importlib.util.spec_from_file_location("sdist_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

# Every entry becomes a real (tiny) file, not a symlink to a shared environment.
POLLUTION = (
    "dashboard/node_modules/react/package.json",
    "nested/node_modules/dependency.js",
    ".venv-test/lib/python3.12/site-packages/local.py",
    "nested/.venv-backup/local.py",
    ".venv/local.py",
    "venv/local.py",
    "venv-old/local.py",
    "src/aiteam/__pycache__/local.pyc",
    "src/aiteam/loose.pyc",
    "src/aiteam/loose.pyo",
    ".pytest_cache/local",
    ".mypy_cache/local",
    ".ruff_cache/local",
    ".tox/local",
    ".nox/local",
    ".hypothesis/local",
    "dashboard/.next/local.js",
    "dashboard/.vite/local.js",
    "htmlcov/index.html",
    ".worktrees/other/local.py",
    "build/local.py",
    "dist/old.tar.gz",
    "dashboard/dist/index.html",
    "dashboard/dist-ssr/local.js",
)
LEGAL_EXTRA = (
    "plugin/dashboard-dist/assets/main.js",
    "src/aiteam/new_feature.py",
    "src/aiteam/build/compiler.py",
    "tests/fixtures/dist/fixture.json",
    "venvtools/source.py",
    "unknown-source/module.py",
)


def write_source(source: Path, *, exclude_artifacts: bool) -> None:
    files = {name: "source fixture\n" for name in check.REQUIRED_FILES | set(LEGAL_EXTRA) | set(POLLUTION)}
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if not exclude_artifacts:
        # Reverse verification: restore the prior configuration without sdist excludes.
        before, section = pyproject.split("[tool.hatch.build.targets.sdist]\n", 1)
        _, after = section.split("[tool.ruff]\n", 1)
        pyproject = before + "[tool.ruff]\n" + after
    files["pyproject.toml"] = pyproject
    for name, body in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def archive_names(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        return {"/".join(Path(member.name).parts[1:]) for member in archive}


def test_backend_excludes_pollution_without_gitignore_and_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_source(source, exclude_artifacts=True)
    assert not (source / ".git").exists()
    assert not (source / ".gitignore").exists()
    path = Path(next(sdist_builder()(str(source)).build(directory=str(tmp_path / "out"))))
    names = archive_names(path)
    assert not (set(POLLUTION) & names)
    assert set(LEGAL_EXTRA) <= names
    assert {"install.py", "LICENSE", "src/aiteam/mcp/entry.py", "tests/conftest.py"} <= names
    assert check.check_archive(path) == []
    assert check.main([str(path)]) == 0


def test_prior_configuration_leaks_real_environment_directory(tmp_path: Path) -> None:
    # Relies on the backend default: Hatchling (1.32 series, the pinned range) has no built-in
    # exclusion for these directories. A release that adds one turns this reverse case red
    # without any regression in pyproject.toml.
    source = tmp_path / "source"
    write_source(source, exclude_artifacts=False)
    path = Path(next(sdist_builder()(str(source)).build(directory=str(tmp_path / "out"))))
    assert ".venv-test/lib/python3.12/site-packages/local.py" in archive_names(path)
    assert "dashboard/node_modules/react/package.json" in archive_names(path)
    assert any("本地环境或开发产物" in issue for issue in check.check_archive(path))
    assert check.main([str(path)]) == 1


def make_archive(path: Path, extra: str | None = None, *, missing: str | None = None) -> None:
    names = check.REQUIRED_FILES | set(LEGAL_EXTRA)
    if extra:
        names = names | {extra}
    with tarfile.open(path, "w:gz") as archive:
        for name in sorted(names - {missing}):
            member = tarfile.TarInfo(f"ai_team_os-1.0/{name}")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))


@pytest.mark.parametrize("extra", POLLUTION, ids=[f"artifact-{index}" for index in range(len(POLLUTION))])
def test_cli_rejects_injected_artifact(tmp_path: Path, extra: str) -> None:
    path = tmp_path / "polluted.tar.gz"
    make_archive(path, extra)
    result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert extra in result.stdout


def test_cli_keeps_unknown_source_and_reports_missing_distribution_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "source.tar.gz"
    make_archive(path)
    assert check.main([str(path)]) == 0
    output = capsys.readouterr().out
    assert f"压缩字节={path.stat().st_size}" in output
    assert f"成员数={len(archive_names(path))}" in output
    assert "判定以内容门禁为准" in output
    make_archive(path, missing="plugin/dashboard-dist/index.html")
    assert check.main([str(path)]) == 1
    assert "缺少必要文件: plugin/dashboard-dist/index.html" in check.check_archive(path)


def test_cli_fails_for_unreadable_archive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "broken.tar.gz"
    path.write_bytes(b"not gzip")
    assert check.main([str(path)]) == 1
    assert "无法读取 sdist" in capsys.readouterr().out


@pytest.mark.parametrize("required", ["1", ""], ids=["ci-required", "local"])
def test_missing_hatchling_skips_locally_but_fails_where_required(monkeypatch, required: str) -> None:
    monkeypatch.setitem(sys.modules, "hatchling.builders.sdist", None)  # import raises ImportError
    monkeypatch.setenv(REQUIRE_HATCHLING, required)
    expected = ImportError if required else pytest.skip.Exception
    with pytest.raises(expected):
        sdist_builder()


def test_build_backend_range_matches_the_tested_range() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    backend = [spec for spec in data["build-system"]["requires"] if spec.startswith("hatchling")]
    tested = [spec for spec in data["project"]["optional-dependencies"]["dev"] if spec.startswith("hatchling")]
    assert backend == tested and len(backend) == 1, (backend, tested)
    assert "<" in backend[0], "an unbounded backend can change the sdist file selection silently"
