#!/usr/bin/env python3
"""I12 — 用量呈现面的量纲白名单机检。

红线（token 用量归因 v1 设计 §4.4 / P1）：

    用量相关的呈现面（API schema 字段名 + 前端展示单位）只允许四种量纲：
    token（四层分列）、次数、时长毫秒、百分比。出现任何第四类之外的量纲即失败。

为什么是白名单而不是禁用词表：黑名单要穷举所有越界写法（多语言、符号、缩写、俚语）
才能生效，漏一个就破防；白名单只需确认四个合法值，新增量纲必须显式过审。

白名单要成立，前提是"没有未申报的呈现面"，所以本检查是三件事而不是一件：

1. **申报完整**：注册表里每个模型的数值字段，与 Pydantic 内省出来的实际字段**双向**
   一致。新加字段没申报 = 红；申报了却已删除 = 也红（注册表腐烂会让白名单静默失效）。
2. **量纲合法**：每个申报的量纲 ∈ 四类白名单；申报为"非用量字段"的必须写明理由。
3. **无未登记呈现面**：``aiteam.types`` 里任何带 token 字段的模型都必须在注册表里；
   前端 ``dashboard/src`` 下任何出现 token 标识符的文件同理。另加一道安全网：呈现面
   上出现金额/工时这类第五类量纲词干即红。
4. **层可用性满覆盖**：``LAYER_AVAILABILITY`` 要对 ``HarnessId`` x（四层 + 子集层）
   逐格有答案，且每个 ``wire_present_unverified`` 格在双语 i18n 里都有文案。

第 4 条是白名单哲学在 harness 维度上的同一条：量纲白名单管"这个数是什么单位"，层
可用性管"这个数**测不测得到**"。缺格的代价与漏申报同型——某个 harness 的某一层没人
回答过能不能测，而呈现面照样会给它画一个 0，那个 0 与"测过了，结果是零"长得一模
一样。所以缺格即红，多格也红（表腐烂会让满覆盖静默失效）。

**第 4 条绿时不打印任何东西**：``check_invariants.sh`` 的 I12 分支按
``✅ 量纲白名单通过: `` 做前缀剥离取摘要，成功期多打一行就会把那一行搅成一段。判据
的可见性由"红了说得清"承担，不由"绿了也吆喝一声"承担。

用法: python3 scripts/check_usage_dimensions.py   （仓库根目录执行）
退出码: 0=全过, 1=有违规。
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
    ALLOWED_DIMENSIONS,
    FORBIDDEN_UNIT_WORDS,
    FRONTEND_IDENTIFIERS,
    FRONTEND_SURFACES,
    NON_USAGE,
    PY_SURFACES,
)

# 标识符里带 token 词干 = 一个 token 量纲的数值（或它的展示载体）。
TOKEN_STEM = re.compile(r"token", re.IGNORECASE)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# camelCase / snake_case 切词：HTTPServer → ["http", "server"]，ctx_pct → ["ctx", "pct"]
WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+")

# 层可用性第三态的呈现文案键。两份都要有：只在一种语言里有文案，另一种语言的用户
# 看到的就是一个没有任何标注的 0。
LAYER_UNVERIFIED_KEY = "layerUnverified"
I18N_FILES = ("dashboard/src/i18n/zh.ts", "dashboard/src/i18n/en.ts")


def _words(identifier: str) -> list[str]:
    return [w.lower() for part in identifier.split("_") for w in WORD.findall(part)]


def _forbidden_word(identifier: str) -> str:
    """返回该标识符命中的第五类量纲词，未命中返回空串。

    单复数同判（``costs`` = ``cost``）；相邻词合并再判一次，接住 ``manHour`` /
    ``man_hour`` 这类拆开后单看都无辜的写法。
    """
    words = _words(identifier)
    for w in words:
        if w in FORBIDDEN_UNIT_WORDS or w.rstrip("s") in FORBIDDEN_UNIT_WORDS:
            return w
    for a, b in zip(words, words[1:]):
        joined = a + b
        if joined in FORBIDDEN_UNIT_WORDS or joined.rstrip("s") in FORBIDDEN_UNIT_WORDS:
            return joined
    return ""


def _is_numeric(annotation: object) -> bool:
    """int / float（含 ``X | None``）为真；bool 不算数值量纲。"""
    args = typing.get_args(annotation)
    candidates = list(args) if args else [annotation]
    return any(c in (int, float) for c in candidates)


def _numeric_fields(model: object) -> set[str]:
    return {
        name
        for name, info in model.model_fields.items()  # type: ignore[attr-defined]
        if _is_numeric(info.annotation)
    }


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """按行返回源码，跳过整行注释（注释里的解释性文字不是呈现面）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "*", "/*")):
            continue
        out.append((lineno, line))
    return out


