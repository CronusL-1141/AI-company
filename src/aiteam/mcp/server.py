"""AI Team OS — MCP Server.

Provides MCP tools that call corresponding API endpoints on the local
FastAPI server (localhost:8000) via HTTP.
MCP Server runs in stdio mode, fully decoupled from the FastAPI process.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastmcp import FastMCP

logger = logging.getLogger(__name__)
_api_process: subprocess.Popen | None = None

API_URL = os.environ.get("AITEAM_API_URL", "http://localhost:8000")

mcp = FastMCP(
    name="ai-team-os",
    instructions="AI Agent Team Operating System — 项目管理、团队创建、Agent管理、会议协作、任务执行、记忆搜索",
)


# ============================================================
# HTTP helper
# ============================================================


def _api_call(method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Unified API call helper using urllib standard library.

    Args:
        method: HTTP method (GET / POST / PUT / DELETE)
        path: API path, e.g., /api/teams
        data: Request body data (used for POST/PUT only)

    Returns:
        API response as a JSON dict
    """
    url = f"{API_URL}{urllib.parse.quote(path, safe='/?&=%')}"
    headers = {"Content-Type": "application/json"}

    body_bytes = None
    if data is not None:
        body_bytes = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass
        return {
            "success": False,
            "error": f"HTTP {e.code}: {e.reason}",
            "detail": error_body,
        }
    except urllib.error.URLError as e:
        return {
            "success": False,
            "error": f"无法连接到 AI Team OS API ({API_URL}): {e.reason}",
            "hint": "请确保 FastAPI 服务已启动: aiteam serve",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"请求失败: {e!s}",
        }


# ============================================================
# Tool 1: team_create
# ============================================================


@mcp.tool()
def team_create(
    name: str,
    mode: str = "coordinate",
    project_id: str = "",
    leader_agent_id: str = "",
) -> dict[str, Any]:
    """Create a new AI Agent team.

    If leader_agent_id is specified, the Leader's old active team will be
    automatically completed. A Leader can only lead one active team at a time.

    Args:
        name: Team name
        mode: Collaboration mode, either "coordinate" or "broadcast"
        project_id: Associated project ID (optional)
        leader_agent_id: Leader agent ID for this team (optional, used to auto-complete old team)

    Returns:
        Created team info including team_id
    """
    payload: dict[str, Any] = {"name": name, "mode": mode}
    if project_id:
        payload["project_id"] = project_id
    if leader_agent_id:
        payload["leader_agent_id"] = leader_agent_id
    result = _api_call("POST", "/api/teams", payload)
    result["_team_standard"] = {
        "permanent_members": {
            "hint": "以下角色为团队常驻成员，创建团队时必须包含，团队存续期间不Kill：",
            "roles": [
                {
                    "name": "qa-observer",
                    "role": "常驻QA观察员",
                    "description": "持续监控系统行为、检查前端显示、发现bug并上报",
                },
                {
                    "name": "bug-fixer",
                    "role": "常驻Bug工程师",
                    "description": "接收QA报告，定位并修复bug，验证修复效果",
                },
            ],
        },
        "temporary_members": {
            "hint": "以下角色按需创建，任务完成后Kill释放资源：",
            "roles": [
                {"name": "developer", "count": "1-3", "description": "开发工程师，负责具体实现"},
                {
                    "name": "researcher",
                    "count": "1-3",
                    "description": "研究员，负责技术调研和方案设计",
                },
                {"name": "tech-lead", "count": 1, "description": "技术负责人，负责架构决策"},
            ],
        },
        "lifecycle_rule": (
            "团队不关闭——只Kill临时成员。QA和Bug-fixer保持团队活跃。需要开发/研究时往团队加人，完成后Kill。"
        ),
    }
    return result


# ============================================================
# Tool 2: team_status
# ============================================================


@mcp.tool()
def team_status(team_id: str) -> dict[str, Any]:
    """Get detailed information and status of a specified team.

    Args:
        team_id: Team ID or team name

    Returns:
        Team details including name, mode, member count, etc.
    """
    return _api_call("GET", f"/api/teams/{team_id}")


# ============================================================
# Tool 3: team_list
# ============================================================


@mcp.tool()
def team_list() -> dict[str, Any]:
    """List all created teams.

    Returns:
        Team list with basic info for each team
    """
    return _api_call("GET", "/api/teams")


# ============================================================
# Tool 4: agent_register
# ============================================================


