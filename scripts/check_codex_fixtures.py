#!/usr/bin/env python3
"""I19 — Codex 磁盘夹具与 golden 的机检薄壳。

夹具与 golden 的实质断言有 190 余条，全部住在 ``tests/unit/test_codex_fixtures.py``
里：MANIFEST 对账、脱敏不变量、路径可解析、15 项 golden 独立复算、每个生产口径键的
归属声明。本脚本**不重写任何一条**——再写一份复算实现就是第二个真相源，两份还会各自
腐烂。它只做四件测试做不到的事，然后把测试跑起来：

1. **跑测试**：pytest 不可用时**失败而不是跳过**。夹具机检被静默跳过，和没有夹具机检
   是同一回事（对照 I7 对 ruff 用的是"未安装则跳过"——那是格式检查，这是证据完整性）。
2. **采集器绑定**：``golden.generator.sha256`` 必须等于 ``compute_codex_golden.py``
   的实测哈希，``golden.manifest_sha256`` 必须等于 ``MANIFEST.json`` 的实测哈希。
   没有这一条，改采集器、改夹具都不会让任何东西红。
3. **总体 pin 同源**：采集器与脱敏器各自声明的冻结总体必须逐字相等。两边不同源时，
   golden 会算在一个总体上、夹具切自另一个总体，而两边各自都自洽——正是查不出来的那种。
4. **README 两数与磁盘一致**：文件数与字节数是同一份 README 里写下的事实，
   靠人维护就会腐烂（本条上线前它已经错了 3 个文件 / 51,188 字节）。

**哪条 golden 将来被哪条机检消费**（本期只登记不消费，防止后续期各写各的口径）：

    G1  / G2  导入基线不得入账          -> I14 回采分级谓词（P0-3）
    G3  / G14 谱系去重                  -> I12 量纲 + I13 覆盖率（P0-3）
    G4        自指份须排除              -> G3 判据的前置（本期）
    G5        哨兵行不得计入用量        -> I12（P0-3）
    G6        子会话不得并进父          -> I13 归因链（P0-2）
    G7        两种调用口径禁混          -> I12 白名单（P0-3）
    G8        系统会话不建 team         -> P0-4 验收
    G9        覆盖率分母                -> I13 + I-CDX-R3（P0-4）
    G10       派工工具名形态            -> I15 生成期硬断言（本期）+ P0-2 验收
    G11       subagent 桶冻结基线 0/2   -> I13（生产滚动桶另算，两桶禁相加）
    G12       0.142 对照基线 38508      -> I-CDX-R8(c) + P0-3 验收
    G13       幻影分区                  -> I12/I14 幻影谓词（P0-3）
    G15       导入计数 32 == 32         -> I-CDX-R5（P0-4 对钉）

用法: python3 scripts/check_codex_fixtures.py   （仓库根目录执行）
退出码: 0=全过, 1=有违规。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "codex"
GOLDEN = FIXTURES / "golden.json"
MANIFEST = FIXTURES / "MANIFEST.json"
README = FIXTURES / "README.md"
COLLECTOR = ROOT / "scripts" / "compute_codex_golden.py"
REDACTOR = ROOT / "scripts" / "redact_codex_fixture.py"
TESTS = ROOT / "tests" / "unit" / "test_codex_fixtures.py"

POPULATION_CONSTANT = "GOLDEN_POPULATION_CLI_VERSIONS"
# README 的体积纪律行：「当前全套 77 个文件、约 3.28 MB（3,437,114 字节）」
README_COUNTS = re.compile(r"当前全套\s*(\d+)\s*个文件.*?（([\d,]+)\s*字节）", re.S)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def literal_constant(path: Path, name: str):
    """从源码里取一个模块级字面量常量，不 import（脚本各有 argparse 与副作用）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name and node.value is not None:
                return ast.literal_eval(node.value)
    return None


def check_present() -> list[str]:
    missing = [str(p.relative_to(ROOT)) for p in (GOLDEN, MANIFEST, README, COLLECTOR, REDACTOR, TESTS)
               if not p.is_file()]
    return [f"夹具面缺文件: {m}" for m in missing]


