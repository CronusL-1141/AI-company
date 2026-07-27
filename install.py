#!/usr/bin/env python3
"""AI Team OS one-click installer."""
import json
import shutil
import subprocess
import sys

# 版本门卫必须先于任何含 PEP 604 注解（X | Y）的 def——Python <3.10 在 def 时
# 即抛 TypeError，放在 main() 里的检查永远执行不到（macOS 自带 3.9.6 首当其冲）。
if sys.version_info < (3, 11):  # noqa: UP036
    _v = ".".join(str(x) for x in sys.version_info[:3])
    print(f"[FAIL] Python 3.11+ required, got {_v} - try: python3 install.py")
    sys.exit(1)

from pathlib import Path

# Marker in the first line of plugin/loop.md. If ~/.claude/loop.md lacks it, the
# file is treated as user-customized and never overwritten.
LOOP_TEMPLATE_SENTINEL = "ai-team-os-loop-template"


def check_command(cmd: str) -> bool:
    """Check if a command is available."""
    return shutil.which(cmd) is not None


def run(args: list[str], cwd: str | None = None, **kwargs) -> subprocess.CompletedProcess:
    """Run subprocess, print friendly error on failure."""
    try:
        return subprocess.run(
            args, cwd=cwd, check=True,
            shell=(sys.platform == "win32" and args[0] in ("npm", "npx")),
            **kwargs,
        )
    except subprocess.CalledProcessError as e:
        print(f"[FAIL] Command failed: {' '.join(args)}")
        raise SystemExit(1) from e
    except FileNotFoundError:
        print(f"[FAIL] Command not found: {args[0]}")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Hook registration surface
# ---------------------------------------------------------------------------
# HOOK_SURFACE is the single source of truth for the source-install chain and
# must stay 1:1 with plugin/hooks/hooks.json (event set, matcher, script, arg and
# timeout). scripts/check_invariants.sh I8 machine-checks the two against each
# other — before that check existed the two paths shipped two different OSes
# (the source path was missing TaskCreated / UserPromptSubmit / PermissionDenied /
# PreCompact entirely, and matched PreToolUse hooks with the wrong matcher).
#
# auto_install.py is deliberately absent: it is the plugin's self-heal entry and
# must never enter the installed chain (it would re-register the chain from
# inside the chain). I8 whitelists it as plugin-only.
#
# Entry form: (event, matcher, [(script, arg, timeout), ...]); matcher "" means
# the group carries no matcher key at all, matching the plugin manifest.
HOOK_SURFACE: list[tuple[str, str, list[tuple[str, str, int]]]] = [
    ("SessionStart", "", [
        ("session_bootstrap.py", "", 15),
        ("send_event.py", "SessionStart", 5),
    ]),
    ("SubagentStart", "", [
        ("inject_subagent_context.py", "", 10),
        ("send_event.py", "SubagentStart", 5),
    ]),
    ("SubagentStop", "", [
        ("send_event.py", "SubagentStop", 5),
    ]),
    # workflow_reminder only speaks for the tools it can act on; send_event keeps
    # the full "*" telemetry surface (its own inert-tool guard caps the cost).
    ("PreToolUse", "Agent|Bash|Edit|Write|Workflow", [
        ("workflow_reminder.py", "PreToolUse", 5),
    ]),
    ("PreToolUse", "*", [
        ("send_event.py", "PreToolUse", 5),
    ]),
    ("PostToolUse", "Agent|Bash|Edit|Write|Workflow", [
        ("workflow_reminder.py", "PostToolUse", 5),
    ]),
    ("PostToolUse", "*", [
        ("send_event.py", "PostToolUse", 5),
    ]),
    # These two read stdin and take no argv (plugin mode registers them from
    # hooks.json; the source install once copied but never registered them —
    # deep-review auto-link and meeting ecosystem writeback silently died,
    # found by end-to-end test 2026-07-10).
    ("PostToolUse", "mcp__ai-team-os__report_save", [
        ("deep_review_link.py", "", 5),
    ]),
    ("PostToolUse", "mcp__ai-team-os__meeting_conclude", [
        ("meeting_ecosystem_writeback.py", "", 5),
    ]),
    ("TaskCreated", "", [
        ("cc_task_bridge.py", "", 5),
    ]),
    ("SessionEnd", "", [
        ("send_event.py", "SessionEnd", 5),
    ]),
    ("Stop", "", [
        ("send_event.py", "Stop", 5),
    ]),
    ("UserPromptSubmit", "", [
        ("context_tracker.py", "", 5),
    ]),
    ("PermissionDenied", "", [
        ("permission_denied_recovery.py", "", 5),
    ]),
    ("PreCompact", "", [
        ("pre_compact_save.py", "", 10),
    ]),
]

