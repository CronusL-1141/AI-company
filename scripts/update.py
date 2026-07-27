#!/usr/bin/env python3
"""AI Team OS updater script.

Usage:
    python scripts/update.py            # full update
    python scripts/update.py --check    # only check for updates, do not apply
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_silent(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a subprocess silently; return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout = result.stdout.strip() if result.stdout else ""
        stderr = result.stderr.strip() if result.stderr else ""
        return result.returncode, stdout, stderr
    except FileNotFoundError:
        return 1, "", f"command not found: {args[0]}"
    except Exception as exc:
        return 1, "", str(exc)


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

def _local_version(project_root: Path) -> str:
    """Read __version__ from src/aiteam/__init__.py."""
    init = project_root / "src" / "aiteam" / "__init__.py"
    try:
        for line in init.read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=")[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


def _remote_version(project_root: Path) -> str | None:
    """Read __version__ from the remote git HEAD (origin/main or origin/master).

    Returns None if git is not available or the repo has no remote.
    """
    code, out, _ = _run_silent(["git", "remote"], cwd=str(project_root))
    if code != 0 or not out:
        return None

    # Fetch without updating local branches (quiet)
    _run_silent(["git", "fetch", "--quiet", "origin"], cwd=str(project_root))

    # Try to cat the __init__.py from the remote default branch
    for branch in ("origin/main", "origin/master"):
        code, out, _ = _run_silent(
            ["git", "show", f"{branch}:src/aiteam/__init__.py"],
            cwd=str(project_root),
        )
        if code == 0 and out:
            for line in out.splitlines():
                if line.startswith("__version__"):
                    return line.split("=")[1].strip().strip('"').strip("'")

    return None


def _git_local_commit(project_root: Path) -> str:
    code, out, _ = _run_silent(["git", "rev-parse", "--short", "HEAD"], cwd=str(project_root))
    return out if code == 0 else "unknown"


def _git_remote_commit(project_root: Path) -> str:
    for branch in ("origin/main", "origin/master"):
        code, out, _ = _run_silent(
            ["git", "rev-parse", "--short", branch],
            cwd=str(project_root),
        )
        if code == 0 and out:
            return out
    return "unknown"


def _is_git_repo(project_root: Path) -> bool:
    code, _, _ = _run_silent(["git", "rev-parse", "--git-dir"], cwd=str(project_root))
    return code == 0


# ---------------------------------------------------------------------------
# Update steps
# ---------------------------------------------------------------------------

def _git_pull(project_root: Path) -> bool:
    """Pull latest commits; return True if there were changes."""
    code, out, err = _run_silent(["git", "pull", "--ff-only"], cwd=str(project_root))
    if code != 0:
        print(f"[WARN] git pull failed: {err}")
        return False
    changed = "Already up to date" not in out
    if changed:
        print(f"[OK] git pull: {out}")
    else:
        print("[OK] git pull: already up to date")
    return changed


def _pip_install(project_root: Path) -> None:
    """Re-install the package in editable mode — best effort, never aborts the update.

    `pip install -e .` is refused outright by PEP 668 externally-managed
    interpreters (Homebrew python3, Debian/Ubuntu system python). This step used
    to run under check=True, so on those machines the updater died at step 2/7 and
    hooks / skills / commands / settings.json were never refreshed — the exact
    drift this updater exists to prevent, and it failed silently as "update
    crashed". An editable install already resolves to the working tree, so a
    failure here is usually a no-op; the run continues either way.
    """
    print("[...] Reinstalling Python package (pip install -e .) ...")
    code, out, err = _run_silent(
        [sys.executable, "-m", "pip", "install", "-e", "."], cwd=str(project_root)
    )
    if code == 0:
        print("[OK] Python package updated")
        return

    combined = f"{err}\n{out}"
    print("[WARN] pip install -e . failed — continuing with the rest of the update")
    if "externally-managed-environment" in combined:
        print("       Interpreter is PEP 668 externally managed (Homebrew / system python).")
        print("       Only needed if dependencies changed; then re-run pip manually with")
        print("       --break-system-packages.")
    else:
        tail = [ln for ln in combined.splitlines() if ln.strip()]
        if tail:
            print(f"       {tail[-1][:160]}")

    import importlib.util

    if importlib.util.find_spec("aiteam") is None:
        print("[WARN] The aiteam package is not importable — MCP tools will not load.")
        print("       Fix the install above before restarting Claude Code.")


def _load_install_module(project_root: Path):
    """Import the root install.py as a module (it owns the install surface).

    Returns None when it cannot be loaded; every caller degrades gracefully.
    """
    install_py = project_root / "install.py"
    if not install_py.exists():
        print("[WARN] install.py not found")
        return None

    import importlib.util

    spec = importlib.util.spec_from_file_location("install_module", install_py)
    if spec is None or spec.loader is None:
        print("[WARN] Could not load install.py")
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:
        print(f"[WARN] Failed to exec install.py: {exc}")
        return None
    return module


def _copy_hooks(project_root: Path) -> None:
    """Refresh hook scripts in ~/.claude/hooks/ai-team-os/.

    Delegates to install.py: keeping a second hook-file list here is exactly how
    the updater fell four scripts behind the installer (it refreshed 4 of the 10
    distributed hooks, so updates silently shipped stale copies of the rest).
    """
    install_mod = _load_install_module(project_root)
    if install_mod is None:
        print("[WARN] Skipping hook refresh")
        return
    install_mod.copy_hook_scripts(project_root)


def _copy_agent_templates(project_root: Path) -> None:
    """Overwrite agent templates in ~/.claude/agents/ (force overwrite for update).

    Source is plugin/agents (25-template superset), matching install.py — updating
    from .claude/agents would silently drop 3 agents that a fresh install now ships.
    """
    src_agents = project_root / "plugin" / "agents"
    dst_agents = Path.home() / ".claude" / "agents"

    if not src_agents.exists():
        print("[SKIP] No agent templates found in plugin/agents/")
        return

    dst_agents.mkdir(parents=True, exist_ok=True)
    updated = 0
    for template in src_agents.glob("*.md"):
        dst = dst_agents / template.name
        shutil.copy2(template, dst)
        updated += 1

    print(f"[OK] Agent templates refreshed ({updated} files) → {dst_agents}")


def _copy_skills(project_root: Path) -> None:
    """Overwrite skill trees in ~/.claude/skills/<name>/ (force refresh for update)."""
    src_skills = project_root / "plugin" / "skills"
    dst_skills = Path.home() / ".claude" / "skills"

    if not src_skills.exists():
        print("[SKIP] No skills found in plugin/skills/")
        return

    dst_skills.mkdir(parents=True, exist_ok=True)
    updated = 0
    for skill_dir in src_skills.iterdir():
        if not skill_dir.is_dir():
            continue
        # dirs_exist_ok merges: our files overwrite, user-added files stay.
        shutil.copytree(skill_dir, dst_skills / skill_dir.name, dirs_exist_ok=True)
        updated += 1

    print(f"[OK] Skills refreshed ({updated} dirs) → {dst_skills}")


def _copy_commands(project_root: Path) -> None:
    """Overwrite command files in ~/.claude/commands/ (force refresh for update)."""
    src_commands = project_root / "plugin" / "commands"
    dst_commands = Path.home() / ".claude" / "commands"

    if not src_commands.exists():
        print("[SKIP] No commands found in plugin/commands/")
        return

    dst_commands.mkdir(parents=True, exist_ok=True)
    updated = 0
    for command in src_commands.glob("*.md"):
        shutil.copy2(command, dst_commands / command.name)
        updated += 1

    print(f"[OK] Commands refreshed ({updated} files) → {dst_commands}")


def _merge_settings(project_root: Path) -> None:
    """Re-run hook and MCP registration logic (merge, never overwrite user config)."""
    # We import the functions from install.py to avoid code duplication.
    install_mod = _load_install_module(project_root)
    if install_mod is None:
        print("[WARN] Skipping settings merge")
        return

    print("[...] Merging MCP server config into settings.json ...")
    install_mod.register_global_mcp(project_root)

    print("[...] Merging hooks config into settings.json ...")
    install_mod.register_hooks(project_root)

    # Refresh the /loop maintenance prompt (reuses install.py's idempotent,
    # sentinel-guarded writer — no duplicated logic).
    if hasattr(install_mod, "install_loop_md"):
        print("[...] Refreshing /loop maintenance prompt ...")
        install_mod.install_loop_md(project_root)


# ---------------------------------------------------------------------------
# Check-only mode
# ---------------------------------------------------------------------------

def check_for_updates(project_root: Path) -> bool:
    """Print version comparison; return True if updates are available."""
    print("Checking for updates...")
    print()

    local_ver = _local_version(project_root)

    if not _is_git_repo(project_root):
        print(f"  Local version : {local_ver}")
        print("  [WARN] Not a git repository — cannot check for remote updates.")
        print("  To get updates, re-clone the repository and re-run install.py.")
        return False

    local_commit = _git_local_commit(project_root)
    print(f"  Local  : v{local_ver} ({local_commit})")

    print("  Fetching remote info...")
    remote_ver = _remote_version(project_root)
    remote_commit = _git_remote_commit(project_root)

    if remote_ver is None:
        print("  Remote : (could not determine — no remote configured)")
        return False

    print(f"  Remote : v{remote_ver} ({remote_commit})")

    if local_commit != remote_commit:
        print()
        print(f"  [UPDATE AVAILABLE] v{local_ver} → v{remote_ver}")
        print("  Run:  python scripts/update.py   (or: python install.py --update)")
        return True
    else:
        print()
        print("  Already up to date.")
        return False


# ---------------------------------------------------------------------------
# Full update
# ---------------------------------------------------------------------------

def run_update(project_root: Path) -> None:
    """Execute the full update sequence."""
    print("=" * 50)
    print("  AI Team OS Updater")
    print("=" * 50)
    print()

    local_ver_before = _local_version(project_root)
    local_commit_before = _git_local_commit(project_root) if _is_git_repo(project_root) else "n/a"

    print(f"  Before: v{local_ver_before} ({local_commit_before})")
    print()

    # Step 1 — git pull (only if git repo)
    if _is_git_repo(project_root):
        print("[1/7] Pulling latest code...")
        _git_pull(project_root)
    else:
        print("[1/7] Not a git repository — skipping git pull")
        print("      To get updates, re-clone and re-run install.py")
    print()

    # Step 2 — pip install -e .
    print("[2/7] Updating Python package...")
    _pip_install(project_root)
    print()

    # Step 3 — copy hook scripts (overwrite)
    print("[3/7] Refreshing hook scripts...")
    _copy_hooks(project_root)
    print()

    # Step 4 — copy agent templates (overwrite)
    print("[4/7] Refreshing agent templates...")
    _copy_agent_templates(project_root)
    print()

    # Step 5 — refresh skills (overwrite)
    print("[5/7] Refreshing skills...")
    _copy_skills(project_root)
    print()

    # Step 6 — refresh commands (overwrite)
    print("[6/7] Refreshing commands...")
    _copy_commands(project_root)
    print()

    # Step 7 — merge settings.json (MCP + hooks, never wipe user config)
    print("[7/7] Merging settings.json (MCP + hooks)...")
    _merge_settings(project_root)
    print()

    # Summary
    local_ver_after = _local_version(project_root)
    local_commit_after = _git_local_commit(project_root) if _is_git_repo(project_root) else "n/a"

    print("=" * 50)
    print("  Update complete!")
    print("=" * 50)
    print()
    if local_ver_before != local_ver_after or local_commit_before != local_commit_after:
        print(f"  v{local_ver_before} ({local_commit_before})  →  v{local_ver_after} ({local_commit_after})")
    else:
        print(f"  Version unchanged: v{local_ver_after} ({local_commit_after})")
    print()
    print("  Restart Claude Code for hook and MCP changes to take effect.")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Team OS update utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/update.py           # full update\n"
            "  python scripts/update.py --check   # check only, no changes\n"
            "  python install.py --update         # same as full update (via install.py)\n"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for updates without applying them",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent

    if args.check:
        has_updates = check_for_updates(project_root)
        sys.exit(0 if not has_updates else 2)
    else:
        run_update(project_root)


if __name__ == "__main__":
    main()
