#!/usr/bin/env python3
"""I13 — 覆盖率同屏红线机检（token 数值不得脱离口径与分母出现）。

红线（token 用量归因 v1 设计 §4.4 / §2.5 / P2）：

    任何呈现面上的 token 数值，若其所在 scope 的 C_measure < 100%，必须在同屏同级
    显示未归因部分。缺失即视为红线违规。

以及它的前置条件——**任何一个 token 数值，脱离口径标签就没有意义**（§0.2）。本库同时
存在两个正交口径，实测差 5~25 倍；把它们并列或相加，就是刚在时间戳上栽过的同类事故。

四条守卫：

1. **口径必标**：注册表里每个 token 量纲字段都要有口径，且口径 ∈ ``aiteam.types``
   认可的封闭集合（TokenMetric 两个成员 + 不参与归因的上下文水位）。
2. **口径同屏**：口径标签必须**出现在字段旁边**（``types.py`` 里该模型的源码块内），
   不是只写在这张注册表里。页面注释是软约束，三个月后的自己会忽略它；写在字段旁边
   的口径，下一个改这行的人躲不开。
3. **aggregate 面必带分母**：跨行聚合的呈现面必须与 ``dispatches_total`` /
   ``unattributed_reasons`` 同层返回，且 ``usage_sum`` 口径的聚合面**不允许**申报缺口
   —— 归因数字的分母没有例外。
4. **row 面 no-data ≠ zero**：一行一条事实的记录面，未采集必须能与"真的是 0"区分
   （列可为 None）；做不到的必须具名申报缺口，机检每次都把它打印出来。

第 3、4 两条今天分别处在"上膛未击发"与"两处已申报缺口"的状态，这是如实的：
阶段 0 只做口径正名，聚合面由阶段 2 落地，前端徽标由阶段 5 落地。

用法: python3 scripts/check_usage_coverage.py   （仓库根目录执行）
退出码: 0=全过（申报缺口只警告不拦）, 1=有违规。
"""

from __future__ import annotations

import re
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from usage_surface import (  # noqa: E402  — 必须在 sys.path 就位之后
    AGGREGATE_REQUIRED_FIELDS,
    COVERAGE_MARKERS,
    FRONTEND_COVERAGE_GAP,
    FRONTEND_SURFACES,
    PY_SURFACES,
)

TYPES_PY = ROOT / "src" / "aiteam" / "types.py"


def _model_source(model: str) -> str:
    """取 ``types.py`` 里一个模型的源码块（class 行到下一个顶层 class 之前）。"""
    text = TYPES_PY.read_text(encoding="utf-8")
    match = re.search(rf"^class {re.escape(model)}\(", text, re.MULTILINE)
    if not match:
        return ""
    tail = text[match.start() :]
    nxt = re.search(r"\n(?=class )", tail)
    return tail[: nxt.start()] if nxt else tail


def _is_optional(annotation: object) -> bool:
    return type(None) in typing.get_args(annotation)


