"""AI Team OS — MCP Server.

提供 MCP tools，通过 HTTP 调用本地 FastAPI (localhost:8000) 的对应 API 端点。
MCP Server 以 stdio 模式运行，与 FastAPI 进程完全解耦。
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
    """统一的 API 调用 helper，使用 urllib 标准库。

    Args:
        method: HTTP 方法 (GET / POST / PUT / DELETE)
        path: API 路径，如 /api/teams
        data: 请求体数据（仅 POST/PUT 使用）

    Returns:
        API 响应的 JSON dict
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
    """创建一个新的 AI Agent 团队。

    如果指定了 leader_agent_id，会自动完成该 Leader 的旧 active 团队。
    一个 Leader 同时只能领导一个 active 团队。

    Args:
        name: 团队名称
        mode: 协作模式，可选 "coordinate"（协调）或 "broadcast"（广播）
        project_id: 关联的项目 ID（可选）
        leader_agent_id: 领导此团队的 Leader agent ID（可选，用于自动完成旧团队）

    Returns:
        创建的团队信息，包含 team_id
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
                {"name": "qa-observer", "role": "常驻QA观察员", "description": "持续监控系统行为、检查前端显示、发现bug并上报"},
                {"name": "bug-fixer", "role": "常驻Bug工程师", "description": "接收QA报告，定位并修复bug，验证修复效果"},
            ],
        },
        "temporary_members": {
            "hint": "以下角色按需创建，任务完成后Kill释放资源：",
            "roles": [
                {"name": "developer", "count": "1-3", "description": "开发工程师，负责具体实现"},
                {"name": "researcher", "count": "1-3", "description": "研究员，负责技术调研和方案设计"},
                {"name": "tech-lead", "count": 1, "description": "技术负责人，负责架构决策"},
            ],
        },
        "lifecycle_rule": "团队不关闭——只Kill临时成员。QA和Bug-fixer保持团队活跃。需要开发/研究时往团队加人，完成后Kill。",
    }
    return result


# ============================================================
# Tool 2: team_status
# ============================================================


@mcp.tool()
def team_status(team_id: str) -> dict[str, Any]:
    """获取指定团队的详细信息和状态。

    Args:
        team_id: 团队 ID 或团队名称

    Returns:
        团队详情，包含名称、模式、成员数等
    """
    return _api_call("GET", f"/api/teams/{team_id}")


# ============================================================
# Tool 3: team_list
# ============================================================


@mcp.tool()
def team_list() -> dict[str, Any]:
    """列出所有已创建的团队。

    Returns:
        团队列表，包含每个团队的基本信息
    """
    return _api_call("GET", "/api/teams")


# ============================================================
# Tool 4: agent_register
# ============================================================


def _load_agent_prompt_template() -> str:
    """加载Agent标准化prompt模板."""
    # server.py位于 src/aiteam/mcp/server.py，需往上4层到项目根目录
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
        "plugin", "config", "agent-prompt-template.md",
    )
    try:
        with open(template_path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("Agent prompt模板文件不存在: %s", template_path)
        return ""


def _render_agent_prompt(role: str, project_path: str = "") -> str:
    """用基本信息填充模板."""
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
    """向团队注册一个新的 AI Agent。

    注册成功后状态自动设为busy。
    规则：一次性任务完成后Leader应Kill该Agent，可能有后续任务的保留。
    工具受限时报告Leader解决。

    如果未提供system_prompt，自动使用标准化prompt模板填充。

    Args:
        team_id: 目标团队 ID 或名称
        name: Agent 名称
        role: Agent 角色描述
        model: 使用的模型，默认 claude-opus-4-6
        system_prompt: Agent 的系统提示词（留空则自动使用标准化模板）

    Returns:
        Agent 信息 + teammates 列表 + team_snapshot（含 pending_tasks 和 recent_meeting）
    """
    effective_prompt = system_prompt
    if not effective_prompt:
        # MCP层无法直接查询project的root_path，模板中{project_path}显示"未指定"
        # hook_translator的auto-register路径可以获取到准确的project_path
        effective_prompt = _render_agent_prompt(role)

    return _api_call("POST", f"/api/teams/{team_id}/agents", {
        "name": name,
        "role": role,
        "model": model,
        "system_prompt": effective_prompt,
    })


# ============================================================
# Tool 5: agent_update_status
# ============================================================


@mcp.tool()
def agent_update_status(
    agent_id: str,
    status: str,
) -> dict[str, Any]:
    """更新 Agent 的运行状态。

    Args:
        agent_id: Agent ID
        status: 新状态，可选 "busy"、"waiting"、"offline"

    Returns:
        更新后的 Agent 信息
    """
    return _api_call("PUT", f"/api/agents/{agent_id}/status", {"status": status})


# ============================================================
# Tool 6: agent_list
# ============================================================


@mcp.tool()
def agent_list(team_id: str) -> dict[str, Any]:
    """列出团队中所有已注册的 Agent。

    Args:
        team_id: 团队 ID 或名称

    Returns:
        Agent 列表，包含每个 Agent 的状态和角色
    """
    return _api_call("GET", f"/api/teams/{team_id}/agents")


# ============================================================
# Tool: context_resolve
# ============================================================


@mcp.tool()
def context_resolve() -> dict[str, Any]:
    """获取当前活跃的OS上下文 — 活跃项目、活跃团队、成员列表、循环状态.

    这是所有简化操作的基础设施。一次调用返回当前工作环境的完整上下文，
    供Leader或其他工具自动填充project_id、team_id等参数。

    Returns:
        包含 project / team / agents / loop 的上下文字典
    """
    result: dict[str, Any] = {"project": None, "team": None, "agents": [], "loop": None}

    try:
        # 获取团队列表，找active团队
        teams_data = _api_call("GET", "/api/teams")
        active_teams = [t for t in teams_data.get("data", []) if t.get("status") == "active"]
        if active_teams:
            team = active_teams[0]
            result["team"] = {"id": team["id"], "name": team["name"]}
            # 获取成员
            agents_data = _api_call("GET", f"/api/teams/{team['id']}/agents")
            result["agents"] = [
                {"name": a["name"], "status": a["status"], "role": a.get("role", "")}
                for a in agents_data.get("data", [])
            ]
            # 项目
            if team.get("project_id"):
                result["project"] = {"id": team["project_id"]}

        # 如果没有从团队获取到项目，尝试直接获取项目列表
        if not result["project"]:
            projects_data = _api_call("GET", "/api/projects")
            projects = projects_data.get("data", [])
            if projects:
                result["project"] = {"id": projects[0]["id"], "name": projects[0].get("name", "")}

        # 获取循环状态（如果有活跃团队）
        if result["team"]:
            loop_data = _api_call("GET", f"/api/teams/{result['team']['id']}/loop/status")
            if loop_data.get("success") is not False:
                result["loop"] = loop_data.get("data") or loop_data

    except Exception as e:
        result["error"] = str(e)

    return result


def _resolve_team_id(team_id: str) -> str:
    """如果 team_id 为空，自动从 context_resolve 获取活跃团队 ID."""
    if team_id:
        return team_id
    ctx = context_resolve()
    team = ctx.get("team")
    if team and team.get("id"):
        return team["id"]
    return ""


def _resolve_project_id(project_id: str) -> str:
    """如果 project_id 为空，自动从 context_resolve 获取活跃项目 ID."""
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
) -> dict[str, Any]:
    """创建团队会议，用于多 Agent 协作讨论。

    规则：根据议题动态添加合适参与者，讨论中发现新方向时随时招募专家。
    讨论结论应转为任务放入任务墙。

    Args:
        topic: 会议讨论主题
        team_id: 团队 ID 或名称（可选，留空则自动使用活跃团队）
        participants: 参会 Agent ID 列表，为空则全员参与

    Returns:
        会议信息，包含 meeting_id 和操作指引
    """
    resolved = _resolve_team_id(team_id)
    if not resolved:
        return {"success": False, "error": "未找到活跃团队，请提供 team_id 或先创建团队"}
    return _api_call("POST", f"/api/teams/{resolved}/meetings", {
        "topic": topic,
        "participants": participants or [],
    })


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
    """在会议中发送讨论消息。

    讨论规则：
    - Round 1: 各自提出观点
    - Round 2+: 必须先读取前人发言，引用并回应具体观点
    - 最后一轮: 汇总共识和分歧

    Args:
        meeting_id: 会议 ID
        agent_id: 发言 Agent 的 ID
        agent_name: 发言 Agent 的名称
        content: 消息内容
        round_number: 讨论轮次，默认 1

    Returns:
        发送成功的消息信息
    """
    return _api_call("POST", f"/api/meetings/{meeting_id}/messages", {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "content": content,
        "round_number": round_number,
    })


# ============================================================
# Tool 9: meeting_read_messages
# ============================================================


@mcp.tool()
def meeting_read_messages(meeting_id: str, limit: int = 100) -> dict[str, Any]:
    """读取会议中的所有讨论消息。

    Args:
        meeting_id: 会议 ID
        limit: 返回消息数量上限，默认 100

    Returns:
        消息列表，按时间顺序排列
    """
    return _api_call("GET", f"/api/meetings/{meeting_id}/messages?limit={limit}")


# ============================================================
# Tool 10: meeting_conclude
# ============================================================


@mcp.tool()
def meeting_conclude(meeting_id: str) -> dict[str, Any]:
    """结束会议，标记为已完成。

    Args:
        meeting_id: 会议 ID

    Returns:
        更新后的会议信息
    """
    result = _api_call("PUT", f"/api/meetings/{meeting_id}/conclude")
    result["_hint"] = (
        "会议结论已自动保存到团队记忆。"
        "可通过 memory_search 或 team_briefing 检索历史决策。"
    )
    return result


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
    """在团队中创建一个任务，等待Agent领取执行。

    规则：设置priority(critical/high/medium/low)和horizon(short/mid/long)。
    有依赖时设depends_on，系统自动管理BLOCKED状态。统筹并行推进，不等一个完成再开下一个。

    Args:
        team_id: 团队 ID 或名称
        description: 任务描述
        title: 任务标题（可选）
        model: 指定使用的模型（可选，仅记录元数据）
        depends_on: 依赖的任务ID列表（可选，任务将在依赖完成后自动解锁）

    Returns:
        创建的任务信息 + related_tasks（相似任务列表，如有）
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
    """将一个大任务拆解为父任务+子任务。

    支持两种方式：
    1. 使用内置模板（template）自动生成子任务
    2. 手动指定子任务列表（subtasks）

    可用模板: web-app, api-service, data-pipeline, library, refactor, bugfix

    Args:
        team_id: 团队 ID 或名称
        title: 父任务标题
        description: 父任务描述
        template: 内置模板名称（可选）
        subtasks: 自定义子任务列表，每项含 title 和可选 description（可选）
        auto_assign: 是否自动分配给匹配角色的 Agent（暂未实现）

    Returns:
        父任务 + 子任务列表
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
) -> dict[str, Any]:
    """在项目中创建新任务（不绑定团队）。

    项目级任务直接挂在项目下，可在项目任务墙中查看。
    适用于尚未分配团队的规划阶段任务。

    Args:
        title: 任务标题
        project_id: 项目 ID（可选，留空则自动使用活跃项目）
        description: 任务描述
        priority: 优先级，可选 "critical" / "high" / "medium" / "low"
        horizon: 时间跨度，可选 "short" / "mid" / "long"
        tags: 标签列表

    Returns:
        创建的任务信息
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
    return _api_call("POST", f"/api/projects/{resolved}/tasks", payload)


