#!/usr/bin/env python3
"""AI Team OS installer — one command to configure everything.

Usage:
  python scripts/install.py          # Full install
  python scripts/install.py --check  # Verify installation
  python scripts/install.py --uninstall  # Remove configuration
"""
import argparse
import json
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Absolute path of the Python that is running this installer.
# Using sys.executable avoids picking up a project venv's Python when
# CC activates a .venv in the working directory.
PYTHON_EXE = sys.executable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_NAME = "ai-team-os"

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Hook event definitions.
# Each event maps to a list of "group specs". A group spec is either:
#   - list[tuple(str, int)]  → one group, no matcher
#   - dict with "scripts" (required) and optional "matcher"
# Module format: "<module_name> [arg]" → python -m aiteam.hooks.<module_name> [arg]
HOOK_EVENTS: dict = {
    "SubagentStart": [
        [("inject_subagent_context", 3000), ("send_event SubagentStart", 2000)],
    ],
    "SubagentStop": [
        [("send_event SubagentStop", 2000)],
    ],
    "PreToolUse": [
        {
            "matcher": "Agent|Bash|Edit|Write",
            "scripts": [
                ("workflow_reminder PreToolUse", 3000),
                ("send_event PreToolUse", 2000),
            ],
        },
    ],
    "PostToolUse": [
        {
            "matcher": "Agent|Bash|Edit|Write",
            "scripts": [
                ("workflow_reminder PostToolUse", 3000),
                ("send_event PostToolUse", 2000),
            ],
        },
    ],
    "SessionStart": [
        [("session_bootstrap", 3000), ("send_event SessionStart", 2000)],
    ],
    "SessionEnd": [
        [("send_event SessionEnd", 2000)],
    ],
    "Stop": [
        [("send_event Stop", 2000)],
    ],
    "UserPromptSubmit": [
        [("context_monitor", 3000)],
    ],
    "PreCompact": [
        [("pre_compact_save", 5000)],
    ],
}

# Marker used to identify hooks that belong to this plugin.
OUR_HOOK_MARKER = "aiteam.hooks."

