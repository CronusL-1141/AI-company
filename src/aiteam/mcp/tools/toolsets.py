"""工具分组开关 + 只读档解析（工具渐进式加载 P2，注册期静态 gating）。

两个正交维度，均在 ``register_all(mcp)`` 注册循环处生效：

1. ``AITEAM_TOOLSETS`` — 按能力域分组挑选注册哪些模块。取值：
   - 缺省 / ``all`` → 全部 166 工具（向后兼容，等于历史行为）；
   - ``default`` → 核心组集合 :data:`DEFAULT_TOOLSETS`（注册后工具数硬顶 ≤50）；
   - 逗号分隔的组名列表，可混入 ``default`` 做增量（如 ``"default,ecosystem"``）；
   - 未知组名 → stderr 警告并忽略该项，绝不因配置错拉不起 server。

2. ``AITEAM_READONLY=1`` — 与分组正交叠加，注册后剔除全部写类工具，
   只留读类。写工具用 :data:`WRITE_TOOLS` **显式清单**判定（不靠命名模式猜，
   避免误伤难查）；清单旁注明生成方法便于维护。

设计规格见 docs/tool-loading-design.md §P2 与 §2.5。
"""

from __future__ import annotations

import sys

# ============================================================
# 能力域分组：模块短名 -> toolset 名
# 分组键按能力域而非动词或物理来源（Composio/JARVIS/ToolBench 行业一致）。
# 大多与模块同名，仅 git_ops→git、error_budget_tool→error_budget 归并重命名。
# ============================================================
MODULE_TOOLSET: dict[str, str] = {
    "team": "team",
    "agent": "agent",
    "meeting": "meeting",
    "task": "task",
    "project": "project",
    "loop": "loop",
    "pipeline": "pipeline",
    "analytics": "analytics",
    "links": "links",
    "reports": "reports",
    "briefing": "briefing",
    "scheduler": "scheduler",
    "task_analysis": "task_analysis",
    "memory": "memory",
    "infra": "infra",
    "file_lock": "file_lock",
    "git_ops": "git",
    "channels": "channels",
    "guardrails": "guardrails",
    "trust": "trust",
    "watchdog": "watchdog",
    "error_budget_tool": "error_budget",
    "ecosystem": "ecosystem",
    "workflows": "workflows",
}

# 全部合法组名（AITEAM_TOOLSETS=all 展开为此集合）。
ALL_TOOLSETS: frozenset[str] = frozenset(MODULE_TOOLSET.values())

# ------------------------------------------------------------
# default 组 = 每会话真正常用的核心能力域。
# 硬顶 ≤50 工具（AnyTool 64 / JARVIS top-5 / 官方 30-50 拐点同源普适护栏）。
# 当前成员工具数：task12 + team7 + memory9 + infra13 + reports3 = 44（留 6 头寸）。
# project / agent 等按需以 "default,project" 增量挂载，不进 default 免破顶。
# ------------------------------------------------------------
DEFAULT_TOOLSETS: frozenset[str] = frozenset(
    {"task", "team", "memory", "infra", "reports"}
)