# ============================================================
# Tool 12: task_status
# ============================================================


@mcp.tool()
def task_status(task_id: str) -> dict[str, Any]:
    """查询任务的当前状态。

    Args:
        task_id: 任务 ID

    Returns:
        任务详情，包含状态、结果等
    """
    return _api_call("GET", f"/api/tasks/{task_id}")


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
    """搜索 AI Team OS 中的记忆存储。

    Args:
        query: 搜索关键词
        scope: 记忆作用域，默认 "global"
        scope_id: 作用域 ID，默认 "system"
        limit: 返回数量上限，默认 10

    Returns:
        匹配的记忆列表
    """
    params = urllib.parse.urlencode({"scope": scope, "scope_id": scope_id, "query": query, "limit": limit})
    return _api_call("GET", f"/api/memory?{params}")


# ============================================================
# Tool 14: event_list
# ============================================================


@mcp.tool()
def event_list(limit: int = 50) -> dict[str, Any]:
    """列出系统中的最近事件。

    Args:
        limit: 返回事件数量上限，默认 50

    Returns:
        事件列表，包含事件类型、来源和时间
    """
    return _api_call("GET", f"/api/events?limit={limit}")


# ============================================================
# Tool 15: os_health_check
# ============================================================


@mcp.tool()
def os_health_check() -> dict[str, Any]:
    """检查 AI Team OS API 服务的健康状态。

    通过访问团队列表端点验证 API 服务是否正常运行。

    Returns:
        健康状态信息，包含 API 可达性和团队数量
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
    """获取团队全景简报 — 一次调用了解团队全部状态。

    返回团队信息、成员状态、最近事件、最近会议、待办任务和操作建议。

    Args:
        team_id: 团队 ID 或团队名称

    Returns:
        团队全景简报，包含 agents / recent_events / recent_meeting / pending_tasks / _hints
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
    """创建一个新项目，自动创建默认 Phase。

    Args:
        name: 项目名称
        description: 项目描述
        root_path: 项目根目录路径（可选，UNIQUE）

    Returns:
        创建的项目信息，包含 project_id
    """
    return _api_call("POST", "/api/projects", {
        "name": name,
        "description": description,
        "root_path": root_path,
    })


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
    """在项目中创建一个新的开发阶段。

    Args:
        project_id: 项目 ID
        name: 阶段名称
        description: 阶段描述
        order: 排序序号，默认 0

    Returns:
        创建的阶段信息，包含 phase_id
    """
    return _api_call("POST", f"/api/projects/{project_id}/phases", {
        "name": name,
        "description": description,
        "order": order,
    })


