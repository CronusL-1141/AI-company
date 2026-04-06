#!/usr/bin/env python3
"""Auto-install aiteam package on first launch.

This hook runs FIRST in SessionStart, before any other hook that depends
on the aiteam package. It uses only stdlib — no third-party imports.

On first marketplace install, aiteam is not pip-installed. This script
detects that and installs it automatically. User needs to restart CC once
after installation for MCP server to pick up the package.
"""
import json
import subprocess
import sys


def main():
    # Force UTF-8 output on Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # Check if aiteam is already importable
    try:
        import aiteam  # noqa: F401
        return  # Already installed, nothing to do
    except ImportError:
        pass

    # Not installed — attempt auto-install from GitHub (PyPI may lag behind)
    print("[AI Team OS] First launch detected — installing dependencies...")
    _GITHUB_URL = "git+https://github.com/CronusL-1141/AI-company.git"
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", _GITHUB_URL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        print("[AI Team OS] Dependencies installed successfully.")
        print("[AI Team OS] Please restart Claude Code to activate all features.")
        # Output as hook result so CC shows the message
        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    "[AI Team OS] Dependencies installed. "
                    "Please restart Claude Code to activate MCP tools. "
                    "This is a one-time setup."
                ),
            }
        }
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
    except subprocess.CalledProcessError as e:
        stderr_text = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
        print(f"[AI Team OS] Auto-install failed: {stderr_text[:200]}", file=sys.stderr)
        print("[AI Team OS] Please run manually: pip install git+https://github.com/CronusL-1141/AI-company.git", file=sys.stderr)
    except Exception as e:
        print(f"[AI Team OS] Auto-install error: {e}", file=sys.stderr)
        print("[AI Team OS] Please run manually: pip install git+https://github.com/CronusL-1141/AI-company.git", file=sys.stderr)


if __name__ == "__main__":
    main()
