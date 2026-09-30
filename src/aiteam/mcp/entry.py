"""MCP server entry point for PyPI / uvx usage.

Called via: uvx --from ai-team-os ai-team-os-serve
Or:         ai-team-os-serve  (after pip install)
"""


def main():
    """Start the API as its own process if needed, then serve MCP.

    The API used to run in a thread of this process. That shared the MCP
    server's environment with the API, so the API held this session's inbox
    token and identity; and since the API module scrubs its own process on
    import, it would also have removed the session id this MCP server needs.
    The subprocess launch is the one every other entry point uses, and it
    passes the API a scrubbed environment.
    """
    from aiteam.mcp.server import _ensure_api_running, mcp

    _ensure_api_running()
    mcp.run()


if __name__ == "__main__":
    main()
