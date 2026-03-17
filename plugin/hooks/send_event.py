#!/usr/bin/env python3
"""AI Team OS — Claude Code Hook事件发送器

CC hook触发时执行此脚本，将事件转发到OS API。
用法: python send_event.py <EventType> (从stdin读取JSON)

注意: 此脚本只使用Python标准库，不依赖任何第三方包，
因为它可能在任何Python环境中被CC直接调用。
"""

import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("AITEAM_API_URL", "http://localhost:8000")

# 大字段截断限制（防止SubagentStop等事件payload过大导致超时）
MAX_FIELD_LEN = 500
MAX_PAYLOAD_BYTES = 32_768  # 整体payload上限32KB，超过则丢弃非必要字段
LARGE_FIELDS = {"last_assistant_message", "agent_transcript_path", "transcript_path"}
# 必须保留的字段（即使payload超限也不丢弃）
ESSENTIAL_FIELDS = {"hook_event_name", "session_id", "tool_name", "tool_input", "cc_team_name"}


def _trim_payload(payload: dict) -> dict:
    """截断过大的字段，防止HTTP超时。

    两级保护:
    1. 已知大字段截断到 MAX_FIELD_LEN (500字符)
    2. 整体超过50KB时，所有字符串字段截断到200字符
    """
    trimmed = {}
    for k, v in payload.items():
        if k in LARGE_FIELDS:
            if isinstance(v, str) and len(v) > MAX_FIELD_LEN:
                trimmed[k] = v[:MAX_FIELD_LEN] + "...(truncated)"
            elif isinstance(v, dict):
                trimmed[k] = str(v)[:MAX_FIELD_LEN] + "...(truncated)"
            else:
                trimmed[k] = v
        elif k == "tool_response" and isinstance(v, dict):
            # 截断工具输出但保留结构
            tr = {}
            for rk, rv in v.items():
                if isinstance(rv, str) and len(rv) > MAX_FIELD_LEN:
                    tr[rk] = rv[:MAX_FIELD_LEN] + "...(truncated)"
                else:
                    tr[rk] = rv
            trimmed[k] = tr
        else:
            trimmed[k] = v

    # 整体大小检查：超过50KB则递归截断所有字符串字段
    payload_str = json.dumps(trimmed)
    if len(payload_str) > 50_000:
        for k, v in trimmed.items():
            if isinstance(v, str) and len(v) > 200:
                trimmed[k] = v[:200] + "...(truncated)"

    return trimmed


def _resolve_cc_team_name(session_id: str, agent_name: str = "") -> str | None:
    """通过agent_name在CC团队配置中查找所属团队名称。

    策略1: 按members.name精确匹配（不依赖sessionId，跨session可靠）
    策略2: fallback按leadSessionId匹配
    只使用标准库，静默处理所有异常。
    """
    teams_dir = os.path.join(os.path.expanduser("~"), ".claude", "teams")
    try:
        config_files = glob.glob(os.path.join(teams_dir, "*", "config.json"))
    except OSError:
        return None

    # 策略1：按agent_name在members列表中查找（跨session可靠）
    if agent_name:
        for config_path in config_files:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                for m in config.get("members", []):
                    if m.get("name", "") == agent_name:
                        return config.get("name")
            except (json.JSONDecodeError, OSError, KeyError):
                continue

    # 策略2：fallback按leadSessionId
    if session_id:
        for config_path in config_files:
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if config.get("leadSessionId") == session_id:
                    return config.get("name")
            except (json.JSONDecodeError, OSError, KeyError):
                continue

    return None


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


# ---------------------------------------------------------------------------
# Supervisor state 文件路径（固定位置）
# ---------------------------------------------------------------------------
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
        pass  # 静默失败，不影响正常工具调用


def _check_leader_doing_too_much(event_data: dict) -> str | None:
    """检查Leader是否连续执行过多工具调用而未委派。

    当连续非委派工具调用次数超过阈值时返回warning文本。
    当Leader调用Agent/TeamCreate/SendMessage时重置计数器。
    """
    tool_name = event_data.get("tool_name", "")
    if not tool_name:
        return None

    state = _load_supervisor_state()
    consecutive = state.get("leader_consecutive_calls", 0)

    if tool_name in _DELEGATION_TOOLS:
        # 委派动作，重置计数器
        state["leader_consecutive_calls"] = 0
        _save_supervisor_state(state)
        return None

    # 非委派工具调用，递增计数器
    consecutive += 1
    state["leader_consecutive_calls"] = consecutive
    _save_supervisor_state(state)

    if consecutive > _LEADER_CONSECUTIVE_THRESHOLD:
        return (
            f"[AI Team OS] B0.9提醒：Leader已连续执行{consecutive}次工具调用。"
            "是否应该委派给团队成员？"
        )

    return None