# ============================================================
# Tool 19: phase_list
# ============================================================


@mcp.tool()
def phase_list(project_id: str) -> dict[str, Any]:
    """列出项目的所有 Phase 及其状态。

    Args:
        project_id: 项目 ID

    Returns:
        Phase 列表，包含每个 Phase 的名称、状态和排序
    """
    return _api_call("GET", f"/api/projects/{project_id}/phases")


# ============================================================
# Tool 20: team_setup_guide
# ============================================================

_PROJECT_TYPE_ROLES: dict[str, dict[str, Any]] = {
    "web-app": {
        "description": "全栈Web应用项目",
        "roles": [
            {"name": "tech-lead", "count": 1, "description": "架构设计、技术决策、代码审查", "template": "tech-lead"},
            {"name": "backend-engineer", "count": "1-2", "description": "API开发、数据库设计、业务逻辑", "template": "team-member"},
            {"name": "frontend-engineer", "count": "1-2", "description": "UI组件、页面交互、响应式布局", "template": "team-member"},
            {"name": "qa-engineer", "count": 1, "description": "端到端测试、跨浏览器兼容性", "template": "team-member"},
        ],
    },
    "api-service": {
        "description": "后端API服务项目",
        "roles": [
            {"name": "tech-lead", "count": 1, "description": "API架构、接口规范、性能优化", "template": "tech-lead"},
            {"name": "backend-engineer", "count": "2-3", "description": "端点开发、中间件、数据持久化", "template": "team-member"},
            {"name": "qa-engineer", "count": 1, "description": "API测试、负载测试、契约测试", "template": "team-member"},
        ],
    },
    "data-pipeline": {
        "description": "数据处理管道项目",
        "roles": [
            {"name": "tech-lead", "count": 1, "description": "管道架构、数据流设计", "template": "tech-lead"},
            {"name": "data-engineer", "count": "2-3", "description": "ETL开发、数据清洗、调度配置", "template": "team-member"},
            {"name": "qa-engineer", "count": 1, "description": "数据质量验证、回归测试", "template": "team-member"},
        ],
    },
    "library": {
        "description": "可复用库/SDK项目",
        "roles": [
            {"name": "tech-lead", "count": 1, "description": "API设计、版本策略、兼容性", "template": "tech-lead"},
            {"name": "developer", "count": "1-2", "description": "核心实现、文档编写", "template": "team-member"},
            {"name": "qa-engineer", "count": 1, "description": "单元测试、集成测试、示例验证", "template": "team-member"},
        ],
    },
    "refactor": {
        "description": "代码重构项目",
        "roles": [
            {"name": "tech-lead", "count": 1, "description": "重构策略、影响分析、渐进式迁移", "template": "tech-lead"},
            {"name": "developer", "count": "1-2", "description": "代码迁移、依赖更新", "template": "team-member"},
            {"name": "qa-engineer", "count": 1, "description": "回归测试、行为一致性验证", "template": "team-member"},
        ],
    },
    "bugfix": {
        "description": "Bug修复项目",
        "roles": [
            {"name": "developer", "count": "1-2", "description": "问题定位、修复实现", "template": "team-member"},
            {"name": "qa-engineer", "count": 1, "description": "复现验证、回归测试", "template": "team-member"},
        ],
    },
}


