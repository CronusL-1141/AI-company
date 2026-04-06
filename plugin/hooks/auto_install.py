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


def _ensure_agent_teams_env():
    """Ensure CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 is in ~/.claude/settings.json.

    Plugin settings.json env field is NOT supported by CC (only 'agent' key works).
    So we write directly to the user's settings.json instead.
    """
    import os
    settings_path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        settings = {}
        if os.path.exists(settings_path):
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)

        env = settings.get("env", {})
        if env.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") == "1":
            return  # Already set

        env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
        settings["env"] = env

        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # Silent failure — non-critical


def main():
    # Force UTF-8 output on Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # Ensure Agent Teams env var is set in user settings
    _ensure_agent_teams_env()

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
