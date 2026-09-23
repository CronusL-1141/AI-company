#!/usr/bin/env python3
"""I8 — hook registration surface machine check.

Two entry points install AI Team OS and each one carries its own hook table:

  * plugin mode  → plugin/hooks/hooks.json          (read by CC, and by
                   auto_install._sync_main_chain when it converges the chain)
  * source mode  → install.py HOOK_SURFACE          (written into ~/.claude/settings.json)

When they disagree the project ships two different operating systems depending on
how the user installed it — the 2026-07-27 audit found the source path missing
four whole events (TaskCreated / UserPromptSubmit / PermissionDenied / PreCompact)
and registering PreToolUse hooks under the wrong matcher. This check pins them
together on (event, matcher, script, arg, timeout) and additionally pins the
bilingual README hook counts to the manifest. Retirement is part of the surface
too: both install paths carry a retired-script list, and the two must agree.

README coverage is deliberately exhaustive: the first version of this check only
pinned the "Hook System (N scripts across M Lifecycle Events)" section heading,
so the same event count restated in the feature list and in the directory tree
drifted unnoticed (v1.10.3 shipped with three stale "12 lifecycle events" copies
while the heading said 11). Every numbered lifecycle-event claim in either README
is now pinned to the manifest, and so is the plugin.json description, which
states the same two counts to every marketplace user.

Usage: python3 scripts/check_hook_surface.py    (from the repo root)
Exit code: 0 = aligned, 1 = drift.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# auto_install.py is the plugin's self-heal entry: it bootstraps the chain from
# outside, so it is registered in hooks.json but must never be in the installed
# chain. It is the only legitimate plugin-side-only hook.
PLUGIN_ONLY_SCRIPTS = {"auto_install.py"}

# `<anything> ".../hooks/<script>.py"[ <arg>]` — same shape both manifests use.
_CMD_RE = re.compile(r'/hooks/([\w-]+\.py)"?(?:\s+(\S+))?\s*$')


def _load_manifest() -> tuple[set[tuple], set[str], list[str], list[str]]:
    """Parse plugin/hooks/hooks.json.

    Returns (comparable_surface, every_registered_script, events, errors). The
    comparable surface excludes PLUGIN_ONLY_SCRIPTS; the script set keeps them
    (they are still part of "how many hooks does this OS ship").
    """
    errors: list[str] = []
    path = ROOT / "plugin" / "hooks" / "hooks.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    surface: set[tuple] = set()
    scripts: set[str] = set()

    for event, groups in data.get("hooks", {}).items():
        for group in groups:
            matcher = group.get("matcher", "")
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                # The self-heal entry ships as a cross-platform `case ... esac`
                # launcher, which no "<interpreter> <script> [arg]" regex can parse.
                plugin_only = next(
                    (name for name in PLUGIN_ONLY_SCRIPTS if name in command), None
                )
                if plugin_only:
                    scripts.add(plugin_only)
                    continue
                match = _CMD_RE.search(command)
                if not match:
                    errors.append(
                        f"hooks.json:{event}: 无法解析 hook 命令 → {command[:80]!r}"
                    )
                    continue
                script, arg = match.group(1), match.group(2) or ""
                scripts.add(script)
                surface.add((event, matcher, script, arg, hook.get("timeout")))
    return surface, scripts, list(data.get("hooks", {})), errors


def _load_installer_surface() -> set[tuple]:
    """Read HOOK_SURFACE out of the root install.py without running it."""
    spec = importlib.util.spec_from_file_location("install_surface", ROOT / "install.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        (event, matcher, script, arg, timeout)
        for event, matcher, entries in module.HOOK_SURFACE
        for script, arg, timeout in entries
    }


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scan_retired_parity(installer: tuple, plugin: tuple) -> list[str]:
    """Both install paths must retire the same hooks.

    The source update (install.py) and the plugin self-heal (auto_install) each
    carry the retired-script list, because auto_install is stdlib-only and runs
    from the plugin cache where install.py does not exist. A name missing on one
    side means that side keeps registering and running a hook the other retired.
    Pure over the two lists so the drift behaviour is unit-testable.
    """
    failures = []
    for name in sorted(set(installer) - set(plugin)):
        failures.append(
            f"plugin/hooks/auto_install.py RETIRED_HOOK_SCRIPTS 缺少 {name}"
            "（install.py 已退役）：插件用户会一直留着它的注册和副本"
        )
    for name in sorted(set(plugin) - set(installer)):
        failures.append(
            f"install.py RETIRED_HOOK_SCRIPTS 缺少 {name}"
            "（auto_install 已退役）：源码安装会一直留着它的注册和副本"
        )
    return failures


def _retired_parity(manifest_scripts: set[str]) -> list[str]:
    installer = tuple(_load_module(ROOT / "install.py", "install_retired").RETIRED_HOOK_SCRIPTS)
    plugin = tuple(
        getattr(
            _load_module(ROOT / "plugin" / "hooks" / "auto_install.py", "auto_install_retired"),
            "RETIRED_HOOK_SCRIPTS",
            (),
        )
    )
    failures = scan_retired_parity(installer, plugin)
    for name in sorted(set(installer) & manifest_scripts):
        failures.append(f"hooks.json 仍注册已退役的 {name}")
    return failures


def _describe(entry: tuple) -> str:
    event, matcher, script, arg, timeout = entry
    shown_matcher = matcher or "(无 matcher)"
    shown_arg = f" {arg}" if arg else ""
    return f"{event} [{shown_matcher}] → {script}{shown_arg} (timeout={timeout})"


_HEADLINE_RE = {
    "README.md": re.compile(r"Hook System \((\d+) scripts? across (\d+) Lifecycle Events"),
    "README.zh-CN.md": re.compile(r"Hook 系统（(\d+) 个脚本 / (\d+) 个生命周期事件"),
}

# Every other place either README states a lifecycle-event count. A bare
# "lifecycle event" with no number in front (e.g. "Record sub-Agent lifecycle
# event") is prose, not a claim, and must not be matched.
_EVENT_ANCHOR_RE = (
    re.compile(r"(\d+)\s+(?:CC\s+)?[Ll]ifecycle\s+[Ee]vents?"),
    re.compile(r"(\d+)\s*个\s*(?:CC\s*)?生命周期事件"),
)


def scan_event_anchors(text: str, event_count: int, name: str) -> list[str]:
    """Report every numbered lifecycle-event claim in ``text`` that isn't ``event_count``.

    Pure over the text so the drift behaviour is unit-testable without touching
    the real READMEs.
    """
    failures = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for regex in _EVENT_ANCHOR_RE:
            for match in regex.finditer(line):
                if int(match.group(1)) != event_count:
                    failures.append(
                        f"{name}:{lineno}: 生命周期事件数声明 \"{match.group(0).strip()}\" "
                        f"≠ 清单实测 {event_count}（plugin/hooks/hooks.json）"
                    )
    return failures


def _readme_counts(script_count: int, event_count: int) -> list[str]:
    """Pin every bilingual README hook count to the manifest."""
    failures: list[str] = []
    for name, pattern in _HEADLINE_RE.items():
        text = (ROOT / name).read_text(encoding="utf-8")
        match = pattern.search(text)
        if not match:
            failures.append(f"{name}: 找不到 Hook 系统标题行——无法核对脚本/事件数")
        else:
            scripts, events = int(match.group(1)), int(match.group(2))
            if scripts != script_count or events != event_count:
                failures.append(
                    f"{name}: 声明 {scripts} 脚本 / {events} 事件 ≠ 清单实测 "
                    f"{script_count} 脚本 / {event_count} 事件（plugin/hooks/hooks.json）"
                )
        failures.extend(scan_event_anchors(text, event_count, name))
    return failures


_PLUGIN_COUNTS_RE = re.compile(r"(\d+) hooks? across (\d+) lifecycle events?")


def scan_plugin_description(description: str, script_count: int, event_count: int) -> list[str]:
    """Pin the plugin.json description's hook and event counts to the manifest.

    Pure over the text so the drift behaviour is unit-testable. A description that
    no longer states the counts is reported too: the claim moved, it did not vanish.
    """
    match = _PLUGIN_COUNTS_RE.search(description)
    if not match:
        return ["plugin.json: description 里找不到「N hooks across M lifecycle events」——无法核对 hook 数"]
    hooks, events = int(match.group(1)), int(match.group(2))
    if hooks != script_count or events != event_count:
        return [
            f"plugin.json: 声明 {hooks} hooks / {events} 事件 ≠ 清单实测 "
            f"{script_count} 脚本 / {event_count} 事件（plugin/hooks/hooks.json）"
        ]
    return []


def _plugin_counts(script_count: int, event_count: int) -> list[str]:
    path = ROOT / "plugin" / ".claude-plugin" / "plugin.json"
    description = json.loads(path.read_text(encoding="utf-8")).get("description", "")
    return scan_plugin_description(description, script_count, event_count)


def main() -> int:
    manifest, scripts, events, failures = _load_manifest()
    installer = _load_installer_surface()

    for entry in sorted(manifest - installer):
        failures.append(f"install.py 缺少（hooks.json 有）: {_describe(entry)}")
    for entry in sorted(installer - manifest):
        failures.append(f"hooks.json 缺少（install.py 有）: {_describe(entry)}")

    # Hook counts: scripts include the plugin-only self-heal entry, events are
    # whatever the manifest actually registers.
    failures.extend(_readme_counts(len(scripts), len(events)))
    failures.extend(_plugin_counts(len(scripts), len(events)))

    # Both install paths retire the same hooks, and nothing retired is still registered.
    failures.extend(_retired_parity(scripts))

    # Every registered script must exist on disk in both distribution copies.
    for script in sorted(scripts):
        if not (ROOT / "plugin" / "hooks" / script).is_file():
            failures.append(f"hooks.json 注册了不存在的脚本: plugin/hooks/{script}")
        elif script not in PLUGIN_ONLY_SCRIPTS and not (
            ROOT / "src" / "aiteam" / "hooks" / script
        ).is_file():
            failures.append(f"注册的 hook 缺少包内孪生副本: src/aiteam/hooks/{script}")

    if failures:
        for failure in failures:
            print(f"❌ {failure}")
        print(f"\n结论: ❌ hook 注册面漂移 {len(failures)} 处")
        return 1

    print(
        f"✅ hook 注册面一致: {len(events)} 事件 / {len(scripts)} 脚本 · "
        f"install.py 与 hooks.json 逐条对齐（事件/matcher/参数/timeout）· 两条安装路径退役清单一致 · "
        f"双语 README 与 plugin.json 数字相符"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
