#!/usr/bin/env python3
"""I15 (+ I-CDX-R8 a/c) - Codex hook manifest generation-time schema check.

The Codex adapter carries its own registration surface. Nothing on the host side
tells you when that surface is wrong: an unknown event name is ignored without a
word, a matcher written in the wrong tool-name face matches nothing while looking
perfectly reasonable, and an un-trusted manifest is skipped whole - no trigger,
no warning, not even a pending record. Every failure mode in this file therefore
looks exactly like "the feature just does not do anything".

So the manifest is checked at generation time instead:

  1:1        plugin/harness/codex/hooks.json is byte-for-byte render() output
  shape      top level carries `hooks` only - no description, no version
  events     only events this harness delivers; no CC-only event names
  timeouts   integer seconds; a session-end handler stays inside its hard budget
  async      no async handlers
  injection  injection handlers carry an explicit additional-context limit
  commands   every handler has both platform columns
  matchers   payload-face only - a dotted model-face name is a silent no-match
  registry   every tool name a matcher mentions is registered in the map
  literals   placeholders come in pairs; no home-directory literal ever

I-CDX-R8(a) additionally forbids reading the harness version from the CLI (that
reports the standalone binary on PATH, which was measured to differ from the
carrying core on the same machine the same day), and R8(c) forbids version
literals outside the two constants in the surface module.

Usage: python3 scripts/check_codex_hook_surface.py    (from the repo root)
Exit code: 0 = aligned, 1 = drift.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

# The repo root must be derived from this file, never from the working
# directory: this repository is worked on through git worktrees, and an
# editable install resolves a bare import to whichever checkout registered it.
# A check that silently inspects a different checkout always passes.
ROOT = Path(__file__).resolve().parent.parent
CODEX_DIR = ROOT / "plugin" / "harness" / "codex"
MANIFEST_PATH = CODEX_DIR / "hooks.json"
SURFACE_PATH = CODEX_DIR / "surface.py"

# Home-directory shaped literals. The manifest is a placeholder rendering; the
# moment somebody pastes their own absolute path in, it stops being portable and
# nothing downstream would notice.
_HOME_LITERALS = (
    "/Users/",
    "/home/",
    "~/",
    "$HOME",
    "%USERPROFILE%",
    "C:\\",
    "\\Users\\",
    ".codex/",
    ".claude",
)

# Model-face names must never reach a matcher. The host compares matchers
# literally only while they are word characters and pipes; a dot turns the whole
# matcher into a regular expression, and the dot then demands a character that
# does not exist between the two segments of the payload-face name.
_MODEL_FACE_RE = (
    re.compile(r"\bcollaboration\.[a-z_]+"),
    re.compile(r"\b(?:functions|tools)\.[a-z_]+"),
)

_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")

# Codex core versions look like 0.1NN.N with an optional pre-release tail. The
# OS's own version (1.x.y) deliberately does not match.
_CODEX_VERSION_RE = re.compile(r"\b0\.1[0-9]{2}\.[0-9]+(?:-[0-9A-Za-z.]+)?\b")

# Reading the version from the CLI, in every spelling worth catching.
_CLI_VERSION_RE = (
    re.compile(r"codex\s+--version"),
    re.compile(r"""["']codex["']\s*,\s*["']--version["']"""),
)

_SCAN_SUFFIXES = (".py", ".json", ".md", ".lock", ".rules", ".toml", ".txt")


