#!/usr/bin/env python3
"""AI Team OS uninstaller.

Removes the install surface of one Claude Code config folder (CLAUDE_CONFIG_DIR,
else the default folder; ``--config-dir`` picks another): the API process,
hooks, hook registrations, MCP registration, agent templates, skills, commands,
loop.md, and the ai-team-os pip package. The OS data directory (aiteam.db,
wherever the resolver finds it: ``aiteam paths`` shows it) and the unread hook
audit under ~/.local/state/ai-team-os (or $XDG_STATE_HOME) are kept unless
--purge-data is given; then the path, size and user_version of what would be
deleted are printed first and the deletion has to be confirmed.

Usage:
    python scripts/uninstall.py               # uninstall, keep the data directories
    python scripts/uninstall.py --dry-run     # show what would be removed
    python scripts/uninstall.py --purge-data  # also delete aiteam.db, reports, logs and the audit (asks first)
"""
import argparse
import functools
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if __name__ == "__main__":
    # Stopping the API reuses the MCP autostart's ownership checks; load them
    # from this checkout so they are available even without the package.
    sys.path.insert(0, str(ROOT / "src"))

# The distribution name in pyproject.toml; the import package is aiteam.
DIST_NAME = "ai-team-os"

# The 25 agent templates installed by AI Team OS (mirrors plugin/agents/).
# Keep in sync with plugin/agents/*.md - test_install_assets.py asserts parity.
AGENT_TEMPLATES = [
    "debate-advocate.md", "debate-critic.md",
    "engineering-ai-engineer.md", "engineering-backend-architect.md",
    "engineering-code-reviewer.md", "engineering-database-optimizer.md",
    "engineering-devops-automator.md", "engineering-frontend-developer.md",
    "engineering-git-workflow-master.md", "engineering-mcp-builder.md",
    "engineering-mobile-developer.md", "engineering-rapid-prototyper.md",
    "engineering-security-engineer.md", "engineering-software-architect.md",
    "engineering-sre.md", "management-project-manager.md",
    "management-tech-lead.md", "specialized-workflow-architect.md",
    "support-meeting-facilitator.md", "support-technical-writer.md",
    "team-member.md",
    "testing-api-tester.md", "testing-bug-fixer.md",
    "testing-performance-benchmarker.md", "testing-qa-engineer.md",
]

# The skill directories installed under ~/.claude/skills/ (mirrors plugin/skills/).
# Keep in sync with plugin/skills/ - test_install_assets.py asserts parity.
SKILL_NAMES = [
    "meeting-facilitate",
    "meeting-participate", "os-channel", "os-release", "os-workflow",
]

# The slash-command files installed under ~/.claude/commands/ (mirrors plugin/commands/).
# Keep in sync with plugin/commands/ - test_install_assets.py asserts parity.
COMMAND_FILES = [
    "os-doctor.md", "os-help.md", "os-hooks.md", "os-meeting.md",
    "os-status.md", "os-task.md", "os-up.md", "os-watcher.md",
]

# Commands retired from plugin/commands/ but possibly still present on machines
# that installed an older version. Removed alongside COMMAND_FILES; mirrors
# install.py RETIRED_COMMAND_FILES (test_install_assets.py asserts parity).
RETIRED_COMMAND_FILES = [
    "os-init.md",   # retired 2026-09-14: it wrote aiteam.yaml, which nothing reads
]

HOOK_MARKERS = [
    "ai-team-os", "workflow_reminder", "send_event",
    "session_bootstrap", "inject_subagent_context",
    # Retired hooks (pipeline subsystem, removed 2026-07). Markers are kept so
    # `uninstall` still strips stale registrations left in older settings.json.
    "pipeline_gate", "autopilot_auto_stop",
    "deep_review_link",
    "meeting_ecosystem_writeback",
]


def _is_our_hook(command: str) -> bool:
    """Both spellings of the runtime dir (absolute, ${CLAUDE_CONFIG_DIR:-...}) contain "ai-team-os"."""
    return any(marker in command for marker in HOOK_MARKERS)


