#!/usr/bin/env python3
"""MCP server bootstrap — activate venv and start server in-process.

Instead of os.execv (breaks stdio on Windows), we modify sys.path
to include the venv's site-packages, then import and run directly.
This preserves stdin/stdout for MCP protocol communication.
"""

import os
import site
import sys
from pathlib import Path


def _activate_venv():
    """Activate venv by injecting its site-packages into sys.path."""
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if not plugin_data:
        return

    venv_dir = Path(plugin_data) / "venv"
    if not venv_dir.exists():
        return

    # Find site-packages: Windows vs Unix
    if sys.platform == "win32":
        site_packages = venv_dir / "Lib" / "site-packages"
    else:
        # Find python version dir (e.g. python3.12)
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
        # Insert at front so venv packages take priority
        site_str = str(site_packages)
        if site_str not in sys.path:
            sys.path.insert(0, site_str)
        # Also add the plugin parent (project root) for editable installs
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
        if plugin_root:
            project_root = str(Path(plugin_root).parent)
            src_dir = str(Path(project_root) / "src")
            if src_dir not in sys.path:
                sys.path.insert(0, src_dir)


if __name__ == "__main__":
    _activate_venv()

    try:
        from aiteam.mcp.server import mcp
        mcp.run()
    except ImportError as e:
        print(f"[AI Team OS] ERROR: {e}", file=sys.stderr)
        print("[AI Team OS] Run 'claude plugin update ai-team-os' and restart CC.", file=sys.stderr)
        sys.exit(1)