@mcp.tool()
def team_setup_guide(project_type: str = "web-app") -> dict[str, Any]:
    """根据项目类型获取推荐的团队角色配置。

    Args:
        project_type: 项目类型，可选值：web-app, api-service, data-pipeline, library, refactor, bugfix

    Returns:
        推荐角色列表和组建提示
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
    """启动公司循环 — Leader持续工作模式。

    启动后循环领取最高优先级任务。每N个任务触发回顾讨论。
    任务不足时应组织会议讨论方向，不能没事找事干。

    提示: 使用 /continuous-mode 获取完整的持续工作协议，
    包括循环领取、暂停恢复、成员管理等详细行为规范。

    Args:
        team_id: 团队 ID 或名称

    Returns:
        循环状态信息，包含当前阶段和周期数
    """
    result = _api_call("POST", f"/api/teams/{team_id}/loop/start")
    return result


# ============================================================
# Tool 22: loop_status
# ============================================================


@mcp.tool()
def loop_status(team_id: str) -> dict[str, Any]:
    """查看公司循环当前状态 — 阶段、周期、已完成任务数。

    Args:
        team_id: 团队 ID 或名称

    Returns:
        循环状态详情，包含 phase / current_cycle / completed_tasks_count
    """
    return _api_call("GET", f"/api/teams/{team_id}/loop/status")


# ============================================================
# Tool 23: loop_next_task
# ============================================================


@mcp.tool()
def loop_next_task(team_id: str, agent_id: str = "") -> dict[str, Any]:
    """获取下一个应执行的任务 — 按优先级×时间跨度×就绪度排序。

    优先领取pinned和critical任务。short优先于mid优先于long。
    BLOCKED任务等依赖完成后自动解锁，无需手动处理。

    Args:
        team_id: 团队 ID 或名称
        agent_id: 指定 Agent ID，优先返回分配给该 Agent 的任务（可选）

    Returns:
        下一个待执行的任务信息，无任务时返回空
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
    """推进循环到下一阶段。

    可用 trigger:
    - tasks_planned: 规划完成 → 执行
    - batch_completed: 一批任务完成 → 监控
    - all_tasks_done: 全部完成 → 回顾
    - issues_found: 发现问题 → 返回执行
    - all_clear: 一切正常 → 回顾
    - new_tasks_added: 有新任务 → 重新规划
    - no_more_tasks: 无更多任务 → 空闲

    Args:
        team_id: 团队 ID 或名称
        trigger: 触发器名称

    Returns:
        更新后的循环状态
    """
    return _api_call("POST", f"/api/teams/{team_id}/loop/advance", {"trigger": trigger})


