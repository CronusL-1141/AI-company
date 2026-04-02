#!/usr/bin/env python3
"""AI Team OS installer — one command to configure everything.

Usage:
  python scripts/install.py          # Full install
  python scripts/install.py --check  # Verify installation
  python scripts/install.py --uninstall  # Remove configuration
"""
import argparse
import json
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_NAME = "ai-team-os"

# Hooks are copied to an ASCII-safe path so Windows has no encoding issues.
HOOKS_TARGET_DIR = Path.home() / ".claude" / "plugins" / PLUGIN_NAME / "hooks"

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Hook event definitions: each value is either a list of (script, timeout)
# tuples (no matcher) or a dict with 'matcher' and 'scripts'.
HOOK_EVENTS: dict = {
    "SubagentStart": [
        ("inject_subagent_context.py", 3000),
        ("send_event.py SubagentStart", 2000),
    ],
    "SubagentStop": [
        ("send_event.py SubagentStop", 2000),
    ],
    "PreToolUse": {
        "matcher": "Agent|Bash|Edit|Write",
        "scripts": [
            ("workflow_reminder.py PreToolUse", 3000),
            ("send_event.py PreToolUse", 2000),
        ],
    },
    "PostToolUse": {
        "matcher": "Agent|Bash|Edit|Write",
        "scripts": [
            ("workflow_reminder.py PostToolUse", 3000),
            ("send_event.py PostToolUse", 2000),
        ],
    },
    "SessionStart": [
        ("session_bootstrap.py", 3000),
        ("send_event.py SessionStart", 2000),
    ],
    "SessionEnd": [
        ("send_event.py SessionEnd", 2000),
    ],
    "Stop": [
        ("send_event.py Stop", 2000),
    ],
    "UserPromptSubmit": [
        ("context_monitor.py", 3000),
    ],
    "PreCompact": [
        ("pre_compact_save.py", 5000),
    ],
}

# Marker used to identify hooks that belong to this plugin.
OUR_HOOK_MARKER = str(HOOKS_TARGET_DIR).replace("\\", "/")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hooks_dir_fwd(hooks_dir: Path) -> str:
    """Return forward-slash string for the hooks directory."""
    return str(hooks_dir).replace("\\", "/")


def _build_command(script_entry: str, hooks_dir: Path) -> str:
    """Build a 'python /path/to/script.py [arg]' command string.

    script_entry examples: 'send_event.py SubagentStart', 'context_monitor.py'
    """
    parts = script_entry.split(" ", 1)
    script_name = parts[0]
    arg = parts[1] if len(parts) > 1 else ""
    script_path = f"{_hooks_dir_fwd(hooks_dir)}/{script_name}"
    cmd = f'python "{script_path}"'
    if arg:
        cmd += f" {arg}"
    return cmd


def _build_hook_group(event: str, hooks_dir: Path) -> dict:
    """Build a single hook group dict for a given event."""
    event_def = HOOK_EVENTS[event]
    if isinstance(event_def, list):
        scripts = event_def
        matcher = None
    else:
        scripts = event_def["scripts"]
        matcher = event_def["matcher"]

    hooks_list = [
        {
            "type": "command",
            "command": _build_command(script, hooks_dir),
            "timeout": timeout,
        }
        for script, timeout in scripts
    ]
    group: dict = {"hooks": hooks_list}
    if matcher:
        group["matcher"] = matcher
    return group


def _is_our_hook(command: str) -> bool:
    """Return True if the hook command was installed by this plugin."""
    hooks_dir_fwd = _hooks_dir_fwd(HOOKS_TARGET_DIR)
    return hooks_dir_fwd in command or PLUGIN_NAME in command


