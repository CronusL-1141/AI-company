#!/usr/bin/env python3
"""I33: three rules of the workflow asset backup (src/aiteam/assets).

1. OS's own files are derived from ``plugin/``, never listed by hand. A template
   added to ``plugin/`` and missing from a hand-written list would be backed up as
   the user's and restored over the next install's copy. Checked twice: the
   production builder, run on a copy of ``plugin/`` with a probe template added to
   each collection and a real one removed, must follow both changes; and no name of
   a template collection (``plugin/agents``, ``plugin/commands``, ``plugin/skills``)
   may appear in a string of the package, alone or as a part of a path.
2. Every deny rule catches its own sample paths, the classifier calls each sample
   unmanaged, and every path of ``MUST_DENY`` (fixed here, apart from the rules:
   deleting a rule together with its samples must still turn this red) is denied.
   A deny rule that misses is how a credential gets uploaded.
3. Nothing in the backup's code (the package, its storage, route, tools and notice
   detector) starts a timer, a thread or a task, or sleeps: triggers come only from
   hook events and explicit calls (no background daemons).

Usage: python3 scripts/check_workflow_assets.py
Exit: 0 all hold, 1 a rule is broken (or the check could not run).
"""

from __future__ import annotations

import ast
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "aiteam" / "assets"
# The backup's code outside the package: none of it may schedule anything either.
COMPANIONS = tuple(ROOT / "src" / "aiteam" / rel for rel in (
    "storage/assets.py", "api/routes/assets.py", "mcp/tools/assets.py",
    "services/notices/detectors/asset_backup.py",
))
sys.path.insert(0, str(ROOT / "src"))

# Paths that must never be read, whatever the deny rules say: credentials, session
# state, ledgers, other tools' backups, OS's own hook chain, Codex's databases.
MUST_DENY = (
    ("claude", ".credentials.json"), ("claude", "sessions/4242.json"), ("claude", "usage-merge.json"),
    ("claude", "usage-cache/2026-11.json"), ("claude", "plugins/cache/p/plugin.json"),
    ("claude", "settings.json.bak-20261111"), ("claude", "paste-cache/a.txt"), ("claude", "session-env/s/env.sh"),
    ("claude", "skills/synced/s/SKILL.md"), ("claude", "hooks/ai-team-os/send_event.py"),
    ("claude", "backups/settings.json"), ("claude", "cache/x.json"),
    ("codex", "auth.json"), ("codex", "installation_id"), ("codex", "state_5.sqlite"),
    ("codex", "memories/.git/HEAD"), ("codex", "log/codex-tui.log"), ("codex", "skills/.system/s/SKILL.md"),
)

# Calls that start background work or wait on a clock.
FORBIDDEN_CALLS = frozenset({
    "sleep", "Timer", "call_later", "call_at", "Thread", "run_forever", "create_task", "ensure_future",
    "start_new_thread", "run_coroutine_threadsafe", "ThreadPoolExecutor", "ProcessPoolExecutor",
})
FORBIDDEN_IMPORTS = frozenset({"sched", "threading", "_thread"})
TEMPLATE_COLLECTIONS = ("agents", "commands", "skills")
PROBE = "zz-i33-manifest-probe"


def _template_names(plugin: Path) -> set[str]:
    """Names of every template OS ships in its collections: file names, stems, skill folders."""
    names = set()
    for collection in TEMPLATE_COLLECTIONS:
        for path in (plugin / collection).iterdir() if (plugin / collection).is_dir() else ():
            names.update({path.name, path.stem})
    return names


def _expected(plugin: Path) -> set[str]:
    """The relative paths OS installs from ``plugin`` (what install.py copies)."""
    expected = {f"{c}/{p.name}" for c in ("agents", "commands") for p in (plugin / c).glob("*.md")}
    expected |= {f"skills/{p.relative_to(plugin / 'skills').as_posix()}" for p in (plugin / "skills").rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts}
    if (plugin / "loop.md").is_file():
        expected.add("loop.md")
    return expected


