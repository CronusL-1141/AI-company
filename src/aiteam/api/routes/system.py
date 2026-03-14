"""AI Team OS — 系统规则查询路由.

提供系统自动执行规则和建议规则的查询接口，
替代CLAUDE.md中冗长的规则描述。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/system", tags=["system"])


# A类：代码自动执行的规则（无需人工干预）
_AUTOMATED_RULES: list[dict] = [
    {
        "id": "A1",
        "category": "agent-lifecycle",
        "name": "Agent主动注册",
        "description": "Agent启动时通过MCP/API注册到OS（source=api）",
        "enforced_by": "src/aiteam/api/routes/agents.py — add_agent",
    },
    {
        "id": "A2",
        "category": "agent-lifecycle",
        "name": "Hook自动兜底",
        "description": "SubagentStart事件自动更新已注册Agent状态为busy",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_subagent_start",
    },
    {
        "id": "A3",
        "category": "agent-lifecycle",
        "name": "SubagentStop状态同步",
        "description": "SubagentStop事件自动将Agent设为idle",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_subagent_stop",
    },
    {
        "id": "A4",
        "category": "agent-lifecycle",
        "name": "SessionEnd对账",
        "description": "会话结束时比较hook/api注册数，所有agent归idle并清除session_id",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_session_end",
    },
    {
        "id": "A5",
        "category": "agent-lifecycle",
        "name": "Stop事件清理",
        "description": "CC进程终止时清理hook-source的BUSY agent状态",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_stop",
    },
    {
        "id": "A6",
        "category": "agent-lifecycle",
        "name": "状态自愈",
        "description": "IDLE Agent收到工具事件时自动修正为BUSY",
        "enforced_by": "src/aiteam/api/hook_translator.py — _self_heal_agent",
    },
    {
        "id": "A7",
        "category": "agent-lifecycle",
        "name": "注册即工作",
        "description": "Agent注册后默认设为busy状态",
        "enforced_by": "src/aiteam/api/routes/agents.py — add_agent (line 53)",
    },
    {
        "id": "A8",
        "category": "session",
        "name": "Session-Leader复用",
        "description": "SessionStart时按项目查找已有Leader复用，避免创建幽灵agent",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_session_start",
    },
    {
        "id": "A9",
        "category": "session",
        "name": "自动创建项目",
        "description": "SessionStart时无匹配项目则按cwd自动创建",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_session_start",
    },
    {
        "id": "A10",
        "category": "conflict-detection",
        "name": "文件编辑冲突检测",
        "description": "同一文件被多个Agent编辑时发出file.edit_conflict事件",
        "enforced_by": "src/aiteam/api/hook_translator.py — _check_file_edit_conflict",
    },
    {
        "id": "A11",
        "category": "conflict-detection",
        "name": "热点文件追踪",
        "description": "内存追踪器统计被多Agent编辑的热点文件，供team_briefing使用",
        "enforced_by": "src/aiteam/api/hook_translator.py — _FileEditTracker",
    },
    {
        "id": "A12",
        "category": "activity-tracking",
        "name": "工具使用记录",
        "description": "PreToolUse/PostToolUse事件自动记录到AgentActivity",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_pre_tool_use / _on_post_tool_use",
    },
    {
        "id": "A13",
        "category": "activity-tracking",
        "name": "current_task自动更新",
        "description": "Agent的当前任务描述随工具调用自动更新",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_pre_tool_use (line 487)",
    },
    {
        "id": "A14",
        "category": "activity-tracking",
        "name": "last_active_at自动更新",
        "description": "每次工具调用自动更新Agent最后活跃时间",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_pre_tool_use / _on_post_tool_use",
    },
    {
        "id": "A15",
        "category": "event-system",
        "name": "事件总线广播",
        "description": "所有状态变更通过EventBus发出事件，供WebSocket实时推送",
        "enforced_by": "src/aiteam/api/event_bus.py + src/aiteam/api/routes/ws.py",
    },
    {
        "id": "A16",
        "category": "type-safety",
        "name": "共享类型定义",
        "description": "所有数据模型集中定义在types.py，各模块只读引用",
        "enforced_by": "src/aiteam/types.py",
    },
    {
        "id": "A17",
        "category": "task-management",
        "name": "任务依赖自动阻塞",
        "description": "有未完成依赖的任务自动标记为blocked状态",
        "enforced_by": "TaskStatus.BLOCKED + depends_on字段",
    },
    {
        "id": "A18",
        "category": "hooks",
        "name": "Hook脚本统一入口",
        "description": "7种CC hook事件通过send_event.py统一POST到/api/hooks/event",
        "enforced_by": ".claude/hooks/send_event.py",
    },
]

# B类：需人工判断的规则（附建议）
_ADVISORY_RULES: list[dict] = [
    {
        "id": "B0",
        "category": "leadership",
        "name": "Leader统筹全局并行推进",
        "description": "Leader的首要任务是统筹全局、并行推进项目进程，兼顾效率和质量",
        "advice": "同时管理多方向工作，动态添加/Kill成员，QA问题分派给bug-fixer后继续推进其他任务，成员工具受限时由Leader安装解决",
    },
    {
        "id": "B0.1",
        "category": "leadership",
        "name": "瓶颈时组织讨论会议",
        "description": "当任务量不足、到达瓶颈或方向不明确时，Leader应组织团队会议讨论下一步方向",
        "advice": "使用loop_review触发回顾会议，充分讨论任务的必要性——不能为了有事干没事找事干。讨论产出的目标转为短/中/长期任务放入任务墙",
    },
    {
        "id": "B0.2",
        "category": "leadership",
        "name": "会议动态成员管理",
        "description": "根据议题动态添加合适参与者，讨论中发现新方向时随时招募相关专家加入",
        "advice": "战略会议→架构师/产品设计师；技术会议→工程师；发现新议题时立即spawn专家加入讨论，不局限于现有团队成员",
    },
    {
        "id": "B0.3",
        "category": "leadership",
        "name": "成员工具限制上报",
        "description": "团队成员遇到工具使用限制或权限不足时应报告给Leader",
        "advice": "Leader负责安装工具、调整配置或提供workaround。MCP工具不可用时用/mcp→Reconnect刷新",
    },
    {
        "id": "B0.4",
        "category": "leadership",
        "name": "添加成员必须用team_name",
        "description": "创建团队成员必须使用Agent(team_name=...)，不得使用普通subagent",
        "advice": "普通subagent不受团队管理，会导致OS状态混乱。出现'Teammates cannot spawn'时应检查团队配置或重建团队，不要降级为subagent",
    },
    {
        "id": "B0.5",
        "category": "leadership",
        "name": "任务墙灵活领取——不局限短期",
        "description": "Leader根据项目进度判断，可直接领取中/长期任务开始实施，不必只做短期",
        "advice": "中长期任务需要时用task_decompose拆分为短期子任务再执行。优先级和时机由Leader自主判断",
    },
    {
        "id": "B1",
        "category": "coordination",
        "name": "文件驱动协调",
        "description": "每个工程师只写自己负责的目录，更新coordination.md状态",
        "advice": "跨目录修改前先在coordination.md声明意图，等待确认",
    },
    {
        "id": "B2",
        "category": "agent-lifecycle",
        "name": "Kill vs 保留Agent",
        "description": "一次性任务完成后Kill，可能有后续任务的保留",
        "advice": "研究/实施团队→Kill；Debug/开发/测试团队→保留",
    },
    {
        "id": "B3",
        "category": "memory",
        "name": "记忆权威层级",
        "description": "信息冲突时: CLAUDE.md > auto-memory > OS MemoryStore > claude-mem",
        "advice": "只记不可推导的人类意图，技术细节交给代码和git",
    },
    {
        "id": "B4",
        "category": "context",
        "name": "上下文管理-WARNING",
        "description": "收到[CONTEXT WARNING]时完成当前最小原子任务后保存进度",
        "advice": "更新memory文件，记录已完成/进行中/下一步计划，提醒用户/compact",
    },
    {
        "id": "B5",
        "category": "context",
        "name": "上下文管理-CRITICAL",
        "description": "收到[CONTEXT CRITICAL]时立即停止，紧急保存所有进度",
        "advice": "不开始任何新任务，立即写入memory文件，强烈提醒用户/compact",
    },
    {
        "id": "B6",
        "category": "meeting",
        "name": "会议讨论规则",
        "description": "Round 1提出观点，Round 2+引用并回应前人发言，最后一轮汇总",
        "advice": "先读取前人消息再发言，避免重复或脱节",
    },
    {
        "id": "B7",
        "category": "output",
        "name": "状态消息长度",
        "description": "状态消息不超过200字",
        "advice": "写入coordination.md时保持简洁",
    },
    {
        "id": "B8",
        "category": "testing",
        "name": "模块测试",
        "description": "每个模块需有对应单元测试",
        "advice": "新模块完成后在tests/目录添加对应测试文件",
    },
    {
        "id": "B9",
        "category": "constraints",
        "name": "不做投资建议",
        "description": "不做具体投资建议或交易信号（继承自研究项目约束）",
        "advice": "提供分析框架和数据，不给出具体买卖建议",
    },
]


@router.get("/rules")
async def list_system_rules() -> dict:
    """列出系统自动执行的所有规则和建议规则.

    - automated_rules (A类): 代码强制执行的规则，无需人工干预
    - advisory_rules (B类): 需要人工判断的规则，附带建议
    """
    return {
        "automated_rules": _AUTOMATED_RULES,
        "advisory_rules": _ADVISORY_RULES,
        "summary": {
            "automated_count": len(_AUTOMATED_RULES),
            "advisory_count": len(_ADVISORY_RULES),
            "categories": sorted(
                {r["category"] for r in _AUTOMATED_RULES + _ADVISORY_RULES}
            ),
        },
    }


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: str) -> dict:
    """查询单条规则详情."""
    rule_id_upper = rule_id.upper()
    for rule in _AUTOMATED_RULES + _ADVISORY_RULES:
        if rule["id"] == rule_id_upper:
            rule_type = "automated" if rule_id_upper.startswith("A") else "advisory"
            return {"rule": rule, "type": rule_type}
    return {"error": f"规则 {rule_id} 不存在"}
