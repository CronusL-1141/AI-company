"""Print connection headers once, using the native MCP helper's working directory.

Run this file directly with system Python. The default mode only checks an
existing API; an explicit runtime-script enables on-demand service startup.
The helper imports no OS server, MCP framework, database or model client.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit


def connection_headers() -> dict[str, str]:
    return {"X-Aiteam-Project-Dir": quote(os.getcwd(), safe="/:.-_\\")}


def _ready(url: str) -> bool:
    try:
        handlers = [urllib.request.ProxyHandler({})]
        opener = urllib.request.build_opener(*handlers)
        with opener.open(url.rstrip("/") + "/api/mcp/http-readiness", timeout=2) as response:
            readiness = json.load(response)
        return readiness.get("status") == "ok" and readiness.get("project_context") == "connection-cwd-v1"
    except (OSError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Emit project headers for one native HTTP MCP connection")
    parser.add_argument("--api-url", required=True, help="Already running local AI Team OS API base URL")
    parser.add_argument("--runtime-script", type=Path, help="Explicit opt-in to on-demand runtime startup")
    parser.add_argument("--runtime-dir", type=Path)
    args = parser.parse_args()
    parsed = urlsplit(args.api_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        print("AI Team OS HTTP MCP requires a local HTTP API base URL.", file=sys.stderr)
        return 1
    api_ready = _ready(args.api_url)
    if args.runtime_script and not api_ready:
        if not args.runtime_script.is_absolute() or not args.runtime_script.is_file() or not args.runtime_dir:
            print("OS runtime 安装路径失效或缺少 runtime-dir，请修复 Codex 安装配置。", file=sys.stderr)
            return 1
        command = [sys.executable, str(args.runtime_script), "ensure", "--api-url", args.api_url,
                   "--runtime-dir", str(args.runtime_dir)]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"OS runtime 启动失败: {type(error).__name__}", file=sys.stderr)
            return 1
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        if result.returncode:
            return 1
        api_ready = _ready(args.api_url)
    if not api_ready:
        command = shlex.join([
            sys.executable, "scripts/codex_adapter.py", "start", "--api-url", args.api_url,
        ])
        print("AI Team OS HTTP API is not ready or lacks project-context support. "
              f"From the AI Team OS repository, start the service before reconnecting:\n{command}", file=sys.stderr)
        return 1
    print(json.dumps(connection_headers(), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
