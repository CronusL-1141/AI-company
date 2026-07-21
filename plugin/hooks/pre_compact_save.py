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


def _yield_if_superseded() -> None:
    """Backup-chain yield: exit 0 if the source-install main chain covers this hook.

    When AI Team OS is present both as a marketplace plugin and via the source
    installer, CC fires two byte-identical copies of every hook. To keep exactly
    one chain speaking, the plugin-mode copy exits silently iff ~/.claude/settings.json
    already registers this same script under ~/.claude/hooks/ai-team-os/. Hooks the
    main chain does not register keep running from the plugin (out-of-box backup —
    no coverage gap). Only the plugin-mode copy ever yields: CLAUDE_PLUGIN_ROOT is
    set by CC for plugin hooks only and __file__ lives under it; the runtime
    main-chain copy and any direct/repo/test run lack that and never yield. Pure
    stdlib, one small file read; any error falls through and runs (fail-safe).
    """
    import os
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return
    try:
        import json
        from pathlib import Path
        here = Path(__file__).resolve()
        if Path(plugin_root).resolve() not in here.parents:
            return
        settings = Path.home() / ".claude" / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except Exception:
        return
    name = os.path.basename(__file__)
    for groups in registered.values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "ai-team-os" in cmd and name in cmd:
                    raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    main()
