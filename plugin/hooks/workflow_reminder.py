#!/usr/bin/env python3
"""工作流提醒 -- 轻量PreToolUse/PostToolUse hook.

只读写本地state文件并输出提醒到stdout，不做HTTP调用，
目标是在100ms内完成以避免CC hook超时。
"""

import json
import os
import re
import sys
import time
from pathlib import Path

_SUPERVISOR_STATE_DIR = os.path.join(
    os.path.expanduser("~"), ".claude", "data", "ai-team-os"
)
_SUPERVISOR_STATE_FILE = os.path.join(_SUPERVISOR_STATE_DIR, "supervisor-state.json")

# Leader委派检查的阈值
_LEADER_CONSECUTIVE_THRESHOLD = 8

# TeamCreate后等待常驻成员的工具调用阈值
_TEAM_WITHOUT_MEMBERS_THRESHOLD = 5

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
        sys.stderr.write(
            "[OS BLOCK] Agent实施任务必须带team_name参数。"
            "规则B0.4要求：添加成员必须用Agent(team_name=...)创建CC团队成员。"
            "请添加team_name参数后重试。"
        )
        sys.exit(2)

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


def _team_has_required_roles(team_name: str) -> bool:
    """检查团队config中是否已有QA和bug-fixer角色。"""
    import json as _json
    config_path = Path.home() / ".claude" / "teams" / team_name / "config.json"
    if not config_path.exists():
        return False
    try:
        data = _json.loads(config_path.read_text(encoding="utf-8"))
        members = data.get("members", [])
        names = [m.get("name", "").lower() for m in members]
        has_qa = any("qa" in n for n in names)
        has_fixer = any("bug-fixer" in n or "fixer" in n for n in names)
        return has_qa and has_fixer
    except Exception:
        return False


