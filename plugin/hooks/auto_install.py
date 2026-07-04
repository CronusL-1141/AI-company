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


def _self_heal_interpreter():
    """Rewrite plugin manifest interpreter tokens to sys.executable (idempotent).

    Static plugin manifests (hooks/hooks.json, .mcp.json) cannot embed
    per-machine absolute paths, so they ship with a generic `python3` token.
    Bare tokens re-create the two failure modes the project already paid for:
    macOS without a `python` shim (command-not-found) and project .venv
    hijacking resolution (e2d0fbb). This hook runs under a working interpreter
    — the same one that pip-installs aiteam — so we rewrite the token to its
    absolute path, restoring the sys.executable invariant for MCP + all hooks.
    Idempotent: rewritten commands no longer start with python/python3.
    Never blocks SessionStart: every failure is swallowed.
    """
    import os
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    exe = sys.executable
    if not root or not exe:
        return
    quoted_exe = f'"{exe}"' if " " in exe else exe

    # hooks/hooks.json — shell-form commands: replace the leading interpreter token
    hooks_path = os.path.join(root, "hooks", "hooks.json")
    try:
        with open(hooks_path, encoding="utf-8") as f:
            data = json.load(f)
        changed = False
        for groups in data.get("hooks", {}).values():
            for group in groups:
                for hook in group.get("hooks", []):
                    cmd = hook.get("command", "")
                    for token in ("python3 ", "python "):
                        if cmd.startswith(token):
                            hook["command"] = quoted_exe + " " + cmd[len(token):]
                            changed = True
                            break
        if changed:
            with open(hooks_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # Silent failure — non-critical

    # .mcp.json — exec form: command field is the bare program (no quoting)
    mcp_path = os.path.join(root, ".mcp.json")
    try:
        with open(mcp_path, encoding="utf-8") as f:
            data = json.load(f)
        server = data.get("mcpServers", {}).get("ai-team-os")
        if server and server.get("command") in ("python", "python3"):
            server["command"] = exe
            with open(mcp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # Silent failure — non-critical


def main():
    # Force UTF-8 output on Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # Ensure Agent Teams env var is set in user settings
    _ensure_agent_teams_env()

    # Self-heal: converge plugin manifests to this (working) interpreter's
    # absolute path so MCP + hooks survive machines without `python`/`python3`
    # on PATH and project-.venv hijacking. Takes effect on next session.
    _self_heal_interpreter()

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
