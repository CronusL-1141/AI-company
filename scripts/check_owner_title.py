#!/usr/bin/env python3
"""I27 - the distributed surface carries no internal owner title.

The repository uses a three-character internal title for its owner in task
briefs and decision memos. Agents copy it into code comments, tool descriptions
and API messages, and from there it ships to every install and to the public
repository. Product text says "用户" instead.

Scope: tracked files and untracked, not ignored files under src/ and plugin/
(the scope the first cleanup used). Old occurrences under scripts/, tests/,
docs/ and the top-level documents are out of scope.

Each file is scanned as bytes for the UTF-8 form of the title and for its
\\uXXXX escape text (either hex case), the way JSON dumps with ensure_ascii and
some JS bundles spell non-ASCII text. A hit prints "path:line: excerpt".
Either directory having no file to scan fails as well: no data is not zero hits.

Blind spots: the title assembled at runtime, other escape syntaxes
(\\U0000XXXX, \\u{XXXX}, HTML entities), symlinks (skipped), and files outside
the two directories, such as dashboard/ sources before they are built into
plugin/dashboard-dist.

This file builds the title from code points, so it carries neither the title
nor its escape text.

Usage: python3 scripts/check_owner_title.py    (from the repo root)
Exit code: 0 = pass, 1 = violation.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCOPE = ("src", "plugin")
# Built from code points: this file must carry neither the title nor its escape text,
# both of which PATTERN matches.
TITLE = "".join(map(chr, (0x7F14, 0x9020, 0x8005)))
REPLACEMENT = "用户"
EXCERPT = 30  # characters of context kept on each side of a hit


def _escape_pattern(text: str) -> bytes:
    parts = []
    for ch in text:
        digits = f"{ord(ch):04x}"
        parts.append(rb"\\u" + "".join(f"[{d}{d.upper()}]" if d.isalpha() else d
                                       for d in digits).encode("ascii"))
    return b"".join(parts)


PATTERN = re.compile(re.escape(TITLE.encode("utf-8")) + b"|" + _escape_pattern(TITLE))


def _scanned_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *SCOPE],
        capture_output=True, check=True, cwd=ROOT,
    ).stdout
    return sorted({p for p in out.decode("utf-8", "surrogateescape").split("\0") if p})


def _excerpt(data: bytes, start: int, end: int) -> str:
    line_start = data.rfind(b"\n", 0, start) + 1
    line_end = data.find(b"\n", end)
    if line_end == -1:
        line_end = len(data)
    before = data[line_start:start].decode("utf-8", "replace")[-EXCERPT:]
    after = data[end:line_end].decode("utf-8", "replace")[:EXCERPT]
    text = before + data[start:end].decode("utf-8", "replace") + after
    return " ".join(text.split())


def main() -> int:
    try:
        files = _scanned_files()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"git ls-files 失败，无法列出 src/ 与 plugin/ 的文件: {exc}")
        return 1

    counts = dict.fromkeys(SCOPE, 0)
    hits = []
    hit_files = set()
    for rel in files:
        path = ROOT / rel
        if path.is_symlink() or not path.is_file():
            continue
        counts[rel.split("/", 1)[0]] += 1
        data = path.read_bytes()
        for m in PATTERN.finditer(data):
            line = data.count(b"\n", 0, m.start()) + 1
            hits.append(f"{rel}:{line}: {_excerpt(data, m.start(), m.end())}")
            hit_files.add(rel)

    problems = [f"{top}/ 下没有可扫描的文件：范围或 git 状态异常，0 处不等于没有"
                for top, n in counts.items() if n == 0]
    if hits:
        problems += hits
        problems.append(f"共 {len(hits)} 处（{len(hit_files)} 个文件），一律改为「{REPLACEMENT}」")
    if problems:
        print("\n".join(problems))
        return 1
    print("，".join(f"{top}/ {n} 个文件" for top, n in counts.items()) + "，0 处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
