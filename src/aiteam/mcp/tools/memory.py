"""Memory and knowledge MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_team_id


def register(mcp):
    """Register all memory-related MCP tools."""

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
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

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
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

    @mcp.tool()
    def memory_add(
        content: str,
        kind: str = "preference",
        scope: str = "global",
        supersedes: str | None = None,
        source_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add a direction-layer memory — the team's shared, cross-task standing preferences.

        方向层 = 低频·高价值密度·跨任务长寿命的偏好/纠正/约束/设计意图。每个派出
        的 agent 出生即注入方向层，"全中文""完成即汇报"这类偏好不再靠手抄进 prompt。

        写入检验（软门槛）：**这条能影响多少未来任务？只影响单个任务的 → 去
        task_memo_add（情景层），不要写这里。** 方向层的价值在小而准，不在多——
        每作用域有效条目 ≤ 40、单条 ≤ 400 字，超限会被拒绝并提示用 memory_reconcile
        先整理。超长内容改写成「触发条件 + 指向权威文件」的**指针条目**（如
        "涉及生产/集群/DB 时遵守只读铁律，详见 ~/.claude/CLAUDE.md"），正文外置。

        kind 四类（决定注入截断优先级 constraint>design>directive>preference）：
        - constraint（禁令/护栏）：一句话、可机检、终身有效。
          如 "所有输出使用中文"、"git 提交绝不自动加 agent 署名"。
        - design（价值排序/设计意图）：缺显式指令时的取舍依据。
          如 "技术决策偏向质量/简洁/健壮/长期可维护，不看重开发成本"。
        - directive（方法论/工作方式）：回答"怎么干"。
          如 "完成即按问题→根因→解法→验证汇报，不攒批次"。
        - preference（格式偏好）：可选，如 "每句一行便于 diff"。

        Args:
            content: 记忆内容（≤ 400 字；超长请改指针条目）
            kind: constraint / design / directive / preference
            scope: global（全局）/ project（当前项目）/ user（用户级）
            supersedes: 可选，被本条置换失效的旧 memory id（偏好被改 = 新条 supersede
                旧条，Zep 失效语义不删除）
            source_refs: 可选，溯源 id 列表（回指 memo/report/meeting，蒸馏提升时用）

        Returns:
            写入结果；超体量红线时返回 success=False 与整理提示
        """
        body: dict[str, Any] = {
            "content": content,
            "kind": kind,
            "scope": scope,
            "source_refs": source_refs or [],
        }
        if supersedes:
            body["supersedes"] = supersedes
        return _api_call("POST", "/api/memories", body)

    @mcp.tool()
    def memory_invalidate(memory_id: str) -> dict[str, Any]:
        """Invalidate a direction-layer memory — mark it invalid without deleting.

        方向层偏好过时/被推翻时显式失效（Zep 失效语义：置 invalid_at 不删除，
        保留可审计轨迹）。失效后不再进注入，也默认不出现在 memory_list。

        Args:
            memory_id: 要失效的方向层记忆 id

        Returns:
            失效后的条目；id 不存在返回错误
        """
        return _api_call("POST", f"/api/memories/{memory_id}/invalidate", {})

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def memory_list(
        kind: str = "",
        include_invalidated: bool = False,
    ) -> dict[str, Any]:
        """List direction-layer memories — valid entries by default, grouped by kind.

        返回当前上下文的方向层条目：global + user 全局条目 + 当前项目的 project
        级条目，按 kind 优先级（constraint>design>directive>preference）+ 时间倒序。
        这是双 hook 常驻注入的同一数据源；用它审阅"派出的 agent 会继承什么"。

        Args:
            kind: 可选，按 kind 过滤（constraint/design/directive/preference）
            include_invalidated: 是否含已失效条目（默认否）

        Returns:
            方向层条目列表
        """
        params_dict: dict[str, Any] = {}
        if kind:
            params_dict["kind"] = kind
        if include_invalidated:
            params_dict["include_invalidated"] = "true"
        qs = urllib.parse.urlencode(params_dict)
        path = "/api/memories" + (f"?{qs}" if qs else "")
        return _api_call("GET", path)

    @mcp.tool()
    def pattern_record(
        type: str,
        task_type: str,
        template: str,
        approach: str,
        result: str = "",
        error: str = "",
        lesson: str = "",
    ) -> dict[str, Any]:
        """Record an agent execution pattern (success or failure) for future learning.

        Stores the pattern in the global execution pattern memory so future agents
        can benefit from this experience when tackling similar tasks.

        Args:
            type: "success" or "failure"
            task_type: Task category (e.g. "api-implementation", "bug-fix", "research")
            template: Agent template name that executed the task
            approach: Description of the approach taken
            result: Result summary (required for success patterns)
            error: Error description (required for failure patterns)
            lesson: Lesson learned (required for failure patterns)

        Returns:
            Record confirmation with memory_id
        """
        params = urllib.parse.urlencode({
            "pattern_type": type,
            "task_type": task_type,
            "agent_template": template,
            "approach": approach,
            "result": result,
            "error": error,
            "lesson": lesson,
        })
        return _api_call("POST", f"/api/execution-patterns/record?{params}")

    @mcp.tool()
    def pattern_search(
        query: str,
        top_k: int = 3,
    ) -> dict[str, Any]:
        """Search historical execution patterns similar to a task description.

        Uses BM25 retrieval to find relevant success/failure patterns recorded
        by agents in previous tasks. Use this before starting a complex task
        to benefit from past experience.

        Args:
            query: Task description or keywords to match against
            top_k: Maximum number of patterns to return (default 3, max 20)

        Returns:
            List of matching patterns with type, approach, and result/lesson
        """
        params = urllib.parse.urlencode({"query": query, "top_k": top_k})
        return _api_call("GET", f"/api/execution-patterns/search?{params}")