def _load_agent_prompt_template() -> str:
    """Load the standardized Agent prompt template."""
    # server.py is at src/aiteam/mcp/server.py, need to go up 4 levels to project root
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        "plugin",
        "config",
        "agent-prompt-template.md",
    )
    try:
        with open(template_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("Agent prompt模板文件不存在: %s", template_path)
        return ""


def _render_agent_prompt(role: str, project_path: str = "") -> str:
    """Fill the template with basic information."""
    template = _load_agent_prompt_template()
    if not template:
        return ""
    return template.replace("{role}", role).replace("{project_path}", project_path or "未指定")


@mcp.tool()
def agent_register(
    team_id: str,
    name: str,
    role: str,
    model: str = "claude-opus-4-6",
    system_prompt: str = "",
) -> dict[str, Any]:
    """Register a new AI Agent to a team.

    Status is automatically set to busy after successful registration.
    Rule: Leader should Kill the Agent after one-time tasks are done; keep those that may have follow-up tasks.
    Report to Leader when tools are restricted.

    If system_prompt is not provided, the standardized prompt template is used automatically.

    Args:
        team_id: Target team ID or name
        name: Agent name
        role: Agent role description
        model: Model to use, default claude-opus-4-6
        system_prompt: Agent's system prompt (leave empty to auto-use standardized template)

    Returns:
        Agent info + teammates list + team_snapshot (with pending_tasks and recent_meeting)
    """
    effective_prompt = system_prompt
    if not effective_prompt:
        # MCP layer cannot directly query project's root_path, template shows "未指定" for {project_path}
        # hook_translator's auto-register path can obtain the accurate project_path
        effective_prompt = _render_agent_prompt(role)

    return _api_call(
        "POST",
        f"/api/teams/{team_id}/agents",
        {
            "name": name,
            "role": role,
            "model": model,
            "system_prompt": effective_prompt,
        },
    )


# ============================================================
# Tool 5: agent_update_status
# ============================================================


@mcp.tool()
def agent_update_status(
    agent_id: str,
    status: str,
) -> dict[str, Any]:
    """Update an Agent's running status.

    Args:
        agent_id: Agent ID
        status: New status, one of "busy", "waiting", "offline"

    Returns:
        Updated Agent info
    """
    return _api_call("PUT", f"/api/agents/{agent_id}/status", {"status": status})


# ============================================================
# Tool 6: agent_list
# ============================================================


@mcp.tool()
def agent_list(team_id: str) -> dict[str, Any]:
    """List all registered Agents in a team.

    Args:
        team_id: Team ID or name

    Returns:
        Agent list with status and role for each Agent
    """
    return _api_call("GET", f"/api/teams/{team_id}/agents")


# ============================================================
# Tool: context_resolve
# ============================================================


@mcp.tool()
def context_resolve() -> dict[str, Any]:
    """Get the current active OS context — active project, active team, member list, loop status.

    This is the infrastructure for all simplified operations. A single call returns
    the complete context of the current working environment, allowing Leader or other
    tools to auto-fill parameters like project_id, team_id, etc.

    Returns:
        Context dict containing project / team / agents / loop
    """
    result: dict[str, Any] = {"project": None, "team": None, "agents": [], "loop": None}

    try:
        # Get team list, find active teams
        teams_data = _api_call("GET", "/api/teams")
        active_teams = [t for t in teams_data.get("data", []) if t.get("status") == "active"]
        if active_teams:
            team = active_teams[0]
            result["team"] = {"id": team["id"], "name": team["name"]}
            # Get members
            agents_data = _api_call("GET", f"/api/teams/{team['id']}/agents")
            result["agents"] = [
                {"name": a["name"], "status": a["status"], "role": a.get("role", "")}
                for a in agents_data.get("data", [])
            ]
            # Project
            if team.get("project_id"):
                result["project"] = {"id": team["project_id"]}

        # If no project was obtained from the team, try fetching the project list directly
        if not result["project"]:
            projects_data = _api_call("GET", "/api/projects")
            projects = projects_data.get("data", [])
            if projects:
                result["project"] = {"id": projects[0]["id"], "name": projects[0].get("name", "")}

        # Get loop status (if there is an active team)
        if result["team"]:
            loop_data = _api_call("GET", f"/api/teams/{result['team']['id']}/loop/status")
            if loop_data.get("success") is not False:
                result["loop"] = loop_data.get("data") or loop_data

    except Exception as e:
        result["error"] = str(e)

    return result


def _resolve_team_id(team_id: str) -> str:
    """If team_id is empty, automatically get the active team ID from context_resolve."""
    if team_id:
        return team_id
    ctx = context_resolve()
    team = ctx.get("team")
    if team and team.get("id"):
        return team["id"]
    return ""


def _resolve_project_id(project_id: str) -> str:
    """If project_id is empty, automatically get the active project ID from context_resolve."""
    if project_id:
        return project_id
    ctx = context_resolve()
    project = ctx.get("project")
    if project and project.get("id"):
        return project["id"]
    return ""


# ============================================================
# Tool 7: meeting_create
# ============================================================


@mcp.tool()
def meeting_create(
    topic: str,
    team_id: str = "",
    participants: list[str] | None = None,
    template: str = "free",
) -> dict[str, Any]:
    """Create a team meeting for multi-Agent collaborative discussion.

    Rule: Dynamically add suitable participants based on the topic; recruit experts
    when new directions emerge during discussion. Meeting conclusions should be
    converted to tasks and placed on the task wall.

    Available templates: brainstorm (4 rounds) / decision (3 rounds) / review (3 rounds) /
              retrospective (3 rounds) / standup (1 round) / debate (3 rounds) /
              lean_coffee (3 rounds) / council (3 rounds) / free (default, auto-recommends based on topic)

    Args:
        topic: Meeting discussion topic
        team_id: Team ID or name (optional, auto-uses active team if empty)
        participants: List of participant Agent IDs; all members join if empty
        template: Meeting template, default "free"

    Returns:
        Meeting info including meeting_id, operation guide, and template round structure
    """
    from aiteam.meeting.templates import TEMPLATE_ROUNDS, recommend_template

    resolved = _resolve_team_id(team_id)
    if not resolved:
        return {"success": False, "error": "未找到活跃团队，请提供 team_id 或先创建团队"}
    result = _api_call(
        "POST",
        f"/api/teams/{resolved}/meetings",
        {
            "topic": topic,
            "participants": participants or [],
        },
    )

    # Auto-recommend template from topic when template is "free" (default)
    auto_selected = False
    if template == "free" and topic:
        recommended, reason = recommend_template(topic)
        if recommended != "brainstorm" or "brainstorm" in topic.lower():
            template = recommended
            auto_selected = True
            result["_auto_selected"] = {"template": recommended, "reason": reason}

    if template and template != "free" and template in TEMPLATE_ROUNDS:
        result["_template"] = {
            "name": template,
            "auto_selected": auto_selected,
            **TEMPLATE_ROUNDS[template],
        }
    else:
        result["_template"] = {
            "name": "free",
            "description": "自由讨论——无预设结构，按需进行多轮讨论",
            "total_rounds": None,
            "rounds": [],
        }
    return result


# ============================================================
# Tool 8: meeting_send_message
# ============================================================


@mcp.tool()
def meeting_send_message(
    meeting_id: str,
    agent_id: str,
    agent_name: str,
    content: str,
    round_number: int = 1,
) -> dict[str, Any]:
    """Send a discussion message in a meeting.

    Discussion rules:
    - Round 1: Each participant presents their views
    - Round 2+: Must read previous speakers' messages first, cite and respond to specific points
    - Final round: Summarize consensus and disagreements

    Args:
        meeting_id: Meeting ID
        agent_id: ID of the speaking Agent
        agent_name: Name of the speaking Agent
        content: Message content
        round_number: Discussion round number, default 1

    Returns:
        Successfully sent message info
    """
    return _api_call(
        "POST",
        f"/api/meetings/{meeting_id}/messages",
        {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "content": content,
            "round_number": round_number,
        },
    )


# ============================================================
# Tool 9: meeting_read_messages
# ============================================================


@mcp.tool()
def meeting_read_messages(meeting_id: str, limit: int = 100) -> dict[str, Any]:
    """Read all discussion messages in a meeting.

    Args:
        meeting_id: Meeting ID
        limit: Maximum number of messages to return, default 100

    Returns:
        Message list in chronological order
    """
    return _api_call("GET", f"/api/meetings/{meeting_id}/messages?limit={limit}")


# ============================================================
# Tool 10: meeting_conclude
# ============================================================


@mcp.tool()
def meeting_conclude(meeting_id: str) -> dict[str, Any]:
    """Conclude a meeting, marking it as completed.

    Args:
        meeting_id: Meeting ID

    Returns:
        Updated meeting info
    """
    result = _api_call("PUT", f"/api/meetings/{meeting_id}/conclude")
    result["_hint"] = "会议结论已自动保存到团队记忆。可通过 memory_search 或 team_briefing 检索历史决策。"
    return result


# ============================================================
# Tool: meeting_template_list
# ============================================================


@mcp.tool()
def meeting_template_list() -> dict[str, Any]:
    """List available meeting templates and their round structures.

    Returns:
        templates: All available templates with round structure details
    """
    from aiteam.meeting.templates import TEMPLATE_ROUNDS

    return {"templates": TEMPLATE_ROUNDS}


# ============================================================
# Tool 11: task_run
# ============================================================


@mcp.tool()
def task_run(
    team_id: str,
    description: str,
    title: str = "",
    model: str | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    """Create a task in a team, waiting for an Agent to pick up and execute.

    Rule: Set priority (critical/high/medium/low) and horizon (short/mid/long).
    Use depends_on for dependencies; the system auto-manages BLOCKED status.
    Coordinate parallel execution — don't wait for one to complete before starting the next.

    Args:
        team_id: Team ID or name
        description: Task description
        title: Task title (optional)
        model: Specify model to use (optional, metadata only)
        depends_on: List of dependency task IDs (optional, task auto-unlocks when dependencies complete)

    Returns:
        Created task info + related_tasks (similar tasks list, if any)
    """
    payload: dict[str, Any] = {"description": description}
    if title:
        payload["title"] = title
    if model:
        payload["model"] = model
    if depends_on:
        payload["depends_on"] = depends_on
    result = _api_call("POST", f"/api/teams/{team_id}/tasks/run", payload)
    return result


# ============================================================
# Tool: task_decompose
# ============================================================


@mcp.tool()
def task_decompose(
    team_id: str,
    title: str,
    description: str = "",
    template: str = "",
    subtasks: list[dict[str, str]] | None = None,
    auto_assign: bool = False,
) -> dict[str, Any]:
    """Decompose a large task into a parent task + subtasks.

    Supports two approaches:
    1. Use a built-in template to auto-generate subtasks
    2. Manually specify a subtask list

    Available templates: web-app, api-service, data-pipeline, library, refactor, bugfix

    Args:
        team_id: Team ID or name
        title: Parent task title
        description: Parent task description
        template: Built-in template name (optional)
        subtasks: Custom subtask list, each with title and optional description (optional)
        auto_assign: Whether to auto-assign to matching-role Agents (not yet implemented)

    Returns:
        Parent task + subtask list
    """
    payload: dict[str, Any] = {
        "title": title,
        "description": description,
        "template": template,
        "auto_assign": auto_assign,
    }
    if subtasks:
        payload["subtasks"] = subtasks
    return _api_call("POST", f"/api/teams/{team_id}/tasks/decompose", payload)


# ============================================================
# Tool: task_create
# ============================================================


@mcp.tool()
def task_create(
    title: str,
    project_id: str = "",
    description: str = "",
    priority: str = "medium",
    horizon: str = "mid",
    tags: list[str] | None = None,
    auto_start: bool = False,
) -> dict[str, Any]:
    """Create a new task in a project (not bound to a team).

    Project-level tasks are attached directly to the project and visible
    on the project task wall. Suitable for planning-phase tasks not yet assigned to a team.

    Args:
        title: Task title
        project_id: Project ID (optional, auto-uses active project if empty)
        description: Task description
        priority: Priority, one of "critical" / "high" / "medium" / "low"
        horizon: Time horizon, one of "short" / "mid" / "long"
        tags: Tag list
        auto_start: If True, immediately set status to 'running' after creation

    Returns:
        Created task info
    """
    resolved = _resolve_project_id(project_id)
    if not resolved:
        return {"success": False, "error": "未找到活跃项目，请提供 project_id 或先创建项目"}
    payload: dict[str, Any] = {
        "title": title,
        "description": description,
        "priority": priority,
        "horizon": horizon,
    }
    if tags:
        payload["tags"] = tags
    result = _api_call("POST", f"/api/projects/{resolved}/tasks", payload)
    # Auto-start: set task to running immediately after creation
    if auto_start and result.get("success") and result.get("data", {}).get("id"):
        task_id = result["data"]["id"]
        _api_call("PUT", f"/api/tasks/{task_id}", {"status": "running"})
        result["data"]["status"] = "running"
        result["message"] = "任务已创建并开始执行"
    return result


# ============================================================
# Tool 12: task_status
# ============================================================


@mcp.tool()
def task_status(task_id: str) -> dict[str, Any]:
    """Query the current status of a task.

    Args:
        task_id: Task ID

    Returns:
        Task details including status, result, etc.
    """
    return _api_call("GET", f"/api/tasks/{task_id}")


# ============================================================
# Tool: task_update
# ============================================================


@mcp.tool()
def task_update(
    task_id: str,
    status: str = "",
    assigned_to: str = "",
    result: str = "",
    priority: str = "",
    tags: list[str] | None = None,
    title: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Update a task's fields (partial update — only provided fields are changed).

    Status transitions automatically set timestamps:
      - running  → started_at = now
      - completed → completed_at = now

    Args:
        task_id: Task ID (required)
        status: New status: pending / blocked / running / completed / failed
        assigned_to: Agent name or ID to assign the task to
        result: Task result text (typically filled when completing)
        priority: Priority: critical / high / medium / low
        tags: New tag list (replaces existing tags)
        title: New task title
        description: New task description

    Returns:
        Updated task data
    """
    payload: dict[str, Any] = {}
    if status:
        payload["status"] = status
    if assigned_to:
        payload["assigned_to"] = assigned_to
    if result:
        payload["result"] = result
    if priority:
        payload["priority"] = priority
    if tags is not None:
        payload["tags"] = tags
    if title:
        payload["title"] = title
    if description:
        payload["description"] = description
    return _api_call("PUT", f"/api/tasks/{task_id}", payload)


# ============================================================
# Tool: task_auto_match
# ============================================================


@mcp.tool()
def task_auto_match(team_id: str) -> dict[str, Any]:
    """Get intelligent task-Agent matching suggestions.

    Analyzes the match between pending unassigned tasks and idle/offline Agents
    in the team, returning recommended assignments sorted by match_score.

    Args:
        team_id: Team ID or name

    Returns:
        Matching suggestions list, each containing task_id, task_title, agent_id, agent_name, match_score
    """
    return _api_call("GET", f"/api/teams/{team_id}/task-matches")


# ============================================================
# Tool 13: memory_search
# ============================================================


@mcp.tool()
def memory_search(
    query: str = "",
    scope: str = "global",
    scope_id: str = "system",
    limit: int = 10,
) -> dict[str, Any]:
    """Search the memory store in AI Team OS.

    Args:
        query: Search keywords
        scope: Memory scope, default "global"
        scope_id: Scope ID, default "system"
        limit: Maximum number of results, default 10

    Returns:
        List of matching memories
    """
    params = urllib.parse.urlencode({"scope": scope, "scope_id": scope_id, "query": query, "limit": limit})
    return _api_call("GET", f"/api/memory?{params}")


# ============================================================
# Tool: team_knowledge
# ============================================================


@mcp.tool()
def team_knowledge(
    team_id: str = "",
    type: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Query the team knowledge base — retrieve accumulated experience and lessons learned.

    Returns memories with scope=team for this team, including:
    - failure_alchemy: Lessons from failure alchemy
    - lesson_learned: Manually recorded experiences
    - loop_review: Loop review summaries

    New Agents should call this tool before joining to get team historical knowledge for quick onboarding.

    Args:
        team_id: Team ID (leave empty to auto-get active team)
        type: Type filter, one of failure_alchemy / lesson_learned / loop_review (empty returns all)
        limit: Maximum number of results, default 20

    Returns:
        Team knowledge memory list
    """
    resolved_id = _resolve_team_id(team_id)
    if not resolved_id:
        return {"success": False, "error": "未找到活跃团队，请传入 team_id"}
    params_dict: dict[str, Any] = {"limit": limit}
    if type:
        params_dict["type"] = type
    params = urllib.parse.urlencode(params_dict)
    return _api_call("GET", f"/api/teams/{resolved_id}/knowledge?{params}")


# ============================================================
# Tool 14: event_list
# ============================================================


@mcp.tool()
def event_list(limit: int = 50) -> dict[str, Any]:
    """List recent events in the system.

    Args:
        limit: Maximum number of events to return, default 50

    Returns:
        Event list with event type, source, and timestamp
    """
    return _api_call("GET", f"/api/events?limit={limit}")


# ============================================================
# Tool 15: os_health_check
# ============================================================


@mcp.tool()
def os_health_check() -> dict[str, Any]:
    """Check the health status of the AI Team OS API service.

    Verifies the API service is running normally by accessing the team list endpoint.

    Returns:
        Health status info including API reachability and team count
    """
    result = _api_call("GET", "/api/teams")
    if result.get("success") is False:
        return {
            "status": "unhealthy",
            "api_url": API_URL,
            "error": result.get("error", "未知错误"),
            "hint": result.get("hint", "请确保 FastAPI 服务已启动: aiteam serve"),
        }
    return {
        "status": "healthy",
        "api_url": API_URL,
        "teams_count": result.get("total", 0),
    }


# ============================================================
# Tool 16: team_briefing
# ============================================================


@mcp.tool()
def team_briefing(team_id: str) -> dict[str, Any]:
    """Get a team panoramic briefing — understand full team status in one call.

    Returns team info, member status, recent events, recent meetings, pending tasks, and action suggestions.

    Args:
        team_id: Team ID or team name

    Returns:
        Team panoramic briefing containing agents / recent_events / recent_meeting / pending_tasks / _hints
    """
    return _api_call("GET", f"/api/teams/{team_id}/briefing")


# ============================================================
# Tool 17: project_create
# ============================================================


@mcp.tool()
def project_create(
    name: str,
    description: str = "",
    root_path: str = "",
) -> dict[str, Any]:
    """Create a new project with a default Phase automatically created.

    Args:
        name: Project name
        description: Project description
        root_path: Project root directory path (optional, UNIQUE)

    Returns:
        Created project info including project_id
    """
    return _api_call(
        "POST",
        "/api/projects",
        {
            "name": name,
            "description": description,
            "root_path": root_path,
        },
    )


# ============================================================
# Tool 18: phase_create
# ============================================================


@mcp.tool()
def phase_create(
    project_id: str,
    name: str,
    description: str = "",
    order: int = 0,
) -> dict[str, Any]:
    """Create a new development phase in a project.

    Args:
        project_id: Project ID
        name: Phase name
        description: Phase description
        order: Sort order, default 0

    Returns:
        Created phase info including phase_id
    """
    return _api_call(
        "POST",
        f"/api/projects/{project_id}/phases",
        {
            "name": name,
            "description": description,
            "order": order,
        },
    )


# ============================================================
# Tool 19: phase_list
# ============================================================


@mcp.tool()
def phase_list(project_id: str) -> dict[str, Any]:
    """List all Phases and their statuses for a project.

    Args:
        project_id: Project ID

    Returns:
        Phase list with name, status, and sort order for each Phase
    """
    return _api_call("GET", f"/api/projects/{project_id}/phases")


# ============================================================
# Tool 20: team_setup_guide
# ============================================================

_PROJECT_TYPE_ROLES: dict[str, dict[str, Any]] = {
    "web-app": {
        "description": "全栈Web应用项目",
        "roles": [
            {
                "name": "tech-lead",
                "count": 1,
                "description": "架构设计、技术决策、代码审查",
                "template": "tech-lead",
            },
            {
                "name": "backend-engineer",
                "count": "1-2",
                "description": "API开发、数据库设计、业务逻辑",
                "template": "team-member",
            },
            {
                "name": "frontend-engineer",
                "count": "1-2",
                "description": "UI组件、页面交互、响应式布局",
                "template": "team-member",
            },
            {
                "name": "qa-engineer",
                "count": 1,
                "description": "端到端测试、跨浏览器兼容性",
                "template": "team-member",
            },
        ],
    },
    "api-service": {
        "description": "后端API服务项目",
        "roles": [
            {
                "name": "tech-lead",
                "count": 1,
                "description": "API架构、接口规范、性能优化",
                "template": "tech-lead",
            },
            {
                "name": "backend-engineer",
                "count": "2-3",
                "description": "端点开发、中间件、数据持久化",
                "template": "team-member",
            },
            {
                "name": "qa-engineer",
                "count": 1,
                "description": "API测试、负载测试、契约测试",
                "template": "team-member",
            },
        ],
    },
    "data-pipeline": {
        "description": "数据处理管道项目",
        "roles": [
            {
                "name": "tech-lead",
                "count": 1,
                "description": "管道架构、数据流设计",
                "template": "tech-lead",
            },
            {
                "name": "data-engineer",
                "count": "2-3",
                "description": "ETL开发、数据清洗、调度配置",
                "template": "team-member",
            },
            {
                "name": "qa-engineer",
                "count": 1,
                "description": "数据质量验证、回归测试",
                "template": "team-member",
            },
        ],
    },
    "library": {
        "description": "可复用库/SDK项目",
        "roles": [
            {
                "name": "tech-lead",
                "count": 1,
                "description": "API设计、版本策略、兼容性",
                "template": "tech-lead",
            },
            {
                "name": "developer",
                "count": "1-2",
                "description": "核心实现、文档编写",
                "template": "team-member",
            },
            {
                "name": "qa-engineer",
                "count": 1,
                "description": "单元测试、集成测试、示例验证",
                "template": "team-member",
            },
        ],
    },
    "refactor": {
        "description": "代码重构项目",
        "roles": [
            {
                "name": "tech-lead",
                "count": 1,
                "description": "重构策略、影响分析、渐进式迁移",
                "template": "tech-lead",
            },
            {
                "name": "developer",
                "count": "1-2",
                "description": "代码迁移、依赖更新",
                "template": "team-member",
            },
            {
                "name": "qa-engineer",
                "count": 1,
                "description": "回归测试、行为一致性验证",
                "template": "team-member",
            },
        ],
    },
    "bugfix": {
        "description": "Bug修复项目",
        "roles": [
            {
                "name": "developer",
                "count": "1-2",
                "description": "问题定位、修复实现",
                "template": "team-member",
            },
            {
                "name": "qa-engineer",
                "count": 1,
                "description": "复现验证、回归测试",
                "template": "team-member",
            },
        ],
    },
}


@mcp.tool()
def team_setup_guide(project_type: str = "web-app") -> dict[str, Any]:
    """Get recommended team role configuration based on project type.

    Args:
        project_type: Project type, options: web-app, api-service, data-pipeline, library, refactor, bugfix

    Returns:
        Recommended role list and setup tips
    """
    config = _PROJECT_TYPE_ROLES.get(project_type)
    if config is None:
        return {
            "success": False,
            "error": f"未知项目类型: {project_type}",
            "available_types": list(_PROJECT_TYPE_ROLES.keys()),
        }
    return {
        "success": True,
        "data": {
            "project_type": project_type,
            "description": config["description"],
            "recommended_roles": config["roles"],
            "tip": "使用CC的Agent tool创建子agent时，指定subagent_type为agent模板名（如tech-lead, team-member）",
        },
    }


# ============================================================
# Tool 21: loop_start
# ============================================================


@mcp.tool()
def loop_start(team_id: str) -> dict[str, Any]:
    """Start the company loop — Leader continuous work mode.

    After starting, continuously picks up highest-priority tasks. Triggers review discussion every N tasks.
    When tasks are insufficient, organize meetings to discuss direction; don't create busywork.

    Tip: Use /continuous-mode to get the full continuous work protocol,
    including loop pickup, pause/resume, member management, and detailed behavioral guidelines.

    Args:
        team_id: Team ID or name

    Returns:
        Loop status info including current phase and cycle count
    """
    result = _api_call("POST", f"/api/teams/{team_id}/loop/start")
    return result


# ============================================================
# Tool 22: loop_status
# ============================================================


@mcp.tool()
def loop_status(team_id: str) -> dict[str, Any]:
    """View current company loop status — phase, cycle, completed task count.

    Args:
        team_id: Team ID or name

    Returns:
        Loop status details including phase / current_cycle / completed_tasks_count
    """
    return _api_call("GET", f"/api/teams/{team_id}/loop/status")


# ============================================================
# Tool 23: loop_next_task
# ============================================================


@mcp.tool()
def loop_next_task(team_id: str, agent_id: str = "") -> dict[str, Any]:
    """Get the next task to execute — sorted by priority x time horizon x readiness.

    Pinned and critical tasks are picked up first. short > mid > long priority.
    BLOCKED tasks auto-unlock when dependencies complete; no manual handling needed.

    Args:
        team_id: Team ID or name
        agent_id: Specify Agent ID to prioritize tasks assigned to that Agent (optional)

    Returns:
        Next pending task info; empty when no tasks available
    """
    payload: dict[str, Any] = {}
    if agent_id:
        payload["agent_id"] = agent_id
    result = _api_call("POST", f"/api/teams/{team_id}/loop/next-task", payload)
    return result


# ============================================================
# Tool 24: loop_advance
# ============================================================


@mcp.tool()
def loop_advance(team_id: str, trigger: str) -> dict[str, Any]:
    """Advance the loop to the next phase.

    Available triggers:
    - tasks_planned: Planning done -> Execute
    - batch_completed: A batch of tasks completed -> Monitor
    - all_tasks_done: All completed -> Review
    - issues_found: Issues found -> Return to Execute
    - all_clear: All clear -> Review
    - new_tasks_added: New tasks added -> Re-plan
    - no_more_tasks: No more tasks -> Idle

    Args:
        team_id: Team ID or name
        trigger: Trigger name

    Returns:
        Updated loop status
    """
    return _api_call("POST", f"/api/teams/{team_id}/loop/advance", {"trigger": trigger})


# ============================================================
# Tool 25: loop_pause
# ============================================================


@mcp.tool()
def loop_pause(team_id: str) -> dict[str, Any]:
    """Pause the loop — preserve current state, can be resumed at any time.

    Args:
        team_id: Team ID or name

    Returns:
        Loop status after pausing
    """
    return _api_call("POST", f"/api/teams/{team_id}/loop/pause")


# ============================================================
# Tool 26: loop_resume
# ============================================================


@mcp.tool()
def loop_resume(team_id: str) -> dict[str, Any]:
    """Resume the loop — continue from where it was paused.

    Args:
        team_id: Team ID or name

    Returns:
        Loop status after resuming
    """
    return _api_call("POST", f"/api/teams/{team_id}/loop/resume")


# ============================================================
# Tool: loop_review
# ============================================================


@mcp.tool()
def loop_review(team_id: str) -> dict[str, Any]:
    """Trigger a company loop review — auto-create a review meeting and generate statistics report.

    The review meeting contains: summary of tasks completed this cycle, failed task analysis, and next-step suggestions.
    Leader and team can discuss and produce new to-do tasks in the meeting.

    Args:
        team_id: Team ID or name

    Returns:
        Review meeting info including meeting_id / stats / topic
    """
    return _api_call("POST", f"/api/teams/{team_id}/loop/review")


# ============================================================
# Tool 27: taskwall_view
# ============================================================


@mcp.tool()
def taskwall_view(
    team_id: str,
    horizon: str = "",
    priority: str = "",
) -> dict[str, Any]:
    """Get the task wall view — categorized by short/mid/long term with intelligent sorting.

    Returns a task list sorted by score, helping Leader quickly understand what to do next.

    Args:
        team_id: Team ID or name
        horizon: Filter by time horizon, one of "short" / "mid" / "long" (empty = all)
        priority: Filter by priority, one of "critical" / "high" / "medium" / "low",
            comma-separated for multiple (empty = all)

    Returns:
        Task wall data grouped by short/mid/long, each group sorted by score descending
    """
    params: list[str] = []
    if horizon:
        params.append(f"horizon={urllib.parse.quote(horizon)}")
    if priority:
        params.append(f"priority={urllib.parse.quote(priority)}")
    qs = f"?{'&'.join(params)}" if params else ""
    return _api_call("GET", f"/api/teams/{team_id}/task-wall{qs}")


# ============================================================
# Tool 28: os_report_issue
# ============================================================


@mcp.tool()
def os_report_issue(
    team_id: str,
    title: str,
    description: str = "",
    severity: str = "medium",
    category: str = "bug",
) -> dict[str, Any]:
    """Report an issue to the team. Issues are created as high-priority tasks, auto-tagged as issue type.

    Severity maps to task priority: critical->critical, high->high, medium->high, low->medium.

    Args:
        team_id: Team ID or name
        title: Issue title
        description: Detailed issue description
        severity: Severity level, one of "critical" / "high" / "medium" / "low"
        category: Issue category, e.g., "bug" / "performance" / "security" / "ux"

    Returns:
        Created Issue task info
    """
    return _api_call(
        "POST",
        f"/api/teams/{team_id}/issues",
        {
            "title": title,
            "description": description,
            "severity": severity,
            "category": category,
        },
    )


# ============================================================
# Tool 29: os_resolve_issue
# ============================================================


@mcp.tool()
def os_resolve_issue(issue_id: str, resolution: str) -> dict[str, Any]:
    """Mark an Issue as resolved with a resolution description.

    Updates the Issue status to resolved and records the resolution.
    The corresponding task is also marked as completed.

    Args:
        issue_id: Issue (task) ID
        resolution: Resolution description

    Returns:
        Updated Issue info
    """
    return _api_call(
        "PUT",
        f"/api/issues/{issue_id}/status",
        {
            "status": "resolved",
            "resolution": resolution,
        },
    )


# ============================================================
# Tool 30: task_memo_read
# ============================================================


@mcp.tool()
def task_memo_read(task_id: str) -> dict[str, Any]:
    """Read all memo records for a task — read before picking up a task to understand historical progress.

    Args:
        task_id: Task ID

    Returns:
        Memo record list in chronological order
    """
    return _api_call("GET", f"/api/tasks/{task_id}/memo")


# ============================================================
# Tool 31: task_memo_add
# ============================================================


@mcp.tool()
def task_memo_add(
    task_id: str,
    content: str,
    memo_type: str = "progress",
    author: str = "leader",
) -> dict[str, Any]:
    """Add a memo record to a task — for tracking progress, recording decisions, marking issues.

    Args:
        task_id: Task ID
        content: Memo content
        memo_type: Type, one of "progress" / "decision" / "issue" / "summary"
        author: Author name, default "leader"

    Returns:
        Added memo record
    """
    return _api_call(
        "POST",
        f"/api/tasks/{task_id}/memo",
        {
            "content": content,
            "type": memo_type,
            "author": author,
        },
    )


# ============================================================
# Tool: agent_template_list
# ============================================================


@mcp.tool()
def agent_template_list() -> dict[str, Any]:
    """List all available Agent templates (from ~/.claude/agents/).

    Returns a template list and a grouped-by-category view to help choose the right Agent role template.

    Returns:
        templates: All template list
        grouped: Templates grouped by category
        total: Total template count
    """
    return _api_call("GET", "/api/agent-templates")


# ============================================================
# Tool: agent_template_recommend
# ============================================================


@mcp.tool()
def agent_template_recommend(task_type: str = "", keywords: str = "") -> dict[str, Any]:
    """Recommend suitable Agent templates based on task type and keywords.

    Args:
        task_type: Task type, e.g., "backend", "frontend", "data-analysis"
        keywords: Keywords, space-separated, e.g., "python api database"

    Returns:
        recommendations: Up to 5 matching templates sorted by relevance
        query: Actual query string used
    """
    params = urllib.parse.urlencode({"task_type": task_type, "keywords": keywords})
    return _api_call("GET", f"/api/agent-templates/recommend?{params}")


# ============================================================
# FastAPI auto-start helpers
# ============================================================


def _is_port_open(host: str = "127.0.0.1", port: int = 8000) -> bool:
    """Check if the specified port is already listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def _cleanup_api() -> None:
    """Terminate the FastAPI subprocess on process exit."""
    global _api_process
    if _api_process is not None and _api_process.poll() is None:
        _api_process.terminate()
        try:
            _api_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _api_process.kill()
        _api_process = None


def _ensure_api_running() -> None:
    """Auto-start the FastAPI subprocess if it is not already running.

    MCP Server communicates in stdio mode, so the subprocess's stdout must be
    redirected to DEVNULL to avoid polluting the MCP protocol channel.
    """
    global _api_process
    if _is_port_open():
        logger.info("FastAPI already running on port 8000, skipping auto-start")
        return
    logger.info("Starting FastAPI subprocess on port 8000...")
    try:
        _api_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "aiteam.api.app:create_app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--factory",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except Exception as exc:
        logger.warning("Failed to start FastAPI subprocess: %s", exc)
        return
    atexit.register(_cleanup_api)
    for _i in range(20):
        time.sleep(0.5)
        if _is_port_open():
            logger.info("FastAPI subprocess is ready")
            return
        if _api_process.poll() is not None:
            logger.warning("FastAPI subprocess exited prematurely (code=%s)", _api_process.returncode)
            _api_process = None
            return
    logger.warning("FastAPI subprocess did not become ready within 10s")


# ============================================================
# Tool: decision_log
# ============================================================


@mcp.tool()
def decision_log(
    team_id: str = "",
    event_type: str = "decision",
    limit: int = 20,
) -> dict[str, Any]:
    """Query team decision log — task assignments, approach selections, Agent scheduling decisions.

    Args:
        team_id: Team ID (empty string to query all teams)
        event_type: Event type or prefix, e.g., "decision", "decision.task_assigned",
                    "knowledge", "intent". Default "decision" returns all decision events.
        limit: Maximum number of results (default 20, max 200)

    Returns:
        Dict containing a decision event list, sorted by time descending.
        Each event's data field contains:
        - rationale: Decision rationale
        - alternatives: Alternative options list
        - outcome: Decision outcome (pending/success/failed)
    """
    params: list[str] = [f"limit={limit}"]
    if team_id:
        params.append(f"team_id={urllib.parse.quote(team_id)}")
    if event_type:
        # Convert plain namespace ("decision") to prefix filter ("decision.")
        type_param = event_type if "." in event_type else f"{event_type}."
        params.append(f"type={urllib.parse.quote(type_param)}")
    query = "&".join(params)
    return _api_call("GET", f"/api/decisions?{query}")


# ============================================================
# Tool: failure_analysis
# ============================================================


@mcp.tool()
def failure_analysis(task_id: str, team_id: str) -> dict[str, Any]:
    """Analyze failed tasks, distill defense rules + training cases + improvement proposals (failure alchemy).

    When a task permanently fails (exceeds retry limit), call this tool for deep failure analysis.
    Automatically generates three learning artifacts saved to team memory:
    - Antibody: Defensive rule suggestions to prevent similar failures
    - Vaccine: Structured failure case for new Agents to reference and learn from
    - Catalyst: System improvement proposals to drive process optimization

    Args:
        task_id: ID of the failed task
        team_id: ID of the owning team

    Returns:
        Dict containing antibody, vaccine, and catalyst artifacts
    """
    return _api_call("POST", f"/api/teams/{team_id}/failure-analysis", {"task_id": task_id})


# ============================================================
# Tool: what_if_analysis
# ============================================================


@mcp.tool()
def what_if_analysis(task_id: str, team_id: str = "") -> dict[str, Any]:
    """Perform What-If analysis on a task — generate multi-approach comparison and recommendation.

    During task planning, generates 2-3 alternative approaches with quick scoring comparison:
    - Approach A: Best role-match assignment (lowest risk)
    - Approach B: Parallel split execution (faster, appears when idle agents >= 2)
    - Approach C: History-driven based on experience (appears when team has memory)

    Args:
        task_id: Task ID to analyze
        team_id: Owning team ID (optional, can be empty if task is already bound to a team)

    Returns:
        Dict containing approaches list, recommendation, and analysis time
    """
    params = f"?team_id={urllib.parse.quote(team_id)}" if team_id else ""
    return _api_call("GET", f"/api/tasks/{task_id}/what-if{params}")


# ============================================================
# Scheduler tools
# ============================================================


def _parse_interval(interval: str) -> int:
    """Parse human-readable interval string to seconds.

    Examples: "2 days" -> 172800, "1 hour" -> 3600, "30 minutes" -> 1800,
              "5 min" -> 300, "1d" -> 86400, "2h" -> 7200
    """
    interval = interval.strip().lower()
    import re

    # Match patterns like "2 days", "1 hour", "30 minutes", "5 min", "1d", "2h", "30m"
    m = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*(d|day|days|h|hr|hour|hours|m|min|mins|minute|minutes|s|sec|second|seconds)", interval
    )
    if not m:
        raise ValueError(
            f"Cannot parse interval '{interval}'. Use formats like '2 days', '1 hour', '30 minutes', '300 seconds'."
        )
    value = float(m.group(1))
    unit = m.group(2)
    if unit in ("d", "day", "days"):
        return int(value * 86400)
    elif unit in ("h", "hr", "hour", "hours"):
        return int(value * 3600)
    elif unit in ("m", "min", "mins", "minute", "minutes"):
        return int(value * 60)
    else:
        return int(value)


@mcp.tool()
def scheduler_create(
    name: str,
    interval: str,
    action_type: str,
    action_config: str = "{}",
    team_id: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Create a scheduled task that triggers automatically on a fixed interval.

    Args:
        name: Task name (unique identifier)
        interval: Human-readable interval, e.g. "2 days", "1 hour", "30 minutes" (minimum 5 minutes)
        action_type: One of "create_task" / "inject_reminder" / "emit_event"
        action_config: JSON string with action parameters.
            - create_task: {"title": "...", "description": "...", "priority": "medium"}
            - inject_reminder: {"message": "..."}
            - emit_event: {"event_type": "...", "data": {...}}
        team_id: Team ID to scope this task (optional)
        description: Human-readable description

    Returns:
        Created scheduled task info
    """
    try:
        interval_seconds = _parse_interval(interval)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    if interval_seconds < 300:
        return {"success": False, "error": f"Interval too short ({interval_seconds}s). Minimum is 300s (5 minutes)."}

    try:
        config = json.loads(action_config) if action_config else {}
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"Invalid action_config JSON: {e}"}

    payload: dict[str, Any] = {
        "name": name,
        "interval_seconds": interval_seconds,
        "action_type": action_type,
        "action_config": config,
        "description": description,
    }
    if team_id:
        payload["team_id"] = team_id

    return _api_call("POST", "/api/scheduler", payload)


@mcp.tool()
def scheduler_list(team_id: str = "") -> dict[str, Any]:
    """List all scheduled tasks, optionally filtered by team.

    Args:
        team_id: Filter by team ID (optional, empty = list all)

    Returns:
        List of scheduled tasks with status and next_run_at
    """
    path = "/api/scheduler"
    if team_id:
        path += f"?team_id={urllib.parse.quote(team_id)}"
    return _api_call("GET", path)


@mcp.tool()
def scheduler_pause(task_id: str) -> dict[str, Any]:
    """Pause a scheduled task (set enabled=False).

    Args:
        task_id: Scheduled task ID

    Returns:
        Updated task info
    """
    return _api_call("PUT", f"/api/scheduler/{task_id}", {"enabled": False})


@mcp.tool()
def scheduler_delete(task_id: str) -> dict[str, Any]:
    """Permanently delete a scheduled task.

    Args:
        task_id: Scheduled task ID

    Returns:
        Deletion result
    """
    return _api_call("DELETE", f"/api/scheduler/{task_id}")


# ============================================================
# Tool: find_skill — Progressive ecosystem skill discovery
# ============================================================


@mcp.tool()
def find_skill(
    task_description: str = "",
    level: int = 1,
    category: str = "",
    skill_id: str = "",
) -> dict[str, Any]:
    """Find ecosystem skills/plugins using a 3-layer progressive loading system.

    Layer 1 (quick recommend): Describe your task and get top 3-5 matching skills
        with one-line descriptions and install commands.
    Layer 2 (category browse): Browse all skills grouped by category
        (memory / code-quality / frontend / security / dev-workflow / etc.).
    Layer 3 (full detail): Get complete documentation for a single skill
        including features, OS complement relationship, and variants.

    Args:
        task_description: What you want to accomplish (used for level=1 matching).
                          Examples: "frontend ui design", "security audit web app",
                          "data science jupyter", "code review PR".
        level: Discovery depth — 1=quick (default), 2=category, 3=full detail.
        category: Category filter for level=2 (e.g., "frontend", "security").
                  Empty string returns all categories.
        skill_id: Skill identifier for level=3 detail lookup
                  (e.g., "vibesec", "superpowers", "claude-mem").

    Returns:
        Dict with level info, results, and hints for deeper exploration.
    """
    from aiteam.mcp.skill_registry import (
        find_skill_category,
        find_skill_detail,
        find_skill_quick,
    )

    if level == 3:
        if not skill_id:
            return {
                "error": "level=3 requires skill_id parameter.",
                "hint": "Use level=1 with task_description to discover skill IDs first.",
            }
        return find_skill_detail(skill_id)

    if level == 2:
        return find_skill_category(category)

    # Default: level 1 quick recommend
    if not task_description:
        return {
            "error": "level=1 requires task_description parameter.",
            "hint": "Describe what you want to do, e.g. 'build a secure REST API'.",
        }
    return find_skill_quick(task_description)


# ============================================================
# Tool: project_list
# ============================================================


@mcp.tool()
def project_list() -> dict[str, Any]:
    """List all projects in the system.

    Returns:
        projects: List of all projects with id, name, description, root_path, etc.
    """
    return _api_call("GET", "/api/projects")


# ============================================================
# Tool: meeting_list
# ============================================================


@mcp.tool()
def meeting_list(
    team_id: str = "",
    status: str = "",
) -> dict[str, Any]:
    """List meetings for a team, optionally filtered by status.

    Args:
        team_id: Team ID or name (optional, auto-uses active team if empty)
        status: Filter by meeting status: "active" or "concluded" (optional, returns all if empty)

    Returns:
        Meeting list with topic, status, participant count, etc.
    """
    resolved = _resolve_team_id(team_id)
    if not resolved:
        return {"success": False, "error": "未找到活跃团队，请提供 team_id 或先创建团队"}
    path = f"/api/teams/{resolved}/meetings"
    if status:
        path += f"?status={urllib.parse.quote(status)}"
    return _api_call("GET", path)


# ============================================================
# Tool: team_close
# ============================================================


@mcp.tool()
def team_close(team_id: str = "") -> dict[str, Any]:
    """Close (complete) a team — sets team status to completed and marks all busy agents as offline.

    Use this when the team's mission is fully done. Members are not deleted,
    but their status is set to offline automatically.

    Args:
        team_id: Team ID or name (optional, auto-uses active team if empty)

    Returns:
        Updated team info with status=completed
    """
    resolved = _resolve_team_id(team_id)
    if not resolved:
        return {"success": False, "error": "未找到活跃团队，请提供 team_id 或先创建团队"}
    return _api_call("PUT", f"/api/teams/{resolved}", {"status": "completed"})


# ============================================================
# Tool: task_list_project
# ============================================================


@mcp.tool()
def task_list_project(
    project_id: str = "",
    horizon: str = "",
    priority: str = "",
) -> dict[str, Any]:
    """Get project-level task wall — all tasks belonging to a project (across all teams).

    Unlike taskwall_view (which is team-scoped), this returns tasks from all teams
    under a project plus standalone project-level tasks.

    Args:
        project_id: Project ID (optional, auto-uses active project if empty)
        horizon: Filter by time horizon: "short" / "mid" / "long" (optional)
        priority: Filter by priority: "critical" / "high" / "medium" / "low" (optional)

    Returns:
        Project task wall with wall (grouped by horizon), completed tasks, and stats
    """
    resolved = _resolve_project_id(project_id)
    if not resolved:
        return {"success": False, "error": "未找到活跃项目，请提供 project_id 或先创建项目"}
    params: list[str] = []
    if horizon:
        params.append(f"horizon={urllib.parse.quote(horizon)}")
    if priority:
        params.append(f"priority={urllib.parse.quote(priority)}")
    qs = f"?{'&'.join(params)}" if params else ""
    return _api_call("GET", f"/api/projects/{resolved}/task-wall{qs}")


# ============================================================
# Tool: agent_activity_query
# ============================================================


@mcp.tool()
def agent_activity_query(
    team_id: str = "",
    agent_id: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Query Agent activity records for a team.

    Returns recent activity log entries sorted by timestamp descending,
    including action type, duration_ms, and result summary.

    Args:
        team_id: Team ID or name (optional, auto-uses active team if empty)
        agent_id: Filter by a specific Agent ID (optional, returns all agents if empty)
        limit: Maximum number of records to return, default 20

    Returns:
        Activity list with agent_name, action, timestamp, duration_ms, etc.
    """
    resolved = _resolve_team_id(team_id)
    if not resolved:
        return {"success": False, "error": "未找到活跃团队，请提供 team_id 或先创建团队"}
    params: list[str] = [f"limit={limit}"]
    if agent_id:
        params.append(f"agent_id={urllib.parse.quote(agent_id)}")
    qs = "?" + "&".join(params)
    return _api_call("GET", f"/api/teams/{resolved}/activities{qs}")


# ============================================================
# Tool: meeting_update
# ============================================================


@mcp.tool()
def meeting_update(
    meeting_id: str,
    topic: str = "",
    participants: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Update meeting fields (topic, participants, notes).

    Use this to add conclusions/notes to a meeting or update its topic.
    To formally conclude a meeting (mark as concluded), use meeting_conclude instead.

    Args:
        meeting_id: Meeting ID (required)
        topic: New topic text (optional)
        participants: Updated participant list (optional)
        notes: Meeting notes or conclusion summary to store (optional)

    Returns:
        Updated meeting info
    """
    payload: dict[str, Any] = {}
    if topic:
        payload["topic"] = topic
    if participants is not None:
        payload["participants"] = participants
    if notes:
        payload["notes"] = notes
    if not payload:
        return {"success": False, "error": "至少需要提供一个更新字段（topic / participants / notes）"}
    return _api_call("PUT", f"/api/meetings/{meeting_id}", payload)


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    _ensure_api_running()
    mcp.run()
