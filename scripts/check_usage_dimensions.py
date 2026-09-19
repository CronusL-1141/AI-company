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
5. **独立价格契约**：具名 Pricing 模型全字段登记并核对类型、量纲和请求口径，
   AST 只放行已登记字段及可追溯引用；旧 schema 和前端不继承价格例外。

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

import ast
import bisect
import re
import sys
import typing
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import AwareDatetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from usage_surface import (  # noqa: E402  — 必须在 sys.path 就位之后
    ALLOWED_DIMENSIONS,
    FORBIDDEN_UNIT_WORDS,
    FRONTEND_IDENTIFIERS,
    FRONTEND_SURFACES,
    NON_USAGE,
    PRICING_DIMENSIONS,
    PRICING_FRONTEND_SURFACES,
    PRICING_I18N_FIELDS,
    PRICING_NON_NUMERIC,
    PRICING_SURFACES,
    PY_SURFACES,
)

# 标识符里带 token 词干 = 一个 token 量纲的数值（或它的展示载体）。
TOKEN_STEM = re.compile(r"token", re.IGNORECASE)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# camelCase / snake_case 切词：HTTPServer → ["http", "server"]，ctx_pct → ["ctx", "pct"]
WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+")
TS_TOKEN = re.compile(
    r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    r'`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|[^\s]', re.DOTALL,
)

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
    """Include Decimal and numeric containers; literals and bool are labels."""
    if typing.get_origin(annotation) is typing.Literal:
        return False
    return annotation in (int, float, Decimal) or any(
        _is_numeric(arg) for arg in typing.get_args(annotation)
    )


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
    registered = {s.model for s in PY_SURFACES} | {s.model for s in PRICING_SURFACES}
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


def _annotation_models(annotation: object) -> set[type]:
    """Find model dependencies through unions and containers, including aliases."""
    found = {annotation} if isinstance(getattr(annotation, "model_fields", None), dict) else set()
    for arg in typing.get_args(annotation):
        found.update(_annotation_models(arg))
    return found


def _has_decimal(annotation: object) -> bool:
    return annotation is Decimal or any(_has_decimal(arg) for arg in typing.get_args(annotation))


def _explicit_non_numeric(annotation: object, models: set[type]) -> bool:
    """Do not let Any or an arbitrary nested schema hide unregistered numbers."""
    if annotation in {str, bool, type(None), datetime, AwareDatetime} or annotation in models:
        return True
    if typing.get_origin(annotation) is typing.Literal:
        return True
    args = typing.get_args(annotation)
    return bool(args) and all(_explicit_non_numeric(arg, models) for arg in args)


