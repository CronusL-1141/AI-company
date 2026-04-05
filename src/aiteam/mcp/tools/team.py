"""Team management MCP tools."""

from __future__ import annotations

import os
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_team_id

_PROJECT_TYPE_ROLES: dict[str, dict[str, Any]] = {
    "web-app": {
        "description": "全栈Web应用项目",
        "roles": [
            {
                "name": "tech-lead",
                "count": 1,
                "description": "架构设计、技术决策、代码审查",
                "template": "management-tech-lead",
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
                "template": "management-tech-lead",
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
                "template": "management-tech-lead",
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
                "template": "management-tech-lead",
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
                "template": "management-tech-lead",
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


def register(mcp):
    """Register all team-related MCP tools."""

    @mcp.tool()
    def team_create(
        name: str,
        mode: str = "coordinate",
        project_id: str = "",
        leader_agent_id: str = "",
    ) -> dict[str, Any]:
        """⚠️ INTERNAL USE ONLY — 请使用CC原生的TeamCreate工具创建团队，不要调用此MCP工具。

        NOTE: For normal workflow, use CC's TeamCreate tool instead — it auto-registers
        the team via hooks. This MCP tool only creates a DB record without CC integration.

        Args:
            name: Team name
            mode: Collaboration mode, either "coordinate" or "broadcast"
            project_id: Associated project ID (optional)
            leader_agent_id: Leader agent ID for this team (optional)

        Returns:
            Created team info including team_id
        """
        payload: dict[str, Any] = {"name": name, "mode": mode}
        if project_id:
            payload["project_id"] = project_id
        if leader_agent_id:
            payload["leader_agent_id"] = leader_agent_id
        result = _api_call("POST", "/api/teams", payload)
        result["_warning"] = "此工具仅创建DB记录不启动真实进程。请使用CC原生TeamCreate+Agent工具。"
        result["_team_standard"] = {
            "members_guidance": {
                "hint": "以下角色按需创建，任务完成后Kill临时成员释放资源：",
                "roles": [
                    {"name": "developer", "count": "1-3", "description": "开发工程师，负责具体实现"},
                    {
                        "name": "researcher",
                        "count": "1-3",
                        "description": "研究员，负责技术调研和方案设计",
                    },
                    {"name": "tech-lead", "count": 1, "description": "技术负责人，负责架构决策"},
                    {
                        "name": "qa-engineer",
                        "count": "0-1",
                        "description": "QA工程师，需要测试验收时创建，测试完成后Kill",
                    },
                    {
                        "name": "bug-fixer",
                        "count": "0-1",
                        "description": "Bug工程师，接收QA报告定位修复，修复完成后Kill",
                    },
                ],
            },
            "lifecycle_rule": (
                "按需创建成员，任务完成后Kill临时成员释放资源。团队保持到项目完成。"
                "需要测试验收时创建QA Agent，不必常驻占用资源。"
            ),
        }
        return result

    @mcp.tool()
    def team_status(team_id: str) -> dict[str, Any]:
        """Get detailed information and status of a specified team.

        Args:
            team_id: Team ID or team name

        Returns:
            Team details including name, mode, member count, etc.
        """
        return _api_call("GET", f"/api/teams/{team_id}")

    @mcp.tool()
    def team_list() -> dict[str, Any]:
        """List all created teams.

        Returns:
            Team list with basic info for each team
        """
        return _api_call("GET", "/api/teams")

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

    @mcp.tool()
    def team_delete(team_id: str) -> dict[str, Any]:
        """Delete a team.

        Args:
            team_id: Team ID or name to delete

        Returns:
            Deletion result
        """
        resolved = _resolve_team_id(team_id)
        if not resolved:
            return {"success": False, "error": "Team not found"}
        return _api_call("DELETE", f"/api/teams/{resolved}")

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
