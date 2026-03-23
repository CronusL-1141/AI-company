#!/usr/bin/env python3
"""MCP server bootstrap — auto-install dependencies on first run.

CC plugin install only copies files, it does NOT run pip install.
This script checks if the aiteam package is available and installs
it automatically before starting the MCP server.
"""

import subprocess
import sys


def _ensure_aiteam_installed():
    """Install aiteam package if not already available."""
    try:
        import aiteam  # noqa: F401
        return True
    except ImportError:
        pass

    # Find pyproject.toml relative to this script
    from pathlib import Path
    plugin_root = Path(__file__).resolve().parent
    project_root = plugin_root.parent  # ai-team-os root

    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        # Try plugin data dir or current working dir
        pyproject = Path.cwd() / "pyproject.toml"

    if pyproject.exists():
        # Install from local source
        print(f"[AI Team OS] Installing dependencies from {pyproject.parent}...", file=sys.stderr)
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(pyproject.parent)],
            capture_output=True,
            timeout=120,
        )
    else:
        # Install from PyPI (if published) or GitHub
        print("[AI Team OS] Installing aiteam package...", file=sys.stderr)
        subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "git+https://github.com/CronusL-1141/AI-company.git"],
            capture_output=True,
            timeout=120,
        )

    # Verify installation
    try:
        import aiteam  # noqa: F401
        print("[AI Team OS] Dependencies installed successfully.", file=sys.stderr)
        return True
    except ImportError:
        print("[AI Team OS] ERROR: Failed to install aiteam package.", file=sys.stderr)
        return False


if __name__ == "__main__":
    if _ensure_aiteam_installed():
        # Start the actual MCP server
        from aiteam.mcp.server import mcp
        mcp.run()
    else:
        sys.exit(1)
