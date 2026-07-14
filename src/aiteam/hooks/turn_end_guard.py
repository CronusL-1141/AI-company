#!/usr/bin/env python3
"""Turn-end guard — Stop hook for 唤醒体系 v2 (docs/wake-loop-v2-design.md §8).

防止 Leader 在 ACTIVE 态"盲停"：有 subagent/run 在飞、却没武装事件 watcher 时，若
Leader 直接结束 turn 就没有任何机制在活干完时叫醒它。guard 在 turn 结束时拦一道
（decision:block，batch0 实测坐实的通道），逼其"武装 watcher 或继续轮询"。

两种模式（argv 驱动，仿 send_event.py）：
    turn_end_guard.py               -> Stop 事件：判断是否阻止盲停
    turn_end_guard.py user-prompt   -> UserPromptSubmit：标记 manual 模式（用户在场）

Stop 决策（batch0 验证的 7 分支，docs/batch0-contract-tests.md 测试④）优先级：
    1. stop_hook_active            -> allow（递归防护，让位给已 block 的其它 hook 如 /goal）
    2. manual 模式在有效期         -> allow（用户在场；UserPromptSubmit 写的标记）
    3. 最近一条用户消息含停止关键词 -> allow（硬约束：用户显式说停必放行，中英）
    4. 查 /api/wake/actionable：
         无活在飞(busy_agents==0 且 live_runs==0) -> allow
         有活 + watcher 已武装                     -> allow（信任 watcher 唤醒）
         有活 + watcher 未武装                     -> block（decision:block + 理由）
    5. 连续 block 次数超上限        -> allow（防误判死拦）

fail-open：任何异常一律 allow（exit 0）。hook 故障绝不能卡死会话——宁可漏拦
（丢一次延迟，/loop 兜底）不可错拦（把用户锁在 block 里）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# ---- 配置常量 ----
# 用户发言后视为 manual 的窗口（秒）。与 session_probe LIVE_WINDOW 口径一致(15min)：
# 用户持续交互时每次发言刷新此窗；停手超窗后自治 guard 重新生效。
MANUAL_TTL = 900
# 连续 block 上限：同一 turn 链上累计 block 达此数即强制放行（防误判死拦）。
MAX_BLOCKS = 3
# block 计数的"连续"判定窗口：上次 block 超此秒数则计数归零（视为新链）。
BLOCK_RESET_AFTER = 300
_API_TIMEOUT = 2.0

_PORT_FILE = Path.home() / ".claude" / "data" / "ai-team-os" / "api_port.txt"
_WAKE_STATE_DIR = Path.home() / ".claude" / "data" / "ai-team-os" / "wake-state"

# 停止关键词（中英）。命中最近一条用户消息即放行——硬约束。
STOP_KEYWORDS = re.compile(
    r"停|收工|别再|暂停|打住|歇|\bstop\b|\bhold on\b|\bhalt\b|\bpause\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# 纯决策函数 —— 全部输入显式传入，无 I/O，可直接单测（同 batch0 guard_unit）。
# ---------------------------------------------------------------------------
def decide(
    *,
    stop_hook_active: bool,
    manual_active: bool,
    stop_keyword_hit: bool,
    work_in_flight: bool,
    watcher_armed: bool,
    block_count: int,
    max_blocks: int = MAX_BLOCKS,
) -> tuple[str, str, str]:
    """返回 (action, branch, reason)，action ∈ {'allow','block'}。优先级见模块文档。"""
    if stop_hook_active:
        return "allow", "stop_hook_active", "another Stop hook already handled this turn"
    if manual_active:
        return "allow", "manual", "user present (manual mode active)"
    if stop_keyword_hit:
        return "allow", "stop_keyword", "user explicitly asked to stop"
    if not work_in_flight:
        return "allow", "safe", "no work in flight"
    if watcher_armed:
        return "allow", "watcher_armed", "work in flight but a watcher is armed to wake"
    if block_count >= max_blocks:
        return "allow", "block_cap", f"released after {block_count} consecutive blocks"
    return (
        "block",
        "danger_zone",
        "OS 检测到有 agent/run 在飞但未武装事件 watcher。请二选一："
        "(a) 后台武装 watcher（bash scripts/os-watch.sh <session_id> <team_id> &）"
        "再停，让活干完时叫醒你；(b) 若确要收工，回复用户/显式说停即放行。",
    )


# ---------------------------------------------------------------------------
# I/O 辅助（全部防御式：失败返回安全默认，绝不抛）
# ---------------------------------------------------------------------------
def _api_base() -> str:
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(_PORT_FILE.read_text().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError, OSError):
        return "http://localhost:8000"


def _safe_sid(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "unknown")


def _state_path(session_id: str) -> Path:
    return _WAKE_STATE_DIR / f"{_safe_sid(session_id)}.json"


def _armed_path(session_id: str) -> Path:
    # watcher(os-watch.sh) 独占写此文件；guard 只读。二者各占独立文件 => 无读改写竞态。
    return _WAKE_STATE_DIR / f"{_safe_sid(session_id)}.armed"


def _watcher_armed(session_id: str) -> bool:
    """读 os-watch.sh 维护的 <sid>.armed 心跳（epoch 秒），未过期即视为已武装。"""
    try:
        armed_until = float(_armed_path(session_id).read_text(encoding="utf-8").strip())
        return armed_until > time.time()
    except Exception:  # noqa: BLE001
        return False


def _load_state(session_id: str) -> dict:
    try:
        return json.loads(_state_path(session_id).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(session_id: str, state: dict) -> None:
    try:
        _WAKE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        _state_path(session_id).write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass  # marker persistence is best-effort


def _last_user_text(transcript_path: str) -> str:
    """尾读 transcript，取最后一条真实 user 消息文本（跳过 tool_result / 空）。"""
    try:
        if not transcript_path:
            return ""
        p = Path(transcript_path)
        if not p.is_file():
            return ""
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > 200_000:
                f.seek(size - 200_000)
            data = f.read().decode("utf-8", errors="replace")
        last = ""
        for line in data.splitlines():
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001 — seek 截断首行等
                continue
            if d.get("type") != "user":
                continue
            msg = d.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                last = content
            elif isinstance(content, list):
                parts = [
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                ]
                if parts:
                    last = " ".join(parts)
        return last
    except Exception:  # noqa: BLE001
        return ""


def _query_actionable(session_id: str, team_id: str) -> dict:
    """查 /api/wake/actionable。失败返回 {}（→ 视为无活在飞，fail-open 放行）。"""
    try:
        base = _api_base()
        qs = f"session_id={session_id}"
        if team_id:
            qs += f"&team_id={team_id}"
        req = urllib.request.Request(f"{base}/api/wake/actionable?{qs}", method="GET")
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# 模式：UserPromptSubmit -> 写 manual 标记（用户在场）
# ---------------------------------------------------------------------------
def _handle_user_prompt(payload: dict) -> None:
    session_id = payload.get("session_id", "")
    state = _load_state(session_id)
    state["manual_until"] = time.time() + MANUAL_TTL
    # 用户回来了：重置 block 计数
    state["block_count"] = 0
    _save_state(session_id, state)
    sys.exit(0)


# ---------------------------------------------------------------------------
# 模式：Stop -> 判停
# ---------------------------------------------------------------------------
def _handle_stop(payload: dict) -> None:
    session_id = payload.get("session_id", "")
    stop_hook_active = bool(payload.get("stop_hook_active"))

    # 递归防护最优先，且这条不需要任何外部 I/O
    if stop_hook_active:
        sys.exit(0)

    state = _load_state(session_id)
    now = time.time()

    manual_active = float(state.get("manual_until", 0) or 0) > now
    watcher_armed = _watcher_armed(session_id)

    # 连续 block 计数（超窗归零）
    last_block_at = float(state.get("last_block_at", 0) or 0)
    block_count = int(state.get("block_count", 0) or 0)
    if now - last_block_at > BLOCK_RESET_AFTER:
        block_count = 0

    last_user = _last_user_text(payload.get("transcript_path", ""))
    stop_keyword_hit = bool(STOP_KEYWORDS.search(last_user)) if last_user else False

    # 只有在前几层豁免都不成立时才查端点（省一次 HTTP）
    work_in_flight = False
    if not (manual_active or stop_keyword_hit):
        verdict = _query_actionable(session_id, payload.get("team_id", ""))
        work_in_flight = (
            int(verdict.get("busy_agents", 0) or 0) > 0
            or int(verdict.get("live_runs", 0) or 0) > 0
        )

    action, branch, reason = decide(
        stop_hook_active=stop_hook_active,
        manual_active=manual_active,
        stop_keyword_hit=stop_keyword_hit,
        work_in_flight=work_in_flight,
        watcher_armed=watcher_armed,
        block_count=block_count,
    )

    if action == "block":
        state["block_count"] = block_count + 1
        state["last_block_at"] = now
        _save_state(session_id, state)
        print(json.dumps({"decision": "block", "reason": reason}))
        sys.exit(0)

    # allow：若曾计数，收敛归零
    if block_count:
        state["block_count"] = 0
        _save_state(session_id, state)
    sys.exit(0)


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 — fail-open
        sys.exit(0)

    mode = sys.argv[1] if len(sys.argv) > 1 else "stop"
    try:
        if mode == "user-prompt":
            _handle_user_prompt(payload)
        else:
            _handle_stop(payload)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — fail-open: a hook fault must never block the session
        sys.exit(0)


if __name__ == "__main__":
    main()