def check_python_schema() -> list[str]:
    """API schema 侧：注册表与 Pydantic 实际字段双向比对 + 量纲白名单。"""
    import aiteam.types as t

    problems: list[str] = []
    for surface in PY_SURFACES:
        model = getattr(t, surface.model, None)
        if model is None:
            problems.append(f"注册表声明的模型 aiteam.types.{surface.model} 不存在 —— 注册表腐烂")
            continue
        actual = _numeric_fields(model)
        declared = set(surface.fields)
        for name in sorted(actual - declared):
            problems.append(
                f"{surface.model}.{name}: 用量呈现面新增了未申报量纲的数值字段 —— "
                f"在 scripts/usage_surface.py 申报其量纲（{'/'.join(ALLOWED_DIMENSIONS)}）或具名豁免"
            )
        for name in sorted(declared - actual):
            problems.append(f"{surface.model}.{name}: 注册表申报了不存在的字段 —— 删字段时请同步注册表")
        for name in sorted(declared & actual):
            spec = surface.fields[name]
            if spec.dimension == NON_USAGE:
                if not spec.note:
                    problems.append(f"{surface.model}.{name}: 申报为非用量字段但没写理由 —— 豁免必须具名")
            elif spec.dimension not in ALLOWED_DIMENSIONS:
                problems.append(
                    f"{surface.model}.{name}: 量纲 '{spec.dimension}' 不在白名单 —— "
                    f"只允许 {'/'.join(ALLOWED_DIMENSIONS)}；新增量纲必须显式过审"
                )

    # 无未登记呈现面：带 token 字段的模型必须在注册表里
    registered = {s.model for s in PY_SURFACES}
    for name in dir(t):
        model = getattr(t, name)
        fields = getattr(model, "model_fields", None)
        if not isinstance(fields, dict) or name in registered:
            continue
        hits = sorted(f for f in fields if TOKEN_STEM.search(f))
        if hits:
            problems.append(
                f"aiteam.types.{name}: 出现 token 字段 {hits} 却未登记为用量呈现面 —— "
                f"在 scripts/usage_surface.py 的 PY_SURFACES 登记"
            )
    return problems


def check_forbidden_units() -> list[str]:
    """安全网：呈现面上出现第五类量纲词干（金额/工时……）即红。"""
    problems: list[str] = []
    targets = [ROOT / "src" / "aiteam" / "types.py"]
    dash = ROOT / "dashboard" / "src"
    if dash.is_dir():
        targets += sorted(dash.rglob("*.ts")) + sorted(dash.rglob("*.tsx"))
    for path in targets:
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in _code_lines(path):
            for ident in IDENTIFIER.findall(line):
                hit = _forbidden_word(ident)
                if hit:
                    problems.append(
                        f"{rel}:{lineno}: 标识符 '{ident}' 命中第五类量纲词 '{hit}' —— "
                        f"用量呈现只以 token 表达，禁止跨量纲换算（P1）"
                    )
    return problems


def check_frontend() -> tuple[list[str], list[str]]:
    """前端：呈现面文件双向登记 + token 标识符量纲白名单。"""
    problems: list[str] = []
    warnings: list[str] = []
    dash = ROOT / "dashboard" / "src"
    if not dash.is_dir():
        warnings.append("dashboard/src 缺失（未构建环境），前端量纲检查跳过")
        return problems, warnings

    declared = set(FRONTEND_SURFACES)
    found: dict[str, set[str]] = {}
    for path in sorted(dash.rglob("*.ts")) + sorted(dash.rglob("*.tsx")):
        rel = path.relative_to(ROOT).as_posix()
        idents = {
            ident
            for _lineno, line in _code_lines(path)
            for ident in IDENTIFIER.findall(line)
            if TOKEN_STEM.search(ident)
        }
        if idents:
            found[rel] = idents

    for rel in sorted(set(found) - declared):
        problems.append(
            f"{rel}: 出现 token 标识符 {sorted(found[rel])} 却未登记为用量呈现面 —— "
            f"在 scripts/usage_surface.py 的 FRONTEND_SURFACES 登记"
        )
    for rel in sorted(declared - set(found)):
        problems.append(f"{rel}: 注册表登记为用量呈现面，但文件已不含 token 标识符（或文件不存在）—— 请同步注册表")
    for rel in sorted(set(found) & declared):
        for ident in sorted(found[rel]):
            dim = FRONTEND_IDENTIFIERS.get(ident)
            if dim is None:
                problems.append(
                    f"{rel}: token 标识符 '{ident}' 未登记量纲 —— "
                    f"在 FRONTEND_IDENTIFIERS 里说清它是什么量纲"
                )
            elif dim not in ALLOWED_DIMENSIONS:
                problems.append(f"{rel}: 标识符 '{ident}' 的量纲 '{dim}' 不在白名单")
    for ident in sorted(set(FRONTEND_IDENTIFIERS) - {i for s in found.values() for i in s}):
        warnings.append(f"FRONTEND_IDENTIFIERS 里的 '{ident}' 已无人使用，可清理")
    return problems, warnings