def check_pricing_schema() -> list[str]:
    """Validate every pricing field without changing the old usage vocabulary."""
    import aiteam.types as t

    problems: list[str] = []
    registered = {s.model: s for s in PRICING_SURFACES}
    if len(registered) != len(PRICING_SURFACES):
        problems.append("Pricing 注册表模型重复")
    pricing_models = set()
    for name in dir(t):
        model = getattr(t, name)
        if not isinstance(getattr(model, "model_fields", None), dict):
            continue
        if name.startswith("Pricing") or model.__name__.startswith("Pricing"):
            pricing_models.add(model)
            if name not in registered or name != model.__name__ or model.__module__ != t.__name__:
                problems.append(f"aiteam.types.{name}: 未登记的 Pricing 模型或重导出")

    for name, surface in registered.items():
        model = getattr(t, name, None)
        fields = getattr(model, "model_fields", None)
        if not isinstance(fields, dict):
            problems.append(f"{name}: 已登记 Pricing 模型不存在")
            continue
        if not name.startswith("Pricing") or name in {s.model for s in PY_SURFACES}:
            problems.append(f"{name}: Pricing 契约不得兼作旧用量呈现面")
        if model.model_config.get("extra") != "forbid":
            problems.append(f"{name}: Pricing 必须禁止未登记的 extra 字段")
        if model.model_computed_fields:
            problems.append(f"{name}: Pricing 计算字段未纳入字段注册，不能隐式导出")
        for field in sorted(set(fields) - set(surface.fields)):
            problems.append(f"{name}.{field}: Pricing 字段未登记量纲或非数值理由")
        for field in sorted(set(surface.fields) - set(fields)):
            problems.append(f"{name}.{field}: Pricing 注册字段不存在")
        for field in sorted(set(fields) & set(surface.fields)):
            info, spec = fields[field], surface.fields[field]
            label = f"{name}.{field}"
            numeric = _is_numeric(info.annotation)
            extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
            if any(alias is not None and alias != field
                   for alias in (info.alias, info.validation_alias, info.serialization_alias)):
                problems.append(f"{label}: Pricing 字段别名不得绕过具名注册")
            if spec.dimension == PRICING_NON_NUMERIC:
                if not _explicit_non_numeric(info.annotation, pricing_models) or not spec.note:
                    problems.append(f"{label}: 非数值登记必须类型非数值且具名说明理由")
            elif spec.dimension not in PRICING_DIMENSIONS:
                problems.append(f"{label}: Pricing 量纲不在独立白名单")
            elif not numeric:
                problems.append(f"{label}: Pricing 数值量纲与字段类型不符")
            elif extra.get("dimension") != spec.dimension:
                problems.append(f"{label}: Pricing 字段量纲元数据与注册表不一致")
            if spec.dimension == "token" and (
                spec.metric != "usage_sum" or extra.get("metric") != "usage_sum"
            ):
                problems.append(f"{label}: Pricing token 必须标注单请求 usage_sum 口径")

    for name in dir(t):
        model = getattr(t, name)
        fields = getattr(model, "model_fields", None)
        if not isinstance(fields, dict) or name in registered:
            continue
        for field, info in fields.items():
            if _annotation_models(info.annotation) & pricing_models:
                problems.append(f"{name}.{field}: 旧模型不得引用或重导出 Pricing 契约")
            extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
            if (_has_decimal(info.annotation) or ("amount" in _words(field) and _is_numeric(info.annotation))
                    or extra.get("dimension") in {"money_usd", "usd_per_million"}):
                problems.append(f"{name}.{field}: 旧模型不得声明价格字段，必须使用独立 Pricing 契约")

    response = getattr(t, "PricingQuoteResponse", None)
    total = getattr(response, "model_fields", {}).get("total_usd")
    if total is not None and (type(None) not in typing.get_args(total.annotation) or total.default == 0):
        problems.append("PricingQuoteResponse.total_usd: 不完整报价必须可返回 null，不得默认 0")
    return problems


