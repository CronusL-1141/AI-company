#!/usr/bin/env python3
"""AI Team OS one-click installer."""
import json
import shutil
import subprocess
import sys
from pathlib import Path


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


def _hook_command_exists(hooks_list: list, command_fragment: str) -> bool:
    """Check if a hook command containing the fragment already exists in list."""
    for group in hooks_list:
        for hook in group.get("hooks", []):
            if command_fragment in hook.get("command", ""):
                return True
    return False


def register_hooks(project_root: Path) -> None:
    """Copy hook scripts to ~/.claude/hooks/ai-team-os/ and register in settings.json."""
    src_hooks_dir = project_root / "plugin" / "hooks"
    # Install hooks to a fixed location independent of clone directory
    installed_hooks_dir = Path.home() / ".claude" / "hooks" / "ai-team-os"
    installed_hooks_dir.mkdir(parents=True, exist_ok=True)

    # Copy hook scripts to ~/.claude/hooks/ai-team-os/
    hook_files = ["send_event.py", "workflow_reminder.py", "session_bootstrap.py",
                  "inject_subagent_context.py"]
    for fname in hook_files:
        src = src_hooks_dir / fname
        dst = installed_hooks_dir / fname
        if src.exists():
            shutil.copy2(src, dst)
    print(f"[OK] Hook scripts copied to {installed_hooks_dir}")

    # Use installed location (not clone dir) for hook commands
    hooks_dir = installed_hooks_dir
    settings_path = Path.home() / ".claude" / "settings.json"

    py = sys.executable  # use same interpreter that ran install.py

    # Quote path for Windows (handles spaces in path); use forward slashes for portability
    def q(p: Path) -> str:
        return f'"{str(p).replace(chr(92), "/")}"'

    se = hooks_dir / "send_event.py"
    wf = hooks_dir / "workflow_reminder.py"
    sb = hooks_dir / "session_bootstrap.py"
    isc = hooks_dir / "inject_subagent_context.py"

    # Build hook entry: returns (fragment, command, timeout) or None if script missing.
    # fragment is a unique substring to detect duplicate hooks in existing commands.
    def py_hook(script: Path, arg: str, timeout: int) -> tuple[str, str, int] | None:
        if not script.exists():
            return None
        cmd = f'{q(py)} {q(script)}' + (f' {arg}' if arg else '')
        # Include arg in fragment to distinguish e.g. "send_event.py PreToolUse" vs "PostToolUse"
        fragment = f'{script.name}" {arg}' if arg else script.name
        return (fragment, cmd, timeout)

    def build_entries(*hooks) -> list[tuple[str, str, int]]:
        return [h for h in hooks if h is not None]

    # Hooks to register: event -> (matcher, list of (fragment, full_command, timeout))
    # PreToolUse / PostToolUse use matcher "*" to match all tools; others use ""
    desired: dict[str, tuple[str, list[tuple[str, str, int]]]] = {
        "PreToolUse": ("*", build_entries(
            py_hook(wf, "PreToolUse", 3),
            py_hook(se, "PreToolUse", 3),
        )),
        "PostToolUse": ("*", build_entries(
            py_hook(wf, "PostToolUse", 3),
            py_hook(se, "PostToolUse", 3),
        )),
        "SessionStart": ("", build_entries(
            py_hook(sb, "SessionStart", 5),
            py_hook(se, "SessionStart", 5),
        )),
        "SubagentStart": ("", build_entries(
            py_hook(isc, "SubagentStart", 5),
            py_hook(se, "SubagentStart", 5),
        )),
        "SubagentStop": ("", build_entries(
            py_hook(se, "SubagentStop", 5),
        )),
        "SessionEnd": ("", build_entries(
            py_hook(se, "SessionEnd", 5),
        )),
        "Stop": ("", build_entries(
            py_hook(se, "Stop", 5),
        )),
    }

    # Load or create settings
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    existing_hooks: dict = settings.setdefault("hooks", {})
    added = 0

    for event, (matcher, entries) in desired.items():
        event_list: list = existing_hooks.setdefault(event, [])

        # Find or create the hook group for the correct matcher
        target_group = None
        for group in event_list:
            if group.get("matcher", "") == matcher:
                target_group = group
                break
        if target_group is None:
            target_group = {"matcher": matcher, "hooks": []}
            event_list.append(target_group)

        for fragment, command, timeout in entries:
            if not _hook_command_exists(event_list, fragment):
                target_group["hooks"].append({
                    "type": "command",
                    "command": command,
                    "timeout": timeout,
                })
                added += 1

    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if added > 0:
        print(f"[OK] Registered {added} new hook(s) into settings.json")
    else:
        print("[OK] All hooks already registered, skipped")


