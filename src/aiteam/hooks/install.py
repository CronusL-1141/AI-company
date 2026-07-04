"""AI Team OS — Hook configuration installer.

Automatically configures hooks in the project's .claude/settings.local.json,
forwarding CC operation events to the OS API.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HOOK_EVENTS = [
    "SubagentStart",
    "SubagentStop",
    "PreToolUse",
    "PostToolUse",
    "SessionStart",
    "SessionEnd",
    "Stop",
]


def get_send_event_source() -> Path:
    """Get the source file path for send_event.py."""
    return Path(__file__).parent / "send_event.py"


def generate_hooks_config(
    api_url: str = "http://localhost:8000",
    script_path: str | Path | None = None,
) -> dict:
    """Generate Claude Code hooks configuration.

    Uses sys.executable (absolute) instead of bare `python` so hooks keep
    working when CC resolves `python` to a project .venv without aiteam
    (e2d0fbb invariant), and an absolute script path so they work from any cwd.

    Parameters
    ----------
    api_url:
        OS API service address, passed to send_event.py via environment variable.
    script_path:
        Absolute path to the installed send_event.py. Falls back to the
        project-relative default when omitted (legacy behavior).
    """
    hooks: dict[str, list] = {}

    py = str(sys.executable).replace("\\", "/")
    script = str(script_path or Path(".claude") / "hooks" / "send_event.py").replace("\\", "/")

    for event in HOOK_EVENTS:
        matcher_config: dict[str, str] = {}
        if event == "PreToolUse":
            matcher_config["matcher"] = "Agent|Bash|Edit|Write"

        hooks[event] = [
            {
                **matcher_config,
                "hooks": [
                    {
                        "type": "command",
                        "command": f'"{py}" "{script}" {event}',
                    }
                ],
            }
        ]

    return hooks


def install_hooks(
    project_dir: str,
    api_url: str = "http://localhost:8000",
) -> str:
    """Install CC hooks configuration in the specified project directory.

    This function is idempotent — running multiple times only updates hooks config,
    without duplicating entries.

    Parameters
    ----------
    project_dir:
        Root directory path of the project to install hooks in.
    api_url:
        OS API service address.

    Returns
    -------
    str
        Absolute path to the generated settings.local.json.
    """
    claude_dir = Path(project_dir) / ".claude"
    claude_dir.mkdir(exist_ok=True)

    # Copy send_event.py to project's .claude/hooks/ dir (avoid CJK path encoding issues)
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    src_script = get_send_event_source()
    dst_script = hooks_dir / "send_event.py"
    shutil.copy2(src_script, dst_script)

    settings_path = claude_dir / "settings.local.json"

    # Read existing config (preserve other settings)
    existing: dict = {}
    if settings_path.exists():
        with open(settings_path, encoding="utf-8") as f:
            try:
                existing = json.load(f)
            except json.JSONDecodeError:
                existing = {}

    # Overwrite hooks config (idempotent)
    existing["hooks"] = generate_hooks_config(api_url, script_path=dst_script.resolve())

    # Write back
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    return str(settings_path)


def uninstall_hooks(project_dir: str) -> bool:
    """Remove hooks configuration.

    Parameters
    ----------
    project_dir:
        Project root directory path.

    Returns
    -------
    bool
        Whether removal was successful (True = hooks removed, False = no hooks found).
    """
    settings_path = Path(project_dir) / ".claude" / "settings.local.json"
    if not settings_path.exists():
        return False

    with open(settings_path, encoding="utf-8") as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError:
            return False

    if "hooks" in config:
        del config["hooks"]
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True

    return False
