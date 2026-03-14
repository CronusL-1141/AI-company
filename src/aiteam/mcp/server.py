"""AI Team OS — MCP Server.

提供 20 个 MCP tools，通过 HTTP 调用本地 FastAPI (localhost:8000) 的对应 API 端点。
MCP Server 以 stdio 模式运行，与 FastAPI 进程完全解耦。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from fastmcp import FastMCP

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
def team_create(name: str, mode: str = "coordinate") -> dict[str, Any]:
    """创建一个新的 AI Agent 团队。

    Args:
        name: 团队名称
        mode: 协作模式，可选 "coordinate"（协调）或 "broadcast"（广播）

    Returns:
        创建的团队信息，包含 team_id
    """
    result = _api_call("POST", "/api/teams", {"name": name, "mode": mode})
    result["_role_suggestions"] = {
        "hint": "建议为团队配置以下角色以提升协作效率：",
        "roles": [
            {"name": "tech-lead", "count": 1, "description": "技术负责人，负责架构决策和任务拆分", "template": "tech-lead"},
            {"name": "developer", "count": "2-3", "description": "开发工程师，负责具体实现", "template": "team-member"},
            {"name": "qa-engineer", "count": 1, "description": "测试工程师，负责质量保障", "template": "team-member"},
        ],
        "tip": "使用CC的Agent tool创建子agent时，指定subagent_type为agent模板名（如tech-lead, team-member）",
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


@mcp.tool()
def agent_register(
    team_id: str,
    name: str,
    role: str,
    model: str = "claude-opus-4-6",
    system_prompt: str = "",
) -> dict[str, Any]:
    """向团队注册一个新的 AI Agent。

    注册成功后返回:
    - teammates: 当前团队其他成员列表（name/role/status/current_task）
    - team_snapshot: 所有成员状态、待办任务详情、最近会议（无需额外调用 team_briefing）

    Args:
        team_id: 目标团队 ID 或名称
        name: Agent 名称
        role: Agent 角色描述
        model: 使用的模型，默认 claude-opus-4-6
        system_prompt: Agent 的系统提示词

    Returns:
        Agent 信息 + teammates 列表 + team_snapshot（含 pending_tasks 和 recent_meeting）
    """
    return _api_call("POST", f"/api/teams/{team_id}/agents", {
        "name": name,
        "role": role,
        "model": model,
        "system_prompt": system_prompt,
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
        status: 新状态，可选 "idle"、"busy"、"offline"

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
# Tool 7: meeting_create
# ============================================================


@mcp.tool()
def meeting_create(
    team_id: str,
    topic: str,
    participants: list[str] | None = None,
) -> dict[str, Any]:
    """创建团队会议，用于多 Agent 协作讨论。

    Args:
        team_id: 团队 ID 或名称
        topic: 会议讨论主题
        participants: 参会 Agent ID 列表，为空则全员参与

    Returns:
        会议信息，包含 meeting_id 和操作指引
    """
    return _api_call("POST", f"/api/teams/{team_id}/meetings", {
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
) -> dict[str, Any]:
    """在团队中创建一个任务，等待Agent领取执行。

    自动检测与已有任务的相似度，若发现相似任务会在 related_tasks 字段中返回警告。
    任务创建后状态为 pending，CC Agent 可通过 team_briefing 查看待办任务并领取。

    Args:
        team_id: 团队 ID 或名称
        description: 任务描述
        title: 任务标题（可选）
        model: 指定使用的模型（可选，仅记录元数据）

    Returns:
        创建的任务信息 + related_tasks（相似任务列表，如有）
    """
    payload: dict[str, Any] = {"description": description}
    if title:
        payload["title"] = title
    if model:
        payload["model"] = model
    return _api_call("POST", f"/api/teams/{team_id}/tasks/run", payload)


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
# Entry point
# ============================================================

if __name__ == "__main__":
    mcp.run()
