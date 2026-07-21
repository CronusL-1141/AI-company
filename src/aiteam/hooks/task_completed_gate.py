#!/usr/bin/env python3
"""CC TaskCompleted hook gate — blocks completion if task has no memo or no result.

Fires when CC's TaskUpdate(status=completed) is used. Checks the OS task wall
to ensure the task has recorded progress (memo) and a result before allowing
completion. Silently passes when API is unreachable.
Uses stdlib only (no aiteam package dependency).
"""

import json
import os
import sys
import urllib.request

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")
_API_TIMEOUT = 2


def _get_api_url() -> str:
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


def _fetch_task(task_id: str) -> dict:
    api_url = _get_api_url()
    url = f"{api_url}/api/tasks/{task_id}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _check_task(task_id: str, task_subject: str) -> None:
    """Check task memo and result. Exits with code 2 if validation fails."""
    resp = _fetch_task(task_id)

    task_data = resp.get("data") or resp
    if isinstance(task_data, dict) and "data" in task_data:
        task_data = task_data["data"]

    result = task_data.get("result") or ""
    config = task_data.get("config") or {}
    memos = config.get("memo") or []

    has_result = bool(result and str(result).strip())
    has_memo = bool(memos)

    if has_result and has_memo:
        sys.exit(0)

    missing_parts = []
    if not has_memo:
        missing_parts.append("memo空")
    if not has_result:
        missing_parts.append("无结果")

    reason = "/".join(missing_parts)
    sys.stderr.write(
        f"[OS BLOCK] 任务 {task_subject} 未记录进展（{reason}），禁止标记完成。"
        "请先 task_memo_add 或 task_update result=...\n"
    )
    sys.exit(2)


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        # Invalid JSON — silent pass, don't block CC
        sys.exit(0)

    task_id = payload.get("task_id", "").strip()
    task_subject = payload.get("task_subject", "").strip() or task_id

    if not task_id:
        sys.exit(0)

    try:
        _check_task(task_id, task_subject)
    except SystemExit:
        raise
    except Exception:
        # API unreachable or any other error — silent pass
        sys.exit(0)


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
