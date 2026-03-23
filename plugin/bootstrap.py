#!/usr/bin/env python3
"""MCP server bootstrap — find venv python and start server.

Handles cross-platform venv path differences:
  Windows: ${CLAUDE_PLUGIN_DATA}/venv/Scripts/python
  Unix:    ${CLAUDE_PLUGIN_DATA}/venv/bin/python
"""

import os
import subprocess
import sys
from pathlib import Path


def _find_venv_python() -> str:
    """Find the venv python executable, cross-platform."""
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    if plugin_data:
        venv_dir = Path(plugin_data) / "venv"
        # Windows
        win_python = venv_dir / "Scripts" / "python.exe"
        if win_python.exists():
            return str(win_python)
        # Unix
        unix_python = venv_dir / "bin" / "python"
        if unix_python.exists():
            return str(unix_python)

    # Fallback: try system python with aiteam installed
    return sys.executable


if __name__ == "__main__":
    venv_python = _find_venv_python()

    if venv_python == sys.executable:
        # No venv, try direct import
        try:
            from aiteam.mcp.server import mcp
            mcp.run()
        except ImportError:
            print("[AI Team OS] ERROR: aiteam not installed. "
                  "Run install-deps.sh or restart Claude Code.", file=sys.stderr)
            sys.exit(1)
    else:
        # Run MCP server using venv python
        os.execv(venv_python, [venv_python, "-m", "aiteam.mcp.server"])
