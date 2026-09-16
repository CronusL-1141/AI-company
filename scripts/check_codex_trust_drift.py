#!/usr/bin/env python3
"""I17b - what this release does to hooks the user has already trusted.

``check_codex_trust_lock.py`` answers "is the lock consistent with the manifest
*in this commit*". That is a necessary check and an insufficient one: it cannot
see the version the user actually installed, so the one failure mode the trust
design is most afraid of stays invisible to it.

The host keys trust on position - ``<event>:<group index>:<handler index>`` -
not on the command it approved. So when a group is removed or inserted in the
middle of an event, every later group in that event slides down one slot and
inherits the slot of its predecessor. The host does not re-ask. Two outcomes,
neither of which produces a single line of output anywhere:

  * a handler the user never approved runs under the approval of the one that
    used to sit in that slot (trust silently transferred), or
  * a handler the user did approve stops firing because its slot now belongs to
    something else (trust silently lost).

This file makes that visible by diffing the current lock against a baseline of
what was last published - ``hook-trust.baseline.lock``, advanced deliberately at
release time. Classification per key:

  ``changed``    slot reused with a different command -> FAIL. This is trust
                 transfer; the release must not ship it unnoticed.
  ``added``      new slot -> the host will ask; list it in the release notes.
  ``removed``    slot gone -> harmless, the host simply drops it.
  ``unchanged``  trust is preserved, nothing to say.

Usage:
    python3 scripts/check_codex_trust_drift.py            # check
    python3 scripts/check_codex_trust_drift.py --advance  # adopt current as baseline

Exit code: 0 = no slot reuse, 1 = at least one slot changed meaning.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODEX_DIR = ROOT / "plugin" / "harness" / "codex"
LOCK_PATH = CODEX_DIR / "hook-trust.lock"
BASELINE_PATH = CODEX_DIR / "hook-trust.baseline.lock"

_COLUMNS = ("command_sha256", "command_windows_sha256")


def _shown(path: Path) -> str:
    """Repo-relative when possible. A checkout-relative path is nicer to read, but
    failing to build the error message is worse than printing an absolute one."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _entries(path: Path) -> dict[str, dict]:
    if not path.is_file():
        raise SystemExit(f"[FAIL] I17b: 缺文件 {_shown(path)}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"[FAIL] I17b: {_shown(path)} 读不出来: {error}") from error
    return {e.get("key", ""): e for e in data.get("entries", [])}


def classify(baseline: dict[str, dict], current: dict[str, dict]) -> dict[str, list[str]]:
    """Split every key into the four trust outcomes above."""
    result: dict[str, list[str]] = {"changed": [], "added": [], "removed": [], "unchanged": []}
    for key in sorted(set(baseline) | set(current)):
        was, now = baseline.get(key), current.get(key)
        if was is None:
            result["added"].append(key)
        elif now is None:
            result["removed"].append(key)
        elif any(was.get(c) != now.get(c) for c in _COLUMNS):
            result["changed"].append(key)
        else:
            result["unchanged"].append(key)
    return result


def _released_version(path: Path) -> str:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("released_version", "?")
    except (OSError, json.JSONDecodeError):
        return "?"


def advance() -> int:
    """Adopt the current lock as the published baseline (a release-time action)."""
    current = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    baseline["entries"] = current.get("entries", [])
    BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"[OK] I17b: 基线已推进到当前清单（{len(baseline['entries'])} 条）。"
        f"记得同批把 released_version 改成本次发布的版本号。"
    )
    return 0


def main() -> int:
    if "--advance" in sys.argv:
        return advance()

    baseline, current = _entries(BASELINE_PATH), _entries(LOCK_PATH)
    verdict = classify(baseline, current)
    since = _released_version(BASELINE_PATH)

    if verdict["changed"]:
        print(
            "[FAIL] I17b: 有授信槽位被复用 —— 宿主按位置记授信，这些槽位在 "
            f"v{since} 里是另一条命令，用户不会被重新询问："
        )
        for key in verdict["changed"]:
            print(f"  · {key}  命令已变，旧授信会原样套到新命令上")
        print(
            "  改法二选一：把改动挪到该事件的尾部（后面没有 handler 会滑位），"
            "或拆成两批发布（先只换脚本内容，零失信；再改注册面）。"
        )
        return 1

    summary = (
        f"较 v{since} 发布面：保持授信 {len(verdict['unchanged'])} 条 · "
        f"新增待授信 {len(verdict['added'])} 条 · 已摘除 {len(verdict['removed'])} 条 · 零槽位复用"
    )
    print(f"[OK] I17b: {summary}")
    if verdict["added"]:
        print("  Release notes 须写明这几条要用户授信（CLI/TUI 走 /hooks，Desktop 走"
              "「设置 → 编码 → 钩子」）：")
        for key in verdict["added"]:
            print(f"  · {key}")
    if verdict["removed"]:
        for key in verdict["removed"]:
            print(f"  · {key} 已摘除，宿主会自然丢弃，无需用户动作")
    return 0


if __name__ == "__main__":
    sys.exit(main())