# Every script the source install distributes, derived from the surface above so
# a new hook can never be registered without being copied.
HOOK_SCRIPTS: tuple[str, ...] = tuple(
    dict.fromkeys(script for _e, _m, entries in HOOK_SURFACE for script, _a, _t in entries)
)

# Hooks that were retired from the manifest. Their runtime copies and settings.json
# entries are actively removed on install/update — otherwise a retired hook keeps
# firing forever on every machine that ever installed it.
RETIRED_HOOK_SCRIPTS: tuple[str, ...] = (
    "task_completed_gate.py",   # retired 2026-07-27: CC task ids are ints, OS ids UUIDs
    "pipeline_gate.py",         # retired with the pipeline subsystem
    "autopilot_auto_stop.py",
)

RUNTIME_HOOKS_DIRNAME = "ai-team-os"


def _installed_hooks_dir() -> Path:
    """Fixed runtime hook location, independent of the clone directory."""
    return Path.home() / ".claude" / "hooks" / RUNTIME_HOOKS_DIRNAME


def _is_our_hook(command: str) -> bool:
    """True only for hook commands this installer owns.

    Deliberately name-based rather than "is 'ai-team-os' in the path": the runtime
    dir also hosts hooks shipped by other branches of this project (e.g. the
    channel sentinel) and users add their own hooks next to ours. Purging by path
    substring would silently delete them on every update.
    """
    marker = f"/hooks/{RUNTIME_HOOKS_DIRNAME}/"
    normalized = command.replace("\\", "/")
    if marker not in normalized:
        return False
    # Isolate the script name: '<py> "<dir>/send_event.py" PreToolUse' → send_event.py
    name = normalized.split(marker, 1)[1].split('"')[0].split(" ")[0].strip()
    return name in HOOK_SCRIPTS or name in RETIRED_HOOK_SCRIPTS


def copy_hook_scripts(project_root: Path) -> int:
    """Refresh ~/.claude/hooks/ai-team-os/ from plugin/hooks/ and drop retired copies."""
    src_hooks_dir = project_root / "plugin" / "hooks"
    installed_hooks_dir = _installed_hooks_dir()
    installed_hooks_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for fname in HOOK_SCRIPTS:
        src = src_hooks_dir / fname
        if src.exists():
            shutil.copy2(src, installed_hooks_dir / fname)
            copied += 1
        else:
            print(f"[WARN] Hook source missing, skipped: {src}")

    for fname in RETIRED_HOOK_SCRIPTS:
        stale = installed_hooks_dir / fname
        if stale.exists():
            stale.unlink()
            print(f"[OK] Removed retired hook: {stale.name}")

    print(f"[OK] Hook scripts copied ({copied}/{len(HOOK_SCRIPTS)}) → {installed_hooks_dir}")
    return copied


