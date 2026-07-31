"""AI Team OS — System rules query routes.

Provides query interfaces for system automated rules and advisory rules,
replacing verbose rule descriptions in CLAUDE.md.

Also exposes a graceful self-shutdown endpoint used by the standardized
restart flow (os_restart_api MCP tool).
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])


# Category A: Code-enforced automated rules (no human intervention needed)
_AUTOMATED_RULES: list[dict] = [
    {
        "id": "A1",
        "category": "agent-lifecycle",
        "name": "Agent落库入口",
        "description": "POST /api/agents 落库的 agent 标 source=api 并默认置 busy。无对应 MCP 工具——子 agent 不必自注册，由 A2 自动收编",
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
        "name": "SubagentStop→等待",
        "description": "SubagentStop事件将Agent设为waiting（等待输入，非关闭）。三状态：busy(工作中)/waiting(等待)/offline(关闭)",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_subagent_stop",
    },
    {
        "id": "A4",
        "category": "agent-lifecycle",
        "name": "SessionEnd→关闭",
        "description": "会话结束时所有agent设为offline（关闭）并清除session_id",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_session_end",
    },
    {
        "id": "A5",
        "category": "agent-lifecycle",
        "name": "Stop→关闭",
        "description": "CC进程终止时hook-source的agent设为offline",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_stop",
    },
    {
        "id": "A6",
        "category": "agent-lifecycle",
        "name": "状态自愈",
        "description": "WAITING Agent收到工具事件时自动修正为BUSY",
        "enforced_by": "src/aiteam/api/hook_translator.py — _self_heal_agent",
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
        "name": "current_task从role自动提取",
        "description": "Agent 落库时 role 若含 ' — ' 分隔符，自动拆成 role + current_task（'前端工程师 — Dashboard开发' → role='前端工程师', current_task='Dashboard开发'）",
        "enforced_by": "src/aiteam/api/hook_translator.py — _on_subagent_start + routes/agents.py — add_agent",
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
        "description": "CC hook 事件经 send_event.py 统一 POST 到 /api/hooks/event（覆盖面以 plugin/hooks/hooks.json 为准）",
        "enforced_by": "plugin/hooks/send_event.py",
    },
]

# Category B: Rules requiring human judgment (with advice)
_ADVISORY_RULES: list[dict] = [
    {
        "id": "B-1",
        "category": "memory",
        "name": "记忆分层职责",
        "description": "用户给出偏好/纠正/设计意图时 Leader 当场 memory_add 落方向层；任务过程记录走 task_memo_add；两者别混",
        "advice": "写入检验：这条能影响多少未来任务？只影响单个任务的去 task_memo。方向层存储上限=注入预算（桶字符配额 global 1200 / project 1500 / user 300，单条 ≤400 字），超限当轮先失效或合并腾位再重试；偏好被改用 supersedes 置换不物删",
    },
    {
        "id": "B0.1",
        "category": "meeting",
        "name": "会议触发条件",
        "description": "凡提案涉及刻意决策修订／砍工具删表／改机检红线，必须先过会（Council 纪律④，用户批准）。其余情况自行判断——任务墙空不等于该开会，不为有事干而开会",
        "advice": "会议以决策上墙收尾：结论转成 decision 记录或任务墙条目，否则视同没开",
    },
    {
        "id": "B0.3",
        "category": "leadership",
        "name": "成员工具受限由Leader解决",
        "description": "成员报工具/权限受限时由 Leader 安装或改配置，不让成员自行绕过",
        "advice": "MCP 工具不可用时用 /mcp → Reconnect 刷新",
    },
    {
        "id": "B0.4",
        "category": "leadership",
        "name": "派发不传 team_name",
        "description": "Agent 的 team_name 参数已废弃且被忽略，勿传；每个会话自带唯一隐式团队，派出的 agent 由 SubagentStart hook 自动收编进 session-<sid8> 容器队",
    },
    {
        "id": "B0.5",
        "category": "leadership",
        "name": "任务墙灵活领取——不局限短期",
        "description": "可直接领取中/长期任务，不必只做短期。拆分后暂不实施的要撤回，避免僵尸任务",
    },
    {
        "id": "B0.6",
        "category": "leadership",
        "name": "项目记忆维护——确保可恢复",
        "description": "阶段性目标完成即落记忆（做了什么/决策原因/当前状态/下一步），保证 compact 或重启后能恢复上下文；任务墙、会议记录、记忆三方保持一致",
    },
    {
        "id": "B0.7",
        "category": "leadership",
        "name": "不空等——派出后继续领任务",
        "description": "等成员结果时不空闲，回任务墙找可并行的活；并行短期任务不超过 3 个",
    },
    {
        "id": "B0.8",
        "category": "leadership",
        "name": "行为变更同步QA",
        "description": "改动影响系统行为或前端显示时，主动告知 QA 要观测什么；纯文档/规则变更不通知",
    },
    {
        "id": "B0.9",
        "category": "leadership",
        "name": "Leader专注统筹，不做具体实施",
        "description": "Leader 只做任务分配、决策、推进。需要读多个文件/写代码/调试的任务一律派给成员；改一行配置这类极小改动才自己做",
        "advice": "用户裁定：Leader 陷入具体实施会导致项目整体停滞",
    },
    {
        "id": "B0.11",
        "category": "leadership",
        "name": "Leader设定agent当前任务",
        "description": "派出 agent 后用 agent_update_status 设 current_task（role 放简短角色名，current_task 放具体任务）——Dashboard 按此展示成员在干什么",
    },
    {
        "id": "B0.12",
        "category": "leadership",
        "name": "任务Memo追踪",
        "description": "领任务前 task_memo_read 读历史；执行中 task_memo_add 记关键进展与决策；完成后 task_memo_add(type='summary') 写总结",
    },
    {
        "id": "B2",
        "category": "agent-lifecycle",
        "name": "常驻成员 vs 临时成员",
        "description": "QA、bug-fixer 属常驻成员，项目期间保留；研究/单次实施类属临时成员，交付后回收。团队保持到项目完成",
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
        "advice": "记录已完成/进行中/下一步计划，提醒用户 /compact",
    },
    {
        "id": "B5",
        "category": "context",
        "name": "上下文管理-CRITICAL",
        "description": "收到[CONTEXT CRITICAL]时立即停止，不开新任务，紧急落盘所有进度并提醒用户 /compact",
    },
    {
        "id": "B0.14",
        "category": "leadership",
        "name": "行动项必须上墙",
        "description": "对话/会议中产生的行动项必须 task_create 上墙——口头承诺不算",
    },
    {
        "id": "B0.16",
        "category": "leadership",
        "name": "Leader自主运转模式",
        "description": "按任务墙优先级自主推进，不逐步等用户确认。战术决策自主做主，仅项目方向/重大架构这类战略决策请示用户；被阻塞时切别的任务继续推进",
        "advice": "用户发言时统一汇报进度 + 列出待决策事项",
    },
    {
        "id": "B0.16b",
        "category": "leadership",
        "name": "待决事项必须入队，不许只留在报告里",
        "description": (
            "子 agent 完工报告里凡写着「留给用户/Leader 裁定」的事项，派发方（Leader）"
            "必须当场 briefing_add 入待决队列。写在报告正文里的裁定项等于没提交——"
            "用户看的是简报队列，不会去翻每份完工报告"
        ),
        "advice": "收割报告时逐条扫「待裁定/待确认/建议由用户决定」，每条一个 briefing_add（带 tags 便于按主题筛）",
    },
    {
        "id": "B0.17",
        "category": "leadership",
        "name": "先研究再实施",
        "description": "系统级新功能设计先做外部研究+竞品分析再动手，不能只看内部代码闭门造车",
    },
    {
        "id": "B6",
        "category": "meeting",
        "name": "会议讨论规则",
        "description": "Round 1提出观点，Round 2+引用并回应前人发言，最后一轮汇总",
        "advice": "先读取前人消息再发言，避免重复或脱节",
    },
    {
        "id": "B6.2",
        "category": "meeting",
        "name": "会议参与者通知",
        "description": "meeting_create 不会自动通知任何人——必须逐一 SendMessage 告知 meeting_id 与议题，否则会议无人到场",
    },
    {
        "id": "B0.18",
        "category": "execution",
        "name": "2-Action持久化规则",
        "description": "每执行2个实质性操作（编辑文件/运行命令/创建资源）后，用 task_memo_add 记录进展——上下文随时可能被压缩，落盘是唯一防线",
    },
    {
        "id": "B0.19",
        "category": "execution",
        "name": "3次失败升级协议",
        "description": "同一任务同一方法连续失败 3 次即换路：改方法、请其他 Agent 协助或上报 Leader；上报会触发 failure_analysis 沉淀",
    },
]


@router.get("/rules")
async def list_system_rules() -> dict:
    """List all system automated rules and advisory rules.

    - automated_rules (Category A): Code-enforced rules, no human intervention needed
    - advisory_rules (Category B): Rules requiring human judgment, with advice
    """
    return {
        "automated_rules": _AUTOMATED_RULES,
        "advisory_rules": _ADVISORY_RULES,
        "summary": {
            "automated_count": len(_AUTOMATED_RULES),
            "advisory_count": len(_ADVISORY_RULES),
            "categories": sorted({r["category"] for r in _AUTOMATED_RULES + _ADVISORY_RULES}),
        },
    }


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: str) -> dict:
    """Query single rule details."""
    rule_id_upper = rule_id.upper()
    for rule in _AUTOMATED_RULES + _ADVISORY_RULES:
        if rule["id"] == rule_id_upper:
            rule_type = "automated" if rule_id_upper.startswith("A") else "advisory"
            return {"rule": rule, "type": rule_type}
    return {"error": f"规则 {rule_id} 不存在"}


def _wal_checkpoint_best_effort() -> None:
    """Run a SQLite WAL checkpoint on the default DB before exit.

    Best-effort only: any failure is logged and swallowed so it never blocks
    the shutdown. Uses stdlib sqlite3 against the file path (synchronous) rather
    than the async engine, since the event loop is being torn down at exit.
    """
    import sqlite3

    from aiteam.storage.connection import DEFAULT_DB_URL

    try:
        # DEFAULT_DB_URL looks like "sqlite+aiosqlite:///<path>"
        if "///" not in DEFAULT_DB_URL:
            return
        db_path = DEFAULT_DB_URL.split("///", 1)[-1]
        if not db_path:
            return
        con = sqlite3.connect(db_path, timeout=5)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.commit()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 — checkpoint must never block exit
        logger.warning("WAL checkpoint before shutdown failed (ignored): %s", exc)


async def _delayed_exit() -> None:
    """Wait briefly so the HTTP response is flushed, then hard-exit the process.

    os._exit is used (not sys.exit) to guarantee the uvicorn worker actually
    terminates and releases the port — sys.exit would only raise SystemExit
    inside the request task and could be swallowed by the server.
    """
    await asyncio.sleep(0.5)
    # 让出治理租约（2026-07-27 首考失败后补位）：本端点用 os._exit 硬退，lifespan
    # 收尾（StateReaper.stop 里的 release）永远不跑——释放必须放在这条真实退出路径上，
    # 否则新实例被死 pid 的租约挡满 TTL（180s），治理静默三分钟。best-effort，绝不拦退出。
    try:
        from aiteam.api import deps as _deps

        repo = getattr(_deps, "_repository", None)
        if repo is not None:
            released = await repo.release_governance_lease(f"api-{os.getpid()}")
            if released:
                logger.info("Governance lease released on shutdown (pid=%d)", os.getpid())
    except Exception:  # noqa: BLE001 — 退出路径绝不因此阻塞
        logger.debug("Lease release on shutdown failed (TTL will expire it)")
    _wal_checkpoint_best_effort()
    os._exit(0)


@router.post("/shutdown")
async def shutdown() -> dict:
    """Gracefully shut down this API process.

    Localhost-only by design: the API binds 127.0.0.1, so no external client can
    reach this endpoint and no extra auth is required. It exists to give the
    standardized restart flow (os_restart_api) a clean way to stop the old
    process before a new version is spawned on the same port.

    Returns immediately with the current PID, then self-exits ~0.5s later after a
    best-effort WAL checkpoint.
    """
    pid = os.getpid()
    logger.info("Graceful shutdown requested (pid=%d)", pid)
    asyncio.create_task(_delayed_exit())
    return {"success": True, "message": "shutting down", "pid": pid}