# ============================================================
# Tool 25: loop_pause
# ============================================================


@mcp.tool()
def loop_pause(team_id: str) -> dict[str, Any]:
    """暂停循环 — 保留当前状态，随时可恢复。

    Args:
        team_id: 团队 ID 或名称

    Returns:
        暂停后的循环状态
    """
    return _api_call("POST", f"/api/teams/{team_id}/loop/pause")


# ============================================================
# Tool 26: loop_resume
# ============================================================


@mcp.tool()
def loop_resume(team_id: str) -> dict[str, Any]:
    """恢复循环 — 从暂停处继续。

    Args:
        team_id: 团队 ID 或名称

    Returns:
        恢复后的循环状态
    """
    return _api_call("POST", f"/api/teams/{team_id}/loop/resume")


# ============================================================
# Tool: loop_review
# ============================================================


@mcp.tool()
def loop_review(team_id: str) -> dict[str, Any]:
    """触发公司循环回顾 — 自动创建回顾会议并生成统计报告。

    回顾会议包含：本轮完成的任务汇总、失败任务分析、下一步建议。
    Leader和团队可在会议中讨论并产出新的待办任务。

    Args:
        team_id: 团队 ID 或名称

    Returns:
        回顾会议信息，包含 meeting_id / stats / topic
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
    """获取任务墙视图 — 按短/中/长期分类，智能排序。

    返回按 score 排序的任务列表，Leader 用此快速了解下一步该做什么。

    Args:
        team_id: 团队 ID 或名称
        horizon: 按时间跨度筛选，可选 "short" / "mid" / "long"（留空=全部）
        priority: 按优先级筛选，可选 "critical" / "high" / "medium" / "low"，逗号分隔多选（留空=全部）

    Returns:
        任务墙数据，按 short/mid/long 分组，每组内按 score 降序
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
    """上报问题到团队。问题作为高优先级任务创建，自动标记为issue类型。

    severity 会映射为任务优先级：critical→critical, high→high, medium→high, low→medium。

    Args:
        team_id: 团队 ID 或名称
        title: 问题标题
        description: 问题详细描述
        severity: 严重程度，可选 "critical" / "high" / "medium" / "low"
        category: 问题分类，如 "bug" / "performance" / "security" / "ux"

    Returns:
        创建的 Issue 任务信息
    """
    return _api_call("POST", f"/api/teams/{team_id}/issues", {
        "title": title,
        "description": description,
        "severity": severity,
        "category": category,
    })