def register_hooks(project_root: Path) -> None:
    """Copy hook scripts to ~/.claude/hooks/ai-team-os/ and register in settings.json.

    Our own entries are rebuilt from HOOK_SURFACE on every run (so a matcher or
    timeout change actually lands, and retired hooks disappear); every hook we do
    not own is left exactly where it was.
    """
    copy_hook_scripts(project_root)

    hooks_dir = _installed_hooks_dir()
    settings_path = Path.home() / ".claude" / "settings.json"

    py = sys.executable  # use same interpreter that ran install.py

    # Quote path for Windows (handles spaces in path); use forward slashes for portability
    def q(p: Path | str) -> str:
        return f'"{str(p).replace(chr(92), "/")}"'

    def build_command(script: str, arg: str) -> str:
        # Byte-identical to auto_install._sync_main_chain's format, so the plugin
        # self-heal path and this installer can never double-register the chain.
        cmd = f"{q(py)} {q(hooks_dir / script)}"
        return f"{cmd} {arg}" if arg else cmd

    # Load or create settings
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}

    existing_hooks: dict = settings.setdefault("hooks", {})

    # 1. Drop our previous entries (stale matchers/timeouts/retired hooks), keep foreign ones.
    removed = 0
    for event in list(existing_hooks):
        groups = existing_hooks.get(event) or []
        surviving_groups = []
        for group in groups:
            kept = [h for h in group.get("hooks", []) if not _is_our_hook(h.get("command", ""))]
            removed += len(group.get("hooks", [])) - len(kept)
            if kept:
                surviving_groups.append({**group, "hooks": kept})
        if surviving_groups:
            existing_hooks[event] = surviving_groups
        else:
            del existing_hooks[event]

    # 2. Re-register the current surface.
    added = 0
    for event, matcher, entries in HOOK_SURFACE:
        entries = [e for e in entries if (hooks_dir / e[0]).exists()]
        if not entries:
            continue
        event_list: list = existing_hooks.setdefault(event, [])
        target_group = next((g for g in event_list if g.get("matcher", "") == matcher), None)
        if target_group is None:
            target_group = {"matcher": matcher, "hooks": []} if matcher else {"hooks": []}
            event_list.append(target_group)
        for script, arg, timeout in entries:
            target_group.setdefault("hooks", []).append({
                "type": "command",
                "command": build_command(script, arg),
                "timeout": timeout,
            })
            added += 1

    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    events = len({event for event, _m, _e in HOOK_SURFACE})
    print(f"[OK] Registered {added} hook(s) across {events} event(s) into settings.json"
          f" (replaced {removed} previous entr{'y' if removed == 1 else 'ies'})")


def copy_agent_templates(project_root: Path, overwrite: bool = False) -> None:
    """Copy plugin/agents/*.md to ~/.claude/agents/.

    Source is plugin/agents (the plugin-mode superset: 25 templates, incl.
    debate-advocate/debate-critic/team-member) rather than .claude/agents (22).
    Using the same source as the plugin keeps independent-install parity with
    marketplace-install — otherwise the source path silently ships 3 fewer agents.

    Args:
        project_root: Root directory of the ai-team-os project.
        overwrite: When True (update mode), overwrite existing templates.
                   When False (fresh install), skip existing files.
    """
    src_agents = project_root / "plugin" / "agents"
    dst_agents = Path.home() / ".claude" / "agents"

    if not src_agents.exists():
        print("[SKIP] No agent templates found in plugin/agents/")
        return

    dst_agents.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0

    for template in src_agents.glob("*.md"):
        dst = dst_agents / template.name
        if dst.exists() and not overwrite:
            skipped += 1
        else:
            shutil.copy2(template, dst)
            copied += 1

    if overwrite:
        print(f"[OK] Agent templates: {copied} refreshed → {dst_agents}")
    else:
        print(f"[OK] Agent templates: {copied} copied, {skipped} already existed (skipped)")