def check_layer_availability() -> list[str]:
    """层可用性矩阵满覆盖 + ``wire_present_unverified`` 的双语文案。

    两件事，同一个理由——**0 有三种含义，而呈现面上它们长得一样**：

    * 满覆盖：``LAYER_AVAILABILITY`` 的键集必须**恰好**等于 ``HarnessId`` x
      （``TOKEN_LAYERS`` + ``TOKEN_SUBSET_LAYERS``）。缺一格 = 有一层没人回答过
      "这个 harness 测不测得到"，而页面照画 0；多一格 = 表里留着一个已删的 harness
      或已改名的层，满覆盖于是变成一句空话。双向比对，两侧都红。
    * 文案：标成 ``wire_present_unverified`` 的格必须在 **zh 与 en 两份** i18n 里
      都有 ``layerUnverified`` 键。没有文案的"未验证态"在页面上与已定真的 0 无从
      区分——那正是这个态被引入要解决的问题，缺文案等于把它引入了又不用。

    i18n 那半边在 ``dashboard/src`` 缺失时（未构建环境 / 纯后端 CI）跳过：文件不在
    就不是"文案缺失"，是"这台机器上没有前端"。满覆盖那半边永远跑。
    """
    import aiteam.types as t

    problems: list[str] = []
    layers = tuple(t.TOKEN_LAYERS) + tuple(t.TOKEN_SUBSET_LAYERS)
    expected = {(h.value, layer) for h in t.HarnessId for layer in layers}
    actual = {(str(h), str(layer)) for h, layer in t.LAYER_AVAILABILITY}

    for harness, layer in sorted(expected - actual):
        problems.append(
            f"LAYER_AVAILABILITY 缺格 ({harness}, {layer}): 没人回答过这个 harness 的这一层"
            f"测不测得到 —— 呈现面照样会画一个 0，而它与'测过了结果是零'无从区分"
        )
    for harness, layer in sorted(actual - expected):
        problems.append(
            f"LAYER_AVAILABILITY 多格 ({harness}, {layer}): 不在 HarnessId x "
            f"(TOKEN_LAYERS + TOKEN_SUBSET_LAYERS) 之内 —— 删 harness / 改层名时请同步该表"
        )
    for key, state in sorted(t.LAYER_AVAILABILITY.items(), key=lambda kv: tuple(map(str, kv[0]))):
        if not isinstance(state, t.LayerState):
            problems.append(
                f"LAYER_AVAILABILITY[{tuple(map(str, key))}] = {state!r} 不是 LayerState 成员 —— "
                f"三态是封闭集合（{'/'.join(s.value for s in t.LayerState)}）"
            )

    unverified = sorted(
        tuple(map(str, key))
        for key, state in t.LAYER_AVAILABILITY.items()
        if state == t.LayerState.WIRE_PRESENT_UNVERIFIED
    )
    if unverified:
        marker = re.compile(rf"\b{re.escape(LAYER_UNVERIFIED_KEY)}\s*:")
        for rel in I18N_FILES:
            path = ROOT / rel
            if not path.is_file():
                continue  # 未构建环境：没有前端，不是缺文案
            if not marker.search(path.read_text(encoding="utf-8")):
                problems.append(
                    f"{rel}: 缺 '{LAYER_UNVERIFIED_KEY}' 键，而层可用性表里有 "
                    f"{len(unverified)} 个 wire_present_unverified 格"
                    f"（{'、'.join('/'.join(k) for k in unverified)}）—— "
                    f"没有文案的未验证态在页面上与已定真的 0 无从区分"
                )
    return problems


def main() -> int:
    problems = check_python_schema() + check_forbidden_units() + check_layer_availability()
    fe_problems, warnings = check_frontend()
    problems += fe_problems

    for w in warnings:
        print(f"⚠️  {w}")
    if problems:
        print("❌ 量纲白名单违规 —— 用量呈现面只许出现 token/次数/时长毫秒/百分比:")
        for p in problems:
            print(f"  {p}")
        print(f"\n共 {len(problems)} 处。规格见 docs/token-attribution-v1-design.md §4.4")
        return 1

    py_fields = sum(len(s.fields) for s in PY_SURFACES)
    print(
        f"✅ 量纲白名单通过: {len(PY_SURFACES)} 个 API schema 呈现面 / {py_fields} 个数值字段 · "
        f"{len(FRONTEND_SURFACES)} 个前端呈现面 / {len(FRONTEND_IDENTIFIERS)} 个 token 标识符，"
        f"零第五类量纲"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