def copy_agent_templates(project_root: Path, overwrite: bool = False) -> None:
    """Copy .claude/agents/*.md to ~/.claude/agents/.

    Args:
        project_root: Root directory of the ai-team-os project.
        overwrite: When True (update mode), overwrite existing templates.
                   When False (fresh install), skip existing files.
    """
    src_agents = project_root / ".claude" / "agents"
    dst_agents = Path.home() / ".claude" / "agents"

    if not src_agents.exists():
        print("[SKIP] No agent templates found in .claude/agents/")
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


def register_global_mcp(project_root: Path) -> None:
    """Merge ai-team-os MCP server into ~/.claude/settings.json mcpServers.

    This registers the MCP server globally so it is available in ALL projects,
    not just when Claude Code is opened from the project directory.

    Falls back to generating a project-level .mcp.json if settings.json cannot
    be written (e.g. permission error).
    """
    settings_path = Path.home() / ".claude" / "settings.json"

    # Load existing settings
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            settings = {}
    else:
        settings = {}

    mcp_servers: dict = settings.setdefault("mcpServers", {})

    if "ai-team-os" in mcp_servers:
        print("[OK] Global MCP server 'ai-team-os' already registered in settings.json, skipped")
        return

    # Register without cwd — works after `pip install` because `aiteam` is on PATH/PYTHONPATH.
    # Use sys.executable so the same interpreter that ran install.py is used.
    mcp_servers["ai-team-os"] = {
        "command": sys.executable,
        "args": ["-m", "aiteam.mcp.server"],
        "env": {
            "AITEAM_API_URL": "http://localhost:8000",
        },
    }

    try:
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print("[OK] Registered global MCP server 'ai-team-os' into ~/.claude/settings.json")
    except OSError as e:
        print(f"[WARN] Could not write to settings.json: {e}")
        print("[WARN] Falling back to project-level .mcp.json")
        _write_project_mcp_json(project_root)


def _write_project_mcp_json(project_root: Path) -> None:
    """Write project-level .mcp.json as fallback when global registration fails."""
    mcp_json = project_root / ".mcp.json"
    config = {
        "mcpServers": {
            "ai-team-os": {
                "command": "python",
                "args": ["-m", "aiteam.mcp.server"],
                "cwd": str(project_root).replace("\\", "/"),
                "env": {
                    "AITEAM_API_URL": "http://localhost:8000",
                },
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

    settings_path = Path.home() / ".claude" / "settings.json"
    has_hooks = False
    has_global_mcp = False
    if settings_path.exists():
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
            has_hooks = bool(cfg.get("hooks"))
            has_global_mcp = "ai-team-os" in cfg.get("mcpServers", {})
        except Exception:
            pass

    # Project-level .mcp.json is present if global registration failed (fallback)
    has_project_mcp = (project_root / ".mcp.json").exists()

    checks = [
        ("Global MCP in ~/.claude/settings.json", has_global_mcp),
        ("Project .mcp.json (fallback)", has_project_mcp),
        ("~/.claude/agents/ templates", has_templates),
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


def _check_package(pkg: str) -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", pkg],
            capture_output=True, text=True,
        )
        return result.returncode == 0
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
    if sys.version_info < (3, 11):
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
