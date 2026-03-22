#!/usr/bin/env python3
"""AI Team OS updater script.

Usage:
    python scripts/update.py            # full update
    python scripts/update.py --check    # only check for updates, do not apply
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(args: list[str], cwd: str | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess; raise SystemExit on failure."""
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            check=True,
            shell=(sys.platform == "win32" and args[0] in ("npm", "npx")),
            capture_output=capture,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[FAIL] Command failed: {' '.join(str(a) for a in args)}")
        if capture and e.stderr:
            print(e.stderr.strip())
        raise SystemExit(1) from e
    except FileNotFoundError:
        print(f"[FAIL] Command not found: {args[0]}")
        raise SystemExit(1)


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
    """Re-install the package in editable mode."""
    print("[...] Reinstalling Python package (pip install -e .) ...")
    _run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=str(project_root))
    print("[OK] Python package updated")


def _copy_hooks(project_root: Path) -> None:
    """Overwrite hook scripts in ~/.claude/hooks/ai-team-os/."""
    src_dir = project_root / "plugin" / "hooks"
    dst_dir = Path.home() / ".claude" / "hooks" / "ai-team-os"
    dst_dir.mkdir(parents=True, exist_ok=True)

    hook_files = [
        "send_event.py",
        "workflow_reminder.py",
        "session_bootstrap.py",
        "inject_subagent_context.py",
    ]
    copied = 0
    for fname in hook_files:
        src = src_dir / fname
        dst = dst_dir / fname
        if src.exists():
            shutil.copy2(src, dst)
            copied += 1
        else:
            print(f"[WARN] Hook source not found, skipping: {src}")

    print(f"[OK] Hook scripts refreshed ({copied} files) → {dst_dir}")


def _copy_agent_templates(project_root: Path) -> None:
    """Overwrite agent templates in ~/.claude/agents/ (force overwrite for update)."""
    src_agents = project_root / ".claude" / "agents"
    dst_agents = Path.home() / ".claude" / "agents"

    if not src_agents.exists():
        print("[SKIP] No agent templates found in .claude/agents/")
        return

    dst_agents.mkdir(parents=True, exist_ok=True)
    updated = 0
    for template in src_agents.glob("*.md"):
        dst = dst_agents / template.name
        shutil.copy2(template, dst)
        updated += 1

    print(f"[OK] Agent templates refreshed ({updated} files) → {dst_agents}")


def _merge_settings(project_root: Path) -> None:
    """Re-run hook and MCP registration logic (merge, never overwrite user config)."""
    # We import the functions from install.py to avoid code duplication.
    install_py = project_root / "install.py"
    if not install_py.exists():
        print("[WARN] install.py not found — skipping settings merge")
        return

    import importlib.util

    spec = importlib.util.spec_from_file_location("install_module", install_py)
    if spec is None or spec.loader is None:
        print("[WARN] Could not load install.py — skipping settings merge")
        return

    install_mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(install_mod)  # type: ignore[union-attr]
    except Exception as exc:
        print(f"[WARN] Failed to exec install.py: {exc}")
        return

    print("[...] Merging MCP server config into settings.json ...")
    install_mod.register_global_mcp(project_root)

    print("[...] Merging hooks config into settings.json ...")
    install_mod.register_hooks(project_root)


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
        print("[1/5] Pulling latest code...")
        _git_pull(project_root)
    else:
        print("[1/5] Not a git repository — skipping git pull")
        print("      To get updates, re-clone and re-run install.py")
    print()

    # Step 2 — pip install -e .
    print("[2/5] Updating Python package...")
    _pip_install(project_root)
    print()

    # Step 3 — copy hook scripts (overwrite)
    print("[3/5] Refreshing hook scripts...")
    _copy_hooks(project_root)
    print()

    # Step 4 — copy agent templates (overwrite)
    print("[4/5] Refreshing agent templates...")
    _copy_agent_templates(project_root)
    print()

    # Step 5 — merge settings.json (MCP + hooks, never wipe user config)
    print("[5/5] Merging settings.json (MCP + hooks)...")
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
