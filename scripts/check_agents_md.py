#!/usr/bin/env python3
"""红线机检 I18 —— AGENTS.md ≡ 标准头 + CLAUDE.md，且 < 32 KiB。

两个宿主读两份规则文件：一侧读 CLAUDE.md，遵循 AGENTS.md 约定的编码助手读
AGENTS.md。副本漂移不会报错、不会崩，只会让两边在两套规则下干活，事后无法分辨谁
按哪一版做的 —— 只有把恒等式钉成机检才拦得住。AGENTS.md 是生成物
（scripts/gen_agents_md.py），所以「改了 CLAUDE.md 忘了重生成」是最常见的红法，修
法就是重跑生成器并同批提交。

判据两条，各自独立报告：
1. AGENTS.md 与「标准头 + 当前 CLAUDE.md」逐字节相等；
2. AGENTS.md 总长 < 32 KiB。

本脚本只读，不改任何文件（尤其不会顺手重生成 AGENTS.md —— 机检的职责是报告漂移，
不是掩盖漂移）。退出码：0=全过，1=不等或超限。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 与生成器共读同一份恒等式定义与上限常量，避免第二真相源。
import gen_agents_md  # noqa: E402

_SHOW_LIMIT = 160


def _show(line: bytes | None) -> str:
    """把一行字节渲染成可读片段；None 表示该侧根本没有这一行。"""
    if line is None:
        return "<该侧无此行>"
    text = line.decode("utf-8", errors="replace")
    if len(text) > _SHOW_LIMIT:
        text = text[:_SHOW_LIMIT] + "…（截断）"
    return repr(text)


def first_difference(expected: bytes, actual: bytes) -> str:
    """定位首处差异并按行给出 —— 红了要能直接看出改哪儿。"""
    exp_lines = expected.splitlines()
    act_lines = actual.splitlines()
    for idx in range(max(len(exp_lines), len(act_lines))):
        exp = exp_lines[idx] if idx < len(exp_lines) else None
        act = act_lines[idx] if idx < len(act_lines) else None
        if exp != act:
            return (
                f"首处差异在第 {idx + 1} 行:\n"
                f"      应为: {_show(exp)}\n"
                f"      实为: {_show(act)}"
            )
    return "逐行内容一致，差异只在行尾符或末尾空白（本机检按字节比对）"


def main() -> int:
    try:
        expected = gen_agents_md.build_agents_md()
    except FileNotFoundError as exc:
        print(f"❌ I18: {exc}")
        return 1

    target = gen_agents_md.TARGET_PATH
    rel = target.relative_to(ROOT)
    if not target.is_file():
        print(f"❌ I18: {rel} 不存在 —— 跑 python3 scripts/gen_agents_md.py 生成并入库")
        return 1

    actual = target.read_bytes()
    limit = gen_agents_md.MAX_BYTES
    problems: list[str] = []

    if actual != expected:
        problems.append(
            f"{rel} ≠ 标准头 + CLAUDE.md（应 {len(expected)} 字节，实 {len(actual)} 字节）\n"
            f"    {first_difference(expected, actual)}"
        )
    if len(actual) >= limit:
        problems.append(f"{rel} 长 {len(actual)} 字节，触及 {limit} 字节上限")

    if problems:
        print("❌ I18 AGENTS.md 与 CLAUDE.md 已漂移:")
        for problem in problems:
            print(f"  {problem}")
        print("\n修法: python3 scripts/gen_agents_md.py，并与 CLAUDE.md 的改动同批提交。")
        return 1

    print(f"✅ I18: {rel} ≡ 标准头 + CLAUDE.md，{len(actual)} 字节 < {limit} 上限")
    return 0


if __name__ == "__main__":
    sys.exit(main())