@functools.cache
def _installer():
    """install.py of this checkout: the same path section (hook_core) and --config-dir handling."""
    spec = importlib.util.spec_from_file_location("_aiteam_uninstall_installer", ROOT / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _core():
    return _installer()._hook_core()


def _config_dir() -> Path:
    return _core().cc_config_dir()


def _api_port(autostart) -> int | None:
    """The port hooks and the MCP server use: AITEAM_API_URL, then the port file, then 8000.

    None when AITEAM_API_URL names another host: there is no local API to stop.
    """
    url = os.environ.get("AITEAM_API_URL")
    if not url:
        return autostart._get_api_port()
    parts = urlsplit(url)
    if parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        return None
    try:
        return parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


def kill_api_process(dry_run: bool) -> None:
    """Stop the AI Team OS API on its port, and nothing else.

    Same checks as the MCP autostart: only processes listening on the port
    count (clients connected to it do not), the listener must be this user's
    API process, and it gets SIGTERM with a grace period before SIGKILL
    (on Windows both are TerminateProcess). Anything unverifiable is left running.
    """
    print("[STEP 1] Stop API server")
    try:
        from aiteam.diagnostics import flush_diagnostics
        from aiteam.mcp import _autostart
    except ImportError as exc:
        print(f"[WARN]   Cannot load the API ownership checks ({exc}); nothing stopped")
        return
    port = _api_port(_autostart)
    if port is None:
        print("[SKIP]   AITEAM_API_URL points to another host; no local API to stop")
        return
    if _autostart.psutil is None:
        print(f"[WARN]   psutil is not installed, so the process on port {port} cannot be verified; "
              "nothing stopped")
        return
    listeners = _autostart._listener_pids(port)
    if not listeners:
        print(f"[SKIP]   No process listening on port {port}")
        return
    owner = _autostart._api_listener_owner(listeners)
    family = _autostart._pin_api_family(owner, listeners) if owner else None
    if family is None:
        pids = ", ".join(str(pid) for pid in sorted(listeners))
        print(f"[SKIP]   Port {port} is held by PID {pids}, not a verifiable AI Team OS API; left running")
        return
    print(f"[STOP]   AI Team OS API PID {owner} on port {port}")
    if dry_run:
        return
    _autostart._terminate_api_family(family, reason="uninstall", port=port)
    # Write the termination record now, before a --purge-data removal of its directory.
    flush_diagnostics()
    remaining = _autostart._listener_pids(port)
    if remaining:
        pids = ", ".join(str(pid) for pid in sorted(remaining))
        print(f"[WARN]   Port {port} is still held by PID {pids}")
    else:
        print("[OK]     API stopped")


def remove_hooks_dir(dry_run: bool) -> None:
    """Remove the config folder's hooks/ai-team-os/ (the copies and their .chain-owner.json)."""
    print("\n[STEP 2] Remove hook scripts")
    hooks_dir = _core().cc_main_chain_dir()
    if hooks_dir.exists():
        print(f"[REMOVE] {hooks_dir}")
        if not dry_run:
            shutil.rmtree(hooks_dir)
    else:
        print(f"[SKIP]   {hooks_dir} (not found)")


def remove_hooks_from_settings(dry_run: bool) -> None:
    """Strip our hook entries from the config folder's settings.json."""
    print("\n[STEP 3] Clean settings.json")
    settings_path = _config_dir() / "settings.json"
    if not settings_path.exists():
        print(f"[SKIP]   {settings_path} (not found)")
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
    """Remove 'ai-team-os' from the mcpServers of the config folder's global config file."""
    print("\n[STEP 4] Remove MCP registration")
    claude_json_path = _core().cc_global_config()
    if not claude_json_path.exists():
        print(f"[SKIP]   {claude_json_path} (not found)")
        return

    try:
        data = json.loads(claude_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    mcp_servers: dict = data.get("mcpServers", {})
    if "ai-team-os" in mcp_servers:
        print(f"[REMOVE] 'ai-team-os' from {claude_json_path} mcpServers")
        if not dry_run:
            del mcp_servers["ai-team-os"]
            claude_json_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8",
            )
    else:
        print("[SKIP]   Not found in mcpServers")


def remove_agent_templates(dry_run: bool) -> None:
    """Delete our agent templates from the config folder's agents/."""
    print("\n[STEP 5] Remove agent templates")
    agents_dir = _config_dir() / "agents"
    if not agents_dir.exists():
        print(f"[SKIP]   {agents_dir}/ (not found)")
        return

    removed = 0
    for name in AGENT_TEMPLATES:
        path = agents_dir / name
        if path.exists():
            if not dry_run:
                path.unlink()
            removed += 1
    print(f"[REMOVE] {removed} agent template(s)")


def remove_skills(dry_run: bool) -> None:
    """Delete our skill directories from the config folder's skills/.

    Only removes the specific skill dirs we ship (SKILL_NAMES), never touching
    unrelated user or third-party skills that live in the same directory.
    """
    print("\n[STEP 5b] Remove skills")
    skills_dir = _config_dir() / "skills"
    if not skills_dir.exists():
        print(f"[SKIP]   {skills_dir}/ (not found)")
        return

    removed = 0
    for name in SKILL_NAMES:
        path = skills_dir / name
        if path.exists():
            if not dry_run:
                shutil.rmtree(path, ignore_errors=True)
            removed += 1
    print(f"[REMOVE] {removed} skill(s)")


def remove_commands(dry_run: bool) -> None:
    """Delete our slash-command files from the config folder's commands/."""
    print("\n[STEP 5c] Remove commands")
    commands_dir = _config_dir() / "commands"
    if not commands_dir.exists():
        print(f"[SKIP]   {commands_dir}/ (not found)")
        return

    removed = 0
    for name in COMMAND_FILES + RETIRED_COMMAND_FILES:
        path = commands_dir / name
        if path.exists():
            if not dry_run:
                path.unlink()
            removed += 1
    print(f"[REMOVE] {removed} command(s)")


def remove_loop_md(dry_run: bool) -> None:
    """Delete the config folder's loop.md, but only if it's our template (has the sentinel)."""
    print("\n[STEP 5d] Remove /loop maintenance prompt")
    loop_md = _config_dir() / "loop.md"
    if not loop_md.exists():
        print(f"[SKIP]   {loop_md} (not found)")
        return
    try:
        text = loop_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(f"[SKIP]   {loop_md} (unreadable)")
        return
    if _installer().LOOP_TEMPLATE_SENTINEL not in text:
        print(f"[SKIP]   {loop_md} looks user-customized - left untouched")
        return
    print(f"[REMOVE] {loop_md}")
    if not dry_run:
        loop_md.unlink(missing_ok=True)


def _tree_size(root: Path) -> int:
    """Bytes of every file under *root* (links not followed)."""
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def choose_purge_target(dry_run: bool) -> Path | None:
    """--purge-data: show what would be deleted, then ask; the directory to delete, or None to keep it.

    The data directory is the one the resolver finds (docs/config-dir-compat-design.md
    §10 item 5), not a fixed path. Its path, size and the database's user_version
    are printed before anything is asked: a deleted database cannot be rebuilt.
    Two databases (conflict) or none found: nothing is deleted.
    """
    core = _core()
    resolution = core.resolve_data_dir()
    print("\n[PURGE]  --purge-data deletes the OS data directory: every project, task, memory and report")
    if resolution.mode == "missing":
        print("[SKIP]   No OS data directory found; nothing to delete")
        return None
    if resolution.mode == "conflict":
        for candidate in resolution.evidence:
            if candidate.state == "database":
                print(f"[KEEP]   {candidate.path}: {candidate.db_size} bytes, user_version {candidate.user_version}")
        print("[KEEP]   Two databases were found; keep one first (aiteam paths compares them). Nothing deleted")
        return None
    path = resolution.path
    facts = core.inspect_data_dir("purge", path)
    real = os.path.realpath(path)
    shown = f"{path} -> {real}" if real != os.path.abspath(path) else str(path)
    if facts.state != "database":
        # The directory comes from the resolver, so from AITEAM_DATA_DIR when that is
        # set: without an OS database in it, it may be any folder of the user's.
        print(f"[KEEP]   {shown} ({resolution.mode}) is not an OS data directory: no readable aiteam.db "
              f"({facts.state}). Nothing deleted")
        return None
    print(f"[PURGE]  {shown} ({resolution.mode})")
    print(f"         {_tree_size(Path(real))} bytes in all; aiteam.db user_version {facts.user_version}")
    if dry_run:
        print("[DRY RUN] would ask for confirmation before deleting it")
        return path
    try:
        answer = input('Type "delete" to delete it for good; anything else keeps it: ')
    except EOFError:
        answer = ""
    if answer.strip().lower() != "delete":
        print("[KEEP]   Not confirmed; the data directory and the unread hook audit are kept")
        return None
    return path


def _purge(path: Path, dry_run: bool) -> None:
    """Delete the data directory (the one a link names, and the link) and what a migration left pointing at it."""
    real = Path(os.path.realpath(path))
    print(f"[REMOVE] {real} (--purge-data: aiteam.db, reports and logs)")
    from aiteam.data_dir_migration import marker_kind

    legacy = Path(os.path.expanduser(_core().LEGACY_DATA_DIR))
    leftover, _problem = marker_kind(legacy, real)
    if leftover:
        print(f"[REMOVE] {legacy} ({leftover} left by the data directory migration)")
    for left in sorted(legacy.parent.glob(legacy.name + ".migrated-*")) if legacy.parent.is_dir() else []:
        print(f"[KEEP]   {left} (an older copy a migration kept; delete it by hand when no longer needed)")
    if dry_run:
        return
    shutil.rmtree(real, ignore_errors=True)
    if path.is_symlink():
        path.unlink()
    if leftover == "symlink":
        legacy.unlink()
    elif leftover == "stub":
        shutil.rmtree(legacy, ignore_errors=True)


def remove_data_dirs(dry_run: bool, purge: Path | None) -> None:
    """Remove runtime state; the data directory itself only when a purge was confirmed (*purge*)."""
    print("\n[STEP 6] Remove data directories")
    try:
        data_dir = _core().os_data_dir()
    except OSError:
        data_dir = None  # nothing found: nothing of ours to remove there

    # The unread hooks' invocation audit (hook_core.InvocationAudit), one
    # subdirectory per host; it follows XDG_STATE_HOME like the hooks do.
    audit_dir = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "ai-team-os"

    if purge is not None:
        _purge(purge, dry_run)
        if audit_dir.exists():
            print(f"[REMOVE] {audit_dir} (--purge-data: unread hook invocation audit)")
            if not dry_run:
                shutil.rmtree(audit_dir, ignore_errors=True)
        return
    if data_dir is not None:
        # Supervisor state and the install path marker go with the install; the data stays.
        for name in ("supervisor-state.json", "install_path.txt"):
            stale = data_dir / name
            if stale.exists():
                print(f"[REMOVE] {stale}")
                if not dry_run:
                    stale.unlink(missing_ok=True)
        print(f"[KEEP]   {data_dir} (aiteam.db and logs; --purge-data deletes it)")
    if audit_dir.exists():
        print(f"[KEEP]   {audit_dir} (unread hook invocation audit; --purge-data deletes it)")

    # Plugin data (venv)
    plugins_data = _config_dir() / "plugins" / "data"
    if plugins_data.exists():
        for d in plugins_data.iterdir():
            if "ai-team-os" in d.name:
                print(f"[REMOVE] {d}")
                if not dry_run:
                    shutil.rmtree(d, ignore_errors=True)


def _installed_version() -> str | None:
    """The ai-team-os version this interpreter has, read by a fresh process."""
    probe = subprocess.run(
        [sys.executable, "-c", "import importlib.metadata as m, sys; print(m.version(sys.argv[1]))", DIST_NAME],
        capture_output=True, text=True,
    )
    return probe.stdout.strip() if probe.returncode == 0 else None


def pip_uninstall(dry_run: bool) -> None:
    """Uninstall the ai-team-os distribution and check that it is gone."""
    print("\n[STEP 7] Uninstall pip package")
    version = _installed_version()
    if version is None:
        print(f"[SKIP]   {DIST_NAME} is not installed for {sys.executable}")
        return
    print(f"[REMOVE] pip uninstall {DIST_NAME} ({version})")
    if dry_run:
        return
    result = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", DIST_NAME, "-y"],
        capture_output=True, text=True,
    )
    left = _installed_version()
    if left is None:
        print(f"[OK]     {DIST_NAME} uninstalled")
    else:
        output = (result.stdout + result.stderr).strip()
        print(f"[WARN]   {DIST_NAME} {left} is still installed (pip exit {result.returncode}): {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Uninstall AI Team OS")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument(
        "--config-dir", metavar="PATH",
        help="Claude Code config folder to uninstall from (default: CLAUDE_CONFIG_DIR, else the default folder)",
    )
    data = parser.add_mutually_exclusive_group()
    data.add_argument(
        "--purge-data", action="store_true",
        help="Also delete the OS data directory (aiteam paths shows where): aiteam.db with every project, "
             "task, memory and report, and the records and logs kept beside it (notice records, "
             "compact-events.jsonl, installs.json, debug logs); and the unread hook audit under "
             "~/.local/state/ai-team-os. Refused when that directory holds no OS database. "
             "Its path, size and user_version are shown and the deletion must be confirmed. "
             "This cannot be undone; back the directory up first",
    )
    data.add_argument(
        "--keep-data", action="store_true",
        help="Keep the data directory. This is the default now; the flag is still accepted",
    )
    args = parser.parse_args()

    _installer().apply_config_dir(args.config_dir)
    print("=" * 50)
    print(f"  AI Team OS Uninstaller {'[DRY RUN]' if args.dry_run else ''}")
    print("=" * 50)
    _installer().print_install_target()
    # Asked before anything is removed, so a "no" leaves an uninstall that keeps the data.
    purge = choose_purge_target(args.dry_run) if args.purge_data else None

    kill_api_process(args.dry_run)
    remove_hooks_dir(args.dry_run)
    remove_hooks_from_settings(args.dry_run)
    remove_mcp_from_claude_json(args.dry_run)
    remove_agent_templates(args.dry_run)
    remove_skills(args.dry_run)
    remove_commands(args.dry_run)
    remove_loop_md(args.dry_run)
    remove_data_dirs(args.dry_run, purge)
    pip_uninstall(args.dry_run)

    print()
    print("=" * 50)
    if args.dry_run:
        print("  Dry run complete - no changes made.")
    else:
        print("  Uninstall complete.")
        print("  *** Restart Claude Code to stop active hooks ***")
    print("=" * 50)


if __name__ == "__main__":
    main()
