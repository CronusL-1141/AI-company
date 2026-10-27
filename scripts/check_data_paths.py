#!/usr/bin/env python3
"""I30: the OS data directory and the host config homes are spelled in one place.

The OS data directory, the Claude Code config directory and the Codex home are
resolved by the path section of ``hook_core.py`` (three byte-identical copies,
I1); package code reads it through ``aiteam.paths``. Every other spelling of
those locations is a second resolver waiting to disagree with the first, which
is how one moved config directory took the database, the MCP registration and
the orphan check down with it (docs/config-dir-compat-design.md §1, §8).

The scan counts lines outside the allowlist that spell one of those locations
(comments and user-facing text included: they have to change as well, and the
ratchet carries the transition). The count must not exceed the ratchet passed
by ``scripts/check_invariants.sh``; a lower count asks for the ratchet to be
lowered, so it can never drift back up unnoticed.

Markdown commands and skills write paths in backticks, never in quotes, so for
``*.md`` and ``*.sh`` a bare home-relative spelling counts too.

Usage: python3 scripts/check_data_paths.py --max N [--list]
Exit: 0 = count equals N, 3 = count below N (lower the ratchet), 1 = over N or
the scan itself is broken (scope missing, allowlist copies disagree).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The patterns are assembled from escaped pieces so this file never matches itself.
_Q = "[\"']"
_HOME = r"Path\.home\(\)\s*/\s*"
_EXPAND = r"expanduser\(\s*"
_HOMES = r"\.(?:claude|codex|aiteam)"
_COMMON = (
    _HOME + _Q + r"\.claude",
    _EXPAND + _Q + r"~/\.claude",
    _Q + r"~/\.claude(?:/|" + _Q + ")",                    # the default folder, with or without a tail
    r"\.claude\.json",
    _EXPAND + _Q + "~" + _Q + r"\s*\)\s*,\s*" + _Q + r"\.claude",
    _HOME + _Q + r"\.codex",
    _EXPAND + _Q + r"~/\.codex",
    _Q + r"~/\.codex",
    _Q + r"\.aiteam" + _Q,
    r"\$HOME/\.claude",
    r"~/\.aiteam",
    r"\bhome\w*\s*/\s*" + _Q + _HOMES,                    # a home variable joined with the folder
    r"\.joinpath\(\s*" + _Q + _HOMES,                      # the folder as a joinpath part
    r"\}/" + _HOMES + r"\b",                                # an f-string interpolation, then the folder
    _Q + r"/" + _HOMES + r"\b",                              # a string starting at the folder (HOME + ...)
)
_BARE = (r"~/\.claude", r"~/\.codex")
PY_PATTERN = re.compile("|".join(_COMMON))
TEXT_PATTERN = re.compile("|".join(_COMMON + _BARE))

ALLOWLIST = (
    "plugin/hooks/hook_core.py",
    "src/aiteam/hooks/hook_core.py",
    "plugin/harness/codex/hooks/hook_core.py",
)
# The path section is built from five constants, each matching a pattern
# (LEGACY_DATA_DIR, DEFAULT_DATA_DIR, CC_DEFAULT_CONFIG_DIR, CC_DEFAULT_GLOBAL_CONFIG,
# CODEX_DEFAULT_HOME). Anything more is a new spelling.
ALLOWLIST_MAX_LINES = 5

SCOPE_DIRS = ("src/aiteam", "plugin", "scripts")


def _files(root: Path) -> list[Path]:
    files: list[Path] = []
    for name in SCOPE_DIRS:
        files += sorted((root / name).rglob("*.py"))
    files.append(root / "install.py")
    files += sorted((root / "plugin" / "commands").glob("*.md"))
    files += sorted((root / "plugin" / "skills").rglob("*.md"))
    files += sorted((root / "scripts").glob("*.sh"))
    return [path for path in files if "__pycache__" not in path.parts and path.is_file()]


def scan(root: Path) -> tuple[dict[str, list[tuple[int, str]]], list[str]]:
    """Hits per file (relative path -> [(line, text)]) and scope problems."""
    problems = [f"扫描范围缺失: {name}" for name in (*SCOPE_DIRS, "install.py") if not (root / name).exists()]
    hits: dict[str, list[tuple[int, str]]] = {}
    for path in _files(root):
        pattern = PY_PATTERN if path.suffix == ".py" else TEXT_PATTERN
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            problems.append(f"{path.relative_to(root)}: 不可读: {exc}")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.setdefault(path.relative_to(root).as_posix(), []).append((number, line.strip()))
    return hits, problems


def check(root: Path, maximum: int) -> tuple[int, int, list[str], dict[str, list[tuple[int, str]]]]:
    """(status, count outside the allowlist, messages, hits outside the allowlist)."""
    hits, problems = scan(root)
    allowed = {}
    for name in ALLOWLIST:
        if not (root / name).is_file():
            problems.append(f"{name}: 路径段所在文件缺失")
            continue
        allowed[name] = len(hits.get(name, []))
    if len(set(allowed.values())) > 1:
        problems.append(f"三份 hook_core.py 的命中数不相等: {allowed}")
    for name, count in allowed.items():
        if count > ALLOWLIST_MAX_LINES:
            problems.append(f"{name}: {count} 行 > {ALLOWLIST_MAX_LINES}（路径段只许五个常量）")
    outside = {name: rows for name, rows in hits.items() if name not in ALLOWLIST}
    count = sum(len(rows) for rows in outside.values())
    if not hits and not allowed:
        problems.append("模式零命中且路径段文件不存在: 扫描器已失效")
    if problems:
        return 1, count, problems, outside
    if count > maximum:
        return 1, count, [f"名单外 {count} 行 > 棘轮 {maximum}"], outside
    if count < maximum:
        return 3, count, [f"名单外 {count} 行 < 棘轮 {maximum}：把 I30_MAX 降到 {count}"], outside
    return 0, count, [f"名单外 {count} 行 = 棘轮 {maximum}；路径段 {sorted(set(allowed.values()))} 行/份"], outside


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max", type=int, required=True, dest="maximum")
    parser.add_argument("--list", action="store_true", help="print every hit outside the allowlist")
    args = parser.parse_args(argv)
    status, count, messages, outside = check(ROOT, args.maximum)
    print("\n".join(messages))
    if args.list or (status == 1 and count > args.maximum):
        for name in sorted(outside):
            for number, line in outside[name]:
                print(f"{name}:{number}: {line[:160]}")
    return status


if __name__ == "__main__":
    sys.exit(main())