def _load_surface():
    """Import the surface module by path, without importing the package."""
    spec = importlib.util.spec_from_file_location("codex_surface", SURFACE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _iter_handlers(manifest: dict):
    """(event, group_index, matcher, handler_index, handler) over the manifest."""
    for event, groups in manifest.get("hooks", {}).items():
        for group_index, group in enumerate(groups):
            matcher = group.get("matcher", "")
            for handler_index, handler in enumerate(group.get("hooks", [])):
                yield event, group_index, matcher, handler_index, handler


def check_render_identity(surface, text: str, errors: list[str]) -> None:
    if text != surface.render():
        errors.append(
            "hooks.json 不等于 surface.render() 的产物 —— 清单被手改过，或改了 surface 没重新生成。"
            "修法：python3 -c \"import importlib.util,pathlib;"
            "s=importlib.util.spec_from_file_location('s','plugin/harness/codex/surface.py');"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "pathlib.Path('plugin/harness/codex/hooks.json').write_text(m.render(),encoding='utf-8')\""
        )


def check_manifest_shape(manifest: dict, errors: list[str]) -> None:
    keys = set(manifest)
    if keys != {"hooks"}:
        errors.append(f"hooks.json 顶层键应恰为 {{hooks}}，实为 {sorted(keys)}")
    if "description" in manifest:
        errors.append("hooks.json 带顶层 description —— 最低支持版本会拒收整份清单")
    if "version" in manifest:
        errors.append("hooks.json 带 version 键 —— 会把发版版本锁步面从七处扩到八处，本期不扩")


def check_events(surface, manifest: dict, errors: list[str]) -> None:
    for event in manifest.get("hooks", {}):
        if event in surface.CC_ONLY_EVENTS:
            errors.append(f"{event}: CC 独有事件名 —— 本 harness 不投递，注册即永不触发且无告警")
        elif event not in surface.CODEX_SUPPORTED_EVENTS:
            errors.append(f"{event}: 不在已知可投递事件集内 —— 先补实测再登记")


def check_surface_manifest_one_to_one(surface, manifest: dict, errors: list[str]) -> None:
    """Compare the two as unordered sets of comparable rows plus ordered keys.

    render() identity already pins the text, but this comparison is what tells a
    human *which* row drifted when somebody edits both sides.
    """
    from_surface = []
    for event, matcher, kind, entries in surface.CODEX_HOOK_SURFACE:
        if kind not in surface.HANDLER_KINDS:
            errors.append(f"{event} [{matcher or '(无 matcher)'}]: kind={kind!r} 不在 {sorted(surface.HANDLER_KINDS)}")
        for script, arg, timeout, limit in entries:
            from_surface.append((event, matcher, script, arg, timeout, limit))

    from_manifest = []
    for event, _gi, matcher, _hi, handler in _iter_handlers(manifest):
        command = handler.get("command", "")
        match = re.search(r"/([\w.-]+\.py)'(?:\s+(\S+))?\s*$", command)
        if not match:
            errors.append(f"{event}: 无法解析 hook 命令 → {command[:90]!r}")
            continue
        from_manifest.append(
            (event, matcher, match.group(1), match.group(2) or "", handler.get("timeout"),
             handler.get("additionalContextLimit"))
        )

    only_surface = [row for row in from_surface if row not in from_manifest]
    only_manifest = [row for row in from_manifest if row not in from_surface]
    for row in only_surface:
        errors.append(f"CODEX_HOOK_SURFACE 有而 hooks.json 无: {row}")
    for row in only_manifest:
        errors.append(f"hooks.json 有而 CODEX_HOOK_SURFACE 无: {row}")


def check_handlers(surface, manifest: dict, errors: list[str]) -> None:
    for event, group_index, _matcher, handler_index, handler in _iter_handlers(manifest):
        where = f"{event}:{group_index}:{handler_index}"

        if handler.get("type") != "command":
            errors.append(f"{where}: type 应为 command，实为 {handler.get('type')!r}")

        if "async" in handler:
            errors.append(f"{where}: 带 async 键 —— 异步 handler 的产出与超时都不可观测，禁用")

        timeout = handler.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            errors.append(f"{where}: timeout 必须是整数秒，实为 {timeout!r}")
        elif timeout < 1:
            errors.append(f"{where}: timeout={timeout} 不合法")
        elif event == "SessionEnd" and timeout > surface.SESSION_END_MAX_TIMEOUT_SEC:
            errors.append(
                f"{where}: SessionEnd 的 timeout={timeout} 超过硬预算 "
                f"{surface.SESSION_END_MAX_TIMEOUT_SEC}s —— 超出部分的产出会被直接丢弃"
            )

        for column in ("command", "command_windows"):
            value = handler.get(column)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{where}: 缺 {column} 或为空 —— 该平台上这条 handler 无法安装")
                continue
            _check_placeholders(where, column, value, surface, errors)

        script = _script_of(handler.get("command", ""))
        limit = handler.get("additionalContextLimit")
        if script in surface.CODEX_INJECTION_SCRIPTS:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                errors.append(
                    f"{where}: 注入型 handler {script} 必须显式带 additionalContextLimit"
                    " —— 缺省时宿主会静默截断且无任何标记"
                )
        elif limit is not None:
            errors.append(f"{where}: 非注入型 handler {script} 不应带 additionalContextLimit")


def _script_of(command: str) -> str:
    match = re.search(r"/([\w.-]+\.py)'", command)
    return match.group(1) if match else ""


def _check_placeholders(where: str, column: str, value: str, surface, errors: list[str]) -> None:
    if value.count("{{") != value.count("}}"):
        errors.append(f"{where}.{column}: 占位符括号不成对 → {value!r}")
    known = {surface.PLACEHOLDER_PY, surface.PLACEHOLDER_HOOKS_DIR}
    for token in _PLACEHOLDER_RE.findall(value):
        if token not in known:
            errors.append(f"{where}.{column}: 未登记的占位符 {token} —— 安装器不会替换它")
    for placeholder in known:
        if placeholder not in value:
            errors.append(f"{where}.{column}: 缺占位符 {placeholder} —— 命令被写死了绝对路径？")


def check_matchers(surface, manifest: dict, errors: list[str]) -> None:
    for event, group_index, matcher, _hi, _handler in _iter_handlers(manifest):
        where = f"{event}:{group_index}"
        for pattern in _MODEL_FACE_RE:
            hit = pattern.search(matcher)
            if hit:
                errors.append(
                    f"{where}: matcher 出现模型面带点全名 {hit.group(0)!r} —— 会被当正则解释并零命中，"
                    "matcher 只能写 hook payload 面形态"
                )
        if matcher in surface.MATCHER_WILDCARDS:
            continue
        for token in matcher.split("|"):
            if token in surface.MATCHER_WILDCARDS or token in surface.MATCHER_PATTERN_TOKENS:
                continue
            if token in surface.TOOL_NAME_REGISTRY:
                continue
            if any(token.startswith(prefix) for prefix in surface.TOOL_NAME_PREFIX_REGISTRY):
                continue
            errors.append(
                f"{where}: matcher 片段 {token!r} 未在工具名三面登记表中登记 —— "
                "未登记名不得进 matcher（否则兜底剥前缀会被当成正式实现）"
            )


def check_no_home_literals(text: str, errors: list[str]) -> None:
    for literal in _HOME_LITERALS:
        if literal in text:
            errors.append(f"hooks.json 出现家目录字面量 {literal!r} —— 清单必须保持占位符形态")


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for base in (CODEX_DIR, ROOT / "src" / "aiteam"):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in _SCAN_SUFFIXES:
                files.append(path)
    return files


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Identity of every Constant node that is a docstring, not a value."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.add(id(value))
    return found


def _code_strings(path: Path) -> list[tuple[int, str]]:
    """(line, text) for every string that is a value in executable code.

    Comments and docstrings are excluded on purpose. Both R8 arms are about what
    the collection path *does*, and a comment cannot invoke anything: the ban on
    reading the version from the CLI is itself written as a comment in more than
    one place, and a raw-text scan would turn every such prohibition into a
    violation of itself. Sequence literals are joined so that an argument vector
    split across elements is caught the same as a single command string.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    skip = _docstring_nodes(tree)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.List | ast.Tuple):
            parts = [element.value for element in node.elts
                     if isinstance(element, ast.Constant) and isinstance(element.value, str)]
            if len(parts) > 1:
                out.append((node.lineno, " ".join(parts)))
    return out


def check_r8_version_source(errors: list[str]) -> None:
    """R8(a): the harness version may never be read from the host CLI."""
    for path in _scan_files():
        if path.suffix != ".py":
            continue
        for line, text in _code_strings(path):
            for pattern in _CLI_VERSION_RE:
                match = pattern.search(text)
                if match:
                    errors.append(
                        f"{path.relative_to(ROOT)}:{line}: 采集路径出现从 CLI 取版本的写法 "
                        f"{match.group(0)!r} —— 它报的是 PATH 上的独立二进制，不等于承载内核；"
                        "版本只认 session_meta.cli_version 与 state_5.threads.cli_version"
                    )


def check_r8_version_constants(errors: list[str]) -> None:
    """R8(c): version literals live in exactly two places, both in the surface.

    Python is scanned at code positions only (a version quoted in a comment is a
    historical statement, not an assertion that has to be refreshed). Data files
    are scanned whole, because there a literal *is* the assertion.
    """
    allowed_prefixes = ("CODEX_MIN_VERSION", "CODEX_KNOWN_UPPER_VERSION")
    for path in _scan_files():
        if path.suffix == ".py":
            source_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line, text in _code_strings(path):
                match = _CODEX_VERSION_RE.search(text)
                if not match:
                    continue
                declaration = source_lines[line - 1] if 0 < line <= len(source_lines) else ""
                if path == SURFACE_PATH and declaration.startswith(allowed_prefixes):
                    continue
                errors.append(
                    f"{path.relative_to(ROOT)}:{line}: 版本字面量 {match.group(0)!r} 散落在常量之外 —— "
                    "已知上界是滚动值，必须只钉 surface.py 的两个常量"
                )
            continue
        for number, line_text in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            match = _CODEX_VERSION_RE.search(line_text)
            if match:
                errors.append(
                    f"{path.relative_to(ROOT)}:{number}: 版本字面量 {match.group(0)!r} 散落在常量之外 —— "
                    "已知上界是滚动值，必须只钉 surface.py 的两个常量"
                )


def main() -> int:
    errors: list[str] = []

    if not MANIFEST_PATH.exists():
        print(f"[FAIL] I15: 缺 {MANIFEST_PATH.relative_to(ROOT)}")
        return 1
    if not SURFACE_PATH.exists():
        print(f"[FAIL] I15: 缺 {SURFACE_PATH.relative_to(ROOT)}")
        return 1

    try:
        surface = _load_surface()
    except Exception as exc:  # noqa: BLE001 - a surface that will not import is itself the finding
        print(f"[FAIL] I15: surface.py 无法导入 —— {type(exc).__name__}: {exc}")
        return 1
    text = MANIFEST_PATH.read_text(encoding="utf-8")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"[FAIL] I15: hooks.json 不是合法 JSON —— {exc}")
        return 1

    check_render_identity(surface, text, errors)
    check_manifest_shape(manifest, errors)
    check_events(surface, manifest, errors)
    check_surface_manifest_one_to_one(surface, manifest, errors)
    check_handlers(surface, manifest, errors)
    check_matchers(surface, manifest, errors)
    check_no_home_literals(text, errors)
    check_r8_version_source(errors)
    check_r8_version_constants(errors)

    if errors:
        print(f"[FAIL] I15/R8: {len(errors)} 处问题")
        for error in errors:
            print(f"  - {error}")
        return 1

    handlers = sum(1 for _ in _iter_handlers(manifest))
    events = len(manifest.get("hooks", {}))
    print(
        f"[OK] I15/R8: Codex hook 清单与 surface 1:1（{events} 事件 / {handlers} handler），"
        "占位符成对、matcher 全为 payload 面且已登记、版本字面量只在两个常量里"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
