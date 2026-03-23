#!/usr/bin/env python3
"""AI Team OS uninstaller.

Removes hooks, agent templates, global MCP registration, and the aiteam package.
Data directory (~/.claude/data/ai-team-os/) must be removed manually.

Usage:
    python scripts/uninstall.py            # full uninstall
    python scripts/uninstall.py --dry-run  # show what would be removed
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# The 22 agent templates installed by AI Team OS
AGENT_TEMPLATES = [
    "engineering-ai-engineer.md",
    "engineering-backend-architect.md",
    "engineering-code-reviewer.md",
    "engineering-database-optimizer.md",
    "engineering-devops-automator.md",
    "engineering-frontend-developer.md",
    "engineering-git-workflow-master.md",
    "engineering-mcp-builder.md",
    "engineering-mobile-developer.md",
    "engineering-rapid-prototyper.md",
    "engineering-security-engineer.md",
    "engineering-software-architect.md",
    "engineering-sre.md",
    "management-project-manager.md",
    "management-tech-lead.md",
    "specialized-workflow-architect.md",
    "support-meeting-facilitator.md",
    "support-technical-writer.md",
    "testing-api-tester.md",
    "testing-bug-fixer.md",
    "testing-performance-benchmarker.md",
    "testing-qa-engineer.md",
]

# Substrings that identify our hooks inside settings.json
HOOK_MARKERS = [
    "ai-team-os",
    "workflow_reminder",
    "send_event",
    "session_bootstrap",
    "inject_subagent_context",
]


def _is_our_hook(command: str) -> bool:
    return any(marker in command for marker in HOOK_MARKERS)


def remove_hooks_dir(dry_run: bool) -> None:
    """Remove ~/.claude/hooks/ai-team-os/ entirely."""
    hooks_dir = Path.home() / ".claude" / "hooks" / "ai-team-os"
    if hooks_dir.exists():
        print(f"[REMOVE] {hooks_dir}")
        if not dry_run:
            shutil.rmtree(hooks_dir)
    else:
        print(f"[SKIP]   {hooks_dir} (not found)")


def remove_hooks_from_settings(dry_run: bool) -> None:
    """Strip our hook entries from ~/.claude/settings.json."""
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
    if not hooks:
        print("[SKIP]   No hooks section in settings.json")
        return

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
            # Drop the group entirely if it has no hooks left
        if new_groups:
            hooks[event] = new_groups
        else:
            events_to_delete.append(event)

    for event in events_to_delete:
        del hooks[event]

    if removed_count == 0:
        print("[SKIP]   No AI Team OS hooks found in settings.json")
        return

    print(f"[REMOVE] {removed_count} hook(s) from ~/.claude/settings.json "
          f"(events cleaned: {', '.join(events_to_delete) or 'none fully emptied'})")
    if not dry_run:
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def remove_mcp_from_claude_json(dry_run: bool) -> None:
    """Remove 'ai-team-os' from mcpServers in ~/.claude.json."""
    claude_json_path = Path.home() / ".claude.json"
    if not claude_json_path.exists():
        print("[SKIP]   ~/.claude.json (not found)")
        return

    try:
        data = json.loads(claude_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN]   Could not parse ~/.claude.json: {exc}")
        return

    mcp_servers: dict = data.get("mcpServers", {})
    if "ai-team-os" not in mcp_servers:
        print("[SKIP]   'ai-team-os' not found in ~/.claude.json mcpServers")
        return

    print("[REMOVE] 'ai-team-os' from ~/.claude.json mcpServers")
    if not dry_run:
        del mcp_servers["ai-team-os"]
        claude_json_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def remove_agent_templates(dry_run: bool) -> None:
    """Delete our 22 agent template files from ~/.claude/agents/."""
    agents_dir = Path.home() / ".claude" / "agents"
    if not agents_dir.exists():
        print("[SKIP]   ~/.claude/agents/ (not found)")
        return

    removed = 0
    missing = 0
    for name in AGENT_TEMPLATES:
        path = agents_dir / name
        if path.exists():
            print(f"[REMOVE] {path}")
            if not dry_run:
                path.unlink()
            removed += 1
        else:
            missing += 1

    print(f"[INFO]   Agent templates: {removed} removed, {missing} not found")


def pip_uninstall_aiteam(dry_run: bool) -> None:
    """Run pip uninstall aiteam -y."""
    print("[REMOVE] pip uninstall aiteam -y")
    if not dry_run:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "aiteam", "-y"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("[OK]     aiteam package uninstalled")
        else:
            output = (result.stdout + result.stderr).strip()
            if "not installed" in output.lower():
                print("[SKIP]   aiteam package was not installed")
            else:
                print(f"[WARN]   pip uninstall returned non-zero: {output}")


def print_data_reminder() -> None:
    data_dir = Path.home() / ".claude" / "data" / "ai-team-os"
    print()
    print("Manual cleanup required:")
    print(f"  Remove data directory: {data_dir}")
    print("  (contains databases and logs — delete manually when safe)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Uninstall AI Team OS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/uninstall.py            # full uninstall\n"
            "  python scripts/uninstall.py --dry-run  # preview only\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without making any changes",
    )
    args = parser.parse_args()

    print("=" * 50)
    if args.dry_run:
        print("  AI Team OS Uninstaller [DRY RUN]")
    else:
        print("  AI Team OS Uninstaller")
    print("=" * 50)
    print()

    remove_hooks_dir(args.dry_run)
    remove_hooks_from_settings(args.dry_run)
    remove_mcp_from_claude_json(args.dry_run)
    remove_agent_templates(args.dry_run)
    pip_uninstall_aiteam(args.dry_run)
    print_data_reminder()

    print()
    print("=" * 50)
    if args.dry_run:
        print("  Dry run complete — no changes made.")
    else:
        print("  Uninstall complete.")
        print("  Restart Claude Code to apply changes.")
    print("=" * 50)


if __name__ == "__main__":
    main()