# ============================================================
# Tool 29: os_resolve_issue
# ============================================================


@mcp.tool()
def os_resolve_issue(issue_id: str, resolution: str) -> dict[str, Any]:
    """标记Issue为已解决，附带解决方案描述。

    将Issue状态更新为 resolved，同时记录解决方案。
    Issue对应的任务也会被标记为 completed。

    Args:
        issue_id: Issue（任务）ID
        resolution: 解决方案描述

    Returns:
        更新后的 Issue 信息
    """
    return _api_call("PUT", f"/api/issues/{issue_id}/status", {
        "status": "resolved",
        "resolution": resolution,
    })


# ============================================================
# Tool 30: task_memo_read
# ============================================================


@mcp.tool()
def task_memo_read(task_id: str) -> dict[str, Any]:
    """读取任务的所有memo记录 — 领取任务前先读取了解历史进度。

    Args:
        task_id: 任务 ID

    Returns:
        memo记录列表，按时间顺序排列
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
    """为任务添加memo记录 — 用于追踪进度、记录决策、标记问题。

    Args:
        task_id: 任务 ID
        content: memo内容
        memo_type: 类型，可选 "progress"(进度) / "decision"(决策) / "issue"(问题) / "summary"(总结)
        author: 作者名称，默认 "leader"

    Returns:
        添加的memo记录
    """
    return _api_call("POST", f"/api/tasks/{task_id}/memo", {
        "content": content,
        "type": memo_type,
        "author": author,
    })


# ============================================================
# Tool: agent_template_list
# ============================================================


@mcp.tool()
def agent_template_list() -> dict[str, Any]:
    """列出所有可用的Agent模板（来自 ~/.claude/agents/）。

    返回模板列表及按类别分组的视图，帮助选择合适的Agent角色模板。

    Returns:
        templates: 所有模板列表
        grouped: 按类别分组的模板
        total: 模板总数
    """
    return _api_call("GET", "/api/agent-templates")


# ============================================================
# Tool: agent_template_recommend
# ============================================================


@mcp.tool()
def agent_template_recommend(task_type: str = "", keywords: str = "") -> dict[str, Any]:
    """根据任务类型和关键词推荐合适的Agent模板。

    Args:
        task_type: 任务类型，如 "backend"、"frontend"、"data-analysis"
        keywords: 关键词，空格分隔，如 "python api database"

    Returns:
        recommendations: 最多5个匹配的模板，按相关度排序
        query: 实际使用的查询字符串
    """
    params = urllib.parse.urlencode({"task_type": task_type, "keywords": keywords})
    return _api_call("GET", f"/api/agent-templates/recommend?{params}")


# ============================================================
# FastAPI auto-start helpers
# ============================================================


def _is_port_open(host: str = "127.0.0.1", port: int = 8000) -> bool:
    """检查指定端口是否已在监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def _cleanup_api() -> None:
    """在进程退出时终止 FastAPI 子进程。"""
    global _api_process
    if _api_process is not None and _api_process.poll() is None:
        _api_process.terminate()
        try:
            _api_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _api_process.kill()
        _api_process = None


