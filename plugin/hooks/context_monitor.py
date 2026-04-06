#!/usr/bin/env python3
"""Context Monitor - UserPromptSubmit Hook.

Reads context usage from statusline's monitor file and warns Claude when > 80%.
Usage: python -m aiteam.hooks.context_monitor
"""

import json
import sys
from pathlib import Path


def _find_monitor_file() -> Path | None:
    """Find the most recent context-monitor.json (project-level first, then global)."""
    claude_dir = Path.home() / ".claude"

    # Strategy 1: project-level monitor (written by statusline per-project)
    projects_dir = claude_dir / "projects"
    if projects_dir.is_dir():
        candidates = list(projects_dir.glob("*/context-monitor.json"))
        if candidates:
            # Pick the most recently modified one
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return candidates[0]

    # Strategy 2: global fallback
    global_file = claude_dir / "context-monitor.json"
    if global_file.exists():
        return global_file

    return None


def main():
    # Force UTF-8 output on Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    monitor_file = _find_monitor_file()

    if not monitor_file:
        return

    try:
        data = json.loads(monitor_file.read_text(encoding="utf-8"))
        pct = data.get("used_percentage", 0)

        if pct >= 90:
            print(
                f"[CONTEXT CRITICAL] 上下文使用率: {pct}%. "
                "立即停止当前工作，保存所有记忆和进度到memory文件，"
                "然后提醒用户执行 /compact。"
                "不要开始任何新任务。"
            )
        elif pct >= 80:
            print(
                f"[CONTEXT WARNING] 上下文使用率: {pct}%. "
                "请尽快完成当前节点任务，然后保存记忆和进度到memory文件，"
                "并提醒用户执行 /compact。"
            )
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        # Silently ignore read errors
        pass


if __name__ == "__main__":
    main()
