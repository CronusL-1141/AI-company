#!/usr/bin/env python3
"""AI Team OS uninstaller.

Removes hooks, agent templates, MCP registration, API process,
data directories, and the aiteam package.

Usage:
    python scripts/uninstall.py            # full uninstall
    python scripts/uninstall.py --dry-run  # show what would be removed
    python scripts/uninstall.py --keep-data  # keep project databases
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# The 22 agent templates installed by AI Team OS
AGENT_TEMPLATES = [
    "engineering-ai-engineer.md", "engineering-backend-architect.md",
    "engineering-code-reviewer.md", "engineering-database-optimizer.md",
    "engineering-devops-automator.md", "engineering-frontend-developer.md",
    "engineering-git-workflow-master.md", "engineering-mcp-builder.md",
    "engineering-mobile-developer.md", "engineering-rapid-prototyper.md",
    "engineering-security-engineer.md", "engineering-software-architect.md",
    "engineering-sre.md", "management-project-manager.md",
    "management-tech-lead.md", "specialized-workflow-architect.md",
    "support-meeting-facilitator.md", "support-technical-writer.md",
    "testing-api-tester.md", "testing-bug-fixer.md",
    "testing-performance-benchmarker.md", "testing-qa-engineer.md",
]

HOOK_MARKERS = [
    "ai-team-os", "workflow_reminder", "send_event",
    "session_bootstrap", "inject_subagent_context",
]


def _is_our_hook(command: str) -> bool:
    return any(marker in command for marker in HOOK_MARKERS)


def kill_api_process(dry_run: bool) -> None:
    """Kill the API server process on port 8000."""
    print("[STEP 1] Stop API server (port 8000)")
    if sys.platform == "win32":
        # Find PID on port 8000
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess"],
                capture_output=True, text=True, timeout=10,
            )
            pid = result.stdout.strip()
            if pid and pid != "0":
                print(f"[KILL]   API process PID {pid}")
                if not dry_run:
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=10)
            else:
                print("[SKIP]   No process on port 8000")
        except Exception as e:
            print(f"[WARN]   Could not check port 8000: {e}")
    else:
        try:
            result = subprocess.run(
                ["lsof", "-ti", ":8000"], capture_output=True, text=True, timeout=10,
            )
            pids = result.stdout.strip().split()
            for pid in pids:
                print(f"[KILL]   API process PID {pid}")
                if not dry_run:
                    subprocess.run(["kill", "-9", pid], capture_output=True, timeout=10)
            if not pids:
                print("[SKIP]   No process on port 8000")
        except Exception:
            print("[SKIP]   Could not check port 8000")


def remove_hooks_dir(dry_run: bool) -> None:
    """Remove ~/.claude/hooks/ai-team-os/."""
    print("\n[STEP 2] Remove hook scripts")
    hooks_dir = Path.home() / ".claude" / "hooks" / "ai-team-os"
    if hooks_dir.exists():
        print(f"[REMOVE] {hooks_dir}")
        if not dry_run:
            shutil.rmtree(hooks_dir)
    else:
        print(f"[SKIP]   {hooks_dir} (not found)")


def remove_hooks_from_settings(dry_run: bool) -> None:
    """Strip our hook entries from ~/.claude/settings.json."""
    print("\n[STEP 3] Clean settings.json")
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        print("[SKIP]   ~/.claude/settings.json (not found)")
        return

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN]   Could not parse settings.json: {exc}")
        return

    hooks: dict = settings.get("hooks", {})
    removed_count = 0
    events_to_delete: list[str] = []

    for event, groups in list(hooks.items()):
        new_groups: list[dict] = []
        for group in groups:
            new_hook_list = [
                h for h in group.get("hooks", [])
                if not _is_our_hook(h.get("command", ""))
            ]
            removed_count += len(group.get("hooks", [])) - len(new_hook_list)
            if new_hook_list:
                new_groups.append({**group, "hooks": new_hook_list})
        if new_groups:
            hooks[event] = new_groups
        else:
            events_to_delete.append(event)

    for event in events_to_delete:
        del hooks[event]

    # Clean empty hooks dict
    if not hooks:
        settings.pop("hooks", None)

    print(f"[REMOVE] {removed_count} hook(s) from settings.json")
    if not dry_run:
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8",
        )


def remove_mcp_from_claude_json(dry_run: bool) -> None:
    """Remove 'ai-team-os' from ~/.claude.json mcpServers."""
    print("\n[STEP 4] Remove MCP registration")
    claude_json_path = Path.home() / ".claude.json"
    if not claude_json_path.exists():
        print("[SKIP]   ~/.claude.json (not found)")
        return

    try:
        data = json.loads(claude_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    mcp_servers: dict = data.get("mcpServers", {})
    if "ai-team-os" in mcp_servers:
        print("[REMOVE] 'ai-team-os' from ~/.claude.json mcpServers")
        if not dry_run:
            del mcp_servers["ai-team-os"]
            claude_json_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
            )
    else:
        print("[SKIP]   Not found in mcpServers")


def remove_agent_templates(dry_run: bool) -> None:
    """Delete our agent templates from ~/.claude/agents/."""
    print("\n[STEP 5] Remove agent templates")
    agents_dir = Path.home() / ".claude" / "agents"
    if not agents_dir.exists():
        print("[SKIP]   ~/.claude/agents/ (not found)")
        return

    removed = 0
    for name in AGENT_TEMPLATES:
        path = agents_dir / name
        if path.exists():
            if not dry_run:
                path.unlink()
            removed += 1
    print(f"[REMOVE] {removed} agent template(s)")


def remove_data_dirs(dry_run: bool, keep_data: bool) -> None:
    """Remove data directories."""
    print("\n[STEP 6] Remove data directories")

    # Supervisor state
    state_dir = Path.home() / ".claude" / "data" / "ai-team-os"
    state_file = state_dir / "supervisor-state.json"
    if state_file.exists():
        print(f"[REMOVE] {state_file}")
        if not dry_run:
            state_file.unlink()

    if keep_data:
        print("[KEEP]   Project databases (--keep-data flag)")
    else:
        if state_dir.exists():
            print(f"[REMOVE] {state_dir}")
            if not dry_run:
                shutil.rmtree(state_dir, ignore_errors=True)

    # Plugin data (venv)
    plugins_data = Path.home() / ".claude" / "plugins" / "data"
    if plugins_data.exists():
        for d in plugins_data.iterdir():
            if "ai-team-os" in d.name:
                print(f"[REMOVE] {d}")
                if not dry_run:
                    shutil.rmtree(d, ignore_errors=True)

    # Install path marker
    install_marker = state_dir / "install_path.txt"
    if install_marker.exists() and not dry_run:
        install_marker.unlink(missing_ok=True)


def pip_uninstall(dry_run: bool) -> None:
    """Uninstall aiteam pip package."""
    print("\n[STEP 7] Uninstall pip package")
    print("[REMOVE] pip uninstall aiteam")
    if not dry_run:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "aiteam", "-y"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print("[OK]     aiteam uninstalled")
        else:
            output = (result.stdout + result.stderr).strip()
            if "not installed" in output.lower():
                print("[SKIP]   aiteam was not installed")
            else:
                print(f"[WARN]   {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Uninstall AI Team OS")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--keep-data", action="store_true", help="Keep project databases")
    args = parser.parse_args()

    print("=" * 50)
    print(f"  AI Team OS Uninstaller {'[DRY RUN]' if args.dry_run else ''}")
    print("=" * 50)

    kill_api_process(args.dry_run)
    remove_hooks_dir(args.dry_run)
    remove_hooks_from_settings(args.dry_run)
    remove_mcp_from_claude_json(args.dry_run)
    remove_agent_templates(args.dry_run)
    remove_data_dirs(args.dry_run, args.keep_data)
    pip_uninstall(args.dry_run)

    print()
    print("=" * 50)
    if args.dry_run:
        print("  Dry run complete — no changes made.")
    else:
        print("  Uninstall complete.")
        print("  *** Restart Claude Code to stop active hooks ***")
    print("=" * 50)


if __name__ == "__main__":
    main()
