#!/usr/bin/env python3
"""生成 AGENTS.md —— 标准头 + CLAUDE.md 共享段。

CLAUDE.md 是本仓库规则的正本，Claude Code 读全文。其中 `<!-- codex:end -->` 标记之前
是两个宿主共守的项目规则（共享段），之后是 Claude Code 宿主专属段（派工模型分层、
Workflow 编排等）。AGENTS.md 只取共享段，供读取 AGENTS.md 约定的编码助手使用——
宿主专属段对它们不是指令，塞进去只会让它们每回合自己过滤一遍。

两个宿主读两份文件，副本一旦漂移就是「两套规则各干各的」，且不会有任何报错；恒等式
由红线机检 I18（scripts/check_agents_md.py）对钉。

本脚本纯确定性：只做字节切片与拼接，不写时间戳、不带随机量，两跑产物逐字节一致。
字节级读写（不走文本模式）以免宿主换行约定把产物改形。只读标准头与 CLAUDE.md，只写
AGENTS.md。共享段末尾归一为恰好一个换行，免得标记前的空行数漂进产物。

标记是块级 HTML 注释，Claude Code 注入前会剥掉，对 CLAUDE.md 的读者零开销。

规矩：改 CLAUDE.md 的提交必须同批重跑本脚本，否则 I18 变红。标记丢了同样红。

退出码：0=已生成，1=源文件缺失 / 标记缺失 / 产物超出 32 KiB 上限（此时不写盘）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HEADER_PATH = ROOT / "plugin" / "harness" / "codex" / "agents_md_header.md"
SOURCE_PATH = ROOT / "CLAUDE.md"
TARGET_PATH = ROOT / "AGENTS.md"

# CLAUDE.md 里划分共享段与宿主专属段的标记。独占一行；只认首次出现。
CODEX_END_MARKER = b"<!-- codex:end -->"

# 32 KiB —— I18 上限。按字节数而非字符数算：读方按文件大小截断，中文一字三字节。
MAX_BYTES = 32 * 1024


class MarkerMissingError(ValueError):
    """CLAUDE.md 里找不到 codex:end 标记 —— 没有标记就没有共享段的定义。"""


def slice_shared(source: bytes, name: str = "CLAUDE.md") -> bytes:
    """从正本全文切出标记之前的共享段，末尾归一为一个换行。纯函数，不碰文件系统。"""
    idx = source.find(CODEX_END_MARKER)
    if idx < 0:
        raise MarkerMissingError(
            f"{name} 缺少标记 {CODEX_END_MARKER.decode()} —— "
            f"共享段无从界定；标记独占一行放在宿主专属段之前"
        )
    return source[:idx].rstrip(b"\n") + b"\n"


def shared_slice() -> bytes:
    """读 CLAUDE.md 并切出共享段。生成器与机检共用。"""
    if not SOURCE_PATH.is_file():
        raise FileNotFoundError(f"缺少源文件: {SOURCE_PATH.relative_to(ROOT)}")
    return slice_shared(SOURCE_PATH.read_bytes(), str(SOURCE_PATH.relative_to(ROOT)))


def build_agents_md() -> bytes:
    """按恒等式拼出 AGENTS.md 的应有内容 —— 生成器与机检共用这一个函数。"""
    if not HEADER_PATH.is_file():
        raise FileNotFoundError(f"缺少源文件: {HEADER_PATH.relative_to(ROOT)}")
    return HEADER_PATH.read_bytes() + shared_slice()


def main() -> int:
    try:
        content = build_agents_md()
    except (FileNotFoundError, MarkerMissingError) as exc:
        print(f"❌ {exc}")
        return 1

    if len(content) >= MAX_BYTES:
        print(
            f"❌ AGENTS.md 将达 {len(content)} 字节，触及 {MAX_BYTES} 字节上限 —— 未写盘。\n"
            f"   先精简 CLAUDE.md 共享段或标准头，再重跑本脚本。"
        )
        return 1

    TARGET_PATH.write_bytes(content)
    header_bytes = len(HEADER_PATH.read_bytes())
    shared_bytes = len(shared_slice())
    print(
        f"✅ 已生成 {TARGET_PATH.relative_to(ROOT)}: {len(content)} 字节 "
        f"（标准头 {header_bytes} + CLAUDE.md 共享段 {shared_bytes}，上限 {MAX_BYTES}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
