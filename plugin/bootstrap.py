#!/usr/bin/env python3
"""MCP server bootstrap for CC plugin installation.

Handles first-run dependency installation and cross-platform venv activation.
CC plugin system has no post-install hook, so bootstrap.py is responsible
for ensuring all dependencies are available before starting the MCP server.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _get_plugin_data_dir() -> Path:
    """Get plugin data directory from args, env, or filesystem discovery."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--plugin-data", default="")
    parser.add_argument("--plugin-root", default="")
    args, _ = parser.parse_known_args()

    # Source 1: command-line args
    if args.plugin_data:
        return Path(args.plugin_data)

    # Source 2: environment variable
    env_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if env_data:
        return Path(env_data)

    # Source 3: filesystem discovery
    claude_dir = Path.home() / ".claude" / "plugins" / "data"
    if claude_dir.exists():
        for d in claude_dir.iterdir():
            if "ai-team-os" in d.name:
                return d

    # Source 4: create default
    default = Path.home() / ".claude" / "plugins" / "data" / "ai-team-os"
    default.mkdir(parents=True, exist_ok=True)
    return default


def _get_plugin_root() -> Path:
    """Get plugin root from args, env, or __file__."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--plugin-root", default="")
    args, _ = parser.parse_known_args()

    if args.plugin_root:
        return Path(args.plugin_root)

    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if env_root:
        return Path(env_root)

    return Path(__file__).resolve().parent


def _get_venv_pip(venv_dir: Path) -> str:
    """Get cross-platform pip path."""
    if sys.platform == "win32":
        return str(venv_dir / "Scripts" / "pip")
    return str(venv_dir / "bin" / "pip")


def _ensure_deps_installed(plugin_data: Path, plugin_root: Path) -> bool:
    """Create venv and install dependencies if needed. Returns True on success."""
    venv_dir = plugin_data / "venv"
    marker = plugin_data / "requirements.txt"
    source_reqs = plugin_root / "requirements.txt"

    # Skip if requirements haven't changed
    if marker.exists() and source_reqs.exists():
        try:
            if marker.read_bytes() == source_reqs.read_bytes():
                return venv_dir.exists()
        except OSError:
            pass

    print("[AI Team OS] Installing dependencies (first run, ~2 min)...", file=sys.stderr)
    plugin_data.mkdir(parents=True, exist_ok=True)

    # Create venv
    if not venv_dir.exists():
        print("[AI Team OS] Creating virtual environment...", file=sys.stderr)
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir)],
            capture_output=True, timeout=60,
        )

    pip = _get_venv_pip(venv_dir)

    # Install requirements
    if source_reqs.exists():
        print("[AI Team OS] Installing pip requirements...", file=sys.stderr)
        subprocess.run(
            [pip, "install", "-q", "-r", str(source_reqs)],
            capture_output=True, timeout=300,
        )

    # Install aiteam package
    project_root = plugin_root.parent
    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        print("[AI Team OS] Installing aiteam (local)...", file=sys.stderr)
        subprocess.run(
            [pip, "install", "-q", "-e", str(project_root)],
            capture_output=True, timeout=120,
        )
    else:
        print("[AI Team OS] Installing aiteam (GitHub)...", file=sys.stderr)
        subprocess.run(
            [pip, "install", "-q", "git+https://github.com/CronusL-1141/AI-company.git"],
            capture_output=True, timeout=300,
        )

    # Save marker
    if source_reqs.exists():
        try:
            import shutil
            shutil.copy2(str(source_reqs), str(marker))
        except OSError:
            pass

    print("[AI Team OS] Dependencies ready.", file=sys.stderr)
    return True


def _activate_venv(plugin_data: Path):
    """Inject venv site-packages into sys.path."""
    venv_dir = plugin_data / "venv"
    if not venv_dir.exists():
        return

    if sys.platform == "win32":
        site_packages = venv_dir / "Lib" / "site-packages"
    else:
        lib_dir = venv_dir / "lib"
        site_packages = None
        if lib_dir.exists():
            for d in lib_dir.iterdir():
                if d.name.startswith("python"):
                    site_packages = d / "site-packages"
                    break
        if not site_packages:
            site_packages = lib_dir / "site-packages" if lib_dir.exists() else None

    if site_packages and site_packages.exists():
        site_str = str(site_packages)
        if site_str not in sys.path:
            sys.path.insert(0, site_str)

    # Add project src dir for editable installs
    plugin_root = _get_plugin_root()
    src_dir = str(plugin_root.parent / "src")
    if os.path.isdir(src_dir) and src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    # Patch sys.executable for API subprocess
    if sys.platform == "win32":
        venv_py = venv_dir / "Scripts" / "python.exe"
    else:
        venv_py = venv_dir / "bin" / "python"
    if venv_py.exists():
        sys.executable = str(venv_py)


if __name__ == "__main__":
    plugin_data = _get_plugin_data_dir()
    plugin_root = _get_plugin_root()

    # Ensure dependencies (creates venv + pip install on first run)
    _ensure_deps_installed(plugin_data, plugin_root)
    _activate_venv(plugin_data)

    try:
        from aiteam.mcp.server import mcp, _ensure_api_running
        import threading
        threading.Thread(target=_ensure_api_running, daemon=True).start()
        mcp.run()
    except ImportError as e:
        print(f"[AI Team OS] ERROR: {e}", file=sys.stderr)
        print("[AI Team OS] Try: claude plugin update ai-team-os", file=sys.stderr)
        sys.exit(1)