# Mapping: plugin/agents/<src> → ~/.claude/agents/<dst>
AGENT_TEMPLATE_MAPPING: dict[str, str] = {
    "engineering-backend-architect.md": "backend-architect.md",
    "engineering-software-architect.md": "software-architect.md",
    "engineering-ai-engineer.md": "ai-engineer.md",
    "engineering-code-reviewer.md": "code-reviewer.md",
    "engineering-frontend-developer.md": "frontend-developer.md",
    "engineering-mobile-developer.md": "mobile-developer.md",
    "engineering-security-engineer.md": "security-engineer.md",
    "engineering-database-optimizer.md": "database-optimizer.md",
    "engineering-devops-automator.md": "engineering-devops-automator.md",
    "engineering-git-workflow-master.md": "git-workflow-master.md",
    "engineering-mcp-builder.md": "engineering-mcp-builder.md",
    "engineering-rapid-prototyper.md": "rapid-prototyper.md",
    "engineering-sre.md": "sre.md",
    "management-project-manager.md": "management-project-manager.md",
    "management-tech-lead.md": "management-tech-lead.md",
    "specialized-workflow-architect.md": "workflow-architect.md",
    "support-meeting-facilitator.md": "support-meeting-facilitator.md",
    "support-technical-writer.md": "technical-writer.md",
    "testing-api-tester.md": "api-tester.md",
    "testing-bug-fixer.md": "testing-bug-fixer.md",
    "testing-performance-benchmarker.md": "performance-benchmarker.md",
    "testing-qa-engineer.md": "testing-qa-engineer.md",
    "debate-advocate.md": "debate-advocate.md",
    "debate-critic.md": "debate-critic.md",
    "team-member.md": "team-member.md",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_command(module_entry: str) -> str:
    """Build a '<python_exe> -m aiteam.hooks.<module> [arg]' command string.

    Uses the absolute path of the installer's Python so the command works even
    when CC activates a project .venv that shadows the system Python.
    module_entry examples: 'send_event SubagentStart', 'context_monitor'
    """
    parts = module_entry.split(" ", 1)
    module_name = parts[0]
    arg = parts[1] if len(parts) > 1 else ""
    # Quote the exe path to handle spaces (e.g. C:\Program Files\Python312\python.exe)
    cmd = f'"{PYTHON_EXE}" -m aiteam.hooks.{module_name}'
    if arg:
        cmd += f" {arg}"
    return cmd


def _build_hook_groups(event: str) -> list[dict]:
    """Build hook group dicts for a given event.

    Each event in HOOK_EVENTS maps to a list of group specs.
    A group spec is either a list of (module, timeout) tuples (no matcher)
    or a dict with 'scripts' and optional 'matcher'.
    """
    group_specs = HOOK_EVENTS[event]
    result: list[dict] = []

    for spec in group_specs:
        if isinstance(spec, list):
            # Simple list of tuples → one group, no matcher
            scripts = spec
            matcher = None
        else:
            # Dict with matcher + scripts
            scripts = spec["scripts"]
            matcher = spec.get("matcher")

        hooks_list = [
            {
                "type": "command",
                "command": _build_command(module_entry),
                "timeout": timeout,
            }
            for module_entry, timeout in scripts
        ]
        group: dict = {"hooks": hooks_list}
        if matcher:
            group["matcher"] = matcher
        result.append(group)

    return result


def _is_our_hook(command: str) -> bool:
    """Return True if the hook command was installed by this plugin."""
    return OUR_HOOK_MARKER in command or PLUGIN_NAME in command


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
# Step 2: Register MCP server in settings.json
# ---------------------------------------------------------------------------

def install_mcp(settings: dict, project_root: Path) -> None:
    """Add ai-team-os MCP server to settings.json AND ~/.mcp.json (idempotent)."""
    print("\n[STEP 2] Register MCP server")

    # MCP config — use the absolute installer Python path so the MCP server
    # is always launched with the Python that has aiteam installed, regardless
    # of any project .venv that CC might activate.
    mcp_entry = {
        "command": PYTHON_EXE,
        "args": ["-m", "aiteam.mcp.server"],
        "env": {"AITEAM_API_URL": "http://localhost:8000"},
    }

    # 2a. Write to settings.json (for current-project sessions)
    mcp_servers: dict = settings.setdefault("mcpServers", {})
    mcp_servers[PLUGIN_NAME] = mcp_entry
    print(f"  [OK]    settings.json mcpServers['{PLUGIN_NAME}']")

    # 2b. Write to ~/.mcp.json (for cross-project global availability)
    global_mcp_path = Path.home() / ".mcp.json"
    global_mcp: dict = {}
    if global_mcp_path.exists():
        try:
            global_mcp = json.loads(global_mcp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    global_mcp.setdefault("mcpServers", {})[PLUGIN_NAME] = mcp_entry
    global_mcp_path.write_text(
        json.dumps(global_mcp, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  [OK]    ~/.mcp.json mcpServers['{PLUGIN_NAME}']")


# ---------------------------------------------------------------------------
# Step 3: Register hooks in settings.json
# ---------------------------------------------------------------------------

def install_hook_events(settings: dict) -> None:
    """Merge our hook events into settings['hooks'] without removing others."""
    print("\n[STEP 3] Register hooks in settings.json")

    existing_hooks: dict = settings.setdefault("hooks", {})

    for event in HOOK_EVENTS:
        new_groups = _build_hook_groups(event)

        if event not in existing_hooks:
            existing_hooks[event] = new_groups
            print(f"  [ADD]   {event}")
            continue

        # Remove stale entries from this plugin, keep everything else.
        groups = existing_hooks[event]
        foreign_groups = [
            g for g in groups
            if not any(_is_our_hook(h.get("command", "")) for h in g.get("hooks", []))
        ]
        existing_hooks[event] = foreign_groups + new_groups
        print(f"  [MERGE] {event}")

    print(f"  [OK]    {len(HOOK_EVENTS)} event(s) configured")


# ---------------------------------------------------------------------------
# Step 4: Apply recommended settings
# ---------------------------------------------------------------------------

def install_recommended_settings(settings: dict) -> None:
    """Set recommended settings for AI Team OS (idempotent)."""
    print("\n[STEP 4] Recommended settings")

    # env: enable Agent Teams (required for core functionality)
    env = settings.setdefault("env", {})
    env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
    print("  [OK]    env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS = 1")

    # effortLevel: high quality output
    settings["effortLevel"] = "high"
    print("  [OK]    effortLevel = high")


# ---------------------------------------------------------------------------
# Step 5: Install agent templates to ~/.claude/agents/
# ---------------------------------------------------------------------------

def install_agent_templates(project_root: Path) -> None:
    """Copy plugin/agents/ templates to ~/.claude/agents/ using the CC subagent_type mapping.

    Already-existing files are overwritten so the installed version stays current
    with the plugin source. Debate roles (sonnet) are copied as-is.
    """
    import shutil

    print("\n[STEP 5] Install agent templates to ~/.claude/agents/")

    agents_dir = Path.home() / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    source_dir = project_root / "plugin" / "agents"
    copied = 0
    missing = 0

    for src_name, dst_name in AGENT_TEMPLATE_MAPPING.items():
        src = source_dir / src_name
        dst = agents_dir / dst_name
        if not src.exists():
            print(f"  [SKIP]  {src_name} not found")
            missing += 1
            continue
        shutil.copy2(src, dst)
        print(f"  [COPY]  {src_name} -> {dst_name}")
        copied += 1

    print(f"  [OK]    {copied} template(s) installed to {agents_dir}")
    if missing:
        print(f"  [WARN]  {missing} source file(s) not found")


# ---------------------------------------------------------------------------
# Full install
# ---------------------------------------------------------------------------

def run_install(project_root: Path) -> int:
    """Execute full install. Returns exit code."""
    print("=" * 55)
    print("  AI Team OS Installer")
    print("=" * 55)

    print(f"  Python: {PYTHON_EXE}")
    print()

    # Step 1: pip install the aiteam package (hooks are now part of the package)
    print("[STEP 1] Install aiteam package")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", "."],
        cwd=str(project_root / "ai-team-os"),
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  [OK]    aiteam package installed (hooks available as python -m aiteam.hooks.*)")
    else:
        print(f"  [WARN]  pip install returned non-zero: {result.stderr.strip()[:200]}")
        print("  Continuing — hooks will work if aiteam is already installed.")

    # Steps 2-4: update settings.json
    settings = _load_settings()
    install_mcp(settings, project_root)
    install_hook_events(settings)
    install_recommended_settings(settings)
    _save_settings(settings)

    # Step 5: agent templates
    install_agent_templates(project_root)

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

    # 1. Hook modules accessible via python -m
    print("\n[1] Hook modules (python -m aiteam.hooks.*)")
    required_hooks = [
        "inject_subagent_context",
        "send_event",
        "workflow_reminder",
        "session_bootstrap",
        "context_monitor",
        "pre_compact_save",
    ]
    for module_name in required_hooks:
        result = subprocess.run(
            [sys.executable, "-c", f"import aiteam.hooks.{module_name}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            print(f"    aiteam.hooks.{module_name}  [OK]")
        else:
            print(f"    aiteam.hooks.{module_name}  [MISSING]")
            issues.append(f"Hook module not importable: aiteam.hooks.{module_name}")

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

    # 6. Recommended settings
    print("\n[6] Recommended settings")
    for key, expected in [
        ("effortLevel", "high"),
    ]:
        actual = settings.get(key)
        if actual == expected:
            print(f"    {key} = {actual}  [OK]")
        else:
            print(f"    {key} = {actual}  [MISMATCH] expected: {expected}")
            issues.append(f"Setting {key} = {actual}, expected {expected}")

    env = settings.get("env", {})
    agent_teams = env.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
    if agent_teams == "1":
        print("    env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS = 1  [OK]")
    else:
        print("    env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS  [MISSING]")
        issues.append("env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS not set (Agent Teams disabled)")

    # 7. Agent templates in ~/.claude/agents/
    print("\n[7] Agent templates in ~/.claude/agents/")
    agents_dir = Path.home() / ".claude" / "agents"
    installed = 0
    for dst_name in AGENT_TEMPLATE_MAPPING.values():
        dst = agents_dir / dst_name
        if dst.exists():
            installed += 1
        else:
            print(f"    [MISSING] {dst_name}")
            issues.append(f"Agent template not installed: {dst_name}")
    if installed == len(AGENT_TEMPLATE_MAPPING):
        print(f"    All {installed} template(s) present  [OK]")
    else:
        print(f"    {installed}/{len(AGENT_TEMPLATE_MAPPING)} template(s) present")

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
    """Remove our hook entries from settings.json."""
    print("=" * 55)
    print("  AI Team OS Uninstaller (config only)")
    print("=" * 55)

    settings = _load_settings()
    changed = False

    # Remove MCP server from settings.json
    mcp_servers: dict = settings.get("mcpServers", {})
    if PLUGIN_NAME in mcp_servers:
        del mcp_servers[PLUGIN_NAME]
        print(f"  [REMOVE] mcpServers['{PLUGIN_NAME}']")
        changed = True
    else:
        print(f"  [SKIP]   '{PLUGIN_NAME}' not in mcpServers")

    # Remove MCP from ~/.mcp.json
    global_mcp_path = Path.home() / ".mcp.json"
    if global_mcp_path.exists():
        try:
            gm = json.loads(global_mcp_path.read_text(encoding="utf-8"))
            if PLUGIN_NAME in gm.get("mcpServers", {}):
                del gm["mcpServers"][PLUGIN_NAME]
                global_mcp_path.write_text(json.dumps(gm, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"  [REMOVE] ~/.mcp.json mcpServers['{PLUGIN_NAME}']")
        except (json.JSONDecodeError, OSError):
            pass

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

    # Remove recommended settings
    for key in ("effortLevel",):
        if key in settings:
            del settings[key]
            print(f"  [REMOVE] {key}")
            changed = True

    env = settings.get("env", {})
    if "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in env:
        del env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"]
        if not env:
            settings.pop("env", None)
        print("  [REMOVE] env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS")
        changed = True

    if changed:
        _save_settings(settings)
        print("  [OK]    settings.json updated")

    # Remove agent templates from ~/.claude/agents/
    agents_dir = Path.home() / ".claude" / "agents"
    removed_agents = 0
    for dst_name in AGENT_TEMPLATE_MAPPING.values():
        dst = agents_dir / dst_name
        if dst.exists():
            dst.unlink()
            print(f"  [REMOVE] ~/.claude/agents/{dst_name}")
            removed_agents += 1
    if removed_agents:
        print(f"  [OK]    {removed_agents} agent template(s) removed")
    else:
        print("  [SKIP]   No agent templates found in ~/.claude/agents/")

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
