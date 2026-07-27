#!/usr/bin/env python3
"""PreCompact Hook — 压缩前把 OS 侧作战态定格成检查点。

Q6 裁定 A（2026-07-27）。这个 hook 从前只做一件事：往
``~/.claude/compact-events.jsonl`` 追加一行时间戳，而那个文件全仓没有任何读
取方——审计原话"往一个没人读的文件追加时间戳"。

现在它真正做事：通知 OS 把这个会话此刻的作战态（在飞 agent / 未完成任务 /
待缔造者裁决项）拍成检查点，压缩后的第一次 SessionStart(source=compact) 由
session_bootstrap 原样递回给 Leader。快照内容由**服务端**从库里取，hook 只
负责报信——这样 hook 保持纯 stdlib、单次 HTTP，也不会因为 hook 少查一样东西
就把检查点做残。

jsonl 保留为离线痕迹：OS 没起来时它是唯一的记录。同时补记 ``raw_bytes``——
历史 159 条里有 155 条 trigger=unknown 且没有 session_id，那只可能是 stdin
为空，但究竟谁在空手调用这个脚本一直没查清。记下入参长度，下次发生就有据可查，
不必再靠推测。

Usage: python -m aiteam.hooks.pre_compact_save
"""

import json
import os
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")
_API_TIMEOUT = 3


def _get_api_url() -> str:
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


def _save_checkpoint(session_id: str, trigger: str, cwd: str) -> bool:
    """Ask the OS to snapshot this session. Returns whether it landed."""
    if not session_id:
        return False
    body = json.dumps({"session_id": session_id, "trigger": trigger, "cwd": cwd}).encode()
    req = urllib.request.Request(
        f"{_get_api_url()}/api/hooks/compact-checkpoint",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            resp.read()
        return True
    except Exception:
        return False


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
            "raw_bytes": len(input_data or ""),
        }
        session_id = ""
        cwd = ""

        if input_data and input_data.strip():
            try:
                parsed = json.loads(input_data)
                record["trigger"] = parsed.get("trigger", "unknown")
                record["transcript_path"] = parsed.get("transcript_path", "")
                session_id = parsed.get("session_id", "")
                cwd = parsed.get("cwd", "")
                record["session_id"] = session_id
            except (json.JSONDecodeError, TypeError):
                pass

        record["checkpoint_saved"] = _save_checkpoint(session_id, record["trigger"], cwd)

        # Offline trace: the only record when the OS is not running.
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
