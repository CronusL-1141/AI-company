"""Print connection headers once, using the native MCP helper's working directory.

Run this file directly with system Python. It deliberately imports no OS server,
MCP framework, database, or model client and leaves no resident process behind.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit


def connection_headers() -> dict[str, str]:
    return {"X-Aiteam-Project-Dir": quote(os.getcwd(), safe="/:.-_\\")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit project headers for one native HTTP MCP connection")
    parser.add_argument("--api-url", required=True, help="Already running local AI Team OS API base URL")
    args = parser.parse_args()
    parsed = urlsplit(args.api_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        print("AI Team OS HTTP MCP requires a local HTTP API base URL.", file=sys.stderr)
        return 1
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(args.api_url.rstrip("/") + "/api/mcp/http-readiness", timeout=2) as response:
            readiness = json.load(response)
            if readiness.get("status") != "ok" or readiness.get("project_context") != "connection-cwd-v1":
                raise ValueError("API does not support connection-scoped project context")
    except (OSError, ValueError):
        command = shlex.join([
            sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app", "--factory",
            "--host", "127.0.0.1", "--port", str(parsed.port or 80),
        ])
        source = shlex.quote(str(Path(__file__).resolve().parents[2]))
        print("AI Team OS HTTP API is not ready or lacks project-context support. "
              f"Start the deployed source before reconnecting:\nPYTHONPATH={source} {command}", file=sys.stderr)
        return 1
    print(json.dumps(connection_headers(), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
