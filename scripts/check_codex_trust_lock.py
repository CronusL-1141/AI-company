#!/usr/bin/env python3
"""I17 - Codex hook trust lock check.

The host trusts hooks per registration declaration, keyed on (manifest path,
snake_case event, group index, handler index). Two consequences drive this file:

  * Replacing a hook script's contents costs nothing. Measured: a trusted script
    was swapped whole, the manifest untouched, and every handler stayed enabled
    and kept firing. So a lock over script bytes would cry wolf on ordinary code
    changes and stay silent on the one change that actually matters.
  * Changing the declaration costs the user a manual re-trust, and until they do
    it the observability layer is not degraded but *gone* - un-trusted hooks are
    skipped without a trigger, without a warning, without even a pending record.

The lock therefore freezes the declaration and only the declaration. This check
recomputes it from the manifest and compares. A mismatch is not a formatting
nit: it is the release-notes trigger, and the printed list is the set of
handlers the user will have to trust again.

Usage: python3 scripts/check_codex_trust_lock.py    (from the repo root)
Exit code: 0 = lock matches the manifest, 1 = drift.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODEX_DIR = ROOT / "plugin" / "harness" / "codex"
MANIFEST_PATH = CODEX_DIR / "hooks.json"
LOCK_PATH = CODEX_DIR / "hook-trust.lock"
SURFACE_PATH = CODEX_DIR / "surface.py"

_COLUMNS = ("command_sha256", "command_windows_sha256")


def _load_surface():
    spec = importlib.util.spec_from_file_location("codex_surface", SURFACE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _by_key(entries: list[dict]) -> dict[str, dict]:
    return {entry.get("key", ""): entry for entry in entries}


def _diff(expected: list[dict], actual: list[dict]) -> list[str]:
    """The un-trust list: which handlers the user would have to trust again."""
    problems: list[str] = []
    expected_map, actual_map = _by_key(expected), _by_key(actual)

    for key in expected_map:
        if key not in actual_map:
            problems.append(f"{key}: lock 里缺这一条 —— 新注册的 handler，用户须为它单独授信")
    for key in actual_map:
        if key not in expected_map:
            problems.append(f"{key}: lock 有而清单无 —— 注册面已摘除，卸载时须从尾部摘")
    for key, want in expected_map.items():
        have = actual_map.get(key)
        if have is None:
            continue
        for column in _COLUMNS:
            if want.get(column) != have.get(column):
                platform = "Unix" if column == "command_sha256" else "Windows"
                problems.append(
                    f"{key}: {platform} 面注册声明已变 —— 该 handler 失信，"
                    f"lock={have.get(column)} 实算={want.get(column)}"
                )

    expected_order = [entry.get("key") for entry in expected]
    actual_order = [entry.get("key") for entry in actual]
    if expected_order != actual_order and set(expected_order) == set(actual_order):
        problems.append(
            "条目顺序与清单不一致 —— 序号是信任键的一部分，中间插删会让其后所有 handler 换键（批量失信）"
        )
    return problems


def main() -> int:
    for path in (MANIFEST_PATH, LOCK_PATH, SURFACE_PATH):
        if not path.exists():
            print(f"[FAIL] I17: 缺 {path.relative_to(ROOT)}")
            return 1

    try:
        surface = _load_surface()
    except Exception as exc:  # noqa: BLE001 - a surface that will not import is itself the finding
        print(f"[FAIL] I17: surface.py 无法导入 —— {type(exc).__name__}: {exc}")
        return 1
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[FAIL] I17: hooks.json 不是合法 JSON —— {exc}")
        return 1

    expected_text = surface.render_trust_lock(manifest)
    actual_text = LOCK_PATH.read_text(encoding="utf-8")
    if expected_text == actual_text:
        entries = json.loads(actual_text).get("entries", [])
        print(f"[OK] I17: hook-trust.lock 与 hooks.json 逐条相符（{len(entries)} 条注册声明）")
        return 0

    try:
        actual = json.loads(actual_text).get("entries", [])
    except json.JSONDecodeError as exc:
        print(f"[FAIL] I17: hook-trust.lock 不是合法 JSON —— {exc}")
        return 1

    expected = json.loads(expected_text).get("entries", [])
    problems = _diff(expected, actual)
    print(f"[FAIL] I17: hook-trust.lock 与 hooks.json 不符 —— 失信清单 {len(problems)} 条")
    for problem in problems:
        print(f"  - {problem}")
    if not problems:
        print("  - 条目相同但文件头字段不同 —— lock 须由 surface.render_trust_lock() 重新生成")
    print(
        "  处置：确认注册面改动是有意的，重新生成 lock，并在 Release notes 写明"
        "「本次升级需重新授信」；授信指引按入口分列（CLI/TUI 走斜杠命令，桌面端走设置 → 编码 → 钩子）"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
