"""MCP server instructions — the only server-level text a tool-search client sees.

CC no longer ships 112 tool schemas up front; it searches tool text and loads what
matches. This paragraph is therefore what decides whether the tools get found at
all, so it is organised as "capability → entry tools" (the way someone phrases a
need) rather than as a list of nouns.

Kept in its own module because it is data, not logic: a long Chinese literal that
would otherwise force an E501 blanket exemption onto server.py.
Constraints are enforced by tests/unit/test_mcp_server_surface.py — <= 2KB (it
rides along in every session), core capabilities covered, and every tool named
here must actually exist.
"""

from __future__ import annotations

INSTRUCTIONS = """AI Team OS —— 多 Agent 团队的持久化治理层。CC 会话是临时的，这里的任务、记忆、报告、观测记录跨会话长存。

核心能力（按能力检索）:
- 跨会话任务台账: 任务上墙、状态流转、逐条 memo 留痕，崩溃或换 session 后接着干 → task_create / task_status / task_update / task_list_project / task_memo_add / task_memo_read
- Agent 观测与归因: 谁在干、干了多久、卡在哪一步、失败根因、哪种 prompt 有效 → agent_list / agent_activity_query / task_execution_trace / diagnose_task_failure / failure_analysis / prompt_effectiveness / event_list / verify_completion
- 记忆治理: 情景层(任务 memo)+方向层(约束/偏好/决策)双层，检索、失效、合并蒸馏 → memory_search / memory_add / memory_list / memory_invalidate / memory_reconcile_candidates / memory_reconcile_apply
- 项目隔离: 按工作目录自动归属，各项目的任务/记忆/报告互不串台 → context_resolve / project_list / project_summary
- 报告知识库: 研究与设计产出落库可检索，不散落成 md 文件 → report_save / report_list / report_read
- 团队协作: 建队派工、频道广播、多方会议与结构化辩论 → team_status / fleet_dispatch / channel_send / channel_read / meeting_create / meeting_send_message / debate_start
- 溯源检索: 任务/报告/提交/工作流之间的引用网络与全局搜索 → link_trace / link_query / unified_search
- 开源生态档案: Claude 生态仓库索引、能力标签、深扫与周报 → ecosystem_search / ecosystem_search_by_capability / ecosystem_repo_get 等 ecosystem_* 全套
- 其余: workflow_* 追踪 CC Workflow 运行、briefing_* 待办简报、find_skill 查技能、os_health_check 自检。

约定: 先 context_resolve 确认项目；研究产出用 report_save 而非直接写文件；要跨会话留住的结论用 memory_add。"""
