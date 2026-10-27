#!/usr/bin/env python3
"""PreCompact Hook — 压缩前把 OS 侧作战态定格成检查点。

Q6 裁定 A（2026-07-27）。这个 hook 从前只做一件事：往 Claude Code 配置目录里的
``compact-events.jsonl`` 追加一行时间戳，而那个文件全仓没有任何读取方——审计原话
"往一个没人读的文件追加时间戳"。

现在它真正做事：通知 OS 把这个会话此刻的作战态（在飞 agent / 未完成任务 /
待用户裁决项）拍成检查点，压缩后的第一次 SessionStart(source=compact) 由
session_bootstrap 原样递回给 Leader。快照内容由**服务端**从库里取，hook 只
负责报信——这样 hook 保持纯 stdlib、单次 HTTP，也不会因为 hook 少查一样东西
就把检查点做残。

jsonl 保留为离线痕迹：OS 没起来时它是唯一的记录。它是 OS 写的东西，所以落在 OS
数据目录（hook_core 路径段），不再写进宿主的配置目录；没有数据目录就不记（运行期
不建目录）。同时补记 ``raw_bytes``——
历史 159 条里有 155 条 trigger=unknown 且没有 session_id，那只可能是 stdin
为空，但究竟谁在空手调用这个脚本一直没查清。记下入参长度，下次发生就有据可查，
不必再靠推测。

Usage: python -m aiteam.hooks.pre_compact_save
"""

import importlib.util
import json
import os
import sys
import urllib.request
from datetime import UTC, datetime

_API_TIMEOUT = 3


def _hook_core():
    """Load the shared hook core next to this file (its path section); None if it cannot load."""
    module = sys.modules.get("hook_core")
    if module is not None:
        return module
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook_core.py")
        spec = importlib.util.spec_from_file_location("hook_core", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["hook_core"] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop("hook_core", None)
        return None


def _get_api_url() -> str:
    """AITEAM_API_URL, then the port file in the OS data directory, then 8000 (hook_core)."""
    core = _hook_core()
    if core is not None:
        return core._get_api_url()
    return os.environ.get("AITEAM_API_URL") or "http://localhost:8000"


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
        core = _hook_core()
        if core is not None:
            with open(core.os_data_dir() / "compact-events.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")

    except Exception:
        # Silently ignore errors - never block compact
        pass


def _main_chain_commands() -> list[str]:
    """The commands this session's main chain runs this script with, when this is the plugin's copy.

    With AI Team OS both a marketplace plugin and a source install, CC fires two
    byte-identical copies of every hook; only the plugin copy defers (CLAUDE_PLUGIN_ROOT
    is set for plugin hooks only, and __file__ lives under it). The main chain is what
    the settings.json of this session's config folder registers under hooks/ai-team-os/;
    the folder comes from hook_core's cc_config_dir, the only name taken from it. Any
    error means no commands, so the hook runs (fail-safe).
    """
    import importlib.util
    import json
    import os
    import sys
    from pathlib import Path
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return []
    try:
        if Path(plugin_root).resolve() not in Path(__file__).resolve().parents:
            return []
        _core = sys.modules.get("hook_core")
        if _core is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook_core.py")
            spec = importlib.util.spec_from_file_location("hook_core", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _core = sys.modules["hook_core"] = module
        settings = _core.cc_config_dir() / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
        commands = [str(hook.get("command", "")) for groups in registered.values() for group in groups
                    for hook in group.get("hooks", [])]
    except Exception:
        return []
    name = os.path.basename(__file__)
    return [command for command in commands if "ai-team-os" in command and name in command]


def _yield_if_superseded() -> None:
    """Exit 0 when the main chain covers this hook; a hook it does not register keeps running here."""
    if _main_chain_commands():
        raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    main()