def _ensure_api_running() -> None:
    """如果 FastAPI 尚未运行，则自动拉起子进程。

    MCP Server 以 stdio 模式通信，子进程的 stdout 必须重定向到 DEVNULL
    以避免污染 MCP 协议通道。
    """
    global _api_process
    if _is_port_open():
        logger.info("FastAPI already running on port 8000, skipping auto-start")
        return
    logger.info("Starting FastAPI subprocess on port 8000...")
    try:
        _api_process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aiteam.api.app:create_app",
             "--host", "127.0.0.1", "--port", "8000", "--factory"],
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
# Tool: failure_analysis
# ============================================================


@mcp.tool()
def failure_analysis(task_id: str, team_id: str) -> dict[str, Any]:
    """分析失败任务，提炼防御规则+培训案例+改进提案（失败炼金术）.

    当任务永久失败（超过重试上限）后，调用此工具对失败进行深度分析，
    自动生成三种学习产物并保存到团队记忆：
    - 抗体：防御规则建议，防止同类失败重演
    - 疫苗：结构化失败案例，供新 Agent 参考学习
    - 催化剂：系统改进提案，推动流程优化

    Args:
        task_id: 失败任务的 ID
        team_id: 所属团队的 ID

    Returns:
        包含 antibody、vaccine、catalyst 三种产物的字典
    """
    return _api_call("POST", f"/api/teams/{team_id}/failure-analysis", {"task_id": task_id})


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    _ensure_api_running()
    mcp.run()