# ============================================================
# 写类工具显式清单（AITEAM_READONLY=1 时剔除）。
#
# 生成方法（可复核维护）：逐工具看其调用的后端 HTTP 动词——
#   POST / PUT / DELETE = 写；GET = 读。在此基础上人工校正两类边角：
#   (a) GET 却有副作用者补入写清单：os_restart_api（重启进程）；
#   (b) POST 却纯分析无持久化者留在读侧：diagnose_task_failure（只回诊断）。
# 未列入者即读类（*_list/*_get/*_search/*_read/*_status/*_check/*_trace/
#   *_query/*_summary/*_recommend 等），只读档保留。
# 按模块分组便于对照 tools/*.py 维护。
# ============================================================
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        # agent
        "agent_register",
        "agent_update_status",
        "fleet_dispatch",  # drives a headless resume subprocess — write/side-effecting
        # briefing
        "briefing_add",
        "briefing_resolve",
        "briefing_dismiss",
        # channels
        "channel_send",
        # ecosystem（scan/apply/tag/claim/pin/mark 等一律写；summary/search/status 为读）
        "ecosystem_scan",
        "ecosystem_scan_periodic",
        "ecosystem_refresh",
        "ecosystem_deep_review_request",
        "ecosystem_deep_review_request_batch",
        "ecosystem_deep_review_cancel",
        "ecosystem_tag_apply_batch",
        "ecosystem_tag_dispatch_llm",
        "ecosystem_tag_apply_llm_result",
        "ecosystem_apply_shallow_summary",
        "ecosystem_apply_architecture_md",
        "ecosystem_apply_debate_result",
        "ecosystem_apply_quality_review",
        "ecosystem_trigger_debate",
        "ecosystem_link_debate_meeting",
        "ecosystem_link_integration_task",
        "ecosystem_start_integration",
        "ecosystem_mark_as_reference",
        "ecosystem_mark_no_value",
        "ecosystem_clear_manual_status",
        "ecosystem_claim_shallow",
        "ecosystem_claim_review",
        "ecosystem_release_claim",
        "ecosystem_pin_active",
        "ecosystem_unpin",
        "ecosystem_quick_setup",
        "ecosystem_data_source_create",
        "ecosystem_scan_profile_update",
        "ecosystem_index_update",
        # error_budget
        "error_budget_update",
        # file_lock（acquire/release 改锁表；list/check 为读）
        "file_lock_acquire",
        "file_lock_release",
        # git
        "git_auto_commit",
        "git_create_pr",
        # infra（os_restart_api 虽走 GET 但重启进程，显式补入）
        "os_restart_api",
        "os_report_issue",
        "os_resolve_issue",
        "send_notification",
        "cross_project_send",
        "model_config_set",
        # loop
        "loop_start",
        "loop_next_task",
        "loop_advance",
        "loop_pause",
        "loop_resume",
        "loop_review",
        # meeting
        "meeting_create",
        "meeting_send_message",
        "meeting_conclude",
        "meeting_update",
        "debate_start",
        "debate_code_review",
        # memory
        "memory_add",
        "memory_invalidate",
        "memory_reconcile_apply",
        "pattern_record",
        # pipeline
        "pipeline_create",
        "pipeline_advance",
        # project
        "project_create",
        "project_update",
        "project_delete",
        "phase_create",
        "dismiss_project_registration",
        # reports
        "report_save",
        # scheduler
        "scheduler_create",
        "scheduler_pause",
        "scheduler_delete",
        # task
        "task_run",
        "task_decompose",
        "task_create",
        "task_update",
        "task_memo_add",
        # task_analysis（failure_analysis 会沉淀防御规则/训练用例，属写；
        #   diagnose_task_failure 只回诊断，留读侧）
        "failure_analysis",
        # team
        "team_create",
        "team_close",
        "team_delete",
        # trust
        "agent_trust_update",
        # watchdog（heartbeat 更新心跳、verify_completion 记录核验结果，均写）
        "agent_heartbeat",
        "verify_completion",
        # workflows
        "workflow_reconcile",
    }
)

# 只读档识别的真值集合。
_READONLY_TRUE = frozenset({"1", "true", "yes", "on"})


def _warn(msg: str) -> None:
    """向 stderr 打警告——stdio server 的 stdout 是 MCP 协议信道，绝不可污染。"""
    print(f"[aiteam.toolsets] {msg}", file=sys.stderr)


def resolve_toolsets(raw: str | None) -> set[str]:
    """解析 AITEAM_TOOLSETS，返回启用的 toolset 名集合。

    - None / "" / "all" → 全部组（等于历史全量行为）；
    - "default" → :data:`DEFAULT_TOOLSETS`；
    - 逗号列表：``default`` 展开为核心组、``all`` 展开为全部，其余按组名收录；
      未知组名 → stderr 警告并忽略；
    - 全部 token 无效 → 警告并回退全部组（保证 server 照常拉起，功能纯增益）。
    """
    if raw is None:
        return set(ALL_TOOLSETS)
    text = raw.strip().lower()
    if text in ("", "all"):
        return set(ALL_TOOLSETS)

    enabled: set[str] = set()
    for token in text.split(","):
        name = token.strip()
        if not name:
            continue
        if name == "all":
            enabled |= set(ALL_TOOLSETS)
        elif name == "default":
            enabled |= set(DEFAULT_TOOLSETS)
        elif name in ALL_TOOLSETS:
            enabled.add(name)
        else:
            _warn(f"未知 toolset «{name}» 已忽略；合法组名：{sorted(ALL_TOOLSETS)}")

    if not enabled:
        _warn("AITEAM_TOOLSETS 无任何合法组名，回退全部组。")
        return set(ALL_TOOLSETS)
    return enabled


def resolve_readonly(raw: str | None) -> bool:
    """解析 AITEAM_READONLY；取值 1/true/yes/on（大小写不敏感）为只读档。"""
    if raw is None:
        return False
    return raw.strip().lower() in _READONLY_TRUE


def module_enabled(module_shortname: str, enabled_toolsets: set[str]) -> bool:
    """判定某模块是否应注册；未映射模块 fail-open（永远注册）保住全量兜底。"""
    toolset = MODULE_TOOLSET.get(module_shortname)
    if toolset is None:
        return True
    return toolset in enabled_toolsets
