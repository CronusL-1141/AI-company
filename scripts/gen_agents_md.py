#!/usr/bin/env python3
"""生成 AGENTS.md —— 标准头 + CLAUDE.md 原文。

AGENTS.md 是 CLAUDE.md 的等价副本，供读取 AGENTS.md 约定的编码助手使用。两个宿主
读两份文件，副本一旦漂移就是「两套规则各干各的」，且不会有任何报错；恒等式由红线
机检 I18（scripts/check_agents_md.py）对钉。

本脚本纯确定性：只做字节拼接，不写时间戳、不带随机量，两跑产物逐字节一致。字节级
读写（不走文本模式）以免宿主换行约定把产物改形。只读标准头与 CLAUDE.md，只写
AGENTS.md。

规矩：改 CLAUDE.md 的提交必须同批重跑本脚本，否则 I18 变红。

退出码：0=已生成，1=源文件缺失或产物超出 32 KiB 上限（此时不写盘）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HEADER_PATH = ROOT / "plugin" / "harness" / "codex" / "agents_md_header.md"
SOURCE_PATH = ROOT / "CLAUDE.md"
TARGET_PATH = ROOT / "AGENTS.md"

# 32 KiB —— I18 上限。按字节数而非字符数算：读方按文件大小截断，中文一字三字节。
MAX_BYTES = 32 * 1024


def build_agents_md() -> bytes:
    """按恒等式拼出 AGENTS.md 的应有内容 —— 生成器与机检共用这一个函数。"""
    for path in (HEADER_PATH, SOURCE_PATH):
        if not path.is_file():
            raise FileNotFoundError(f"缺少源文件: {path.relative_to(ROOT)}")
    return HEADER_PATH.read_bytes() + SOURCE_PATH.read_bytes()


def main() -> int:
    try:
        content = build_agents_md()
    except FileNotFoundError as exc:
        print(f"❌ {exc}")
        return 1

    if len(content) >= MAX_BYTES:
        print(
            f"❌ AGENTS.md 将达 {len(content)} 字节，触及 {MAX_BYTES} 字节上限 —— 未写盘。\n"
            f"   先精简 CLAUDE.md 或标准头，再重跑本脚本。"
        )
        return 1

    TARGET_PATH.write_bytes(content)
    header_bytes = len(HEADER_PATH.read_bytes())
    source_bytes = len(SOURCE_PATH.read_bytes())
    print(
        f"✅ 已生成 {TARGET_PATH.relative_to(ROOT)}: {len(content)} 字节 "
        f"（标准头 {header_bytes} + CLAUDE.md {source_bytes}，上限 {MAX_BYTES}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
