#!/usr/bin/env python3
"""Auto-install aiteam package on first launch.

This hook runs FIRST in SessionStart, before any other hook that depends
on the aiteam package. It uses only stdlib — no third-party imports.

On first marketplace install, aiteam is not pip-installed. This script
detects that and installs it automatically. User needs to restart CC once
after installation for MCP server to pick up the package.
"""
import json
import subprocess
import sys

# Retired 2026-07-27 (batch 5): _ensure_agent_teams_env() silently wrote
# CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 into the user's *global* settings.json on
# every SessionStart. Two reasons it had to go: (1) CC's Agent Teams tool group is
# gone, so the flag no longer switches anything on; (2) a plugin editing the user's
# global config behind their back is out of bounds — the only user-global writes
# this project performs must come from an explicit install/update run.


def _self_heal_interpreter():
    """Rewrite plugin manifest interpreter tokens to sys.executable (idempotent).

    Static plugin manifests (hooks/hooks.json, .mcp.json) cannot embed
    per-machine absolute paths, so they ship with a generic `python3` token.
    Bare tokens re-create the two failure modes the project already paid for:
    macOS without a `python` shim (command-not-found) and project .venv
    hijacking resolution (e2d0fbb). This hook runs under a working interpreter
    — the same one that pip-installs aiteam — so we rewrite the token to its
    absolute path, restoring the sys.executable invariant for MCP + all hooks.
    Idempotent: rewritten commands no longer start with python/python3.
    Never blocks SessionStart: every failure is swallowed.
    """
    import os
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    exe = sys.executable
    if not root or not exe:
        return
    quoted_exe = f'"{exe}"' if " " in exe else exe

    # hooks/hooks.json — shell-form commands: replace the leading interpreter token
    hooks_path = os.path.join(root, "hooks", "hooks.json")
    try:
        with open(hooks_path, encoding="utf-8") as f:
            data = json.load(f)
        changed = False
        for groups in data.get("hooks", {}).values():
            for group in groups:
                for hook in group.get("hooks", []):
                    cmd = hook.get("command", "")
                    for token in ("python3 ", "python "):
                        if cmd.startswith(token):
                            hook["command"] = quoted_exe + " " + cmd[len(token):]
                            changed = True
                            break
        if changed:
            with open(hooks_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # Silent failure — non-critical

    # .mcp.json — exec form: command field is the bare program (no quoting)
    mcp_path = os.path.join(root, ".mcp.json")
    try:
        with open(mcp_path, encoding="utf-8") as f:
            data = json.load(f)
        server = data.get("mcpServers", {}).get("ai-team-os")
        if server and server.get("command") in ("python", "python3"):
            server["command"] = exe
            with open(mcp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # Silent failure — non-critical


GITHUB_URL = "git+https://github.com/CronusL-1141/AI-company.git"

RUNTIME_HOOKS_DIRNAME = "ai-team-os"

# Hooks retired from the manifest. The self-heal sync removes their runtime copies
# and settings.json entries, the same way install.py's update does; otherwise a
# plugin user keeps firing a retired hook on every event forever. Mirror of
# install.py RETIRED_HOOK_SCRIPTS (I8 pins the two lists together).
RETIRED_HOOK_SCRIPTS = (
    "task_completed_gate.py",
    "pipeline_gate.py",
    "autopilot_auto_stop.py",
    "cc_task_bridge.py",
    "meeting_ecosystem_writeback.py",
)


def _version_tuple(v: str) -> tuple:
    """Parse a dotted version into a comparable int tuple ('1.10.2' → (1,10,2)).

    Stdlib-only (no `packaging`): stops at the first non-digit in each part, so
    pre-release suffixes degrade gracefully to their numeric prefix.
    """
    out = []
    for part in str(v).split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return tuple(out)


def _plugin_version():
    """Version declared by the bundled plugin (CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json)."""
    import os
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if not root:
        return None
    try:
        from pathlib import Path
        data = json.loads((Path(root) / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        return data.get("version")
    except Exception:
        return None


def _installed_version():
    """Version of the pip-installed aiteam package, or None if not importable."""
    try:
        import aiteam
        return getattr(aiteam, "__version__", None)
    except Exception:
        return None


def _main_chain_registered() -> bool:
    """True if ~/.claude/settings.json already registers any ai-team-os runtime hook."""
    try:
        from pathlib import Path
        settings = Path.home() / ".claude" / "settings.json"
        cfg = json.loads(settings.read_text(encoding="utf-8"))
        for groups in cfg.get("hooks", {}).values():
            for group in groups:
                for hook in group.get("hooks", []):
                    if "ai-team-os" in hook.get("command", ""):
                        return True
    except Exception:
        pass
    return False


def _pip_install(upgrade: bool):
    """Install/upgrade aiteam from GitHub (PyPI may lag). Returns (ok, error_or_None).

    The marketplace plugin dir is not pip-installable (no pyproject), so GitHub is
    the source for both fresh installs and version upgrades. Never raises.
    """
    args = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        args.append("--upgrade")
    args.append(GITHUB_URL)
    try:
        # Kept under the hooks.json SessionStart timeout (300s) so a slow install
        # ends with a rendered progress card instead of a hard hook kill.
        proc = subprocess.run(args, capture_output=True, text=True, timeout=280)
        if proc.returncode == 0:
            return True, None
        return False, (proc.stderr or proc.stdout or "").strip()[-300:]
    except Exception as e:  # noqa: BLE001 — must never block SessionStart
        return False, str(e)[:300]


def _sync_main_chain():
    """Converge every entry point onto one runtime chain (the '单运行时' core).

    Copies the plugin's hook scripts to ~/.claude/hooks/ai-team-os/ and registers
    them in ~/.claude/settings.json with absolute sys.executable paths, reading the
    plugin's own hooks.json as the source of truth (minus auto_install itself: the
    self-heal entry must never be in the installed chain). Absolute paths make the
    chain Windows-safe (the plugin's `python3` token can't launch there) and let the
    yield sentinel dedupe the plugin backup copies.

    Same semantics as install.py register_hooks: every entry we own (a current
    manifest script or a retired one, matched by script name inside the runtime
    dir) is dropped and the current manifest is rebuilt, so retired hooks and
    stale matchers disappear while every hook we do not own stays where it was.
    Commands use install.py's exact format, so re-runs and a parallel source
    install converge on the same chain. Stdlib-only; never raises. Returns the
    number of hooks that were not registered before this run.
    """
    import os
    import re
    import shutil
    from pathlib import Path

    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if not root:
        return 0
    root = Path(root)
    src_hooks = root / "hooks"
    runtime = Path.home() / ".claude" / "hooks" / RUNTIME_HOOKS_DIRNAME
    settings_path = Path.home() / ".claude" / "settings.json"

    try:
        manifest = json.loads((src_hooks / "hooks.json").read_text(encoding="utf-8"))
    except Exception:
        return 0

    # 1. Copy every hook script (except the self-heal entry) to the runtime dir,
    #    then drop the copies of retired hooks.
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        for py in src_hooks.glob("*.py"):
            if py.name == "auto_install.py":
                continue
            shutil.copy2(py, runtime / py.name)
    except Exception:
        pass  # partial copy is still better than none; never block
    for name in RETIRED_HOOK_SCRIPTS:
        try:
            (runtime / name).unlink()
        except Exception:
            pass  # absent (the common case) or not removable; never block

    py_exe = str(sys.executable).replace("\\", "/")
    runtime_fwd = str(runtime).replace("\\", "/")

    def _build_cmd(script: str, arg: str) -> str:
        cmd = f'"{py_exe}" "{runtime_fwd}/{script}"'
        return f"{cmd} {arg}" if arg else cmd

    _extract = re.compile(r'/hooks/([\w-]+\.py)"?(?:\s+(\S+))?\s*$')

    # 2. Parse the manifest into the chain to register.
    surface = []  # (event, matcher, [(command, timeout)])
    current = set()
    for event, groups in manifest.get("hooks", {}).items():
        for group in groups:
            rebuilt = []
            for hook in group.get("hooks", []):
                m = _extract.search(hook.get("command", ""))
                if not m:
                    continue
                script, arg = m.group(1), (m.group(2) or "")
                if script == "auto_install.py":
                    continue
                current.add(script)
                rebuilt.append((_build_cmd(script, arg), hook.get("timeout")))
            if rebuilt:
                surface.append((event, group.get("matcher", ""), rebuilt))
    owned = current | set(RETIRED_HOOK_SCRIPTS)

    marker = f"/hooks/{RUNTIME_HOOKS_DIRNAME}/"

    def _is_ours(command: str) -> bool:
        # Name-based, never "path contains ai-team-os": the runtime dir also hosts
        # hooks other parts of the project install, and users add their own there.
        normalized = command.replace("\\", "/")
        if marker not in normalized:
            return False
        name = normalized.split(marker, 1)[1].split('"')[0].split(" ")[0].strip()
        return name in owned

    # 3. Load settings, drop our previous entries, rebuild the current surface.
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except Exception:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    before = json.dumps(settings, indent=2, ensure_ascii=False)
    existing_hooks = settings.setdefault("hooks", {})
    if not isinstance(existing_hooks, dict):
        existing_hooks = settings["hooks"] = {}

    previous = set()
    for event in list(existing_hooks):
        surviving = []
        for group in existing_hooks.get(event) or []:
            if not isinstance(group, dict):
                surviving.append(group)
                continue
            hooks = group.get("hooks", [])
            kept = []
            for hook in hooks:
                command = hook.get("command", "") if isinstance(hook, dict) else ""
                if isinstance(command, str) and _is_ours(command):
                    previous.add(command)
                else:
                    kept.append(hook)
            if kept:
                surviving.append({**group, "hooks": kept} if len(kept) != len(hooks) else group)
        if surviving:
            existing_hooks[event] = surviving
        else:
            del existing_hooks[event]

    added = 0
    for event, matcher, rebuilt in surface:
        event_list = existing_hooks.setdefault(event, [])
        target = next(
            (g for g in event_list if isinstance(g, dict) and g.get("matcher", "") == matcher), None
        )
        if target is None:
            target = {"matcher": matcher, "hooks": []} if matcher else {"hooks": []}
            event_list.append(target)
        for command, timeout in rebuilt:
            entry = {"type": "command", "command": command}
            if timeout is not None:
                entry["timeout"] = timeout
            target.setdefault("hooks", []).append(entry)
            if command not in previous:
                added += 1

    after = json.dumps(settings, indent=2, ensure_ascii=False)
    if after == before and settings_path.exists():
        return added
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(after, encoding="utf-8")
    except Exception:
        return 0
    return added


def _emit_card(lines) -> None:
    """Emit a SessionStart progress checklist as additionalContext (stdout JSON only)."""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    sys.stdout.write(json.dumps(output, ensure_ascii=False))


def main():
    # Diagnostics go to stderr; stdout is reserved for the hook-result JSON.
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass

    # Self-heal: converge the plugin manifest to this interpreter's absolute path.
    _self_heal_interpreter()

    plugin_ver = _plugin_version()
    installed_ver = _installed_version()

    fresh = installed_ver is None
    behind = fresh or (
        plugin_ver is not None
        and installed_ver is not None
        and _version_tuple(plugin_ver) > _version_tuple(installed_ver)
    )

    # Up to date and already converged onto the runtime chain → zero output (no noise).
    if not behind and _main_chain_registered():
        return

    card = ["[AI Team OS] 安装状态:"]

    if behind:
        pip_ok, pip_err = _pip_install(upgrade=not fresh)
        if pip_ok and fresh:
            card.append(f"  ✓ 依赖包已安装（v{plugin_ver or '?'}）")
        elif pip_ok:
            card.append(f"  ✓ 依赖包已升级 → v{plugin_ver or '?'}（原 v{installed_ver}）")
        else:
            card.append(f"  ✗ 依赖包{'安装' if fresh else '升级'}失败")
            if sys.version_info < (3, 11):  # noqa: UP036 — 自愈入口须在旧 Python 上给出可读诊断
                v = ".".join(str(x) for x in sys.version_info[:3])
                card.append(f"    需 Python 3.11+（当前 {v}）——请用更高版本重启会话")
            else:
                card.append("    可能为网络问题，稍后重试或手动执行：")
            card.append(f"    pip install --upgrade {GITHUB_URL}")
            if pip_err:
                sys.stderr.write(f"[AI Team OS] pip failed: {pip_err}\n")
    else:
        # Current version, but the runtime chain isn't registered yet (first plugin run).
        card.append(f"  ✓ 依赖包已就绪（v{installed_ver}）")

    # Register/refresh the absolute-path runtime main chain (Windows-safe, dedup-able).
    synced = _sync_main_chain()
    if synced:
        card.append(f"  ✓ 主链已注册（{synced} 个 hook，绝对路径）")
    elif _main_chain_registered():
        card.append("  ✓ 主链已就位")
    card.append("  ✓ MCP 服务已配置")
    card.append("  → 重启 Claude Code 以解锁全部工具（一次性）")

    _emit_card(card)


if __name__ == "__main__":
    # Always exit 0: the cross-platform launcher in hooks.json chains a fallback
    # interpreter with `||`, so a non-zero exit here would re-run the whole thing.
    # A failed auto-install must never block the session either (SessionStart).
    try:
        main()
    except Exception:
        pass