def check_manifest() -> list[str]:
    from aiteam.assets.os_manifest import build_claude_manifest

    problems = []
    built, expected = set(build_claude_manifest(ROOT).files), _expected(ROOT / "plugin")
    if built != expected:
        problems.append(f"OS 清单与 plugin/ 不符：缺 {sorted(expected - built)[:5]}，多 {sorted(built - expected)[:5]}")
    with tempfile.TemporaryDirectory(prefix="i33-") as temp:
        copy = Path(temp)
        shutil.copy2(ROOT / "install.py", copy / "install.py")
        shutil.copytree(ROOT / "plugin", copy / "plugin",
                        ignore=shutil.ignore_patterns("dashboard-dist", "__pycache__"))
        removed = sorted((copy / "plugin" / "agents").glob("*.md"))[0]
        removed.unlink()
        probes = {f"agents/{PROBE}.md", f"commands/{PROBE}.md", f"skills/{PROBE}/SKILL.md"}
        for rel in probes:
            (copy / "plugin" / rel).parent.mkdir(parents=True, exist_ok=True)
            (copy / "plugin" / rel).write_text("probe\n", encoding="utf-8")
        probed = build_claude_manifest(copy)
        if not probes <= set(probed.files):
            problems.append(f"plugin/ 里新增的模板没有进 OS 清单：{sorted(probes - set(probed.files))}"
                            "（清单是手写的？）")
        if f"agents/{removed.name}" in probed.files:
            problems.append(f"plugin/ 里删掉的模板仍在 OS 清单里：agents/{removed.name}（清单是手写的？）")
    names = _template_names(ROOT / "plugin")
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and names & set(node.value.replace("\\", "/").split("/")):
                problems.append(f"{path.relative_to(ROOT)}:{node.lineno}: 手写了 OS 模板名 {node.value!r}"
                                "（OS 清单须由 plugin/ 推导）")
    return problems


def check_deny_samples() -> list[str]:
    from aiteam.assets.catalog import classify, denied_by, deny_rules, segments
    from aiteam.types import AssetHost

    problems, checked = [], 0
    for host in AssetHost:
        for rule in deny_rules(host):
            if not rule.samples:
                problems.append(f"{host}: 拒绝规则 {rule.pattern!r} 没有样例路径")
            for sample in rule.samples:
                checked += 1
                path = segments(sample)
                if path is None or not rule.catches(path):
                    problems.append(f"{host}: 拒绝规则 {rule.pattern!r} 拦不住自己的样例 {sample!r}")
                if classify(host, sample) is not None:
                    problems.append(f"{host}: 样例 {sample!r} 被分类为 {classify(host, sample)}，应为不受管")
    if not checked:
        problems.append("拒绝表没有任何样例：检查失效")
    for host, sample in MUST_DENY:
        if denied_by(AssetHost(host), sample) is None:
            problems.append(f"{host}: 必须拒绝的 {sample!r} 没有任何拒绝规则拦住")
    return problems


def check_no_timers() -> list[str]:
    problems = []
    files = sorted(PACKAGE.rglob("*.py"))
    missing = [path for path in COMPANIONS if not path.is_file()]
    if not files or missing:
        absent = [str(path.relative_to(ROOT)) for path in missing] or [str(PACKAGE.relative_to(ROOT))]
        return [f"扫描范围缺失 {absent}：检查失效"]
    files += list(COMPANIONS)
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name in FORBIDDEN_CALLS:
                    problems.append(f"{path.relative_to(ROOT)}:{node.lineno}: 调用 {name}"
                                    "（不许定时器、线程、后台任务）")
            elif isinstance(node, ast.Import | ast.ImportFrom):
                modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for module in modules:
                    if module.split(".")[0] in FORBIDDEN_IMPORTS:
                        problems.append(f"{path.relative_to(ROOT)}:{node.lineno}: 导入 {module}（不许线程与调度器）")
    return problems


def main() -> int:
    sections = (("OS 清单由 plugin/ 推导", check_manifest), ("拒绝表样例不受管", check_deny_samples),
                ("aiteam.assets 无定时器", check_no_timers))
    failed = False
    for title, check in sections:
        try:
            problems = check()
        except Exception as exc:  # noqa: BLE001 - a check that cannot run is a failure, not a pass
            problems = [f"检查无法运行：{type(exc).__name__}: {exc}"]
        if problems:
            failed = True
            print(f"❌ {title}:")
            for problem in problems:
                print(f"   {problem}")
    if failed:
        return 1
    print("OS 清单由 plugin/ 推导、拒绝表样例全部不受管、无定时器与后台任务")
    return 0


if __name__ == "__main__":
    sys.exit(main())