def _load_settings() -> dict:
    """Load ~/.claude/settings.json, returning {} if missing or invalid."""
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_settings(settings: dict) -> None:
    """Write settings dict back to ~/.claude/settings.json."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Step 1: Copy hook scripts
# ---------------------------------------------------------------------------

def install_hooks(source_dir: Path, target_dir: Path) -> None:
    """Copy hook scripts from plugin/hooks/ to ~/.claude/plugins/ai-team-os/hooks/."""
    print(f"[STEP 1] Install hook scripts -> {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for py_file in source_dir.glob("*.py"):
        dest = target_dir / py_file.name
        shutil.copy2(str(py_file), str(dest))
        copied += 1
        print(f"  [COPY]  {py_file.name}")

    if copied == 0:
        print(f"  [WARN]  No .py files found in {source_dir}")
    else:
        print(f"  [OK]    {copied} file(s) copied")


# ---------------------------------------------------------------------------
# Step 2: Register MCP server in settings.json
# ---------------------------------------------------------------------------

def install_mcp(settings: dict, project_root: Path) -> None:
    """Add ai-team-os MCP server entry to settings['mcpServers'] (idempotent)."""
    print("\n[STEP 2] Register MCP server in settings.json")

    mcp_servers: dict = settings.setdefault("mcpServers", {})
    cwd = str(project_root / "ai-team-os").replace("\\", "/")

    # Use forward-slash python path for cross-platform safety.
    python_exe = str(sys.executable).replace("\\", "/")

    mcp_servers[PLUGIN_NAME] = {
        "command": python_exe,
        "args": ["-m", "aiteam.mcp.server"],
        "cwd": cwd,
        "env": {"AITEAM_API_URL": "http://localhost:8000"},
    }
    print(f"  [OK]    mcpServers['{PLUGIN_NAME}'] -> {python_exe}")


# ---------------------------------------------------------------------------
# Step 3: Register hooks in settings.json
# ---------------------------------------------------------------------------

def install_hook_events(settings: dict, hooks_dir: Path) -> None:
    """Merge our hook events into settings['hooks'] without removing others."""
    print("\n[STEP 3] Register hooks in settings.json")

    existing_hooks: dict = settings.setdefault("hooks", {})

    for event in HOOK_EVENTS:
        new_group = _build_hook_group(event, hooks_dir)

        if event not in existing_hooks:
            existing_hooks[event] = [new_group]
            print(f"  [ADD]   {event}")
            continue

        # Remove stale entries from this plugin, keep everything else.
        groups = existing_hooks[event]
        foreign_groups = [
            g for g in groups
            if not any(_is_our_hook(h.get("command", "")) for h in g.get("hooks", []))
        ]
        existing_hooks[event] = foreign_groups + [new_group]
        print(f"  [MERGE] {event}")

    print(f"  [OK]    {len(HOOK_EVENTS)} event(s) configured")


# ---------------------------------------------------------------------------
# Full install
# ---------------------------------------------------------------------------

def run_install(project_root: Path) -> int:
    """Execute full install. Returns exit code."""
    print("=" * 55)
    print("  AI Team OS Installer")
    print("=" * 55)

    source_hooks = project_root / "ai-team-os" / "plugin" / "hooks"
    if not source_hooks.exists():
        print(f"\n[ERROR] Hook source directory not found: {source_hooks}")
        print("        Are you running from the repo root?")
        return 1

    # Step 1: copy hooks
    install_hooks(source_hooks, HOOKS_TARGET_DIR)

    # Steps 2+3: update settings.json
    settings = _load_settings()
    install_mcp(settings, project_root)
    install_hook_events(settings, HOOKS_TARGET_DIR)
    _save_settings(settings)

    print("\n" + "=" * 55)
    print("  Install complete.")
    print("  *** Restart Claude Code to activate hooks ***")
    print("=" * 55)
    return 0


# ---------------------------------------------------------------------------
# --check mode
# ---------------------------------------------------------------------------

def run_check() -> int:
    """Verify the installation. Returns exit code (0=ok, 1=issues found)."""
    print("=" * 55)
    print("  AI Team OS Install Check")
    print("=" * 55)

    issues: list[str] = []

    # 1. Hooks directory and key files
    print("\n[1] Hook scripts")
    required_hooks = [
        "inject_subagent_context.py",
        "send_event.py",
        "workflow_reminder.py",
        "session_bootstrap.py",
        "context_monitor.py",
        "pre_compact_save.py",
    ]
    if HOOKS_TARGET_DIR.exists():
        print(f"    Directory: {HOOKS_TARGET_DIR}  [OK]")
        for fname in required_hooks:
            fpath = HOOKS_TARGET_DIR / fname
            status = "[OK]" if fpath.exists() else "[MISSING]"
            print(f"    {fname}: {status}")
            if not fpath.exists():
                issues.append(f"Missing hook: {fname}")
    else:
        print(f"    [MISSING] {HOOKS_TARGET_DIR}")
        issues.append(f"Hooks directory missing: {HOOKS_TARGET_DIR}")

    # 2. MCP server in settings.json
    print("\n[2] MCP server in settings.json")
    settings = _load_settings()
    mcp_servers = settings.get("mcpServers", {})
    if PLUGIN_NAME in mcp_servers:
        entry = mcp_servers[PLUGIN_NAME]
        print(f"    Found: {entry.get('command')}  [OK]")
    else:
        print(f"    [MISSING] '{PLUGIN_NAME}' not in mcpServers")
        issues.append("MCP server not registered in settings.json")

    # 3. Hook events in settings.json
    print("\n[3] Hook events in settings.json")
    hooks_cfg = settings.get("hooks", {})
    registered = 0
    for event in HOOK_EVENTS:
        groups = hooks_cfg.get(event, [])
        found = any(
            _is_our_hook(h.get("command", ""))
            for g in groups
            for h in g.get("hooks", [])
        )
        status = "[OK]" if found else "[MISSING]"
        print(f"    {event}: {status}")
        if found:
            registered += 1
        else:
            issues.append(f"Hook event not registered: {event}")
    print(f"    Total: {registered}/{len(HOOK_EVENTS)} events")

    # 4. aiteam package import
    print("\n[4] Python package: aiteam")
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import aiteam; print(aiteam.__file__)"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            pkg_path = result.stdout.strip()
            print(f"    import aiteam  [OK]  ({pkg_path})")
        else:
            print(f"    import aiteam  [FAIL]")
            print(f"    {result.stderr.strip()}")
            issues.append("aiteam package not importable")
    except Exception as exc:
        print(f"    [ERROR] {exc}")
        issues.append(f"Could not run python check: {exc}")

    # 5. API connectivity
    print("\n[5] API server (http://localhost:8000)")
    try:
        with urllib.request.urlopen("http://localhost:8000/health", timeout=3) as resp:
            print(f"    HTTP {resp.status}  [OK]")
    except urllib.error.HTTPError as exc:
        # Any HTTP response means the server is running.
        print(f"    HTTP {exc.code}  [OK] (server responding)")
    except Exception:
        print("    [UNREACHABLE] API server not running (start with: python -m aiteam.api)")
        # Not a hard failure — user may not have started the server yet.

    # Summary
    print("\n" + "=" * 55)
    if issues:
        print(f"  {len(issues)} issue(s) found:")
        for issue in issues:
            print(f"    - {issue}")
        print("\n  Run: python scripts/install.py")
        print("=" * 55)
        return 1
    else:
        print("  All checks passed.")
        print("=" * 55)
        return 0


# ---------------------------------------------------------------------------
# --uninstall mode
# ---------------------------------------------------------------------------

def run_uninstall() -> int:
    """Remove hooks directory and our entries from settings.json."""
    print("=" * 55)
    print("  AI Team OS Uninstaller (config only)")
    print("=" * 55)

    # Step 1: remove hooks directory
    print(f"\n[STEP 1] Remove hooks directory: {HOOKS_TARGET_DIR}")
    if HOOKS_TARGET_DIR.exists():
        shutil.rmtree(str(HOOKS_TARGET_DIR))
        print("  [OK]    Removed")
    else:
        print("  [SKIP]  Not found")

    # Step 2: strip our entries from settings.json
    print("\n[STEP 2] Clean settings.json")
    settings = _load_settings()
    changed = False

    # Remove MCP server
    mcp_servers: dict = settings.get("mcpServers", {})
    if PLUGIN_NAME in mcp_servers:
        del mcp_servers[PLUGIN_NAME]
        print(f"  [REMOVE] mcpServers['{PLUGIN_NAME}']")
        changed = True
    else:
        print(f"  [SKIP]   '{PLUGIN_NAME}' not in mcpServers")

    # Remove our hook entries from each event
    hooks_cfg: dict = settings.get("hooks", {})
    removed_hooks = 0
    events_to_delete: list[str] = []
    for event, groups in list(hooks_cfg.items()):
        new_groups = []
        for group in groups:
            new_hook_list = [
                h for h in group.get("hooks", [])
                if not _is_our_hook(h.get("command", ""))
            ]
            removed_hooks += len(group.get("hooks", [])) - len(new_hook_list)
            if new_hook_list:
                new_groups.append({**group, "hooks": new_hook_list})
        if new_groups:
            hooks_cfg[event] = new_groups
        else:
            events_to_delete.append(event)

    for event in events_to_delete:
        del hooks_cfg[event]

    if not hooks_cfg:
        settings.pop("hooks", None)

    if removed_hooks:
        print(f"  [REMOVE] {removed_hooks} hook command(s) from settings.json")
        changed = True
    else:
        print("  [SKIP]   No matching hooks found")

    if changed:
        _save_settings(settings)
        print("  [OK]    settings.json updated")

    print("\n" + "=" * 55)
    print("  Uninstall complete (data/DB preserved).")
    print("  *** Restart Claude Code to deactivate hooks ***")
    print("=" * 55)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Team OS installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/install.py          # Full install\n"
            "  python scripts/install.py --check  # Verify installation\n"
            "  python scripts/install.py --uninstall  # Remove configuration\n"
        ),
    )
    parser.add_argument("--check", action="store_true", help="Verify installation")
    parser.add_argument("--uninstall", action="store_true", help="Remove configuration")
    args = parser.parse_args()

    # Determine project root: parent of the directory containing this script.
    # Expected layout: <project_root>/ai-team-os/scripts/install.py
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent.parent  # scripts/ -> ai-team-os/ -> project_root/

    if args.check:
        sys.exit(run_check())
    elif args.uninstall:
        sys.exit(run_uninstall())
    else:
        sys.exit(run_install(project_root))


if __name__ == "__main__":
    main()
