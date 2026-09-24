#!/usr/bin/env python3
"""I22 - user-visible hook lines leave through one exit (``user_notice.py``).

Every line a supported hook shows the user goes through ``user_notice.emit``,
which is where the prefix, the 160-column limit, the colour rules, the host's
field whitelist and the one-document-per-run rule are enforced. A hook that
writes ``systemMessage`` itself, or spells out user-facing wording (restart
instructions, "tell Claude ...", trust prompts, command names shown to the
user), bypasses all of that and brings back the noise the notice ledger exists
to budget. On Claude Code 2.1.281 a PreToolUse ``permissionDecisionReason``
is shown to the user too (as the red "hook error" line of a block), so it is a
user-facing field like ``systemMessage``.

Checks the CC hook directories and ``plugin/harness/codex/hooks/*.py``:

1. The literals ``systemMessage`` and ``permissionDecisionReason`` appear only
   in ``user_notice.py``.
2. The user-facing words below appear only in ``user_notice.py``.
3. Exceptions live in ``ALLOWED`` with a written reason (empty today).
4. Every sibling module a distributed hook loads (``user_notice.py``) is copied
   by the source installer (``install.py`` HOOK_SCRIPTS + HOOK_SUPPORT_MODULES):
   a hook copied without it silently loses every user line.

Usage: python3 scripts/check_user_notice_exit.py [repo root]
Exit code: 0 = clean, 1 = violation.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

EXIT_MODULE = "user_notice.py"
HOOK_DIRS = ("plugin/hooks", "src/aiteam/hooks", "plugin/harness/codex/hooks")
USER_FACING = (
    "systemMessage",
    "permissionDecisionReason",
    "重启 Claude Code",
    "请重启",
    "授信",
    "已装好",
    "对 Claude 说",
    "对 Codex 说",
    "/os-doctor",
    "/os-up",
    "Restart Claude Code",
    "Tell Claude",
    "Tell Codex",
)
# (path relative to the repo root, literal) -> why this hook may carry it.
ALLOWED: dict[tuple[str, str], str] = {}

_SIBLING_REF = re.compile(r"""["']([A-Za-z_][\w-]*\.py)["']|^\s*(?:from|import)\s+([A-Za-z_]\w*)""", re.M)


def _load_installer(root: Path):
    spec = importlib.util.spec_from_file_location("_i22_installer", root / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(root: Path) -> tuple[list[str], str]:
    problems: list[str] = []
    scanned = 0
    for directory in HOOK_DIRS:
        for path in sorted((root / directory).glob("*.py")):
            if path.name == EXIT_MODULE:
                continue
            scanned += 1
            relative = path.relative_to(root).as_posix()
            lines = path.read_text(encoding="utf-8").splitlines()
            for literal in USER_FACING:
                if (relative, literal) in ALLOWED:
                    continue
                for number, line in enumerate(lines, start=1):
                    if literal in line:
                        problems.append(
                            f"{relative}:{number}: {literal!r} outside {EXIT_MODULE} "
                            "(user lines go through user_notice.emit / emit_block)"
                        )

    installer = _load_installer(root)
    distributed = set(installer.HOOK_SCRIPTS)
    shipped = distributed | set(getattr(installer, "HOOK_SUPPORT_MODULES", ()))
    plugin_dir = root / "plugin" / "hooks"
    siblings = {path.name for path in plugin_dir.glob("*.py")}
    for name in sorted(distributed):
        path = plugin_dir / name
        if not path.is_file():
            continue
        for match in _SIBLING_REF.finditer(path.read_text(encoding="utf-8")):
            referenced = match.group(1) or f"{match.group(2)}.py"
            if referenced in siblings and referenced != name and referenced not in shipped:
                problems.append(
                    f"plugin/hooks/{name} loads {referenced}, which install.py does not copy "
                    "(add it to HOOK_SUPPORT_MODULES)"
                )

    summary = (f"{scanned} hook scripts free of user-facing literals; "
               f"{len(shipped) - len(distributed)} support module(s) shipped with the source install")
    return sorted(set(problems)), summary


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    problems, summary = check(root)
    if problems:
        print("\n".join(problems))
        return 1
    print(f"[OK] I22: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