def check_python() -> tuple[list[str], list[str]]:
    import aiteam.types as t

    allowed_metrics = t.TOKEN_METRIC_LABELS
    problems: list[str] = []
    warnings: list[str] = []

    for surface in PY_SURFACES:
        model = getattr(t, surface.model, None)
        if model is None:
            problems.append(f"注册表声明的模型 aiteam.types.{surface.model} 不存在")
            continue
        if surface.kind not in ("row", "aggregate"):
            problems.append(f"{surface.model}: kind='{surface.kind}' 非法，只能是 row / aggregate")
            continue
        source = _model_source(surface.model)
        fields = model.model_fields
        token_fields = {n: s for n, s in surface.fields.items() if s.dimension == "token"}

        for name, spec in sorted(token_fields.items()):
            # 守卫 1：口径必标
            if not spec.metric:
                problems.append(
                    f"{surface.model}.{name}: token 数值未标口径 —— "
                    f"脱离口径的 token 数没有意义（usage_sum 与 ctx_last 实测差 5~25 倍）"
                )
                continue
            if spec.metric not in allowed_metrics:
                problems.append(
                    f"{surface.model}.{name}: 口径 '{spec.metric}' 不在 aiteam.types."
                    f"TOKEN_METRIC_LABELS（{sorted(allowed_metrics)}）"
                )
                continue
            # 守卫 2：口径同屏（标注要贴在字段旁边，不能只活在注册表里）。
            # 不区分大小写：写字面量 "ctx_last" 与写符号 ``TokenMetric.CTX_LAST``
            # 同等有效——要的是口径在字段旁边看得见，不是要一种特定拼法。
            if spec.metric.lower() not in source.lower():
                problems.append(
                    f"{surface.model}.{name}: 口径 '{spec.metric}' 只写在注册表里，"
                    f"types.py 的 {surface.model} 定义块内看不到 —— 口径标注必须与字段同屏"
                )

        if surface.kind == "aggregate":
            # 守卫 3：聚合面必带分母与未归因分类
            missing = [f for f in AGGREGATE_REQUIRED_FIELDS if f not in fields]
            if missing:
                problems.append(
                    f"{surface.model}: 聚合呈现面缺同层覆盖率字段 {missing} —— "
                    f"数值与分母、未归因必须同生共死，否则就是局部冒充全貌"
                )
            if surface.coverage_gap and any(s.metric == "usage_sum" for s in token_fields.values()):
                problems.append(
                    f"{surface.model}: usage_sum 聚合面申报了覆盖率缺口 "
                    f"{sorted(surface.coverage_gap)} —— 归因数字的分母不接受缺口申报"
                )
            continue

        # 守卫 4：row 面 no-data ≠ zero
        for name in sorted(token_fields):
            info = fields.get(name)
            if info is None:
                continue  # 字段存在性由 I12 双向比对负责
            optional = _is_optional(info.annotation)
            declared = name in surface.coverage_gap
            if not optional and not declared:
                problems.append(
                    f"{surface.model}.{name}: 非 Optional 的 token 列，0 同时表示"
                    f"'未采集'与'真的是 0' —— 要么改成可空，要么在 coverage_gap 具名申报"
                )
            elif optional and declared:
                problems.append(
                    f"{surface.model}.{name}: 字段已可空却仍挂着 coverage_gap 申报 —— 缺口已收口，请清理注册表"
                )
            elif declared:
                warnings.append(f"已申报覆盖率缺口 {surface.model}.{name}: {surface.coverage_gap[name]}")
    return problems, warnings


def check_frontend() -> tuple[list[str], list[str]]:
    """前端：usage_sum 数值一旦上页面，必须同屏带未归因标注。

    今天这条是上膛未击发——四层用量列还没有出现在任何页面上。阶段 5 的 ``/usage``
    页一落地它就自动生效，那正是最容易"先把数字放上去，覆盖率下个版本再说"的时刻。
    """
    import aiteam.types as t

    problems: list[str] = []
    warnings: list[str] = []
    dash = ROOT / "dashboard" / "src"
    if not dash.is_dir():
        warnings.append("dashboard/src 缺失（未构建环境），前端覆盖率检查跳过")
        return problems, warnings

    layers = re.compile("|".join(re.escape(x) for x in t.TOKEN_LAYERS))
    for path in sorted(dash.rglob("*.ts")) + sorted(dash.rglob("*.tsx")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not layers.search(text):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if not any(m in text for m in COVERAGE_MARKERS):
            problems.append(
                f"{rel}: 呈现了 usage_sum 四层用量却没有任何未归因/覆盖率标注 —— "
                f"同屏红线（需出现其一：{'/'.join(COVERAGE_MARKERS)}）"
            )
    warnings.append(f"前端整体缺口: {FRONTEND_COVERAGE_GAP}")
    return problems, warnings


def main() -> int:
    py_problems, py_warnings = check_python()
    fe_problems, fe_warnings = check_frontend()
    problems = py_problems + fe_problems

    for w in py_warnings + fe_warnings:
        print(f"⚠️  {w}")
    if problems:
        print("❌ 覆盖率同屏红线违规 —— token 数值不得脱离口径与分母出现:")
        for p in problems:
            print(f"  {p}")
        print(f"\n共 {len(problems)} 处。规格见 docs/token-attribution-v1-design.md §4.4 / §2.5")
        return 1

    token_fields = sum(
        1 for s in PY_SURFACES for spec in s.fields.values() if spec.dimension == "token"
    )
    gaps = sum(len(s.coverage_gap) for s in PY_SURFACES)
    print(
        f"✅ 覆盖率同屏红线通过: {token_fields} 个 token 字段全部带口径且口径与字段同屏 · "
        f"{len(FRONTEND_SURFACES)} 个前端呈现面零裸 usage_sum · 已申报缺口 {gaps} 处（见上方警告）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
