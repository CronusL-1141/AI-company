#!/usr/bin/env python3
"""I29: Dashboard English copy does not use em dashes.

Only en.ts is in scope, with no exceptions: missing-data markers use "-" too.
Raw characters and TypeScript Unicode escapes are checked; composed runtime
strings are not.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path("dashboard/src/i18n/en.ts")
PATTERN = re.compile("\u2014" + r"|\\u2014|\\u\{0*2014\}", re.IGNORECASE)


def check(root: Path) -> list[str]:
    try:
        text = (root / SOURCE).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"{SOURCE}: 无法读取英文文案: {exc}"]
    if not text.strip():
        return [f"{SOURCE}: 英文文案为空，不能视为检查通过"]
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        if PATTERN.search(line):
            problems.append(f"{SOURCE}:{number}: {line.strip()}")
    return problems


def main() -> int:
    problems = check(ROOT)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"{SOURCE}: 无 em dash（无例外）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
