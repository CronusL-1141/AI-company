#!/usr/bin/env python3
"""MCP server bootstrap for CC plugin installation.

Resolves venv path from multiple sources (args > env > file-based discovery),
injects venv site-packages into sys.path, starts API server, then runs MCP.

CC plugin .mcp.json env vars have known bugs (not passed to subprocess),
so paths are passed via command-line args instead.
"""

import argparse
import os
import sys
from pathlib import Path


def _find_plugin_data() -> Path | None:
    """Find plugin data dir from args, env, or filesystem discovery."""
    # Source 1: command-line args (most reliable, passed via .mcp.json args)
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--plugin-data", default="")
    parser.add_argument("--plugin-root", default="")
    args, _ = parser.parse_known_args()

    if args.plugin_data and Path(args.plugin_data).exists():
        return Path(args.plugin_data)

    # Source 2: environment variable (works in hooks, may not work in .mcp.json)
    env_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if env_data and Path(env_data).exists():
        return Path(env_data)

    # Source 3: filesystem discovery (hardcoded fallback)
    # CC stores plugin data at ~/.claude/plugins/data/{marketplace-name}-{plugin-name}/
    claude_dir = Path.home() / ".claude" / "plugins" / "data"
    if claude_dir.exists():
        for d in claude_dir.iterdir():
            if "ai-team-os" in d.name and (d / "venv").exists():
                return d

    return None


def _activate_venv(plugin_data: Path | None):
    """Inject venv site-packages into sys.path."""
    if not plugin_data:
        return

    venv_dir = plugin_data / "venv"
    if not venv_dir.exists():
        return

    # Find site-packages: Windows vs Unix
    if sys.platform == "win32":
        site_packages = venv_dir / "Lib" / "site-packages"
    else:
        lib_dir = venv_dir / "lib"
        if lib_dir.exists():
            for d in lib_dir.iterdir():
                if d.name.startswith("python"):
                    site_packages = d / "site-packages"
                    break
            else:
                site_packages = lib_dir / "site-packages"
        else:
            return

    if site_packages.exists():
        site_str = str(site_packages)
        if site_str not in sys.path:
            sys.path.insert(0, site_str)

    # Also try adding project src dir for editable installs
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if not plugin_root:
        # Infer from __file__
        plugin_root = str(Path(__file__).resolve().parent)
    project_root = str(Path(plugin_root).parent)
    src_dir = os.path.join(project_root, "src")
    if os.path.isdir(src_dir) and src_dir not in sys.path:
        sys.path.insert(0, src_dir)


def _wait_for_venv(max_wait: int = 180):
    """Wait for venv to be created by install-deps.sh (runs in parallel).

    On first install, SessionStart hook runs install-deps.sh which creates
    the venv. MCP server starts in parallel and may beat it. Wait here.
    """
    import time
    plugin_data = _find_plugin_data()
    if plugin_data and (plugin_data / "venv").exists():
        return plugin_data  # Already exists

    # Venv doesn't exist yet — wait for install-deps.sh to create it
    print("[AI Team OS] Waiting for dependencies to install (first run)...", file=sys.stderr)
    for i in range(max_wait):
        time.sleep(1)
        plugin_data = _find_plugin_data()
        if plugin_data and (plugin_data / "venv").exists():
            # Give pip a moment to finish writing
            time.sleep(3)
            print(f"[AI Team OS] Venv ready after {i+1}s.", file=sys.stderr)
            return plugin_data
    print("[AI Team OS] Timeout waiting for venv. Trying system python.", file=sys.stderr)
    return plugin_data


if __name__ == "__main__":
    plugin_data = _find_plugin_data()

    # First install: venv may not exist yet (install-deps.sh running in parallel)
    if not plugin_data or not (plugin_data / "venv").exists():
        plugin_data = _wait_for_venv()

    _activate_venv(plugin_data)

    try:
        from aiteam.mcp.server import mcp, _ensure_api_running

        # Patch sys.executable to venv python so API subprocess uses venv too
        if plugin_data and (plugin_data / "venv").exists():
            if sys.platform == "win32":
                venv_py = plugin_data / "venv" / "Scripts" / "python.exe"
            else:
                venv_py = plugin_data / "venv" / "bin" / "python"
            if venv_py.exists():
                sys.executable = str(venv_py)

        # Start API in background thread — must not block MCP initialization
        # CC kills MCP server if it doesn't respond to initialize within seconds
        import threading
        threading.Thread(target=_ensure_api_running, daemon=True).start()
        mcp.run()
    except ImportError as e:
        print(f"[AI Team OS] ERROR: {e}", file=sys.stderr)
        print("[AI Team OS] Try: claude plugin update ai-team-os", file=sys.stderr)
        sys.exit(1)
