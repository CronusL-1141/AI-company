#!/usr/bin/env python3
"""红线机检 I11 — 全库只许有一个时钟。

事故背景（docs/utc-unification-design.md）：这个库曾同时跑两个墙钟，核心域写
``datetime.now()``（宿主本地），ecosystem 域写 ``datetime.now(tz=UTC)``。SQLite
落库把 aware datetime 的 offset 静默剥掉，两制的行长得一模一样，于是跨域比较
返回一个偏 8 小时的答案而**不抛任何异常**。它活了几个月，一次审计抓到三处。

双墙钟不是谁决定的，是一个模块一个模块随手写出来的。所以修完还不够——没有机检，
同样的事会再发生一次，而且同样不会有人看见。

三条守卫：

1. Python 侧不许有裸墙钟：``datetime.now()`` / ``datetime.now(UTC)`` /
   ``datetime.fromtimestamp()`` 一律走 ``aiteam.clock``。
2. Python 侧不许手工去偏移：``astimezone().replace(tzinfo=None)`` 是把值搬回本地
   时钟的老动作，已由 ``to_naive_utc()`` 取代。
3. 前端不许裸解析服务端时间串：``new Date(<字符串>)`` 对不带偏移的串按浏览器本地
   时区读，正是 ecosystem 页时间偏早 8 小时的成因。一律走 ``@/lib/datetime``。

退出码：0=全过，1=有违规。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# hook 脚本是脱离 aiteam 包运行的独立进程（且必须与 plugin/hooks 逐字节一致，I1），
# 无法 import aiteam.clock；它们本就只用 UTC。
PY_EXEMPT_DIRS = ("src/aiteam/hooks",)
PY_EXEMPT_FILES = (
    "src/aiteam/clock.py",
    "src/aiteam/storage/utc_type.py",
    "scripts/check_clock_convention.py",  # 本文件的模式串会匹配自己
)
SCRIPT_EXEMPT = (
    "scripts/hn_monitor.py",  # 与生产数据无关的独立小工具
    # 平移脚本的职责就是把本地墙钟值换算成 UTC，比较"本地 now"与"UTC now"正是它的
    # 判据本身。它也刻意不 import aiteam —— 要能对着一个裸 .db 文件跑，不依赖包。
    "scripts/migrate_timestamps_utc.py",
)

TS_EXEMPT = ("dashboard/src/lib/datetime.ts",)

PY_RULES = [
    (r"\bdatetime\.now\(", "裸墙钟调用 —— 改用 aiteam.clock.utc_now()"),
    (
        r"\bdatetime\.fromtimestamp\(",
        "裸 fromtimestamp 会给时刻贴上宿主本地偏移 —— 改用 aiteam.clock.from_timestamp()",
    ),
    (
        r"astimezone\(\)\s*\.replace\(tzinfo=None\)",
        "手工去偏移 —— 改用 aiteam.clock.to_naive_utc()",
    ),
]

# new Date(<非空、非纯数字、非模板串的参数>)
TS_RULE = re.compile(r"new Date\(\s*(?!\s*\))(?![\d`])")


def _iter_lines(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "*", "/*")):
            continue
        yield lineno, line


def check_python() -> list[str]:
    problems: list[str] = []
    for base in ("src", "scripts"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in PY_EXEMPT_FILES or rel in SCRIPT_EXEMPT:
                continue
            if any(rel.startswith(d) for d in PY_EXEMPT_DIRS):
                continue
            for lineno, line in _iter_lines(path):
                for pattern, why in PY_RULES:
                    if re.search(pattern, line):
                        problems.append(f"{rel}:{lineno}: {why}\n    {line.strip()}")
    return problems


def check_dashboard() -> list[str]:
    problems: list[str] = []
    src = ROOT / "dashboard" / "src"
    if not src.is_dir():
        return problems
    for path in sorted(src.rglob("*.ts")) + sorted(src.rglob("*.tsx")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in TS_EXEMPT:
            continue
        for lineno, line in _iter_lines(path):
            for m in TS_RULE.finditer(line):
                tail = line[m.end() :]
                arg = tail.split(")")[0].strip()
                # 纪元数值（如 ts * 1000）是绝对时刻，本来就没有时区歧义
                if "* 1000" in arg or re.fullmatch(r"[\d_.]+", arg):
                    continue
                problems.append(
                    f"{rel}:{lineno}: 裸解析服务端时间串 —— 改用 @/lib/datetime\n"
                    f"    {line.strip()}"
                )
    return problems


def main() -> int:
    problems = check_python() + check_dashboard()
    if problems:
        print("❌ 时钟约定违规 —— 库里不许出现第二个时钟:")
        for p in problems:
            print(f"  {p}")
        print(f"\n共 {len(problems)} 处。规格见 docs/utc-unification-design.md")
        return 1
    py = sum(1 for _ in (ROOT / "src").rglob("*.py"))
    ts = sum(1 for _ in (ROOT / "dashboard" / "src").rglob("*.ts*")) if (
        ROOT / "dashboard" / "src"
    ).is_dir() else 0
    print(f"✅ 时钟约定统一: {py} 个 Python 模块 / {ts} 个前端模块，零裸墙钟、零裸解析")
    return 0


if __name__ == "__main__":
    sys.exit(main())
