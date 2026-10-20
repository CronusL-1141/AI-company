#!/usr/bin/env python3
"""检查真实 sdist 成员：拒绝本地环境/构建缓存，保留安装、源码与分发资源。

只读 tar 目录，不解包、不按体积猜测污染，也不拒绝未知源码目录。
用法：python3 scripts/check_sdist_contents.py dist/ai_team_os-*.tar.gz
"""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path, PurePosixPath

LOCAL_DIRECTORIES = frozenset({
    "node_modules", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", ".hypothesis", ".next", ".vite", "htmlcov", ".git",
})
LOCAL_PREFIXES = ((".worktrees",), ("build",), ("dist",), ("dashboard", "dist"), ("dashboard", "dist-ssr"))
REQUIRED_FILES = frozenset({
    "pyproject.toml", "README.md", "README.zh-CN.md", "LICENSE", "install.py",
    "src/aiteam/__init__.py", "src/aiteam/mcp/entry.py", "dashboard/package.json",
    "plugin/.claude-plugin/plugin.json", "plugin/dashboard-dist/index.html",
    "tests/conftest.py", "scripts/check_sdist_contents.py",
})


def is_local_artifact(parts: tuple[str, ...]) -> bool:
    """parts 相对于 sdist 顶层项目目录；dist 只排已知开发产物位置。"""
    return (
        any(part in LOCAL_DIRECTORIES or part.startswith((".venv", "venv-")) for part in parts)
        or any(parts[:len(prefix)] == prefix for prefix in LOCAL_PREFIXES)
        or bool(parts and parts[-1].endswith((".pyc", ".pyo")))
    )


def inspect_archive(path: Path) -> tuple[list[str], int]:
    problems: list[str] = []
    roots: set[str] = set()
    regular_files: set[str] = set()
    member_count = 0
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive:
            member_count += 1
            name = PurePosixPath(member.name)
            parts = name.parts
            if name.is_absolute() or ".." in parts or not parts:
                problems.append(f"非法成员路径: {member.name}")
                continue
            roots.add(parts[0])
            if len(parts) == 1:
                if not member.isdir():
                    problems.append(f"成员缺少顶层项目目录: {member.name}")
                continue
            relative_parts = parts[1:]
            if is_local_artifact(relative_parts):
                problems.append(f"本地环境或开发产物: {member.name}")
            if member.isfile():
                regular_files.add("/".join(relative_parts))
    if len(roots) != 1:
        problems.append(f"应只有一个顶层项目目录，实为 {sorted(roots)}")
    for required in sorted(REQUIRED_FILES - regular_files):
        problems.append(f"缺少必要文件: {required}")
    if not any(name.startswith("plugin/dashboard-dist/assets/") and name.endswith(".js") for name in regular_files):
        problems.append("缺少分发前端 JS: plugin/dashboard-dist/assets/*.js")
    return problems, member_count


def check_archive(path: Path) -> list[str]:
    return inspect_archive(path)[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path, nargs="+", help="实际构建出的 .tar.gz（可传多个）")
    args = parser.parse_args(argv)
    failed = False
    for path in args.archives:
        try:
            problems, member_count = inspect_archive(path)
            compressed_bytes = path.stat().st_size
        except (OSError, tarfile.TarError) as exc:
            problems = [f"无法读取 sdist: {exc}"]
        if problems:
            failed = True
            print(f"❌ {path}: sdist 内容检查失败（{len(problems)} 项）")
            for problem in problems[:20]:
                print(f"  {problem}")
            if len(problems) > 20:
                print(f"  …其余 {len(problems) - 20} 项省略")
        else:
            print(
                f"✅ {path}: sdist 内容检查通过（压缩字节={compressed_bytes}，成员数={member_count}；"
                "体积仅供核对，判定以内容门禁为准）"
            )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