def check_generator_binding() -> list[str]:
    problems = []
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    generator = golden.get("generator") or {}
    if generator.get("script") != "scripts/compute_codex_golden.py":
        problems.append(f"golden.generator.script 应指向采集器，实为 {generator.get('script')!r}")
    actual = sha256_file(COLLECTOR)
    if generator.get("sha256") != actual:
        problems.append(
            "golden.generator.sha256 与采集器实测不符 —— 改了采集器却没重算 golden："
            f"登记 {str(generator.get('sha256'))[:16]}… 实测 {actual[:16]}…")

    manifest_actual = sha256_file(MANIFEST)
    if golden.get("manifest_sha256") != manifest_actual:
        problems.append(
            "golden.manifest_sha256 与 MANIFEST 实测不符 —— 改了夹具却没重算 golden："
            f"登记 {str(golden.get('manifest_sha256'))[:16]}… 实测 {manifest_actual[:16]}…")

    # 反向不得成立：MANIFEST 登记 golden 就成环，两份互记谁都改不动。
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if "golden.json" in {e["path"] for e in manifest["files"]}:
        problems.append("MANIFEST 登记了 golden.json —— 两份互记哈希会成环，golden 只能单向记 MANIFEST")
    return problems


def check_population_pin() -> list[str]:
    problems = []
    collector = literal_constant(COLLECTOR, POPULATION_CONSTANT)
    redactor = literal_constant(REDACTOR, POPULATION_CONSTANT)
    if not collector:
        problems.append(f"采集器未声明 {POPULATION_CONSTANT} —— 生产口径总体会随本机新增会话滚动")
    if not redactor:
        problems.append(f"脱敏器未声明 {POPULATION_CONSTANT} —— 重跑脱敏会把新语料卷进夹具")
    if collector and redactor and tuple(collector) != tuple(redactor):
        problems.append(
            f"冻结总体两处不同源: 采集器 {list(collector)} vs 脱敏器 {list(redactor)} —— "
            "golden 会算在一个总体上、夹具切自另一个总体，两边各自还都自洽")

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    declared = (golden.get("population") or {}).get("cli_versions")
    if collector and list(collector) != declared:
        problems.append(f"golden.population.cli_versions {declared} 与采集器常量 {list(collector)} 不符")
    return problems


def check_readme_counts() -> list[str]:
    files = sorted(p for p in FIXTURES.rglob("*") if p.is_file())
    disk_count = len(files)
    disk_bytes = sum(p.stat().st_size for p in files)

    hit = README_COUNTS.search(README.read_text(encoding="utf-8"))
    if not hit:
        return ["夹具 README 里找不到体积纪律那两个数（文件数 / 字节数），无法对账"]
    stated_count = int(hit.group(1))
    stated_bytes = int(hit.group(2).replace(",", ""))

    problems = []
    if stated_count != disk_count:
        problems.append(f"夹具 README 写 {stated_count} 个文件，磁盘实为 {disk_count}")
    if stated_bytes != disk_bytes:
        problems.append(f"夹具 README 写 {stated_bytes:,} 字节，磁盘实为 {disk_bytes:,}")
    if problems:
        problems.append("README 改这两个数时须保持字符长度不变，否则它自己的体积会再次让本条红")
    return problems


def run_tests() -> list[str]:
    """跑夹具测试。pytest 不可用 = 失败，不是跳过。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(TESTS), "-q", "--no-header"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return []
    tail = (proc.stdout or proc.stderr).strip().splitlines()[-12:]
    reason = "pytest 不可用" if "No module named pytest" in (proc.stderr or "") else "夹具测试未通过"
    return [f"{reason}（退出码 {proc.returncode}）:"] + [f"    {line}" for line in tail]


def main() -> int:
    problems = check_present()
    if not problems:
        problems = (check_generator_binding() + check_population_pin()
                    + check_readme_counts() + run_tests())
    if problems:
        print("❌ I19 Codex 夹具与 golden 机检未通过:")
        for p in problems:
            print(f"  {p}")
        return 1

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    files = [p for p in FIXTURES.rglob("*") if p.is_file()]
    print(
        f"✅ I19 通过: {len(golden['items'])} 项 golden 绑定采集器 "
        f"{golden['generator']['sha256'][:12]}… · 冻结总体 "
        f"{'/'.join(golden['population']['cli_versions'])}（{golden['population']['rollouts']} 份）· "
        f"夹具 {len(files)} 文件 {sum(p.stat().st_size for p in files):,} 字节与 README 一致"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