def _check_workflow_reminders(event_data: dict, state: dict) -> list[str]:
    """基于工具调用模式生成工作流提醒."""
    tool_name = event_data.get("tool_name", "")
    warnings = []
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
            if now - last_memo > 300:  # 5分钟冷却
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

    # 4. 距上次查看任务墙超过15分钟
    if tool_name == "taskwall_view":
        state["last_taskwall_view"] = now
    else:
        last_view = state.get("last_taskwall_view", 0)
        if last_view > 0 and (now - last_view) > 900:  # 15分钟
            minutes = int((now - last_view) / 60)
            warnings.append(
                f"[OS提醒] 距上次查看任务墙已{minutes}分钟。"
                "→ 建议 taskwall_view 查看当前任务状态"
            )
            state["last_taskwall_view"] = now  # 提醒后重置，避免每次都提醒

    return warnings


def _check_team_has_permanent_members(event_data: dict) -> str | None:
    """检查TeamCreate后是否及时添加常驻成员。

    在PostToolUse中检测到TeamCreate完成时，设置标记。
    在后续PreToolUse中，如果连续多次未看到Agent调用，输出提醒。
    """
    tool_name = event_data.get("tool_name", "")
    event_name = event_data.get("hook_event_name", "")
    state = _load_supervisor_state()

    if event_name == "PostToolUse" and tool_name == "TeamCreate":
        # TeamCreate刚完成，开始监控
        state["team_created_waiting"] = True
        state["calls_since_team_create"] = 0
        _save_supervisor_state(state)
        return None

    if not state.get("team_created_waiting", False):
        return None

    if event_name != "PreToolUse":
        return None

    # PreToolUse阶段，检查是否在创建成员
    if tool_name == "Agent":
        # Leader正在添加成员，重置监控
        state["team_created_waiting"] = False
        state["calls_since_team_create"] = 0
        _save_supervisor_state(state)
        return None

    # 非Agent调用，递增计数
    calls_since = state.get("calls_since_team_create", 0) + 1
    state["calls_since_team_create"] = calls_since
    _save_supervisor_state(state)

    if calls_since >= _TEAM_WITHOUT_MEMBERS_THRESHOLD:
        # 已提醒过，重置（避免反复提醒）
        state["team_created_waiting"] = False
        state["calls_since_team_create"] = 0
        _save_supervisor_state(state)
        return (
            "[AI Team OS] B0.10提醒：团队已创建但尚未添加常驻成员（QA+bug-fixer）。"
            "请立即创建。"
        )

    return None


def main() -> None:
    try:
        # Windows下stdin默认用GBK解码，CC发送的是UTF-8，强制用buffer读取
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            return

        payload = json.loads(raw)

        # CC hook payload不自带事件类型名，通过命令行参数注入
        if len(sys.argv) > 1 and "hook_event_name" not in payload:
            payload["hook_event_name"] = sys.argv[1]

        # SubagentStart/SubagentStop: 注入CC团队名称
        event_name = payload.get("hook_event_name", "")
        if event_name in ("SubagentStart", "SubagentStop") and "cc_team_name" not in payload:
            session_id = payload.get("session_id", "")
            agent_name = payload.get("agent_type", "")
            cc_team = _resolve_cc_team_name(session_id, agent_name)
            if cc_team:
                payload["cc_team_name"] = cc_team

        # 行为检查：收集所有warnings
        warnings = []
        supervisor_state = _load_supervisor_state()

        if event_name == "PreToolUse":
            w = _check_agent_team_name(payload)
            if w:
                warnings.append(w)
            w = _check_leader_doing_too_much(payload)
            if w:
                warnings.append(w)
            w = _check_team_has_permanent_members(payload)
            if w:
                warnings.append(w)

        if event_name == "PostToolUse":
            w = _check_team_has_permanent_members(payload)
            if w:
                warnings.append(w)

        # 工作流提醒（PreToolUse和PostToolUse都检查）
        if event_name in ("PreToolUse", "PostToolUse"):
            wf_warnings = _check_workflow_reminders(payload, supervisor_state)
            warnings.extend(wf_warnings)
            _save_supervisor_state(supervisor_state)

        for w in warnings:
            print(w)

        # 截断大字段
        payload = _trim_payload(payload)

        # 整体payload大小检查：超过上限则只保留必要字段
        data = json.dumps(payload).encode("utf-8")
        if len(data) > MAX_PAYLOAD_BYTES:
            stripped = {k: v for k, v in payload.items() if k in ESSENTIAL_FIELDS}
            stripped["_stripped"] = True
            stripped["_original_size"] = len(data)
            event_name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
            sys.stderr.write(
                f"[aiteam-hook] {event_name}: payload too large "
                f"({len(data)} bytes > {MAX_PAYLOAD_BYTES}), stripped to essentials\n"
            )
            data = json.dumps(stripped).encode("utf-8")
        req = urllib.request.Request(
            f"{API_URL}/api/hooks/event",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=1.5) as resp:
            result = json.loads(resp.read().decode())
            # 返回决策给CC（对于PreToolUse等需要决策的hook）
            if "decision" in result:
                print(json.dumps(result))

    except urllib.error.URLError as e:
        # OS服务未启动，输出到stderr方便调试（不阻塞CC）
        event_name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
        sys.stderr.write(f"[aiteam-hook] {event_name}: API unreachable - {e}\n")
    except Exception as e:
        # 其他错误也记录到stderr
        event_name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
        sys.stderr.write(f"[aiteam-hook] {event_name}: error - {e}\n")


if __name__ == "__main__":
    main()