def copy_skills(project_root: Path, overwrite: bool = False) -> None:
    """Copy plugin/skills/<name>/ trees to ~/.claude/skills/<name>/.

    CC discovers user-level skills at ~/.claude/skills/<name>/SKILL.md, so each
    skill is a whole directory (SKILL.md plus nested resources like templates/).
    We copy the tree recursively. Without this, uninstalling the plugin drops the
    /os-* skills entirely on independent installs (they were never distributed).

    Args:
        project_root: Root directory of the ai-team-os project.
        overwrite: When True (update mode), refresh files in existing skill dirs.
                   When False (fresh install), skip skills whose dir already exists
                   (never clobber a user-customized skill).
    """
    src_skills = project_root / "plugin" / "skills"
    dst_skills = Path.home() / ".claude" / "skills"

    if not src_skills.exists():
        print("[SKIP] No skills found in plugin/skills/")
        return

    dst_skills.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0

    for skill_dir in sorted(p for p in src_skills.iterdir() if p.is_dir()):
        dst = dst_skills / skill_dir.name
        if dst.exists() and not overwrite:
            skipped += 1
            continue
        # dirs_exist_ok merges on update: our files overwrite, user-added files stay.
        shutil.copytree(skill_dir, dst, dirs_exist_ok=True)
        copied += 1

    if overwrite:
        print(f"[OK] Skills: {copied} refreshed → {dst_skills}")
    else:
        print(f"[OK] Skills: {copied} copied, {skipped} already existed (skipped)")


def copy_commands(project_root: Path, overwrite: bool = False) -> None:
    """Copy plugin/commands/*.md to ~/.claude/commands/.

    CC discovers user-level slash commands at ~/.claude/commands/*.md. Without
    this, the /os-* commands vanish when the plugin is uninstalled on independent
    installs (they were never distributed by the source installer).

    Args:
        project_root: Root directory of the ai-team-os project.
        overwrite: When True (update mode), overwrite existing command files.
                   When False (fresh install), skip existing files.
    """
    src_commands = project_root / "plugin" / "commands"
    dst_commands = Path.home() / ".claude" / "commands"

    if not src_commands.exists():
        print("[SKIP] No commands found in plugin/commands/")
        return

    dst_commands.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0

    for command in sorted(src_commands.glob("*.md")):
        dst = dst_commands / command.name
        if dst.exists() and not overwrite:
            skipped += 1
        else:
            shutil.copy2(command, dst)
            copied += 1

    if overwrite:
        print(f"[OK] Commands: {copied} refreshed → {dst_commands}")
    else:
        print(f"[OK] Commands: {copied} copied, {skipped} already existed (skipped)")


def install_loop_md(project_root: Path) -> None:
    """Install the OS `/loop` maintenance prompt to ~/.claude/loop.md (idempotent).

    session_bootstrap.py tells every session the maintenance prompt lives at
    ~/.claude/loop.md, so a source install must actually write it (previously only
    the now-deprecated scripts/install.py did — this is the surviving single path).
    Installed at user level so bare `/loop` picks it up in any project. If the
    destination exists without our sentinel it is treated as user-customized and
    left untouched.
    """
    src = project_root / "plugin" / "loop.md"
    dst = Path.home() / ".claude" / "loop.md"

    if not src.is_file():
        print(f"[WARN] loop.md template missing at {src}")
        return

    if dst.exists():
        existing = dst.read_text(encoding="utf-8", errors="replace")
        if LOOP_TEMPLATE_SENTINEL not in existing:
            print("[SKIP] ~/.claude/loop.md exists and looks user-customized — left untouched")
            return

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"[OK] /loop maintenance prompt → {dst}")


