#!/usr/bin/env python3
"""工作流提醒 -- 轻量PreToolUse/PostToolUse hook.

只读写本地state文件并输出提醒到stdout，不做HTTP调用，
目标是在100ms内完成以避免CC hook超时。
"""

import json
import os
import sys
import time

_SUPERVISOR_STATE_DIR = os.path.join(
    os.path.expanduser("~"), ".claude", "data", "ai-team-os"
)
_SUPERVISOR_STATE_FILE = os.path.join(_SUPERVISOR_STATE_DIR, "supervisor-state.json")

# Leader委派检查的阈值
_LEADER_CONSECUTIVE_THRESHOLD = 8

# TeamCreate后等待Agent调用的阈值
_TEAM_WITHOUT_MEMBERS_THRESHOLD = 3

# 视为"委派"动作的工具名（调用这些工具会重置计数器）
_DELEGATION_TOOLS = {"Agent", "TeamCreate", "SendMessage"}


def _load_supervisor_state() -> dict:
    """加载supervisor状态文件，不存在或损坏时返回默认值。"""
    try:
        with open(_SUPERVISOR_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_supervisor_state(state: dict) -> None:
    """保存supervisor状态到文件。"""
    try:
        os.makedirs(_SUPERVISOR_STATE_DIR, exist_ok=True)
        with open(_SUPERVISOR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError:
        pass


def _check_agent_team_name(event_data: dict) -> str | None:
    """检查Agent工具调用是否带team_name。返回warning文本或None。"""
    tool_name = event_data.get("tool_name", "")
    if tool_name != "Agent":
        return None

    tool_input = json.dumps(event_data.get("tool_input", {}), ensure_ascii=False).lower()

    # 只读类型的subagent不需要team_name
    readonly_types = ["explore", "plan", "code-reviewer", "security-reviewer", "python-reviewer"]
    for rt in readonly_types:
        if rt in tool_input:
            return None

    # 检查是否有team_name
    if "team_name" in tool_input:
        return None

    # 检查是否包含实施关键词
    impl_keywords = [
        "write", "create", "implement", "edit", "fix", "build",
        "开发", "实现", "修复", "编写", "创建", "构建",
    ]
    has_impl = any(kw in tool_input for kw in impl_keywords)

    if has_impl:
        return (
            "[AI Team OS WARNING] 检测到Agent实施任务但未使用team_name参数。"
            "规则B0.4要求：添加成员必须用Agent(team_name=...)创建CC团队成员。"
            "请添加team_name参数。"
        )

    return None


def _check_leader_doing_too_much(event_data: dict, state: dict) -> str | None:
    """检查Leader是否连续执行过多工具调用而未委派。

    当连续非委派工具调用次数超过阈值时返回warning文本。
    当Leader调用Agent/TeamCreate/SendMessage时重置计数器。
    """
    tool_name = event_data.get("tool_name", "")
    if not tool_name:
        return None

    consecutive = state.get("leader_consecutive_calls", 0)

    if tool_name in _DELEGATION_TOOLS:
        state["leader_consecutive_calls"] = 0
        return None

    consecutive += 1
    state["leader_consecutive_calls"] = consecutive

    if consecutive > _LEADER_CONSECUTIVE_THRESHOLD:
        return (
            f"[AI Team OS] B0.9提醒：Leader已连续执行{consecutive}次工具调用。"
            "是否应该委派给团队成员？"
        )

    return None


def _check_team_has_permanent_members(event_data: dict, state: dict) -> str | None:
    """检查TeamCreate后是否及时添加常驻成员。"""
    tool_name = event_data.get("tool_name", "")
    event_name = event_data.get("hook_event_name", "")

    if event_name == "PostToolUse" and tool_name == "TeamCreate":
        state["team_created_waiting"] = True
        state["calls_since_team_create"] = 0
        return None

    if not state.get("team_created_waiting", False):
        return None

    if event_name != "PreToolUse":
        return None

    if tool_name == "Agent":
        state["team_created_waiting"] = False
        state["calls_since_team_create"] = 0
        return None

    calls_since = state.get("calls_since_team_create", 0) + 1
    state["calls_since_team_create"] = calls_since

    if calls_since >= _TEAM_WITHOUT_MEMBERS_THRESHOLD:
        state["team_created_waiting"] = False
        state["calls_since_team_create"] = 0
        return (
            "[AI Team OS] B0.10提醒：团队已创建但尚未添加常驻成员（QA+bug-fixer）。"
            "请立即创建。"
        )

    return None


def _check_workflow_reminders(event_data: dict, state: dict) -> list[str]:
    """基于工具调用模式生成工作流提醒."""
    tool_name = event_data.get("tool_name", "")
    warnings: list[str] = []
    now = time.time()

    # 1. TeamCreate后提醒：任务是否上墙
    if tool_name == "TeamCreate":
        warnings.append(
            "[OS提醒] 新团队已创建。此工作方向是否已在任务墙创建对应任务？"
            "→ 使用 task_run 或 task_create 添加任务"
        )

    # 2. Agent(team_name)创建前提醒：是否有历史memo
    if tool_name == "Agent":
        input_str = str(event_data.get("tool_input", {}))
        if "team_name" in input_str:
            last_memo = state.get("last_memo_reminder", 0)
            if now - last_memo > 300:
                warnings.append(
                    "[OS提醒] 分配新成员前：此任务是否有历史工作记录？"
                    "→ 建议先 task_memo_read 查看前置工作"
                )
                state["last_memo_reminder"] = now

    # 3. SendMessage(shutdown)前提醒：任务完成了吗
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        if "shutdown" in input_str.lower():
            warnings.append(
                "[OS提醒] 关闭Agent前：此Agent的任务是否已标记完成？"
                "→ 建议更新任务状态并添加总结memo (task_memo_add type=summary)"
            )

    # 4. TeamDelete时通知OS关闭对应团队
    if tool_name == "TeamDelete":
        try:
            import urllib.request
            api_url = os.environ.get("AITEAM_API_URL", "http://localhost:8000")
            # 关闭所有active团队（TeamDelete意味着当前团队工作结束）
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            for t in teams:
                if t.get("status") == "active":
                    close_req = urllib.request.Request(
                        f"{api_url}/api/teams/{t['id']}",
                        data=json.dumps({"status": "completed"}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="PUT",
                    )
                    urllib.request.urlopen(close_req, timeout=2)
        except Exception:
            pass  # 静默处理

    # 5. 距上次查看任务墙超过15分钟
    if tool_name in ("taskwall_view", "mcp__ai-team-os__taskwall_view"):
        state["last_taskwall_view"] = now
    else:
        last_view = state.get("last_taskwall_view", 0)
        if last_view > 0 and (now - last_view) > 900:
            minutes = int((now - last_view) / 60)
            warnings.append(
                f"[OS提醒] 距上次查看任务墙已{minutes}分钟。"
                "→ 建议 taskwall_view 查看当前任务状态"
            )
            state["last_taskwall_view"] = now

    return warnings


def main() -> None:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception:
        return

    # CC hook payload不自带事件类型名，通过命令行参数注入
    if len(sys.argv) > 1 and "hook_event_name" not in payload:
        payload["hook_event_name"] = sys.argv[1]

    event_name = payload.get("hook_event_name", "")
    state = _load_supervisor_state()
    warnings: list[str] = []

    if event_name == "PreToolUse":
        w = _check_agent_team_name(payload)
        if w:
            warnings.append(w)
        w = _check_leader_doing_too_much(payload, state)
        if w:
            warnings.append(w)
        w = _check_team_has_permanent_members(payload, state)
        if w:
            warnings.append(w)

    if event_name == "PostToolUse":
        w = _check_team_has_permanent_members(payload, state)
        if w:
            warnings.append(w)

    # 工作流提醒（PreToolUse和PostToolUse都检查）
    if event_name in ("PreToolUse", "PostToolUse"):
        wf_warnings = _check_workflow_reminders(payload, state)
        warnings.extend(wf_warnings)

    _save_supervisor_state(state)

    # PreToolUse/PostToolUse hooks通过hookSpecificOutput注入文本到对话
    output = {"hookSpecificOutput": {"hookEventName": event_name}}
    if event_name == "PreToolUse":
        output["hookSpecificOutput"]["permissionDecision"] = "allow"
    if warnings:
        output["hookSpecificOutput"]["additionalContext"] = "\n".join(warnings)
    sys.stdout.write(json.dumps(output))


if __name__ == "__main__":
    main()