def _check_team_has_permanent_members(event_data: dict, state: dict) -> str | None:
    """持续检查当前active团队是否有常驻成员（状态直查法）。

    不依赖TeamCreate事件触发，而是每20次工具调用主动扫描
    ~/.claude/teams/ 下所有团队config，检查是否缺少QA和bug-fixer。
    """
    event_name = event_data.get("hook_event_name", "")
    if event_name != "PreToolUse":
        return None

    # 节流：每20次工具调用检查一次
    check_count = state.get("permanent_member_check_count", 0) + 1
    state["permanent_member_check_count"] = check_count
    if check_count % 20 != 0:
        return None

    # 扫描所有团队config，找到有成员但缺常驻角色的团队
    teams_dir = Path.home() / ".claude" / "teams"
    if not teams_dir.exists():
        return None

    try:
        for team_dir in teams_dir.iterdir():
            if not team_dir.is_dir():
                continue
            config_path = team_dir / "config.json"
            if not config_path.exists():
                continue
            data = json.loads(config_path.read_text(encoding="utf-8"))
            members = data.get("members", [])
            if len(members) < 2:
                continue  # 团队刚创建，还没开始组建
            if not _team_has_required_roles(team_dir.name):
                return (
                    f"[AI Team OS] B0.10提醒：团队「{team_dir.name}」缺少常驻成员"
                    "（QA+bug-fixer）。请创建以确保质量保障。"
                )
    except Exception:
        pass

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

    # 5. TeamCreate后检查是否已有active团队
    if tool_name == "TeamCreate":
        try:
            import urllib.request
            api_url = os.environ.get("AITEAM_API_URL", "http://localhost:8000")
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            active_teams = [t for t in teams if t.get("status") == "active"]
            # 新创建的团队也会是active，所以检查是否有>1个active团队
            if len(active_teams) > 1:
                other = active_teams[0].get("name", "未知")
                warnings.append(
                    f"[OS提醒] 已存在活跃团队「{other}」。"
                    "建议：①在已有团队中添加成员 ②先关闭旧团队再创建新的"
                )
        except Exception:
            pass  # API不可用时静默跳过

    # 6. SendMessage后检查并行任务分配情况（含空闲Agent+pending任务匹配建议）
    if tool_name == "SendMessage":
        try:
            import urllib.request
            api_url = os.environ.get("AITEAM_API_URL", "http://localhost:8000")
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            active_teams = [t for t in teams if t.get("status") == "active"]
            if active_teams:
                team_id = active_teams[0].get("id", "")
                if team_id:
                    req2 = urllib.request.Request(
                        f"{api_url}/api/teams/{team_id}/agents", method="GET",
                    )
                    with urllib.request.urlopen(req2, timeout=2) as resp2:
                        agents = json.loads(resp2.read().decode("utf-8")).get("data", [])
                    non_leader_agents = [a for a in agents if a.get("role") != "leader"]
                    busy_count = sum(1 for a in non_leader_agents if a.get("status") == "busy")
                    idle_agents = [
                        a for a in non_leader_agents
                        if a.get("status") in ("waiting", "offline")
                    ]
                    if busy_count < 3 and idle_agents:
                        # 尝试获取pending任务做匹配建议
                        match_hints: list[str] = []
                        try:
                            req3 = urllib.request.Request(
                                f"{api_url}/api/teams/{team_id}/tasks", method="GET",
                            )
                            with urllib.request.urlopen(req3, timeout=2) as resp3:
                                tasks = json.loads(resp3.read().decode("utf-8")).get("data", [])
                            pending_tasks = [
                                t for t in tasks
                                if t.get("status") in ("pending",) and not t.get("assigned_to")
                            ]
                            for idle in idle_agents[:3]:  # 最多显示3个空闲Agent
                                agent_role = (idle.get("role") or idle.get("name") or "").lower()
                                agent_name = idle.get("name", "?")
                                # 找tags与agent role有交集的任务
                                matched = next(
                                    (
                                        t for t in pending_tasks
                                        if any(
                                            tag.lower() in agent_role or agent_role in tag.lower()
                                            for tag in (t.get("tags") or [])
                                        )
                                    ),
                                    pending_tasks[0] if pending_tasks else None,
                                )
                                if matched:
                                    tags_str = ",".join(matched.get("tags") or [])
                                    hint = (
                                        f"空闲Agent: {agent_name}({idle.get('role','')}), "
                                        f"待办: {matched['title']}"
                                        + (f"(tags:{tags_str})" if tags_str else "")
                                        + " → 建议分配"
                                    )
                                    match_hints.append(hint)
                        except Exception:
                            pass
                        if match_hints:
                            warnings.append(
                                f"[OS提醒] 当前仅{busy_count}个成员在工作，有空闲Agent可并行分配：\n"
                                + "\n".join(f"  • {h}" for h in match_hints)
                            )
                        else:
                            warnings.append(
                                f"[OS提醒] 当前仅{busy_count}个成员在工作。"
                                "可以并行分配更多任务给空闲成员，提高效率"
                            )
        except Exception:
            pass  # API不可用时静默跳过

    # 7. 距上次查看任务墙超过15分钟
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

    # 9. Handoff提醒：Agent汇报完成时，提醒分配后续任务
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["完成", "completed", "done", "finished", "汇报"]
        is_completion = any(kw in input_str.lower() for kw in completion_keywords)
        # 排除shutdown消息（规则3已处理）
        is_shutdown = "shutdown" in input_str.lower()
        if is_completion and not is_shutdown:
            try:
                import urllib.request
                api_url = os.environ.get("AITEAM_API_URL", "http://localhost:8000")
                req = urllib.request.Request(f"{api_url}/api/teams", method="GET")
                with urllib.request.urlopen(req, timeout=2) as resp:
                    teams = json.loads(resp.read().decode("utf-8")).get("data", [])
                active_teams = [t for t in teams if t.get("status") == "active"]
                if active_teams:
                    team_id = active_teams[0].get("id", "")
                    if team_id:
                        req2 = urllib.request.Request(
                            f"{api_url}/api/teams/{team_id}/tasks", method="GET",
                        )
                        with urllib.request.urlopen(req2, timeout=2) as resp2:
                            tasks = json.loads(resp2.read().decode("utf-8")).get("data", [])
                        pending = [
                            t for t in tasks
                            if t.get("status") == "pending" and not t.get("assigned_to")
                        ]
                        if pending:
                            pending_titles = "、".join(
                                t["title"] for t in pending[:3]
                            )
                            more = f"等{len(pending)}个" if len(pending) > 3 else ""
                            warnings.append(
                                f"[OS提醒] Agent已完成汇报，仍有待分配任务：{pending_titles}{more}。"
                                "→ 是否分配给空闲成员继续推进？"
                            )
            except Exception:
                pass

    # 10. meeting_create后提醒通知参与者并使用skill
    if tool_name in ("meeting_create", "mcp__ai-team-os__meeting_create"):
        warnings.append(
            "[OS提醒] 会议已创建。请：1)逐一通知参与者(SendMessage)告知meeting_id "
            "2)建议参与者使用 /meeting-participate skill参加讨论 "
            "3)主持人使用 /meeting-facilitate skill引导讨论"
        )

    # 11. meeting_conclude后提醒行动项上墙
    if tool_name in ("meeting_conclude", "mcp__ai-team-os__meeting_conclude"):
        warnings.append(
            "[OS提醒] 会议已结束。请将讨论结论中的行动项转化为任务墙任务。"
            "→ 使用 task_create 添加任务，确保口头承诺不遗忘"
        )

    # 12. 任务标记完成时提醒QA验收
    if tool_name in ("task_status", "mcp__ai-team-os__task_status"):
        input_str = str(event_data.get("tool_input", {}))
        if "completed" in input_str.lower():
            warnings.append(
                "[OS提醒] 任务标记完成。是否涉及系统行为变更？"
                "→ 如是，请通知QA Agent进行验收测试"
            )

    # 13. 瓶颈检测：所有任务完成或多任务blocked时提醒开会
    # 每50次工具调用检查一次（节流）
    bottleneck_count = state.get("bottleneck_check_count", 0) + 1
    state["bottleneck_check_count"] = bottleneck_count
    if bottleneck_count % 50 == 0:
        try:
            import urllib.request
            api_url = os.environ.get("AITEAM_API_URL", "http://localhost:8000")
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            for t in teams:
                if t.get("status") != "active":
                    continue
                tid = t["id"]
                req2 = urllib.request.Request(f"{api_url}/api/teams/{tid}/tasks")
                with urllib.request.urlopen(req2, timeout=2) as resp2:
                    tasks = json.loads(resp2.read().decode("utf-8")).get("data", [])
                pending = [tk for tk in tasks if tk.get("status") == "pending"]
                running = [tk for tk in tasks if tk.get("status") == "running"]
                blocked = [tk for tk in tasks if tk.get("status") == "blocked"]
                if not pending and not running and not blocked:
                    warnings.append("[OS提醒] 所有任务已完成，建议组织方向讨论会议确定下一步")
                elif len(blocked) > len(running) and len(blocked) >= 2:
                    warnings.append(f"[OS提醒] {len(blocked)}个任务阻塞中，建议组织协调会议疏通")
        except Exception:
            pass

    # 14. 汇报格式校验
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["完成", "completed", "done", "finished", "汇报"]
        if any(kw in input_str.lower() for kw in completion_keywords) and "shutdown" not in input_str.lower():
            required_fields = ["完成内容", "修改文件", "测试结果"]
            missing = [f for f in required_fields if f not in input_str]
            if missing and len(input_str) > 100:  # 只检查较长的汇报
                warnings.append(
                    f"[OS提醒] 汇报可能缺少标准字段：{', '.join(missing)}。"
                    "标准格式：完成内容/修改文件/测试结果/建议任务状态/建议memo"
                )

    # ── 安全护栏规则 ──────────────────────────────────────────

    tool_input = event_data.get("tool_input", {})

    # S1: 危险命令拦截（Bash）
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        cmd_lower = cmd.lower()
        # 递归删除根目录/主目录 → exit(2)硬阻断
        if re.search(r"rm\s+-[^\s]*r[^\s]*\s+(/|~/|~)(\s|$|[^a-zA-Z])", cmd):
            sys.stderr.write("[OS BLOCK] 危险：禁止递归删除根目录/主目录")
            sys.exit(2)
        # 递归删除其他危险目标 → warning
        if re.search(r"rm\s+-[^\s]*r[^\s]*\s+\*", cmd):
            warnings.append(
                "[安全] 危险：检测到递归删除通配符命令，请确认操作目标"
            )
        # 数据库破坏性操作
        if re.search(r"\b(DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE)\b", cmd, re.IGNORECASE):
            warnings.append(
                "[安全] 危险：检测到数据库破坏性操作（DROP/TRUNCATE），请确认"
            )
        # force push
        if "push" in cmd_lower and "--force" in cmd_lower:
            warnings.append(
                "[安全] 注意：检测到force push，可能覆盖远程历史"
            )
        # 过度开放权限
        if "chmod 777" in cmd:
            warnings.append(
                "[安全] 安全：过度开放的文件权限（chmod 777），建议使用更严格的权限"
            )
        # S3: 敏感文件提交拦截（git add） → exit(2)硬阻断
        if "git add" in cmd_lower:
            block_patterns = [".env", "id_rsa", ".pem", ".key"]
            for pat in block_patterns:
                if pat in cmd_lower:
                    sys.stderr.write(f"[OS BLOCK] 禁止提交敏感文件（{pat}）")
                    sys.exit(2)
            # credentials保持warning（文件名歧义较大，不一定是密钥文件）
            if "credentials" in cmd_lower:
                warnings.append(
                    "[安全] 安全：检测到尝试提交credentials文件，"
                    "请确认该文件不包含密钥信息且已在.gitignore中"
                )

    # S2: 敏感信息检测（Write/Edit）
    if tool_name in ("Write", "Edit"):
        # 获取要写入的内容
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
        # 硬编码密钥检测
        if re.search(
            r"(password|secret|api_key|token)\s*=\s*['\"][^'\"]+['\"]",
            content,
            re.IGNORECASE,
        ):
            warnings.append(
                "[安全] 安全：检测到可能的硬编码密钥，建议使用环境变量"
            )
        # .env文件写入提醒
        file_path = tool_input.get("file_path", "")
        if file_path.endswith(".env") or "/.env" in file_path or "\\.env" in file_path:
            warnings.append(
                "[安全] 注意：.env文件不应提交到版本库，请确认.gitignore包含.env"
            )

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
