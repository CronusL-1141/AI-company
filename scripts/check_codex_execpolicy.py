#!/usr/bin/env python3
"""I16 - Codex execution-policy rules check (warning level).

The adapter ships a user-level rules file so that what the OS runs on a user's
machine is declared rather than implied. The host can validate that file itself,
which is the only validator that matters: a rules file this repository considers
fine but the host rejects would be discovered at install time on somebody else's
machine, silently degrading into "no policy at all".

Three states, and only the last one is red:

  rules file absent      -> WARN. Rules generation lands in a later phase; this
                            check goes in first so it can never be forgotten.
  host CLI absent        -> WARN. Continuous integration has no such binary and
                            most contributors will not either. A check that is
                            red on every machine without the tool teaches people
                            to ignore the whole invariant suite.
  validator says no      -> FAIL, with the validator's own output.

Deliberately absent: any attempt to read the CLI version to decide whether it is
a stable build. The only version sources this project trusts are the two session
fields; asking the binary reports the standalone one on PATH, which was measured
to differ from the carrying core on the same machine on the same day
(I-CDX-R8(a) makes that a static ban). So an unusable validator is reported as a
warning rather than being judged against a version.

Usage: python3 scripts/check_codex_execpolicy.py    (from the repo root)
Exit code: 0 = pass or warn, 1 = the host rejected the rules.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "plugin" / "harness" / "codex" / "ai-team-os.rules"

HOST_CLI = "codex"
_SUBCOMMAND = ("execpolicy", "check")

# Substrings that mean "this build has no such subcommand" rather than "your
# rules are wrong". Getting this distinction wrong in either direction is the
# only way this check can lie.
_UNSUPPORTED_MARKERS = (
    "unrecognized subcommand",
    "unknown subcommand",
    "unexpected argument",
    "no such subcommand",
    "invalid subcommand",
)


def main() -> int:
    if not RULES_PATH.exists():
        print(
            f"[WARN] I16: 未找到 {RULES_PATH.relative_to(ROOT)} —— execpolicy rules 归后续阶段，"
            "本期只先把机检位子占住"
        )
        return 0

    binary = shutil.which(HOST_CLI)
    if binary is None:
        print(f"[WARN] I16: 本机没有 {HOST_CLI} 可执行文件 —— 跳过 rules 校验（CI 与多数开发机都没有）")
        return 0

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [binary, *_SUBCOMMAND, str(RULES_PATH)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("[WARN] I16: rules 校验超时 —— 不作红判，请人工跑一次")
        return 0
    except OSError as exc:
        print(f"[WARN] I16: 无法执行 rules 校验 —— {exc}")
        return 0

    output = f"{completed.stdout}{completed.stderr}".strip()
    if completed.returncode == 0:
        print(f"[OK] I16: {RULES_PATH.relative_to(ROOT)} 通过宿主 execpolicy 校验")
        return 0

    lowered = output.lower()
    if any(marker in lowered for marker in _UNSUPPORTED_MARKERS):
        print(f"[WARN] I16: 本机 {HOST_CLI} 不支持 {' '.join(_SUBCOMMAND)} 子命令 —— 跳过校验")
        return 0

    print(f"[FAIL] I16: 宿主拒收 {RULES_PATH.relative_to(ROOT)}（退出码 {completed.returncode}）")
    for line in output.splitlines():
        print(f"  - {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
