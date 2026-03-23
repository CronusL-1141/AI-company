#!/usr/bin/env python3
"""Context Monitor - UserPromptSubmit Hook
Reads context usage from statusline's monitor file and warns Claude when > 80%.
"""

import json
from pathlib import Path


def main():
    monitor_file = Path.home() / ".claude" / "context-monitor.json"

    if not monitor_file.exists():
        return

    try:
        data = json.loads(monitor_file.read_text(encoding="utf-8"))
        pct = data.get("used_percentage", 0)

        if pct >= 90:
            print(
                f"[CONTEXT CRITICAL] \u4e0a\u4e0b\u6587\u4f7f\u7528\u7387: {pct}%. "
                "\u7acb\u5373\u505c\u6b62\u5f53\u524d\u5de5\u4f5c\uff0c\u4fdd\u5b58\u6240\u6709\u8bb0\u5fc6\u548c\u8fdb\u5ea6\u5230memory\u6587\u4ef6\uff0c"
                "\u7136\u540e\u63d0\u9192\u7528\u6237\u6267\u884c /compact\u3002"
                "\u4e0d\u8981\u5f00\u59cb\u4efb\u4f55\u65b0\u4efb\u52a1\u3002"
            )
        elif pct >= 80:
            print(
                f"[CONTEXT WARNING] \u4e0a\u4e0b\u6587\u4f7f\u7528\u7387: {pct}%. "
                "\u8bf7\u5c3d\u5feb\u5b8c\u6210\u5f53\u524d\u8282\u70b9\u4efb\u52a1\uff0c\u7136\u540e\u4fdd\u5b58\u8bb0\u5fc6\u548c\u8fdb\u5ea6\u5230memory\u6587\u4ef6\uff0c"
                "\u5e76\u63d0\u9192\u7528\u6237\u6267\u884c /compact\u3002"
            )
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        # Silently ignore read errors
        pass


if __name__ == "__main__":
    main()
