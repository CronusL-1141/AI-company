#!/usr/bin/env python3
"""AI Team OS updater script.

The install surface comes from install.py (one definition: hooks, MCP, agent
templates, skills, commands, loop.md); this script adds pulling the code,
reinstalling the package, and moving the OS data directory out of the legacy
location before the surface is refreshed (docs/config-dir-compat-design.md §4.1).

Usage:
    python scripts/update.py                   # full update
    python scripts/update.py --check           # only check for updates, do not apply
    python scripts/update.py --config-dir DIR  # update the install in another Claude Code config folder
"""

import argparse
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
    """__version__ of this checkout; install.py owns the reader."""
    install_mod = _load_install_module(project_root)
    return install_mod.source_version(project_root) if install_mod is not None else "unknown"


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


def _verify_deps(project_root: Path, install_mod) -> None:
    """Prove the declared dependencies actually import; install whatever is missing.

    This closes the hole left by the tolerant `_pip_install` above. On a PEP 668
    interpreter that step is refused outright, so a release that adds a dependency
    leaves the user without it - and the updater cannot warn about it, because it
    does not know whether this version added one. The user sees a clean update and
    then a tool that fails on first call.

    Checking importability answers the question directly, and the shared helper
    reads the dependency list from pyproject so nothing has to be kept in sync here.
    """
    verify = getattr(install_mod, "verify_runtime_dependencies", None)
    if verify is None:
        print("[WARN] install.py has no dependency verifier - skipping dependency check")
        return
    try:
        verify(project_root)
    except Exception as exc:  # noqa: BLE001 - a check must never abort the update
        print(f"[WARN] dependency check failed: {type(exc).__name__} - continuing")


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


def _refresh_install_surface(project_root: Path, install_mod) -> None:
    """Hooks (copied, registered, owner recorded), the MCP server, agent templates, skills, commands, loop.md.

    All of it is install.py's: keeping a second hook-file list or a second copy
    loop here is exactly how the updater fell behind the installer before (it
    refreshed 4 of the 10 distributed hooks, and never dropped retired commands).
    """
    print("[...] Merging MCP server config ...")
    install_mod.register_global_mcp(project_root)
    print("[...] Copying and registering hooks ...")
    install_mod.register_hooks(project_root)
    print("[...] Refreshing agent templates, skills, commands and the /loop prompt ...")
    install_mod.copy_agent_templates(project_root, overwrite=True)
    install_mod.copy_skills(project_root, overwrite=True)
    install_mod.copy_commands(project_root, overwrite=True)
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

def run_update(project_root: Path, *, config_dir: str | None = None, migrate_data: bool = True,
               offline: bool = False) -> None:
    """Execute the full update sequence.

    Order (docs/config-dir-compat-design.md §5.4): code and package first, then the
    data directory (its migration needs the package and must come before the
    install surface), then the surface, then the record. *config_dir* is the
    Claude Code config folder to update (else the session's); *offline* skips
    pulling and every package install; *migrate_data* False leaves the data where it is.
    """
    install_mod = _load_install_module(project_root)
    if install_mod is None:
        print("[FAIL] install.py could not be loaded - nothing was updated")
        sys.exit(1)
    install_mod.apply_config_dir(config_dir)

    print("=" * 50)
    print("  AI Team OS Updater")
    print("=" * 50)
    print()
    install_mod.print_install_target()

    git_repo = _is_git_repo(project_root)
    local_ver_before = install_mod.source_version(project_root)
    local_commit_before = _git_local_commit(project_root) if git_repo else "n/a"

    print(f"  Before: v{local_ver_before} ({local_commit_before})")
    print()

    # Step 1 - git pull (only if git repo)
    if offline:
        print("[1/5] --offline - not pulling code")
    elif git_repo:
        print("[1/5] Pulling latest code...")
        _git_pull(project_root)
    else:
        print("[1/5] Not a git repository - skipping git pull")
        print("      To get updates, re-clone and re-run install.py")
    print()
    # The rest runs the installer as pulled just now, not the one loaded before the pull.
    install_mod = _load_install_module(project_root) or install_mod

    # Step 2 - pip install -e ., then prove the dependencies actually import
    if offline:
        print("[2/5] --offline - not installing packages")
    else:
        print("[2/5] Updating Python package...")
        _pip_install(project_root)
        _verify_deps(project_root, install_mod)
    print()

    # Step 3 - the data directory: out of the legacy location (seven gates), or as it is
    print("[3/5] OS data directory...")
    migration = install_mod.migrate_data_dir(project_root) if migrate_data else None
    if not migrate_data:
        print("[SKIP] --no-migrate-data - the data directory stays where it is")
    data_dir = install_mod.prepare_data_dir()
    print()

    # Step 4 - the install surface (hooks, MCP, agents, skills, commands, loop.md; never wipes user config)
    print("[4/5] Refreshing the install surface...")
    _refresh_install_surface(project_root, install_mod)
    print()

    # Step 5 - the install registry (the migration outcome reaches the data_dir_legacy notice through it)
    print("[5/5] Recording the install...")
    if data_dir is not None:
        install_mod.record_install(project_root, migration=migration)
    else:
        print("[WARN] No data directory to record the install in")
    print()

    # Summary
    local_ver_after = install_mod.source_version(project_root)
    local_commit_after = _git_local_commit(project_root) if git_repo else "n/a"

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
    parser.add_argument(
        "--config-dir",
        metavar="PATH",
        help="Claude Code config folder to update (default: CLAUDE_CONFIG_DIR, else the default folder)",
    )
    parser.add_argument(
        "--no-migrate-data",
        action="store_true",
        help="Leave the OS data directory where it is (no migration this time)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not pull code or install packages; refresh from this checkout as it is",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent

    if args.check:
        has_updates = check_for_updates(project_root)
        sys.exit(0 if not has_updates else 2)
    run_update(project_root, config_dir=args.config_dir, migrate_data=not args.no_migrate_data,
               offline=args.offline)


if __name__ == "__main__":
    main()
