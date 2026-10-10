#!/usr/bin/env python3
"""I26 - wake push construction gate (syntax-level, tracked and untracked files).

Spec: docs/wake-layer-relocation-design.md, section 9.2 (what the API must not
inherit from the session that starts it) and section 12 (the I26 items). This
file currently holds two items of I26. The remaining items land together with
the code they guard.

Item 2 - the API process holds no session credentials.
  The API is a shared, long-lived service, and whichever session happens to
  start it must not hand it that session's inbox credentials or identity. A
  process holding the inbox token can write to that session's socket as the
  session's own child instead of as an outside peer.
  (a) Every call under src/, plugin/, scripts/ and the top-level *.py files
      that starts the API passes env= taken from the scrub function: a direct
      call, or a name every binding of which in that scope is such a call. A
      call "starts the API" when a string naming the API module reaches it
      through literals, local names or a same-file function that returns the
      command. Every such string has to reach a checked launch or be listed in
      NOT_LAUNCHED; an in-process start is listed in IN_PROCESS and has to
      scrub its own environment before the launch. Each entry names the file,
      the enclosing function and the literal, and must match exactly one string.
  (b) The first statement of the API module (after the docstring and
      __future__ imports) imports the process scrub, and the next one calls it.
      Importing the API module therefore scrubs the importing process, so no
      code outside src/aiteam/api/ may import it.
  (c) The scrub list in the scrub module contains the messaging prefix and the
      two session id names; the scrub functions exist once and use the list; the
      scrub module imports the standard library only (the Codex scripts load it
      by path, beside whatever aiteam copy is installed).
  (d) Outside the scrub module and this file, no code names the inbox token
      variable (Python string literals, plus the raw text of shell and JS/TS
      files).
  (e) wake_push.py and session_registry.py hold no literal ending in ".key"
      and no "auth" literal (the frame type that authenticates as the session).
Item 1, unix socket part - under src/ and plugin/, asyncio.open_unix_connection
  and socket.AF_UNIX appear only in wake_push.py.

Blind spots (left to review and to the subprocess tests that read a launched
API's initial environment): module paths or variable names assembled at
runtime; once a command reaches one checked launch, its other uses through an
attribute, a container or another module are not followed; and a scrubbed dict
repopulated through method calls (env.update(os.environ)). A command string that
reaches no launch at all fails the check rather than slipping through.

Usage: python3 scripts/check_wake_push.py    (from the repo root)
Exit code: 0 = pass, 1 = violation.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = "scripts/check_wake_push.py"

SCRUB_MODULE = "src/aiteam/session_env.py"
SCRUB_CALL = "scrub_session_env"
SELF_SCRUB_CALL = "scrub_process_env"
NAME_TEST = "is_session_env_name"
PREFIX_LIST = "SESSION_ENV_PREFIXES"
NAME_LIST = "SESSION_ENV_NAMES"
REQUIRED_PREFIXES = frozenset({"CLAUDE_CODE_MESSAGING_"})
REQUIRED_NAMES = frozenset({"CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"})
TOKEN_NAME = "CLAUDE_CODE_MESSAGING_" + "TOKEN"

API_MODULE = "src/aiteam/api/app.py"
API_PACKAGE = "src/aiteam/api/"
API_DOTTED = "aiteam.api.app"

# Callables that start a process (matched on the called name or attribute).
LAUNCHERS = frozenset({
    "Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput",
    "create_subprocess_exec", "create_subprocess_shell", "run_process", "open_process",
    "spawn", "system", "popen", "posix_spawn", "posix_spawnp",
    "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe",
})
# Each entry covers exactly one string: (path, enclosing function, the literal itself).
# A second string in the same scope, even an identical one, needs its own launch check.
# Strings naming the API module that start nothing -> reason.
NOT_LAUNCHED = {
    ("src/aiteam/services/external_exec_guard.py", "_is_api_command", "aiteam.api.app:create_app"):
        "process identity match to preserve shared API families; starts no API",
    ("src/aiteam/mcp/_autostart.py", "<module>", "aiteam.api.app:create_app"):
        "process identity match in _is_api_command",
    ("install.py", "main", "    python -m uvicorn aiteam.api.app:create_app --factory --host 0.0.0.0 --port 8000"):
        "printed manual start instructions",
}
# In-process starts: the enclosing function calls scrub_process_env() before the launch.
IN_PROCESS = {
    ("src/aiteam/cli/commands/up_cmd.py", "up", "aiteam.api.app:create_app"),
}

SOCKET_NAMES = frozenset({"open_unix_connection", "AF_UNIX"})
SOCKET_HOME = "wake_push.py"
KEY_FILES = frozenset({"wake_push.py", "session_registry.py"})
RAW_TEXT_SUFFIXES = (".sh", ".js", ".mjs", ".cjs", ".ts", ".tsx")
CONTAINERS = (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Starred, ast.JoinedStr,
              ast.FormattedValue, ast.BinOp, ast.IfExp, ast.keyword)


def _scanned_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True, cwd=ROOT,
    ).stdout.splitlines()
    files = []
    for path in sorted(set(out)):
        top = path.split("/", 1)[0]
        if path == SELF or path.startswith("tests/"):
            continue
        if top in ("src", "plugin", "scripts") or "/" not in path:
            files.append(path)
    return files


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _called_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


class FileIndex:
    """Parent links, enclosing scopes and name bindings for one parsed file."""

    def __init__(self, path: str, tree: ast.Module):
        self.path = path
        self.tree = tree
        self.parent: dict[int, ast.AST] = {}
        self.scope: dict[int, ast.AST] = {}
        self.qualname: dict[int, str] = {id(tree): "<module>"}
        self._index(tree, tree, "")

    def _index(self, node: ast.AST, scope: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            self.parent[id(child)] = node
            self.scope[id(child)] = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                self.qualname[id(child)] = name
                inner = child if not isinstance(child, ast.ClassDef) else scope
                self._index(child, inner, f"{name}.")
            else:
                self._index(child, scope, prefix)

    def scope_name(self, node: ast.AST) -> str:
        return self.qualname.get(id(self.scope.get(id(node), self.tree)), "<module>")

    def walk_scope(self, scope: ast.AST):
        """Nodes whose enclosing function scope is exactly ``scope``."""
        for node in ast.walk(scope):
            if node is not scope and self.scope.get(id(node)) is scope:
                yield node

    def bindings(self, name: str, scope: ast.AST) -> list[ast.AST]:
        """Statements or nodes that bind ``name`` in ``scope`` (module scope if unbound there)."""
        found = []
        for node in self.walk_scope(scope):
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
                found.append(self.parent[id(node)])
            elif isinstance(node, ast.arg) and node.arg == name:
                found.append(node)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if any((alias.asname or alias.name.split(".")[0]) == name for alias in node.names):
                    found.append(node)
        if not found and scope is not self.tree:
            return self.bindings(name, self.tree)
        return found


def _launch_carriers(index: FileIndex, start: ast.AST, seen: set) -> list[ast.Call]:
    """Launch calls that ``start`` (a literal, a name load or a builder call) flows into."""
    node = start
    while True:
        parent = index.parent.get(id(node))
        if parent is None:
            return []
        if isinstance(parent, ast.Call):
            if node is parent.func:
                return []
            if _called_name(parent) in LAUNCHERS:
                return [parent]
            node = parent
            continue
        if isinstance(parent, CONTAINERS):
            node = parent
            continue
        break
    carriers: list[ast.Call] = []
    if isinstance(parent, (ast.Assign, ast.AnnAssign)) and node is parent.value:
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        scope = index.scope.get(id(parent), index.tree)
        for target in targets:
            if not isinstance(target, ast.Name) or ("name", id(scope), target.id) in seen:
                continue
            seen.add(("name", id(scope), target.id))
            search = index.tree if scope is index.tree else scope
            for use in ast.walk(search):
                if isinstance(use, ast.Name) and use.id == target.id and isinstance(use.ctx, ast.Load):
                    carriers += _launch_carriers(index, use, seen)
    elif isinstance(parent, ast.Return):
        function = index.scope.get(id(parent))
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and ("builder", id(function)) not in seen:
            seen.add(("builder", id(function)))
            for use in ast.walk(index.tree):
                if isinstance(use, ast.Call) and _called_name(use) == function.name:
                    carriers += _launch_carriers(index, use, seen)
    return carriers


def _is_scrub_call(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Call) and _called_name(node) == SCRUB_CALL


def _env_from_scrub(index: FileIndex, call: ast.Call) -> bool:
    value = next((kw.value for kw in call.keywords if kw.arg == "env"), None)
    if _is_scrub_call(value):
        return True
    if not isinstance(value, ast.Name):
        return False
    scope = index.scope.get(id(call), index.tree)
    bindings = index.bindings(value.id, scope)
    return bool(bindings) and all(
        isinstance(b, (ast.Assign, ast.AnnAssign)) and _is_scrub_call(b.value) for b in bindings
    )


def _scrubs_before(index: FileIndex, call: ast.Call) -> bool:
    scope = index.scope.get(id(call), index.tree)
    return any(
        isinstance(node, ast.Call) and _called_name(node) == SELF_SCRUB_CALL and node.lineno < call.lineno
        for node in index.walk_scope(scope)
    )


def check_launches(index: FileIndex, docstrings: set[int], problems: list[str], seen_sites: Counter) -> int:
    """(a): every string naming the API module reaches a checked launch or is registered."""
    checked: set[int] = set()
    for node in ast.walk(index.tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and API_DOTTED in node.value and id(node) not in docstrings):
            continue
        site = (index.path, index.scope_name(node), node.value)
        carriers = _launch_carriers(index, node, set())
        if not carriers:
            if site in NOT_LAUNCHED:
                seen_sites[site] += 1
            else:
                problems.append(f"{index.path}:{node.lineno}: 提到 API 模块的字符串看不出由哪一处拉起；"
                                "拉起须带 env=scrub_session_env(...)，不拉起的用途登记进 NOT_LAUNCHED")
            continue
        for call in carriers:
            if id(call) in checked:
                continue
            checked.add(id(call))
            launch_site = (index.path, index.scope_name(call), node.value)
            if launch_site in IN_PROCESS:
                seen_sites[launch_site] += 1
                if not _scrubs_before(index, call):
                    problems.append(f"{index.path}:{call.lineno}: 进程内拉起 API 之前没有调用 {SELF_SCRUB_CALL}()")
            elif not _env_from_scrub(index, call):
                problems.append(f"{index.path}:{call.lineno}: 拉起 API 的 {_called_name(call)}() 没有带 "
                                f"env={SCRUB_CALL}(...)（或全部赋值都来自它的名字），"
                                "会把拉起者会话的凭据与身份带进 API")
    return len(checked)


def check_api_module(tree: ast.Module, problems: list[str]) -> None:
    """(b): the API module scrubs its own process before anything else runs."""
    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body.pop(0)
    while body and isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__":
        body.pop(0)
    first = body[0] if body else None
    second = body[1] if len(body) > 1 else None
    imports_scrub = (isinstance(first, ast.ImportFrom) and first.module == "aiteam.session_env"
                     and any(alias.name == SELF_SCRUB_CALL and alias.asname is None for alias in first.names))
    calls_scrub = (isinstance(second, ast.Expr) and isinstance(second.value, ast.Call)
                   and isinstance(second.value.func, ast.Name) and second.value.func.id == SELF_SCRUB_CALL)
    if not (imports_scrub and calls_scrub):
        problems.append(f"{API_MODULE}: 首条语句须为 from aiteam.session_env import {SELF_SCRUB_CALL}，"
                        f"紧接着调用 {SELF_SCRUB_CALL}()（覆盖 aiteam up 与手工起服务）")


def check_api_imports(index: FileIndex, problems: list[str]) -> None:
    """(b): importing the API module scrubs the importer, so only the API package may import it."""
    if index.path.startswith(API_PACKAGE):
        return
    for node in ast.walk(index.tree):
        hit = False
        if isinstance(node, ast.Import):
            hit = any(alias.name == API_DOTTED for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            hit = node.module == API_DOTTED or (node.module == "aiteam.api"
                                                and any(alias.name == "app" for alias in node.names))
        if hit:
            problems.append(f"{index.path}:{node.lineno}: 在 API 进程之外 import 了 {API_DOTTED}；"
                            "导入它会清掉本进程的会话环境，拉起 API 应走子进程")


def check_scrub_module(problems: list[str], definitions: dict[str, list[str]]) -> None:
    """(c): the scrub list and the scrub functions."""
    path = ROOT / SCRUB_MODULE
    if not path.is_file():
        problems.append(f"{SCRUB_MODULE}: 缺失（剔除函数与必剔名单的唯一出处）")
        return
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=SCRUB_MODULE)
    literals: dict[str, set[str]] = {}
    functions: dict[str, ast.AST] = {}
    for node in tree.body:
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target = node.target.id
        if target in (PREFIX_LIST, NAME_LIST):
            literals[target] = {n.value for n in ast.walk(node.value)
                                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        if isinstance(node, ast.FunctionDef):
            functions[node.name] = node
    missing_prefixes = REQUIRED_PREFIXES - literals.get(PREFIX_LIST, set())
    missing_names = REQUIRED_NAMES - literals.get(NAME_LIST, set())
    if missing_prefixes:
        problems.append(f"{SCRUB_MODULE}: {PREFIX_LIST} 缺必剔前缀 {sorted(missing_prefixes)}")
    if missing_names:
        problems.append(f"{SCRUB_MODULE}: {NAME_LIST} 缺必剔变量 {sorted(missing_names)}")
    test = functions.get(NAME_TEST)
    used = {n.id for n in ast.walk(test) if isinstance(n, ast.Name)} if test else set()
    if not {PREFIX_LIST, NAME_LIST} <= used:
        problems.append(f"{SCRUB_MODULE}: {NAME_TEST}() 须同时引用 {PREFIX_LIST} 与 {NAME_LIST}")
    for name in (SCRUB_CALL, SELF_SCRUB_CALL):
        function = functions.get(name)
        if function is None or not any(isinstance(n, ast.Call) and _called_name(n) == NAME_TEST
                                       for n in ast.walk(function)):
            problems.append(f"{SCRUB_MODULE}: {name}() 缺失或没有按 {NAME_TEST}() 剔除")
    # The Codex scripts load this file by path from their own tree, next to whatever
    # aiteam copy the interpreter has installed: it may import the standard library only.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [("." * node.level) + (node.module or "")]
        else:
            continue
        for module in modules:
            top = module.split(".")[0]
            if module.startswith(".") or (top != "__future__" and top not in sys.stdlib_module_names):
                problems.append(f"{SCRUB_MODULE}:{node.lineno}: import {module}；剔除模块只许导入标准库"
                                "（Codex 脚本按路径加载它，不能依赖 aiteam 包或第三方包）")
    for name in (SCRUB_CALL, SELF_SCRUB_CALL, NAME_TEST):
        others = [p for p in definitions.get(name, []) if p != SCRUB_MODULE]
        if others:
            problems.append(f"{', '.join(others)}: 另定义了 {name}()，剔除函数只许一份（{SCRUB_MODULE}）")


def check_python_file(index: FileIndex, problems: list[str]) -> None:
    """(d), (e) and the unix socket part of item 1 for one parsed file."""
    name = Path(index.path).name
    for node in ast.walk(index.tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if TOKEN_NAME in node.value and index.path != SCRUB_MODULE:
                problems.append(f"{index.path}:{node.lineno}: 出现收件 token 变量名字面量；"
                                f"会话凭据只许在 {SCRUB_MODULE} 里按前缀剔除，别处不得读写")
            if name in KEY_FILES and (node.value.endswith(".key") or node.value == "auth"):
                problems.append(f"{index.path}:{node.lineno}: 出现 {node.value!r}；"
                                "OS 不读会话 .key 文件，也不发 auth 帧")
        if index.path.startswith(("src/", "plugin/")) and name != SOCKET_HOME:
            names = []
            if isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
            for hit in SOCKET_NAMES.intersection(names):
                problems.append(f"{index.path}:{node.lineno}: 用了 {hit}；unix socket 只许出现在 {SOCKET_HOME}")


def main() -> int:
    problems: list[str] = []
    definitions: dict[str, list[str]] = {}
    seen_sites: Counter = Counter()
    launches = parsed = 0
    for path in _scanned_files():
        full = ROOT / path
        if path.endswith(RAW_TEXT_SUFFIXES):
            try:
                text = full.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                continue
            if TOKEN_NAME in text:
                problems.append(f"{path}: 出现收件 token 变量名；会话凭据只许在 {SCRUB_MODULE} 里按前缀剔除")
            continue
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(full.read_text(encoding="utf-8"), filename=path)
        except FileNotFoundError:
            continue
        except (OSError, SyntaxError, UnicodeError) as exc:
            problems.append(f"{path}: 无法解析（{type(exc).__name__}），I26 看不见它")
            continue
        parsed += 1
        index = FileIndex(path, tree)
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
                    SCRUB_CALL, SELF_SCRUB_CALL, NAME_TEST):
                definitions.setdefault(node.name, []).append(path)
        launches += check_launches(index, docstrings, problems, seen_sites)
        check_api_imports(index, problems)
        check_python_file(index, problems)
        if path == API_MODULE:
            check_api_module(tree, problems)
    if not (ROOT / API_MODULE).is_file():
        problems.append(f"{API_MODULE}: 缺失")
    check_scrub_module(problems, definitions)
    for site in sorted(set(NOT_LAUNCHED) | IN_PROCESS):
        if seen_sites[site] == 0:
            problems.append(f"{site[0]} {site[1]}: 登记项已找不到对应的 API 模块字符串，删掉这条登记")
        elif seen_sites[site] > 1:
            problems.append(f"{site[0]} {site[1]}: 登记项 {site[2]!r} 命中 {seen_sites[site]} 处；"
                            "一条登记只覆盖一个字符串，多出的那处要么带剔除后的环境拉起，要么单独登记")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"{parsed} 个 .py 文件，{launches} 处拉起 API 全部带剔除后的环境，API 模块首句自清；"
          f"必剔名单齐全，token 变量名零外引；unix socket 零处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
