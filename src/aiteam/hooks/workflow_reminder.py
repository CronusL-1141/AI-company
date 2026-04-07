#!/usr/bin/env python3
"""Workflow reminder — lightweight PreToolUse/PostToolUse hook.

Only reads/writes local state files and outputs reminders to stdout, no HTTP calls.
Goal is to complete within 100ms to avoid CC hook timeout.
Usage: python -m aiteam.hooks.workflow_reminder <PreToolUse|PostToolUse>
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

_SUPERVISOR_STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os")
_SUPERVISOR_STATE_FILE = os.path.join(_SUPERVISOR_STATE_DIR, "supervisor-state.json")
_PORT_FILE = os.path.join(_SUPERVISOR_STATE_DIR, "api_port.txt")


def _get_api_url() -> str:
    """Return current API URL. AITEAM_API_URL env var takes highest priority."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"

# Threshold for Leader delegation check
_LEADER_CONSECUTIVE_THRESHOLD = 8

# Tool names considered "delegation" actions (calling these resets the counter)
_DELEGATION_TOOLS = {"Agent", "TeamCreate", "SendMessage"}

# Infrastructure tools only Leader can do — don't count toward B0.9 threshold
_INFRA_TOOLS = {
    # MCP task management (Leader managing task wall)
    "mcp__ai-team-os__task_create", "mcp__ai-team-os__task_update",
    "mcp__ai-team-os__taskwall_view", "mcp__ai-team-os__task_status",
    # MCP team/project management
    "mcp__ai-team-os__team_create", "mcp__ai-team-os__team_list",
    "mcp__ai-team-os__agent_template_recommend", "mcp__ai-team-os__agent_template_list",
    # MCP meeting management
    "mcp__ai-team-os__meeting_create", "mcp__ai-team-os__meeting_conclude",
}


_API_TIMEOUT = 2


def _api_call(method: str, path: str, body: dict | None = None, project_id: str | None = None) -> dict | None:
    """Make a JSON API call to the OS backend. Returns parsed response or None on failure."""
    api_url = _get_api_url()
    url = f"{api_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if project_id:
        headers["X-Project-Id"] = project_id
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


_PROJECT_ID_CACHE_TTL = 300  # 5 minutes