def _pricing_source_contract(source: str) -> tuple[list[str], set[tuple[int, int, int, int]]]:
    """Allow only named declarations, metadata literals and self field references."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"types.py: Pricing AST 解析失败: {exc}"], set()
    registered = {s.model: s for s in PRICING_SURFACES}
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    allowed: set[tuple[int, int, int, int]] = set()
    permitted_refs: set[ast.AST] = set()
    problems: list[str] = []
    seen_classes: set[str] = set()
    annotations: dict[str, dict[str, ast.expr]] = {}

    def exempt(node: ast.AST) -> None:
        allowed.add((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset))

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not node.name.startswith("Pricing"):
            continue
        if node.name not in registered:
            problems.append(f"types.py:{node.lineno}: Pricing 类 {node.name} 未登记")
            continue
        if node.name in seen_classes or parents[node] is not tree:
            problems.append(f"{node.name}: Pricing 类必须在模块顶层唯一定义")
        seen_classes.add(node.name)
        offset = node.col_offset + 6
        allowed.add((node.lineno, offset, node.lineno, offset + len(node.name)))
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            exempt(node.body[0].value)
        if len(node.bases) != 1 or not isinstance(node.bases[0], ast.Name) or node.bases[0].id != "BaseModel":
            problems.append(f"{node.name}: Pricing 模型必须独立直接继承 BaseModel")
        specs = registered[node.name].fields
        annotations[node.name] = {}
        for field in node.body:
            if not isinstance(field, ast.AnnAssign) or not isinstance(field.target, ast.Name):
                continue
            spec = specs.get(field.target.id)
            if spec is None:
                if field.target.id != "model_config":
                    problems.append(f"{node.name}.{field.target.id}: Pricing 源码字段未登记")
                continue
            annotations[node.name][field.target.id] = field.annotation
            exempt(field.target)
            permitted_refs.update(ast.walk(field.annotation))
            if (isinstance(field.value, ast.Call) and isinstance(field.value.func, ast.Name)
                    and field.value.func.id == "Field" and isinstance(field.annotation, ast.Name)):
                for keyword in field.value.keywords:
                    if (keyword.arg == "default_factory" and isinstance(keyword.value, ast.Name)
                            and keyword.value.id == field.annotation.id and keyword.value.id in registered):
                        permitted_refs.add(keyword.value)
            for value in ast.walk(field):
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    continue
                if value.value == spec.dimension or (
                    field.target.id == "currency" and value.value == "USD"
                ):
                    exempt(value)

    def reference_model(expr: ast.AST | None, bindings: dict[str, str | None]) -> str | None:
        if isinstance(expr, ast.Name):
            return bindings.get(expr.id)
        if isinstance(expr, ast.Attribute):
            model = reference_model(expr.value, bindings)
            annotation = annotations.get(model, {}).get(expr.attr)
            if annotation is not None:
                names = {node.id for node in ast.walk(annotation) if isinstance(node, ast.Name)}
                names &= registered.keys()
                return next(iter(names)) if len(names) == 1 else None
        if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            local = dict(bindings)
            for generator in expr.generators:
                if isinstance(generator.target, ast.Name):
                    local[generator.target.id] = reference_model(generator.iter, local)
            return reference_model(expr.elt, local)
        return None

    for node in ast.walk(tree):
        owner = parents.get(node)
        method = None
        ancestors = []
        while owner is not None and not isinstance(owner, ast.ClassDef):
            ancestors.append(owner)
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method = owner
            owner = parents.get(owner)
        specs = registered[owner.name].fields if owner is not None and owner.name in registered else {}
        if (isinstance(node, ast.Name) and node.id in registered
                and (node in permitted_refs or (specs and method is not None))):
            exempt(node)
        if specs and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            validators = {"field_validator", "model_validator"}
            if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id in validators
                   for d in node.decorator_list):
                offset = node.col_offset + (10 if isinstance(node, ast.AsyncFunctionDef) else 4)
                allowed.add((node.lineno, offset, node.lineno, offset + len(node.name)))
        if specs and method is not None and isinstance(node, ast.Attribute):
            bindings: dict[str, str | None] = {"self": owner.name}
            for statement in method.body:
                if statement.end_lineno >= node.lineno:
                    for target in ast.walk(statement):
                        if (isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store)
                                and (target.lineno, target.col_offset) < (node.lineno, node.col_offset)):
                            bindings.pop(target.id, None)
                    break
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    model = reference_model(statement.value, bindings)
                    for target in targets:
                        if isinstance(target, ast.Name):
                            bindings[target.id] = model
                else:
                    # Unknown control flow cannot establish a safe receiver type.
                    for target in ast.walk(statement):
                        if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store):
                            bindings.pop(target.id, None)
            for ancestor in reversed(ancestors):
                if isinstance(ancestor, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
                    for generator in ancestor.generators:
                        if isinstance(generator.target, ast.Name):
                            bindings[generator.target.id] = reference_model(generator.iter, bindings)
            receiver = reference_model(node.value, bindings)
            if receiver in registered and node.attr in registered[receiver].fields:
                # Exempt the attribute only; its receiver is still checked.
                allowed.add((
                    node.end_lineno, node.end_col_offset - len(node.attr),
                    node.end_lineno, node.end_col_offset,
                ))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in specs:
            exempt(node)
        references: list[str] = []
        if isinstance(node, ast.Name):
            references = [node.id]
        elif isinstance(node, ast.Attribute):
            references = [node.attr]
        elif isinstance(node, ast.alias):
            references = [node.name.rsplit(".", 1)[-1]]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            references = IDENTIFIER.findall(node.value)
        for ref in references:
            if (ref.startswith("Pricing") and node not in permitted_refs
                    and not (specs and method is not None)):
                # Class documentation is not a schema export.
                parent = parents.get(node)
                if isinstance(parent, ast.Expr) and owner is not None and owner.body[0] is parent:
                    continue
                problems.append(f"types.py:{node.lineno}: Pricing 引用 {ref} 不在已登记字段类型内")
    return problems, allowed


def check_pricing_source() -> list[str]:
    source = (ROOT / "src" / "aiteam" / "types.py").read_text(encoding="utf-8")
    return _pricing_source_contract(source)[0]


def check_forbidden_units() -> list[str]:
    """安全网：呈现面上出现第五类量纲词干（金额/工时……）即红。"""
    problems: list[str] = []
    frontend = {s.path: s for s in PRICING_FRONTEND_SURFACES}
    targets = [ROOT / "src" / "aiteam" / "types.py"]
    dash = ROOT / "dashboard" / "src"
    if dash.is_dir():
        targets += sorted(dash.rglob("*.ts")) + sorted(dash.rglob("*.tsx"))
    for path in targets:
        rel = path.relative_to(ROOT).as_posix()
        exemptions = set()
        if path.suffix == ".py":
            _, exemptions = _pricing_source_contract(path.read_text(encoding="utf-8"))
        elif rel in PRICING_I18N_FIELDS:
            _, exemptions = _pricing_i18n_contract(path.read_text(encoding="utf-8"), PRICING_I18N_FIELDS[rel])
        for lineno, line in _code_lines(path):
            for match in IDENTIFIER.finditer(line):
                ident = match.group()
                surface = frontend.get(rel)
                if surface is not None and ident in surface.identifiers:
                    spec = surface.identifiers[ident]
                    if spec.dimension in PRICING_DIMENSIONS or (spec.dimension == PRICING_NON_NUMERIC and spec.note):
                        continue
                # AST offsets are UTF-8 byte offsets; regex offsets are characters.
                start = len(line[:match.start()].encode("utf-8"))
                end = len(line[:match.end()].encode("utf-8"))
                if any((a, b) <= (lineno, start) and (lineno, end) <= (c, d)
                       for a, b, c, d in exemptions):
                    continue
                hit = _forbidden_word(ident)
                if hit:
                    problems.append(
                        f"{rel}:{lineno}: 标识符 '{ident}' 命中第五类量纲词 '{hit}' —— "
                        f"用量呈现只以 token 表达，禁止跨量纲换算（P1）"
                    )
    return problems


def _typescript_interfaces(source: str) -> dict[str, set[str]]:
    """Read bounded interface/type object declarations, without requiring Node."""
    tokens = [m.group() for m in TS_TOKEN.finditer(source) if not m.group().startswith(("//", "/*"))]
    interfaces: dict[str, set[str]] = {}
    for index, token in enumerate(tokens[:-2]):
        if token not in {"interface", "type"}:
            continue
        name = tokens[index + 1]
        cursor = index + 2
        if token == "type":
            if tokens[cursor:cursor + 1] != ["="]:
                continue
            cursor += 1
            if tokens[cursor:cursor + 1] == ["|"]:
                cursor += 1
            if tokens[cursor:cursor + 1] != ["{"]:
                if tokens[cursor].startswith("Pricing"):
                    interfaces[name] = {"<unregistered schema alias>"}
                continue
        if tokens[cursor] != "{":
            interfaces[name] = {"<unsupported schema declaration>"}
            continue
        fields = set()
        depth = 1
        cursor += 1
        while cursor < len(tokens) and depth:
            value = tokens[cursor]
            if depth == 1 and re.fullmatch(r"[A-Za-z_$][\w$]*|\"[^\"]+\"|'[^']+'", value):
                after = cursor + 1 + (tokens[cursor + 1:cursor + 2] == ["?"])
                if tokens[after:after + 1] == [":"]:
                    field = value.strip("\"'")
                    if field in fields and token == "interface":
                        fields.add("<duplicate schema field>")
                    fields.add(field)
            if depth == 1 and value == "[" and tokens[cursor - 1] in {"{", ";", ","}:
                fields.add("<unregistered index signature>")
            if value == "{":
                depth += 1
            elif value == "}":
                depth -= 1
                if depth == 0 and token == "type" and tokens[cursor + 1:cursor + 3] == ["|", "{"]:
                    depth = 1
                    cursor += 2
            cursor += 1
        if name in interfaces or depth:
            fields.add("<duplicate or unfinished schema>")
        interfaces[name] = fields
    return interfaces


def _pricing_i18n_contract(source: str, fields: dict) -> tuple[list[str], set[tuple[int, int, int, int]]]:
    """Limit translated units to exact message keys, never their surrounding file."""
    tokens = [m for m in TS_TOKEN.finditer(source) if not m.group().startswith(("//", "/*"))]
    spans: dict[str, list[tuple[int, int]]] = {}
    namespaces = {key.split(".", 1)[0] for key in fields}
    for index, match in enumerate(tokens[:-2]):
        namespace = match.group().strip("\"'")
        if namespace not in namespaces or [t.group() for t in tokens[index + 1:index + 3]] != [":", "{"]:
            continue
        depth = 1
        current = None
        start = 0
        for cursor in range(index + 3, len(tokens)):
            token = tokens[cursor].group()
            if depth == 1 and cursor + 1 < len(tokens) and tokens[cursor + 1].group() == ":":
                if current is not None:
                    spans.setdefault(current, []).append((start, tokens[cursor].start()))
                current = namespace + "." + token.strip("\"'")
                start = tokens[cursor].start()
            if token in {"{", "[", "("}:
                depth += 1
            elif token in {"}", "]", ")"}:
                depth -= 1
            if depth == 0:
                if current is not None:
                    spans.setdefault(current, []).append((start, tokens[cursor].start()))
                break
    offsets = [0] + [m.end() for m in re.finditer("\n", source)]
    allowed = set()
    problems = []
    for key, identifiers in fields.items():
        if len(spans.get(key, [])) != 1:
            problems.append(f"Pricing 文案 {key} 缺失或重复")
            continue
        start, end = spans[key][0]
        found = set()
        for match in IDENTIFIER.finditer(source, start, end):
            ident = match.group()
            if ident not in identifiers:
                continue
            spec = identifiers[ident]
            if spec.dimension not in PRICING_DIMENSIONS and not (
                spec.dimension == PRICING_NON_NUMERIC and spec.note
            ):
                problems.append(f"Pricing 文案 {key}.{ident} 未登记合法单位或文案理由")
                continue
            found.add(ident)
            line = bisect.bisect_right(offsets, match.start()) - 1
            left = len(source[offsets[line]:match.start()].encode("utf-8"))
            right = len(source[offsets[line]:match.end()].encode("utf-8"))
            allowed.add((line + 1, left, line + 1, right))
        for ident in sorted(set(identifiers) - found):
            problems.append(f"Pricing 文案 {key}.{ident} 已无人使用")
    return problems, allowed


def check_pricing_frontend() -> list[str]:
    """Check exact account files and identifiers, including complete TS schemas."""
    problems: list[str] = []
    dash = ROOT / "dashboard" / "src"
    if not dash.is_dir():
        return problems
    registered = {s.path: s for s in PRICING_FRONTEND_SURFACES}
    models = {s.model: s for s in PRICING_SURFACES}
    if len(registered) != len(PRICING_FRONTEND_SURFACES):
        problems.append("Pricing 前端路径重复登记")
    for rel, fields in PRICING_I18N_FIELDS.items():
        path = ROOT / rel
        if not path.is_file():
            problems.append(f"{rel}: 已登记 Pricing 文案文件不存在")
        else:
            errors, _ = _pricing_i18n_contract(path.read_text(encoding="utf-8"), fields)
            problems.extend(f"{rel}: {error}" for error in errors)
    for rel, surface in registered.items():
        if not (ROOT / rel).is_file():
            problems.append(f"{rel}: 已登记 Pricing 前端文件不存在")
        if rel in FRONTEND_SURFACES:
            problems.append(f"{rel}: 旧用量前端面不得改为 Pricing 豁免面")
        for ident, spec in surface.identifiers.items():
            if spec.dimension not in PRICING_DIMENSIONS and not (
                spec.dimension == PRICING_NON_NUMERIC and spec.note
            ):
                problems.append(f"{rel}: Pricing 标识符 {ident} 未声明合法量纲或非数值理由")
    for path in sorted(dash.rglob("*.ts")) + sorted(dash.rglob("*.tsx")):
        rel = path.relative_to(ROOT).as_posix()
        surface = registered.get(rel)
        identifiers = {ident for _, line in _code_lines(path) for ident in IDENTIFIER.findall(line)}
        for ident in sorted(identifiers):
            if ident.startswith("Pricing") or (surface is not None and TOKEN_STEM.search(ident)):
                if surface is None or ident not in surface.identifiers:
                    problems.append(f"{rel}: Pricing/token 标识符 {ident} 未按文件具名登记")
        if surface is None:
            continue
        declarations = _typescript_interfaces(path.read_text(encoding="utf-8"))
        for model in sorted(set(declarations) | set(surface.interfaces) | set(surface.local_interfaces)):
            if model in surface.local_interfaces:
                expected = set(surface.local_interfaces[model])
            elif model in surface.interfaces and model in models:
                expected = set(models[model].fields)
            else:
                problems.append(f"{rel}: TypeScript schema {model} 未具名登记")
                continue
            actual = declarations.get(model, set())
            if actual != expected:
                problems.append(
                    f"{rel}: {model} 字段登记不一致，新增 {sorted(actual - expected)}，"
                    f"缺失 {sorted(expected - actual)}"
                )
        for ident in sorted(set(surface.identifiers) - identifiers):
            problems.append(f"{rel}: Pricing 标识符 {ident} 已无人使用，请同步注册表")
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
        if rel in {s.path for s in PRICING_FRONTEND_SURFACES}:
            continue  # Checked by its own exact path and field registry.
        exemptions = set()
        if rel in PRICING_I18N_FIELDS:
            _, exemptions = _pricing_i18n_contract(path.read_text(encoding="utf-8"), PRICING_I18N_FIELDS[rel])
        idents = {
            match.group()
            for lineno, line in _code_lines(path)
            for match in IDENTIFIER.finditer(line)
            if TOKEN_STEM.search(match.group())
            and (lineno, len(line[:match.start()].encode("utf-8")),
                 lineno, len(line[:match.end()].encode("utf-8"))) not in exemptions
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
    problems = (check_python_schema() + check_pricing_schema() + check_pricing_source() + check_pricing_frontend()
                + check_forbidden_units() + check_layer_availability())
    fe_problems, warnings = check_frontend()
    problems += fe_problems

    for w in warnings:
        print(f"⚠️  {w}")
    if problems:
        print("❌ 量纲白名单违规 —— 旧用量四量纲与独立 Pricing 契约检查:")
        for p in problems:
            print(f"  {p}")
        print(f"\n共 {len(problems)} 处。规格见 docs/token-attribution-v1-design.md §4.4")
        return 1

    py_fields = sum(len(s.fields) for s in PY_SURFACES)
    print(
        f"✅ 量纲白名单通过: {len(PY_SURFACES)} 个 API schema 呈现面 / {py_fields} 个数值字段 · "
        f"{len(FRONTEND_SURFACES)} 个前端呈现面 / {len(FRONTEND_IDENTIFIERS)} 个 token 标识符，"
        f"旧面零第五类量纲；{len(PRICING_SURFACES)} 个独立 Pricing 契约"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