def register_global_mcp(project_root: Path) -> None:
    """Register ai-team-os MCP server globally + project-level fallback.

    CC loads global MCP from ~/.claude.json (NOT ~/.claude/settings.json).
    We also generate project-level .mcp.json as a reliable fallback.
    """
    mcp_entry = {
        "command": sys.executable,
        "args": ["-m", "aiteam.mcp.server"],
    }

    # Method 1: Try CLI command (most reliable)
    try:
        import json as _json
        result = subprocess.run(
            ["claude", "mcp", "add-json", "--scope", "global", "ai-team-os",
             _json.dumps(mcp_entry)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            print("[OK] Registered global MCP via 'claude mcp add-json --scope global'")
        else:
            raise RuntimeError(result.stderr)
    except Exception:
        # Method 2: Direct write to ~/.claude.json (runtime state file)
        claude_json_path = Path.home() / ".claude.json"
        try:
            if claude_json_path.exists():
                claude_json = json.loads(claude_json_path.read_text(encoding="utf-8"))
            else:
                claude_json = {}

            mcp_servers: dict = claude_json.setdefault("mcpServers", {})
            if "ai-team-os" not in mcp_servers:
                mcp_servers["ai-team-os"] = mcp_entry
                claude_json_path.write_text(
                    json.dumps(claude_json, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                print("[OK] Registered global MCP in ~/.claude.json")
            else:
                print("[OK] Global MCP 'ai-team-os' already in ~/.claude.json")
        except Exception as e:
            print(f"[WARN] Could not register global MCP: {e}")

    # Only generate project-level .mcp.json if global registration failed
    claude_json_path = Path.home() / ".claude.json"
    global_ok = False
    if claude_json_path.exists():
        try:
            cj = json.loads(claude_json_path.read_text(encoding="utf-8"))
            global_ok = "ai-team-os" in cj.get("mcpServers", {})
        except Exception:
            pass
    if not global_ok:
        print("[WARN] Global MCP registration failed, falling back to project-level .mcp.json")
        _write_project_mcp_json(project_root)


def _write_project_mcp_json(project_root: Path) -> None:
    """Write project-level .mcp.json as fallback when global registration fails."""
    mcp_json = project_root / ".mcp.json"
    config = {
        "mcpServers": {
            "ai-team-os": {
                # sys.executable, not bare "python": this fallback fires exactly when
                # global registration failed — don't stack a venv-hijack risk on top (e2d0fbb).
                "command": sys.executable,
                "args": ["-m", "aiteam.mcp.server"],
                "cwd": str(project_root).replace("\\", "/"),
            }
        }
    }
    mcp_json.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"[OK] Generated project-level .mcp.json at {mcp_json}")


def verify_installation(project_root: Path) -> bool:
    """Run post-install checks and report results."""
    print()
    print("Verifying installation...")

    agents_dir = Path.home() / ".claude" / "agents"
    has_templates = agents_dir.exists() and any(agents_dir.glob("*.md"))

    # Skills / commands: derive expected names from the plugin source (source of
    # truth) and confirm each landed in the user-level dir. Deriving avoids a
    # hardcoded list drifting out of sync when assets are added/removed.
    skills_ok, skills_detail = _verify_assets_present(
        project_root / "plugin" / "skills",
        Path.home() / ".claude" / "skills",
        lambda p: [d.name for d in p.iterdir() if d.is_dir()],
        lambda dst, name: (dst / name / "SKILL.md").exists(),
    )
    commands_ok, commands_detail = _verify_assets_present(
        project_root / "plugin" / "commands",
        Path.home() / ".claude" / "commands",
        lambda p: [f.name for f in p.glob("*.md")],
        lambda dst, name: (dst / name).exists(),
    )

    settings_path = Path.home() / ".claude" / "settings.json"
    has_hooks = False
    if settings_path.exists():
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
            has_hooks = bool(cfg.get("hooks"))
        except Exception:
            pass

    # CC loads global MCP from ~/.claude.json (NOT ~/.claude/settings.json) —
    # must match where register_global_mcp actually writes, or verify always fails.
    claude_json_path = Path.home() / ".claude.json"
    has_global_mcp = False
    if claude_json_path.exists():
        try:
            cj = json.loads(claude_json_path.read_text(encoding="utf-8"))
            has_global_mcp = "ai-team-os" in cj.get("mcpServers", {})
        except Exception:
            pass

    # Project-level .mcp.json is present if global registration failed (fallback)
    has_project_mcp = (project_root / ".mcp.json").exists()

    checks = [
        ("Global MCP in ~/.claude.json", has_global_mcp),
        ("Project .mcp.json (fallback)", has_project_mcp),
        (f"~/.claude/agents/ templates{_asset_suffix(agents_dir, '*.md')}", has_templates),
        (f"~/.claude/skills/ ({skills_detail})", skills_ok),
        (f"~/.claude/commands/ ({commands_detail})", commands_ok),
        ("~/.claude/loop.md (/loop prompt)", (Path.home() / ".claude" / "loop.md").exists()),
        ("~/.claude/settings.json hooks", has_hooks),
        ("Hook scripts (plugin/hooks/)", (project_root / "plugin" / "hooks" / "send_event.py").exists()),
        ("Python package (aiteam)", _check_package("aiteam")),
    ]

    all_ok = True
    for label, ok in checks:
        # Global MCP is required; project .mcp.json is optional (fallback only)
        required = label != "Project .mcp.json (fallback)"
        status = "[OK]" if ok else ("[WARN]" if not required else "[FAIL]")
        print(f"  {status} {label}")
        if not ok and required:
            all_ok = False

    return all_ok


def _asset_suffix(dst_dir: Path, pattern: str) -> str:
    """Return a ' (N present)' suffix for a verify label, or '' if dir absent."""
    if not dst_dir.exists():
        return ""
    return f" ({len(list(dst_dir.glob(pattern)))} present)"


def _verify_assets_present(src_dir: Path, dst_dir: Path, list_expected, is_present):
    """Confirm every asset the source ships also landed under dst_dir.

    Args:
        src_dir: Plugin source directory (source of truth for expected names).
        dst_dir: User-level install directory to check.
        list_expected: callable(src_dir) -> list[str] of expected asset names.
        is_present: callable(dst_dir, name) -> bool, True if the asset is installed.

    Returns:
        (ok, detail) where ok is True when all expected assets are present and
        detail is a short "<present>/<expected> present" string for the report.
    """
    if not src_dir.exists():
        return True, "no source"
    expected = list_expected(src_dir)
    if not expected:
        return True, "none to install"
    present = sum(1 for name in expected if is_present(dst_dir, name))
    return present == len(expected), f"{present}/{len(expected)} present"


def _check_package(pkg: str) -> bool:
    """Check importability by module name (dist name is 'ai-team-os', module is 'aiteam' —
    `pip show aiteam` always fails, which made this check a guaranteed false FAIL)."""
    try:
        import importlib.util
        return importlib.util.find_spec(pkg) is not None
    except Exception:
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="AI Team OS installer / updater",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python install.py            # fresh install\n"
            "  python install.py --update   # update existing installation\n"
        ),
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Run in update mode (delegates to scripts/update.py)",
    )
    args = parser.parse_args()

    if args.update:
        # Delegate to the dedicated update script
        update_script = Path(__file__).resolve().parent / "scripts" / "update.py"
        if not update_script.exists():
            print("[FAIL] scripts/update.py not found — cannot run update")
            sys.exit(1)
        import importlib.util
        spec = importlib.util.spec_from_file_location("update_module", update_script)
        if spec is None or spec.loader is None:
            print("[FAIL] Could not load scripts/update.py")
            sys.exit(1)
        update_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(update_mod)  # type: ignore[union-attr]
        update_mod.run_update(Path(__file__).resolve().parent)
        return

    print("=" * 50)
    print("  AI Team OS Installer")
    print("=" * 50)
    print()

    project_root = Path(__file__).resolve().parent

    # 1. Check Python version
    if sys.version_info < (3, 11):  # noqa: UP036 — 运行时门卫：低版本环境需友好报错
        print(f"[FAIL] Python 3.11+ required. Current: {sys.version}")
        sys.exit(1)
    print(f"[OK] Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    # 2. Check Node.js (optional, for Dashboard)
    has_node = check_command("node")
    has_npm = check_command("npm")
    if has_node and has_npm:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True)
        print(f"[OK] Node.js {result.stdout.strip()}")
    else:
        print("[WARN] Node.js/npm not found — Dashboard build will be skipped")
        print("       Dashboard is optional; core functionality is unaffected")

    print()

    # 3. Install Python packages
    print("[...] Installing Python dependencies...")
    try:
        run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=str(project_root))
        print("[OK] Python dependencies installed")
    except SystemExit:
        print("[WARN] pip install -e . failed — trying direct dependency install...")
        try:
            run([sys.executable, "-m", "pip", "install",
                 "fastapi", "uvicorn", "sqlalchemy", "aiosqlite",
                 "pydantic", "pydantic-settings", "pyyaml", "anyio", "fastmcp"],
                cwd=str(project_root))
            print("[OK] Core dependencies installed (fallback)")
        except SystemExit:
            print("[WARN] Some dependencies may be missing — continuing with setup")
    print()

    # 4. Build Dashboard (optional)
    dashboard_dir = project_root / "dashboard"
    if dashboard_dir.exists() and has_node and has_npm:
        print("[...] Installing Dashboard dependencies...")
        run(["npm", "install"], cwd=str(dashboard_dir))
        print("[OK] Dashboard dependencies installed")

        print("[...] Building Dashboard...")
        run(["npm", "run", "build"], cwd=str(dashboard_dir))
        print("[OK] Dashboard built")
        print()
    elif dashboard_dir.exists():
        print("[SKIP] Dashboard build skipped (Node.js required)")
        print()

    # 5. Create data directory and record install path for update checker
    data_dir = Path.home() / ".claude" / "data" / "ai-team-os"
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"[OK] Data directory: {data_dir}")

    # Save project root path so session_bootstrap.py can locate the repo for update checks
    install_path_file = data_dir / "install_path.txt"
    install_path_file.write_text(str(project_root), encoding="utf-8")
    print(f"[OK] Install path recorded: {project_root}")

    # 6. Register MCP server globally into ~/.claude/settings.json
    # This makes ai-team-os tools available in ALL projects, not just this directory.
    # Falls back to writing a project-level .mcp.json if global registration fails.
    print("[...] Registering MCP server globally...")
    register_global_mcp(project_root)

    # 7. Register hooks into ~/.claude/settings.json
    print("[...] Registering hooks into ~/.claude/settings.json...")
    register_hooks(project_root)

    # 8. Copy agent templates to ~/.claude/agents/
    print("[...] Copying agent templates to ~/.claude/agents/...")
    copy_agent_templates(project_root)

    # 8b. Copy skills to ~/.claude/skills/ and commands to ~/.claude/commands/
    print("[...] Copying skills to ~/.claude/skills/...")
    copy_skills(project_root)
    print("[...] Copying commands to ~/.claude/commands/...")
    copy_commands(project_root)

    # 8c. Install the /loop maintenance prompt to ~/.claude/loop.md
    print("[...] Installing /loop maintenance prompt to ~/.claude/loop.md...")
    install_loop_md(project_root)

    # 9. Verify installation
    all_ok = verify_installation(project_root)

    # 10. Done
    print()
    print("=" * 50)
    print("  Installation complete!")
    print("=" * 50)
    print()
    print("Next steps:")
    print()
    print("  Step 1 — Start the API server (required for MCP tools):")
    print("    python -m uvicorn aiteam.api.app:create_app --factory --host 0.0.0.0 --port 8000")
    print()
    print("  Step 2 — Restart Claude Code")
    print("    Hooks and MCP tools activate on next launch.")
    print("    Verify: run /mcp in Claude Code and check ai-team-os tools are mounted.")
    print()
    print("  Step 3 — Verify the API is running:")
    print("    curl http://localhost:8000/api/health")
    print("    Expected: {\"status\": \"ok\"}")
    print()
    if dashboard_dir.exists() and has_node:
        print("  Optional — Start the Dashboard:")
        print("    cd dashboard && npm run dev")
        print("    Visit: http://localhost:5173")
        print()
    print("  Step 4 — Create your first team in Claude Code:")
    print('    /os-up  (or type: "Create a web dev team with frontend, backend, and QA")')
    print()
    if not all_ok:
        print("[WARN] Some checks failed. Review the output above before proceeding.")
    print("For more information: plugin/README.md")


if __name__ == "__main__":
    main()