def _resolve_project_id() -> str | None:
    """Resolve current project ID from cwd via OS API, with file-based cache (TTL 5 min)."""
    # Check cache first
    state = _load_supervisor_state()
    cached = state.get("cached_project_id")
    cached_at = state.get("cached_project_id_at", 0)
    if cached and (time.time() - cached_at) < _PROJECT_ID_CACHE_TTL:
        return cached

    # Resolve via API
    api_url = _get_api_url()
    cwd = os.getcwd()
    try:
        req = urllib.request.Request(
            f"{api_url}/api/context/resolve",
            data=json.dumps({"cwd": cwd}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        project_id = data.get("project_id") or data.get("project", {}).get("id")

        # Cache result
        if project_id:
            state["cached_project_id"] = project_id
            state["cached_project_id_at"] = time.time()
            _save_supervisor_state(state)

        return project_id
    except Exception:
        return cached  # Return stale cache on failure


def _get_running_pipeline_subtask(
    api_url: str, project_id: str | None = None
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (subtask_id, parent_task_id, stage_name, next_stage_name) for the current running pipeline.

    Scans active teams for a running task with a pipeline, finds the current pending/running stage,
    and returns its subtask_id. Returns (None, None, None, None) when not found.
    """
    try:
        headers: dict[str, str] = {}
        if project_id:
            headers["X-Project-Id"] = project_id
        req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            teams = json.loads(resp.read().decode()).get("data", [])
        active_teams = [t for t in teams if t.get("status") == "active"]
        if not active_teams:
            return None, None, None, None

        team_id = active_teams[0].get("id", "")
        if not team_id:
            return None, None, None, None

        req2 = urllib.request.Request(f"{api_url}/api/teams/{team_id}/tasks", method="GET", headers=headers)
        with urllib.request.urlopen(req2, timeout=_API_TIMEOUT) as resp2:
            tasks = json.loads(resp2.read().decode()).get("data", [])

        for task in tasks:
            if task.get("status") not in ("running", "in_progress"):
                continue
            pipeline = (task.get("config") or {}).get("pipeline")
            if not pipeline:
                continue

            stages = pipeline.get("stages", [])
            current_idx = pipeline.get("current_stage_index", 0)
            if current_idx >= len(stages):
                continue

            current_stage = stages[current_idx]
            subtask_id = current_stage.get("subtask_id")
            stage_name = current_stage.get("name", "")

            # Find next stage name
            next_stage_name = None
            for s in stages[current_idx + 1:]:
                if s.get("status") != "skipped":
                    next_stage_name = s.get("name")
                    break

            return subtask_id, task.get("id"), stage_name, next_stage_name

    except Exception:
        pass

    return None, None, None, None


def _bind_subtask_running(api_url: str, project_id: str | None = None) -> str | None:
    """Bind current pipeline stage subtask to running status when an agent is dispatched.

    Returns a context string describing the bound subtask, or None when no pipeline found.
    """
    subtask_id, parent_task_id, stage_name, _ = _get_running_pipeline_subtask(api_url, project_id=project_id)
    if not subtask_id:
        return None

    result = _api_call("PUT", f"/api/tasks/{subtask_id}", {"status": "running"}, project_id=project_id)
    if result and result.get("success"):
        return f"已关联 pipeline 子任务 {subtask_id}（阶段: {stage_name}）"
    return None


def _advance_pipeline_on_completion(api_url: str, project_id: str | None = None) -> str | None:
    """Mark current pipeline subtask completed and advance pipeline when agent reports done.

    Returns a reminder text for Leader, or None when no pipeline found.
    """
    subtask_id, parent_task_id, stage_name, next_stage_name = _get_running_pipeline_subtask(
        api_url, project_id=project_id
    )
    if not subtask_id or not parent_task_id:
        return None

    # Mark subtask as completed
    _api_call("PUT", f"/api/tasks/{subtask_id}", {"status": "completed"}, project_id=project_id)

    # Advance the pipeline to the next stage
    advance_result = _api_call("POST", f"/api/tasks/{parent_task_id}/pipeline/advance", {}, project_id=project_id)

    if next_stage_name:
        return (
            f"[OS提醒] Pipeline 已自动推进：{stage_name} → {next_stage_name}。"
            f"请为下一阶段分配合适的 Agent。"
        )
    elif advance_result and advance_result.get("success"):
        return f"[OS提醒] Pipeline 最后阶段 {stage_name} 已完成，整个 Pipeline 已关闭。"

    return None


def _load_supervisor_state() -> dict:
    """Load supervisor state file; return default value if missing or corrupted."""
    try:
        with open(_SUPERVISOR_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_supervisor_state(state: dict) -> None:
    """Save supervisor state to file."""
    try:
        os.makedirs(_SUPERVISOR_STATE_DIR, exist_ok=True)
        with open(_SUPERVISOR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError:
        pass


def _check_agent_team_name(event_data: dict) -> str | None:
    """Check if Agent tool call includes team_name. Return warning text or None."""
    tool_name = event_data.get("tool_name", "")
    if tool_name != "Agent":
        return None

    tool_input_dict = event_data.get("tool_input", {})
    tool_input = json.dumps(tool_input_dict, ensure_ascii=False).lower()

    # Read-only / non-implementation CC built-in types: exempt from team_name
    readonly_builtins = [
        "explore", "plan",  # CC built-in read-only
        "claude-code-guide",  # Documentation lookup
    ]
    subagent_type = tool_input_dict.get("subagent_type", "").lower()
    has_team = bool(tool_input_dict.get("team_name"))
    if subagent_type in readonly_builtins:
        if has_team and subagent_type in ("explore", "plan"):
            return (
                "[OS提醒] Explore/Plan 是 CC 内置只读类型，不支持 SendMessage 团队通讯。"
                "请改用 OS 模板（如 software-architect、testing-qa-engineer）+ team_name 进行团队协作。"
            )
        return None  # Solo use is fine

    # OS agent templates that don't require team context (review/support roles)
    readonly_templates = [
        "code-reviewer", "security-reviewer", "python-reviewer",
        "refactor-cleaner", "security-reviewer", "tdd-guide",
    ]
    for rt in readonly_templates:
        if rt in tool_input:
            return None

    # Check if agent is a team member — ONLY explicit team_name counts.
    # name alone is not enough (can create named but untracked local agents).
    if tool_input_dict.get("team_name"):
        return None

    # All non-readonly agents MUST be trackable team members.
    # Local agents bypass OS monitoring — block unconditionally.
    sys.stderr.write(
        "[OS BLOCK] Local agents not allowed. All agents must be team members. "
        "Flow: TeamCreate(team_name=...) then Agent(team_name=..., name=..., subagent_type=...). "
        "Only explore/plan agents are exempt. "
        "If Agent tool lacks team_name param, ensure CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 "
        "is set in ~/.claude/settings.json and restart CC."
    )
    sys.exit(2)


def _check_leader_doing_too_much(event_data: dict, state: dict) -> str | None:
    """Check if Leader is making too many consecutive tool calls without delegating.

    Returns warning text when consecutive non-delegation tool calls exceed threshold.
    Resets counter when Leader calls Agent/TeamCreate/SendMessage.
    """
    tool_name = event_data.get("tool_name", "")
    if not tool_name:
        return None

    consecutive = state.get("leader_consecutive_calls", 0)

    if tool_name in _DELEGATION_TOOLS:
        state["leader_consecutive_calls"] = 0
        return None

    # Infrastructure tools (task wall, team mgmt) don't count toward threshold
    if tool_name in _INFRA_TOOLS:
        return None

    consecutive += 1
    state["leader_consecutive_calls"] = consecutive

    # Remind once at threshold+1, then every 10 calls after (avoid noise)
    over = consecutive - _LEADER_CONSECUTIVE_THRESHOLD
    if over == 1 or (over > 1 and over % 10 == 0):
        return (
            f"[AI Team OS] B0.9提醒：Leader已连续执行{consecutive}次工具调用。"
            "是否应该委派给团队成员？"
        )

    return None




def _check_workflow_reminders(event_data: dict, state: dict, project_id: str | None = None) -> list[str]:
    """Generate workflow reminders based on tool call patterns."""
    tool_name = event_data.get("tool_name", "")
    warnings: list[str] = []
    now = time.time()

    # 1. After TeamCreate: remind whether task is on the task wall
    if tool_name == "TeamCreate":
        warnings.append(
            "[OS提醒] 新团队已创建。此工作方向是否已在任务墙创建对应任务？"
            "→ 使用 task_run 或 task_create 添加任务"
        )

    # 2. Before Agent creation: check task wall, template usage, and historical memos
    if tool_name == "Agent":
        input_dict = event_data.get("tool_input", {})
        input_str = str(input_dict)
        has_team = bool(input_dict.get("team_name") or input_dict.get("name"))

        if has_team:
            # 2a. Check if active team has running/in_progress tasks; also check pipeline
            has_active_task = False
            running_tasks: list[dict] = []
            try:
                import urllib.request

                api_url = _get_api_url()
                _ph: dict[str, str] = {}
                if project_id:
                    _ph["X-Project-Id"] = project_id
                req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_ph)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    teams = json.loads(resp.read().decode("utf-8")).get("data", [])
                active_teams = [t for t in teams if t.get("status") == "active"]
                if active_teams:
                    team_id = active_teams[0].get("id", "")
                    if team_id:
                        req2 = urllib.request.Request(
                            f"{api_url}/api/teams/{team_id}/tasks",
                            method="GET",
                            headers=_ph,
                        )
                        with urllib.request.urlopen(req2, timeout=2) as resp2:
                            tasks = json.loads(resp2.read().decode("utf-8")).get(
                                "data", []
                            )
                        running_tasks = [
                            t
                            for t in tasks
                            if t.get("status") in ("running", "in_progress")
                        ]
                        has_active_task = len(running_tasks) > 0
            except Exception:
                has_active_task = True  # API unavailable, don't block

            # Fallback: check project-level tasks (team_id=None) when no team tasks are running
            if not has_active_task:
                try:
                    if active_teams and active_teams[0].get("project_id"):
                        pid = active_teams[0]["project_id"]
                        proj_req = urllib.request.Request(
                            f"{api_url}/api/projects/{pid}/tasks/running-count",
                            method="GET",
                            headers=_ph,
                        )
                        with urllib.request.urlopen(proj_req, timeout=_API_TIMEOUT) as proj_resp:
                            proj_data = json.loads(proj_resp.read().decode("utf-8"))
                        if proj_data.get("count", 0) > 0:
                            has_active_task = True
                except Exception:
                    pass

            if not has_active_task:
                warnings.append(
                    "[OS提醒] 当前无进行中任务。创建Agent执行工作前，"
                    "请先用 task_create 将任务上墙再分配。"
                    "→ 标准流程：task_create → Agent(team_name=...)"
                )
            else:
                # 2a-TW. Pre-check: verify dispatched work matches a task wall item
                try:
                    agent_prompt = (input_dict.get("prompt", "") + " " + input_dict.get("description", "")).lower()
                    if agent_prompt.strip() and project_id:
                        tw_data = _api_call("GET", f"/api/projects/{project_id}/task-wall?limit=20&include_completed=false", project_id=project_id)
                        if tw_data:
                            tw_tasks: list[dict] = []
                            for hg in (tw_data.get("wall") or {}).values():
                                if isinstance(hg, list):
                                    tw_tasks.extend(hg)
                            tw_pending = [t for t in tw_tasks if t.get("status") in ("pending", "running")]
                            # Quick keyword match check
                            agent_words = set(agent_prompt.replace("—", " ").replace("-", " ").split())
                            agent_words -= {"的", "是", "在", "了", "和", "与", "a", "the", "to", "for", "of"}
                            matched_any = False
                            for t in tw_pending:
                                title_words = set((t.get("title") or "").lower().replace("—", " ").replace("-", " ").split())
                                if len(title_words & agent_words) >= 2:
                                    matched_any = True
                                    break
                            if not matched_any and tw_pending:
                                tw_titles = "、".join(t.get("title", "?")[:20] for t in tw_pending[:3])
                                warnings.append(
                                    f"[OS提醒] 此Agent工作未匹配到任务墙项（墙上有：{tw_titles}��。"
                                    "确认此工作已在任务墙登记？→ task_create 上墙"
                                )
                except Exception:
                    pass  # Advisory only

            # 2-CP1. Pipeline subtask binding: mark current stage subtask as running
            if has_active_task:
                try:
                    _bind_api_url = _get_api_url()
                    bind_msg = _bind_subtask_running(_bind_api_url, project_id=project_id)
                    if bind_msg:
                        warnings.append(f"[OS提醒] {bind_msg}")
                except Exception:
                    pass  # Binding is optional — never block agent dispatch

            # 2d. Pipeline enforcement: check if running tasks have a pipeline
            if has_active_task and running_tasks:
                # Detect tasks without pipeline (no 'pipeline' key in config)
                tasks_without_pipeline = [
                    t for t in running_tasks
                    if not (t.get("config") or {}).get("pipeline")
                ]
                if tasks_without_pipeline:
                    no_pipeline_warnings = state.get("no_pipeline_warnings", 0)
                    if no_pipeline_warnings == 0:
                        warnings.append(
                            "[OS提醒] 当前任务没有工作流管道。"
                            "建议指定 task_type(feature/bugfix/research/refactor/quick-fix/spike/hotfix)"
                        )
                        state["no_pipeline_warnings"] = 1
                    elif no_pipeline_warnings == 1:
                        warnings.append(
                            "[OS提醒] 第二次提醒：请为任务指定 task_type 设定工作流管道"
                        )
                        state["no_pipeline_warnings"] = 2
                    else:
                        # Hard block on third+ occurrence
                        sys.stderr.write(
                            "[OS BLOCK] 必须先为任务指定 task_type。"
                            "最轻量选项：quick-fix（只有 Implement→Test）"
                        )
                        sys.exit(2)
                else:
                    # Running tasks all have pipelines — reset warning counter
                    state["no_pipeline_warnings"] = 0

                # 2e. Pipeline pending stage detection with progressive enforcement
                tasks_with_pipeline = [
                    t for t in running_tasks
                    if (t.get("config") or {}).get("pipeline")
                ]
                pending_stage_info: list[tuple[str, str, str]] = []  # (task_title, stage_name, agent_template)
                for t in tasks_with_pipeline:
                    pipeline = t["config"]["pipeline"]
                    stages = pipeline.get("stages", [])
                    current_idx = pipeline.get("current_stage_index", 0)
                    # Look for pending stages after the current index
                    for stage in stages[current_idx + 1:]:
                        if stage.get("status") == "pending":
                            pending_stage_info.append((
                                t.get("title", t.get("id", "?")),
                                stage["name"],
                                stage.get("agent_template", ""),
                            ))
                            break  # Only report first pending per task

                if pending_stage_info:
                    pending_warnings = state.get("pipeline_pending_warnings", 0)
                    first_title, first_stage, first_tpl = pending_stage_info[0]
                    if pending_warnings == 0:
                        warnings.append(
                            f"[OS提醒] Pipeline 阶段 '{first_stage}' 就绪（任务: {first_title}），"
                            f"推荐 agent_template: {first_tpl}"
                        )
                        state["pipeline_pending_warnings"] = 1
                    elif pending_warnings == 1:
                        warnings.append(
                            f"[OS提醒] 请先推进 pipeline（阶段 '{first_stage}' 等待中），"
                            "再分配其他工作"
                        )
                        state["pipeline_pending_warnings"] = 2
                    else:
                        sys.stderr.write(
                            f"[OS BLOCK] 必须先推进 pipeline 阶段 '{first_stage}' 再做其他工作。"
                            f"使用 pipeline_advance 推进当前任务的 pipeline。"
                        )
                        sys.exit(2)

                # 2f. Agent type matching check vs pipeline recommended template.
                # Skipped for meeting-mode stages: any role can attend a meeting.
                subagent_type_for_check = input_dict.get("subagent_type", "")
                if subagent_type_for_check and tasks_with_pipeline:
                    # Find the current active stage's recommended template
                    recommended_template: str | None = None
                    current_stage_name: str | None = None
                    current_stage_mode: str = "agent"
                    for t in tasks_with_pipeline:
                        pipeline = t["config"]["pipeline"]
                        stages = pipeline.get("stages", [])
                        current_idx = pipeline.get("current_stage_index", 0)
                        if current_idx < len(stages):
                            current_stage = stages[current_idx]
                            if current_stage.get("status") in ("pending", "running"):
                                recommended_template = current_stage.get("agent_template", "")
                                current_stage_name = current_stage.get("name", "")
                                current_stage_mode = current_stage.get("mode", "agent")
                                break

                    if recommended_template and current_stage_name:
                        if current_stage_mode == "meeting":
                            # Meeting stages allow any role — skip type check entirely
                            pass
                        elif subagent_type_for_check == recommended_template:
                            # Correct template — reset pending warnings counter
                            state["pipeline_pending_warnings"] = 0
                        else:
                            warnings.append(
                                f"[OS提醒] 当前阶段 '{current_stage_name}' 推荐 {recommended_template}，"
                                f"但你派出了 {subagent_type_for_check}。确认使用？"
                            )

        # 2b. Template usage reminder (check if subagent_type is a known template)
        subagent_type = input_dict.get("subagent_type", "")
        if subagent_type in ("general-purpose", "") or not subagent_type:
            last_tpl = state.get("last_template_reminder", 0)
            if now - last_tpl > 600:  # 10-min cooldown
                warnings.append(
                    "[OS提醒] 使用通用Agent前，请确认是否有匹配的Agent模板。"
                    "→ agent_template_recommend(task描述) 查看推荐，"
                    "或用 /agents 浏览全部26个专业模板"
                )
                state["last_template_reminder"] = now

        # 2c. Memo reminder (with 5-min cooldown)
        if has_team:
            last_memo = state.get("last_memo_reminder", 0)
            if now - last_memo > 300:
                warnings.append(
                    "[OS提醒] 分配新成员前：此任务是否有历史工作记录？"
                    "→ 建议先 task_memo_read 查看前置工作"
                )
                state["last_memo_reminder"] = now

    # 3. Before SendMessage(shutdown): remind about task completion
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        if "shutdown" in input_str.lower():
            warnings.append(
                "[OS提醒] 关闭Agent前：此Agent的任务是否已标记完成？"
                "→ 建议更新任务状态并添加总结memo (task_memo_add type=summary)"
            )

    # 4. On TeamDelete: notify OS to close the corresponding team
    if tool_name == "TeamDelete":
        try:
            import urllib.request

            api_url = _get_api_url()
            _tdh: dict[str, str] = {}
            if project_id:
                _tdh["X-Project-Id"] = project_id
            # Close all active teams (TeamDelete means current team work is done)
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_tdh)
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            for t in teams:
                if t.get("status") == "active":
                    _close_h = {"Content-Type": "application/json"}
                    if project_id:
                        _close_h["X-Project-Id"] = project_id
                    close_req = urllib.request.Request(
                        f"{api_url}/api/teams/{t['id']}",
                        data=json.dumps({"status": "completed"}).encode(),
                        headers=_close_h,
                        method="PUT",
                    )
                    urllib.request.urlopen(close_req, timeout=2)
        except Exception:
            pass  # Silently handle

    # 5. After TeamCreate: check if active teams already exist
    if tool_name == "TeamCreate":
        try:
            import urllib.request

            api_url = _get_api_url()
            _tch: dict[str, str] = {}
            if project_id:
                _tch["X-Project-Id"] = project_id
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_tch)
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            active_teams = [t for t in teams if t.get("status") == "active"]
            # Newly created team is also active, so check if >1 active teams
            if len(active_teams) > 1:
                other = active_teams[0].get("name", "未知")
                warnings.append(
                    f"[OS提醒] 已存在活跃团队「{other}」。"
                    "建议：①在已有团队中添加成员 ②先关闭旧团队再创建新的"
                )
        except Exception:
            pass  # Silently skip when API unavailable

    # 6. After SendMessage: check parallel task assignment (idle Agent + pending task matching)
    if tool_name == "SendMessage":
        try:
            import urllib.request

            api_url = _get_api_url()
            _smh: dict[str, str] = {}
            if project_id:
                _smh["X-Project-Id"] = project_id
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_smh)
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            active_teams = [t for t in teams if t.get("status") == "active"]
            if active_teams:
                team_id = active_teams[0].get("id", "")
                if team_id:
                    req2 = urllib.request.Request(
                        f"{api_url}/api/teams/{team_id}/agents",
                        method="GET",
                        headers=_smh,
                    )
                    with urllib.request.urlopen(req2, timeout=2) as resp2:
                        agents = json.loads(resp2.read().decode("utf-8")).get("data", [])
                    non_leader_agents = [a for a in agents if a.get("role") != "leader"]
                    busy_count = sum(1 for a in non_leader_agents if a.get("status") == "busy")
                    idle_agents = [
                        a for a in non_leader_agents if a.get("status") in ("waiting", "offline")
                    ]
                    if busy_count < 3 and idle_agents:
                        # Try to fetch pending tasks for matching suggestions
                        match_hints: list[str] = []
                        try:
                            req3 = urllib.request.Request(
                                f"{api_url}/api/teams/{team_id}/tasks",
                                method="GET",
                                headers=_smh,
                            )
                            with urllib.request.urlopen(req3, timeout=2) as resp3:
                                tasks = json.loads(resp3.read().decode("utf-8")).get("data", [])
                            pending_tasks = [
                                t
                                for t in tasks
                                if t.get("status") in ("pending",) and not t.get("assigned_to")
                            ]
                            for idle in idle_agents[:3]:  # Show at most 3 idle Agents
                                agent_role = (idle.get("role") or idle.get("name") or "").lower()
                                agent_name = idle.get("name", "?")
                                # Find tasks whose tags overlap with agent role
                                matched = next(
                                    (
                                        t
                                        for t in pending_tasks
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
                                        f"空闲Agent: {agent_name}({idle.get('role', '')}), "
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
            pass  # Silently skip when API unavailable

    # 7. More than 15 minutes since last task wall view
    if tool_name in ("taskwall_view", "mcp__ai-team-os__taskwall_view"):
        state["last_taskwall_view"] = now
    else:
        last_view = state.get("last_taskwall_view", 0)
        if last_view == 0:
            # First tool call in session — start 15-min countdown from now
            state["last_taskwall_view"] = now
        elif (now - last_view) > 900:
            minutes = int((now - last_view) / 60)
            warnings.append(
                f"[OS提醒] 距上次查看任务墙已{minutes}分钟。→ 建议 taskwall_view 查看当前任务状态"
            )
            state["last_taskwall_view"] = now

    # 9. Handoff reminder: when Agent reports completion, remind to assign follow-up tasks
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["完成", "completed", "done", "finished", "汇报"]
        is_completion = any(kw in input_str.lower() for kw in completion_keywords)
        # Exclude shutdown messages (already handled by rule 3)
        is_shutdown = "shutdown" in input_str.lower()
        if is_completion and not is_shutdown:
            # 9-CP2. Pipeline auto-advance: mark subtask completed and advance pipeline
            try:
                _advance_api_url = _get_api_url()
                advance_msg = _advance_pipeline_on_completion(_advance_api_url, project_id=project_id)
                if advance_msg:
                    warnings.append(advance_msg)
            except Exception:
                pass  # Advancing is optional — never block completion message

            try:
                api_url = _get_api_url()
                _r9h: dict[str, str] = {}
                if project_id:
                    _r9h["X-Project-Id"] = project_id
                req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_r9h)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    teams = json.loads(resp.read().decode("utf-8")).get("data", [])
                active_teams = [t for t in teams if t.get("status") == "active"]
                if active_teams:
                    team_id = active_teams[0].get("id", "")
                    if team_id:
                        req2 = urllib.request.Request(
                            f"{api_url}/api/teams/{team_id}/tasks",
                            method="GET",
                            headers=_r9h,
                        )
                        with urllib.request.urlopen(req2, timeout=2) as resp2:
                            tasks = json.loads(resp2.read().decode("utf-8")).get("data", [])
                        pending = [
                            t
                            for t in tasks
                            if t.get("status") == "pending" and not t.get("assigned_to")
                        ]
                        if pending:
                            pending_titles = "、".join(t["title"] for t in pending[:3])
                            more = f"等{len(pending)}个" if len(pending) > 3 else ""
                            warnings.append(
                                f"[OS提醒] Agent已完成汇报，仍有待分配任务：{pending_titles}{more}。"
                                "→ 是否分配给空闲成员继续推进？"
                            )
            except Exception:
                pass

    # 10. After meeting_create: remind to notify participants and use skills
    if tool_name in ("meeting_create", "mcp__ai-team-os__meeting_create"):
        warnings.append(
            "[OS提醒] 会议已创建。请：1)逐一通知参与者(SendMessage)告知meeting_id "
            "2)建议参与者使用 /meeting-participate skill参加讨论 "
            "3)主持人使用 /meeting-facilitate skill引导讨论"
        )

    # 11. After meeting_conclude: remind to add action items to task wall
    if tool_name in ("meeting_conclude", "mcp__ai-team-os__meeting_conclude"):
        warnings.append(
            "[OS提醒] 会议已结束。请将讨论结论中的行动项转化为任务墙任务。"
            "→ 使用 task_create 添加任务，确保口头承诺不遗忘"
        )

    # 12. When task marked complete: remind QA acceptance testing
    if tool_name in ("task_status", "mcp__ai-team-os__task_status"):
        input_str = str(event_data.get("tool_input", {}))
        if "completed" in input_str.lower():
            warnings.append(
                "[OS提醒] 任务标记完成。是否涉及系统行为变更？→ 如是，请通知QA Agent进行验收测试"
            )

    # 13. Bottleneck detection: remind to hold meeting when all tasks done or many blocked
    # Check every 50 tool calls (throttled)
    bottleneck_count = state.get("bottleneck_check_count", 0) + 1
    state["bottleneck_check_count"] = bottleneck_count
    if bottleneck_count % 50 == 0:
        try:
            import urllib.request

            api_url = _get_api_url()
            _b13h: dict[str, str] = {}
            if project_id:
                _b13h["X-Project-Id"] = project_id
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_b13h)
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            for t in teams:
                if t.get("status") != "active":
                    continue
                tid = t["id"]
                req2 = urllib.request.Request(f"{api_url}/api/teams/{tid}/tasks", headers=_b13h)
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

    # 14. Report format validation
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["完成", "completed", "done", "finished", "汇报"]
        if (
            any(kw in input_str.lower() for kw in completion_keywords)
            and "shutdown" not in input_str.lower()
        ):
            required_fields = ["完成内容", "修改文件", "测试结果"]
            missing = [f for f in required_fields if f not in input_str]
            if missing and len(input_str) > 100:  # Only check longer reports
                warnings.append(
                    f"[OS提醒] 汇报可能缺少标准字段：{', '.join(missing)}。"
                    "标准格式：完成内容/修改文件/测试结果/建议任务状态/建议memo"
                )

    # ── Safety guardrail rules ──────────────────────────────────────────

    tool_input = event_data.get("tool_input", {})

    # S1: Dangerous command interception (Bash)
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        # Strip heredoc blocks (<<'EOF'...EOF, <<"EOF"...EOF, <<EOF...EOF) so that
        # text inside commit messages or other string literals does not trigger S1.
        # Only the executable shell syntax outside heredoc delimiters is scanned.
        cmd_for_s1 = re.sub(r"<<['\"]?(\w+)['\"]?.*?\n.*?\1", "", cmd, flags=re.DOTALL)
        cmd_lower = cmd_for_s1.lower()
        # Recursive delete of root/home directory -> exit(2) hard block
        if re.search(r"rm\s+-[^\s]*[rR][^\s]*\s+(/|~/|~)(\s|$|[^a-zA-Z])", cmd_for_s1):
            sys.stderr.write("[OS BLOCK] Dangerous: recursive delete of root/home directory blocked")
            sys.exit(2)
        # Recursive delete of other dangerous targets -> warning
        if re.search(r"rm\s+-[^\s]*[rR][^\s]*\s+\*", cmd_for_s1):
            warnings.append("[安全] 危险：检测到递归删除通配符命令，请确认操作目标")
        # Destructive database operations
        if re.search(r"\b(DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE)\b", cmd_for_s1, re.IGNORECASE):
            warnings.append("[安全] 危险：检测到数据库破坏性操作（DROP/TRUNCATE），请确认")
        # force push
        if "push" in cmd_lower and "--force" in cmd_lower:
            warnings.append("[安全] 注意：检测到force push，可能覆盖远程历史")
        # Overly permissive file permissions
        if "chmod 777" in cmd_for_s1:
            warnings.append("[安全] 安全：过度开放的文件权限（chmod 777），建议使用更严格的权限")
        # S3: Sensitive file commit interception (git add) -> exit(2) hard block
        if "git add" in cmd_lower:
            block_patterns = [".env", "id_rsa", ".pem", ".key"]
            for pat in block_patterns:
                if pat in cmd_lower:
                    sys.stderr.write(f"[OS BLOCK] 禁止提交敏感文件（{pat}）")
                    sys.exit(2)
            # credentials keep as warning (filename is ambiguous, may not be a key file)
            if "credentials" in cmd_lower:
                warnings.append(
                    "[安全] 安全：检测到尝试提交credentials文件，"
                    "请确认该文件不包含密钥信息且已在.gitignore中"
                )

    # 15. Team directory cleanup reminder: check every 100 tool calls
    team_cleanup_count = state.get("team_cleanup_check_count", 0) + 1
    state["team_cleanup_check_count"] = team_cleanup_count
    if team_cleanup_count % 100 == 0:
        teams_dir = Path.home() / ".claude" / "teams"
        if teams_dir.exists():
            try:
                team_dirs = [p for p in teams_dir.iterdir() if p.is_dir()]
                if len(team_dirs) > 5:
                    warnings.append(
                        f"[OS提醒] 检测到 {len(team_dirs)} 个历史团队目录，建议清理："
                        "使用 TeamDelete 或手动删除 ~/.claude/teams/ 下的旧目录"
                    )
            except Exception:
                pass

    # ── Safety guardrail rules ──────────────────────────────────────────

    # S2: Sensitive information detection (Write/Edit)
    if tool_name in ("Write", "Edit"):
        # Get content to be written
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
        # Hardcoded secret detection
        if re.search(
            r"(password|secret|api_key|token)\s*=\s*['\"][^'\"]+['\"]",
            content,
            re.IGNORECASE,
        ):
            warnings.append("[安全] 安全：检测到可能的硬编码密钥，建议使用环境变量")
        # .env file write reminder
        file_path = tool_input.get("file_path", "")
        if file_path.endswith(".env") or "/.env" in file_path or "\\.env" in file_path:
            warnings.append("[安全] 注意：.env文件不应提交到版本库，请确认.gitignore包含.env")
        # Reports data directory hard block: only block writes to the actual reports data
        # dirs under ~/.claude/data/. Any other .md write (README, docs, src) is allowed.
        # Conditions (both must be true):
        #   1. Path contains the exact data dir prefix for reports
        #   2. File extension is .md
        _fp_normalized = file_path.replace("\\", "/")
        _is_report_data_dir = (
            ".claude/data/ai-team-os/reports/" in _fp_normalized
            or (
                ".claude/data/ai-team-os/projects/" in _fp_normalized
                and "/reports/" in _fp_normalized
            )
        )
        if _is_report_data_dir and file_path.endswith(".md"):
            warnings.append(
                "[OS提醒] 报告应通过 report_save 工具保存到数据库（直接写文件不会被系统追踪）。"
                "→ report_save(author=你的名字, topic=主题, content=markdown内容,"
                " report_type=research/design/analysis/meeting-minutes)"
            )
        # File lock conflict detection: warn when another agent holds the lock
        if file_path:
            try:
                from aiteam.api.file_lock import check_lock
                lock_info = check_lock(file_path)
                if lock_info.get("locked"):
                    held_by = lock_info.get("held_by", "unknown")
                    expires_in = lock_info.get("expires_in", 0)
                    warnings.append(
                        f"[OS提醒] 文件冲突警告：{file_path} 已被 {held_by} 锁定"
                        f"（剩余 {int(expires_in)} 秒）。"
                        "建议等待锁释放或通过Leader协调。"
                        "→ file_lock_list() 查看所有锁状态"
                    )
            except Exception:
                pass  # Lock check is advisory — never block editing

    return warnings


def _post_tool_taskwall_sync(event_data: dict, state: dict, project_id: str | None = None) -> list[str]:
    """PostToolUse: auto-sync task wall when Agent dispatched or completion reported.

    1. After Agent dispatch → find matching pending task on project wall → auto-update to running
    2. After SendMessage(completion) → remind to update task to completed
    """
    tool_name = event_data.get("tool_name", "")
    warnings: list[str] = []

    if not project_id:
        return warnings

    # 1. After Agent dispatch: auto-link to task wall item
    if tool_name == "Agent":
        input_dict = event_data.get("tool_input", {})
        # Only for team agents (non-readonly)
        if not input_dict.get("team_name"):
            return warnings

        agent_prompt = input_dict.get("prompt", "")
        agent_desc = input_dict.get("description", "")
        agent_text = f"{agent_desc} {agent_prompt}".lower()

        if not agent_text.strip():
            return warnings

        try:
            # Query project task wall for pending tasks
            wall_data = _api_call("GET", f"/api/projects/{project_id}/task-wall?limit=20&include_completed=false", project_id=project_id)
            if not wall_data:
                return warnings

            wall_tasks: list[dict] = []
            for horizon_group in (wall_data.get("wall") or {}).values():
                if isinstance(horizon_group, list):
                    wall_tasks.extend(horizon_group)

            # Find matching pending task by keyword overlap
            pending_tasks = [t for t in wall_tasks if t.get("status") in ("pending", "running")]
            best_match: dict | None = None
            best_score = 0

            for task in pending_tasks:
                title = (task.get("title") or "").lower()
                tags = [t.lower() for t in (task.get("tags") or [])]
                desc = (task.get("description") or "").lower()

                # Simple keyword overlap scoring
                score = 0
                title_words = set(title.replace("—", " ").replace("-", " ").split())
                agent_words = set(agent_text.replace("—", " ").replace("-", " ").split())
                # Remove common stop words
                stop_words = {"的", "是", "在", "了", "和", "与", "a", "the", "to", "and", "for", "of", "in", "on"}
                title_words -= stop_words
                agent_words -= stop_words

                overlap = title_words & agent_words
                score += len(overlap) * 2

                # Tag matching
                for tag in tags:
                    if tag in agent_text:
                        score += 3

                # Description keyword overlap
                if desc:
                    desc_words = set(desc.replace("—", " ").replace("-", " ").split()) - stop_words
                    score += len(desc_words & agent_words)

                if score > best_score:
                    best_score = score
                    best_match = task

            if best_match and best_score >= 3:
                task_id = best_match["id"]
                task_title = best_match.get("title", "")
                task_status = best_match.get("status", "pending")

                if task_status == "pending":
                    # Auto-update to running
                    _api_call("PUT", f"/api/tasks/{task_id}", {"status": "running"}, project_id=project_id)
                    warnings.append(
                        f"[OS提醒] 已自动关联任务墙：「{task_title}」→ running"
                    )
                else:
                    warnings.append(
                        f"[OS提醒] 当前工作关联任务墙：「{task_title}」（状态: {task_status}）"
                    )

                # Save matched task ID for later completion tracking
                state["last_dispatched_task_id"] = task_id
                state["last_dispatched_task_title"] = task_title
            elif best_score < 3 and pending_tasks:
                # No good match — warn to create on wall
                warnings.append(
                    "[OS提醒] 此Agent工作未匹配到任务墙项。建议先用 task_create 上墙，确保工作可追踪。"
                    f"当前任务墙有 {len(pending_tasks)} 个待办任务"
                )

        except Exception:
            pass  # Task wall sync is advisory — never block

    # 2. After SendMessage with completion keywords: suggest updating task status
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["完成", "completed", "done", "finished", "汇报"]
        is_completion = any(kw in input_str.lower() for kw in completion_keywords)
        is_shutdown = "shutdown" in input_str.lower()

        if is_completion and not is_shutdown:
            last_task_id = state.get("last_dispatched_task_id")
            last_task_title = state.get("last_dispatched_task_title")
            if last_task_id:
                # Auto-update task to completed
                _api_call("PUT", f"/api/tasks/{last_task_id}", {"status": "completed"}, project_id=project_id)
                warnings.append(
                    f"[OS提醒] 已自动更新任务墙：「{last_task_title}」→ completed"
                )
                # Clear tracking
                state.pop("last_dispatched_task_id", None)
                state.pop("last_dispatched_task_title", None)

    return warnings


def main() -> None:
    # Force UTF-8 output on Windows (default is gbk, causes garbled Chinese)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception:
        return

    # CC hook payload doesn't include event type name; inject via CLI arg
    if len(sys.argv) > 1 and "hook_event_name" not in payload:
        payload["hook_event_name"] = sys.argv[1]

    event_name = payload.get("hook_event_name", "")
    state = _load_supervisor_state()
    warnings: list[str] = []

    # Resolve project ID once; propagate to all project-scoped API calls
    project_id = _resolve_project_id()

    if event_name == "PreToolUse":
        w = _check_agent_team_name(payload)
        if w:
            warnings.append(w)
        w = _check_leader_doing_too_much(payload, state)
        if w:
            warnings.append(w)
    if event_name == "PostToolUse":
        # Auto-update task wall when Agent is dispatched or reports completion
        post_warnings = _post_tool_taskwall_sync(payload, state, project_id=project_id)
        warnings.extend(post_warnings)

    # Workflow reminders (checked for both PreToolUse and PostToolUse)
    if event_name in ("PreToolUse", "PostToolUse"):
        wf_warnings = _check_workflow_reminders(payload, state, project_id=project_id)
        warnings.extend(wf_warnings)

    _save_supervisor_state(state)

    # PreToolUse/PostToolUse hooks inject text into conversation via hookSpecificOutput
    output = {"hookSpecificOutput": {"hookEventName": event_name}}
    if event_name == "PreToolUse":
        output["hookSpecificOutput"]["permissionDecision"] = "allow"
    if warnings:
        output["hookSpecificOutput"]["additionalContext"] = "\n".join(warnings)
    sys.stdout.write(json.dumps(output))


if __name__ == "__main__":
    main()
