#!/usr/bin/env python3
"""I20 - Codex adapter isolation (static arm).

"One core, two adapters" is a claim about what the Codex work can and cannot
reach. This check turns that claim into three assertions a diff review cannot
make as cheaply:

  1. One-way dependency. Nothing under plugin/harness/codex/ names the CC home
     directory, writes the CC global settings file, or reaches into the CC
     installer's symbols. The adapter may not quietly grow a second opinion
     about how CC is installed.
  2. Traversal isolation. The CC installer's source constants are still exactly
     {hooks, agents, skills, commands, loop.md} - the adapter directory is not
     in its walk, so installing CC cannot pick up a single Codex file. This is
     the reverse direction of assertion 1 and it is what makes "adding files
     here changes nothing on the CC side" true rather than hoped for.
  3. Same-name ban. No Codex entry script may share a file name with a CC hook.
     Three same-named scripts in one tree make the byte-identity invariants
     unreadable and make it impossible to tell, from a runtime path alone, which
     harness a file belongs to.

The runtime half of I20 - install twice, byte-identical result, CC settings
untouched - needs the installer and lands with it in a later phase. Only the
static half is here, on purpose: a check that cannot run without a Codex install
would be red on every machine that has none.

Usage: python3 scripts/check_codex_isolation.py    (from the repo root)
Exit code: 0 = isolated, 1 = leak.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODEX_DIR = ROOT / "plugin" / "harness" / "codex"
SURFACE_PATH = CODEX_DIR / "surface.py"
INSTALLER_PATH = ROOT / "install.py"
CC_HOOKS_DIR = ROOT / "plugin" / "hooks"

# The CC installer's source constants, frozen. Growing this set is exactly the
# change that would put the adapter directory into the CC install path.
EXPECTED_SOURCE_DIRS = {"hooks", "agents", "skills", "commands", "loop.md"}

# CC-side symbols the adapter must never reach for. Word boundaries matter:
# CODEX_HOOK_SURFACE is the adapter's own table and must not be mistaken for the
# installer's HOOK_SURFACE.
CC_INSTALLER_SYMBOLS = (
    "HOOK_SURFACE",
    "HOOK_SCRIPTS",
    "RETIRED_HOOK_SCRIPTS",
    "RUNTIME_HOOKS_DIRNAME",
    "register_hooks",
    "_installed_hooks_dir",
    "_is_our_hook",
)

FORBIDDEN_LITERALS = (
    (".claude", "CC 家目录字面量 —— 适配器不得知道 CC 装在哪"),
    ("settings.json", "CC 全局配置文件名 —— 适配器不得有任何写入点"),
)

# One narrow exemption, and it defends itself. The shared hook core is a
# verbatim third copy pinned byte-for-byte by I1; it legitimately names the OS
# data directory, which happens to live under the CC home path today. The
# exemption is void the moment the copy stops being verbatim, so it cannot be
# used to smuggle anything in.
VERBATIM_COPIES = {
    Path("hooks/hook_core.py"): CC_HOOKS_DIR / "hook_core.py",
}

_SOURCE_DIR_RE = re.compile(r'project_root\s*/\s*"plugin"\s*/\s*"([^"]+)"')


def _load_surface():
    spec = importlib.util.spec_from_file_location("codex_surface", SURFACE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _adapter_files() -> list[Path]:
    return [path for path in sorted(CODEX_DIR.rglob("*")) if path.is_file()]


def check_one_way_dependency(errors: list[str]) -> None:
    for path in _adapter_files():
        relative = path.relative_to(CODEX_DIR)
        source = VERBATIM_COPIES.get(relative)
        if source is not None:
            if not source.exists():
                errors.append(f"{relative}: 逐字节副本的源 {source.relative_to(ROOT)} 不存在 —— 豁免作废")
                continue
            if source.read_bytes() != path.read_bytes():
                errors.append(
                    f"{relative}: 与 {source.relative_to(ROOT)} 不再逐字节相同 —— "
                    "它的字面量豁免以「逐字节副本」为唯一前提，前提没了豁免就没了"
                )
                continue
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for number, line in enumerate(text.splitlines(), start=1):
            for literal, why in FORBIDDEN_LITERALS:
                if literal in line:
                    errors.append(f"{relative}:{number}: 出现 {literal!r} —— {why}")

        if path.suffix != ".py":
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if re.search(r"^\s*(?:from\s+install\s+import|import\s+install\b)", line):
                errors.append(f"{relative}:{number}: 引用了 CC 安装器模块 —— 适配器对它是零依赖")
            for symbol in CC_INSTALLER_SYMBOLS:
                if re.search(rf"\b{re.escape(symbol)}\b", line):
                    errors.append(f"{relative}:{number}: 引用 CC 安装器符号 {symbol} —— 适配器不得复用它")


def check_traversal_isolation(errors: list[str]) -> None:
    if not INSTALLER_PATH.exists():
        errors.append("缺 install.py —— 无法断言 CC 安装路径的取源面")
        return
    text = INSTALLER_PATH.read_text(encoding="utf-8")
    found = set(_SOURCE_DIR_RE.findall(text))
    if not found:
        errors.append("install.py 里没解析出任何取源常量 —— 取源写法变了，本判据须同批更新")
        return
    if found != EXPECTED_SOURCE_DIRS:
        extra = sorted(found - EXPECTED_SOURCE_DIRS)
        missing = sorted(EXPECTED_SOURCE_DIRS - found)
        detail = []
        if extra:
            detail.append(f"多出 {extra}")
        if missing:
            detail.append(f"缺 {missing}")
        errors.append(
            "install.py 的取源常量集合已变（" + "；".join(detail) + "）—— "
            "CC 安装面一旦扩张，plugin/harness 就可能被卷进 CC 用户机"
        )
    if re.search(r'"harness"', text):
        errors.append("install.py 出现 \"harness\" 字面量 —— CC 安装路径不得知道适配器目录的存在")


def check_same_name_ban(surface, errors: list[str]) -> None:
    if not CC_HOOKS_DIR.exists():
        errors.append("缺 plugin/hooks —— 无法断言同名禁令")
        return
    cc_names = {path.name for path in CC_HOOKS_DIR.glob("*.py")}
    for script in surface.CODEX_HOOK_SCRIPTS:
        if script in cc_names:
            errors.append(
                f"入口脚本 {script} 与 CC hook 同名 —— 同名会让副本对钉读不懂，"
                "也让运行时路径分不出属于哪个 harness"
            )


def main() -> int:
    if not CODEX_DIR.exists():
        print(f"[FAIL] I20: 缺 {CODEX_DIR.relative_to(ROOT)}")
        return 1
    if not SURFACE_PATH.exists():
        print(f"[FAIL] I20: 缺 {SURFACE_PATH.relative_to(ROOT)}")
        return 1

    errors: list[str] = []
    # The text scans run first and unconditionally: a surface module that no
    # longer imports is itself one of the leaks this check exists to name, and a
    # traceback would hide every other finding behind it.
    check_one_way_dependency(errors)
    check_traversal_isolation(errors)
    try:
        surface = _load_surface()
    except Exception as exc:  # noqa: BLE001 - any import failure is a finding
        errors.append(f"surface.py 无法导入（{type(exc).__name__}: {exc}）—— 纯数据模块不该有导入副作用")
        surface = None
    if surface is not None:
        check_same_name_ban(surface, errors)

    if errors:
        print(f"[FAIL] I20: {len(errors)} 处隔离破口")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(
        f"[OK] I20: 适配器单向依赖成立；install.py 取源集合仍为 {sorted(EXPECTED_SOURCE_DIRS)}；"
        f"{len(surface.CODEX_HOOK_SCRIPTS)} 个入口脚本名与 CC hook 零重名"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
