#!/usr/bin/env python3
"""I21 —— 两侧的信道读者标识必须互斥。

信道未读徽章按 (reader, channel, project_id) 记已读水位。reader 是**角色标识**，由
各自入口的注册面用 argv 显式写死：CC 侧 install.py 的 HOOK_SURFACE 写 leader-cc，
Codex 侧 surface.py 的 CODEX_HOOK_SURFACE 写 leader-codex。

把这件事变成机检，是因为配错的后果既严重又完全无声：两侧若共用同一个 reader，
任意一侧读完消息推进水位，**另一侧的徽章会一起消失**——发给它的消息它永远不会被
提示，而两侧各自的测试全都照常通过。这正是这个功能反复要防的形状：全绿，用户那边
什么都不发生。

三条断言：
1. 两侧各自注册的 reader 集合不相交。
2. 同一侧内部不出现两个不同的 reader（一个入口只有一个身份）。
3. reader 非空且形如角色标识——空串会让 hook 静默退出，等于这个功能没装。

Codex 侧尚未落地该 hook 时跳过对比而非报错：适配器分批交付是既定节奏，缺席不是
违规；但只要两侧都出现了，就必须互斥。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 本机检只关心承载 reader 身份的入口。别的 hook 的 argv 是事件名，不参与。
READER_CARRYING_SCRIPTS = ("channel_unread.py", "channel_unread_codex.py")

# 角色标识的形状：字母数字连字符下划线，不含空格与路径分隔符。
_READER_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,100}$")


def _load(path: Path, name: str):
    """按文件路径加载纯数据模块。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 建立 spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cc_readers(errors: list[str]) -> set[str]:
    """从 install.py 的 HOOK_SURFACE 收集 CC 侧的 reader。

    条目形状：(event, matcher, [(script, arg, timeout), ...])
    """
    try:
        install = _load(ROOT / "install.py", "_i21_install")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"install.py 无法导入（{type(exc).__name__}: {exc}）")
        return set()

    readers: set[str] = set()
    for _event, _matcher, entries in getattr(install, "HOOK_SURFACE", []):
        for script, arg, *_rest in entries:
            if script in READER_CARRYING_SCRIPTS:
                readers.add((arg or "").strip())
    return readers


def _codex_readers(errors: list[str]) -> set[str]:
    """从 surface.py 的 CODEX_HOOK_SURFACE 收集 Codex 侧的 reader。

    条目形状：(event, matcher, kind, [(script, arg, timeout, extra), ...])
    """
    surface_path = ROOT / "plugin" / "harness" / "codex" / "surface.py"
    if not surface_path.exists():
        return set()
    try:
        surface = _load(surface_path, "_i21_surface")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"surface.py 无法导入（{type(exc).__name__}: {exc}）")
        return set()

    readers: set[str] = set()
    for entry in getattr(surface, "CODEX_HOOK_SURFACE", []):
        handlers = entry[-1]
        for handler in handlers:
            script = handler[0]
            arg = handler[1] if len(handler) > 1 else ""
            if script in READER_CARRYING_SCRIPTS:
                readers.add((arg or "").strip())
    return readers


def _check_shape(side: str, readers: set[str], errors: list[str]) -> None:
    for reader in readers:
        if not reader:
            errors.append(
                f"{side} 注册了空的 reader —— hook 拿不到身份会静默退出，等于没装"
            )
        elif not _READER_RE.match(reader):
            errors.append(f"{side} 的 reader {reader!r} 不是合法角色标识")
    if len(readers) > 1:
        errors.append(
            f"{side} 出现 {len(readers)} 个不同 reader（{sorted(readers)}）—— "
            "一个入口只应有一个身份，多身份会让水位记在谁头上变得不可预期"
        )


def evaluate(cc: set[str], codex: set[str]) -> list[str]:
    """给定两侧的 reader 集合，返回问题列表（空列表 = 通过）。

    与文件读取分离，好让单测直接构造场景——一个从未被证明能变红的机检等于没有。
    """
    errors: list[str] = []
    _check_shape("CC 侧", cc, errors)
    _check_shape("Codex 侧", codex, errors)

    overlap = cc & codex
    if overlap:
        errors.append(
            f"两侧共用 reader {sorted(overlap)} —— 一侧读完推进水位会连带清掉另一侧的"
            "徽章，发给它的消息永远不会被提示，而两侧测试都照常通过"
        )
    return errors


def main() -> int:
    errors: list[str] = []
    cc = _cc_readers(errors)
    codex = _codex_readers(errors)
    errors.extend(evaluate(cc, codex))

    if errors:
        print(f"[FAIL] I21: {len(errors)} 处读者身份问题")
        for error in errors:
            print(f"  - {error}")
        return 1

    if not cc and not codex:
        print("[OK] I21: 两侧均未注册信道读者入口（该能力尚未落地，无可比对）")
        return 0
    if not codex:
        print(f"[OK] I21: CC 侧 reader={sorted(cc)}；Codex 侧尚未落地该入口，暂无可比对")
        return 0
    if not cc:
        print(f"[OK] I21: Codex 侧 reader={sorted(codex)}；CC 侧尚未落地该入口，暂无可比对")
        return 0

    print(f"[OK] I21: 读者身份互斥（CC {sorted(cc)} ∩ Codex {sorted(codex)} = ∅）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
