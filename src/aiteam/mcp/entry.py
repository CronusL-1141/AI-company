"""MCP server entry point for PyPI / uvx usage.

Called via: uvx --from ai-team-os ai-team-os-serve
Or:         ai-team-os-serve  (after pip install)
"""

import threading


def _start_api_in_process():
    """Start FastAPI server in a background thread (no subprocess).

    When running via uvx, subprocess-based uvicorn launch may fail because
    sys.executable points to uvx's isolated venv which may not have uvicorn
    on PATH. Running in-process avoids this entirely.
    """
    try:
        import uvicorn
        from aiteam.api.app import create_app

        app = create_app()
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
    except Exception as e:
        import sys
        print(f"[AI Team OS] API server failed: {e}", file=sys.stderr)


def main():
    """Start MCP server with API auto-start in background thread."""
    from aiteam.mcp.server import mcp, _is_port_open

    # Start API if not already running
    if not _is_port_open():
        threading.Thread(target=_start_api_in_process, daemon=True).start()

    mcp.run()


if __name__ == "__main__":
    main()
