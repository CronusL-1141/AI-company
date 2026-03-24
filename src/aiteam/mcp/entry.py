"""MCP server entry point for PyPI / uvx usage.

Called via: uvx --from ai-team-os ai-team-os-serve
Or:         ai-team-os-serve  (after pip install)
"""

import threading


def main():
    """Start MCP server with API auto-start in background thread."""
    from aiteam.mcp.server import mcp, _ensure_api_running

    threading.Thread(target=_ensure_api_running, daemon=True).start()
    mcp.run()


if __name__ == "__main__":
    main()
