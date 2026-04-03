#!/usr/bin/env python3
"""PreCompact Hook - Safety net for context preservation.

Fires when auto-compact or manual /compact triggers, records the event.
Usage: python -m aiteam.hooks.pre_compact_save
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def main():
    # Force UTF-8 output on Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        input_data = sys.stdin.buffer.read().decode("utf-8")
        record = {
            "trigger": "unknown",
            "timestamp": datetime.now(UTC).isoformat(),
        }

        if input_data and input_data.strip():
            try:
                parsed = json.loads(input_data)
                record["trigger"] = parsed.get("trigger", "unknown")
                record["transcript_path"] = parsed.get("transcript_path", "")
                record["session_id"] = parsed.get("session_id", "")
            except (json.JSONDecodeError, TypeError):
                pass

        # Append compact event log
        log_path = Path.home() / ".claude" / "compact-events.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    except Exception:
        # Silently ignore errors - never block compact
        pass


if __name__ == "__main__":
    main()
