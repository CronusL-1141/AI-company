"""Memory and knowledge MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register all memory-related MCP tools."""

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def memory_search(
        query: str = "",
        scope: str = "global",
        scope_id: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search the memory store in AI Team OS.

        Args:
            query: Search keywords
            scope: Memory scope, default "global"
            scope_id: Scope ID；**留空**时服务端按上下文推导（global→system、
                user→user、project→当前项目或未注册目录的指纹临时桶）。只有需要
                跨作用域精确指定时才显式传（如某 team 的 scope_id）。
            limit: Maximum number of results, default 10

        Returns:
            List of matching memories
        """
        params_dict: dict[str, Any] = {"scope": scope, "query": query, "limit": limit}
        # 留空则不发 scope_id，让服务端 get_scoped_repository 按 X-Project-Dir 推导——
        # 否则旧默认 "system" 会让未注册目录 memory_search 查不到自己的临时桶。
        if scope_id:
            params_dict["scope_id"] = scope_id
        params = urllib.parse.urlencode(params_dict)
        return _api_call("GET", f"/api/memory?{params}")

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
        task_memo_add（情景层），不要写这里。**

        体量红线是**单一轴：存储上限 = 注入预算**。方向层按桶计字符配额——
        global 1200 字 + 每个 project 1500 字 + user 300 字，一个会话实际继承
        3000 字；单条仍 ≤ 400 字。存得下的一定传得到，写不进去的就是真的没位置：
        超限时本工具返回该桶**全部有效条目**（id / kind / 字数 / 全文）+ 用量缺口，
        要求**当轮**先 memory_invalidate（可用 content_match 子串定位）或
        memory_reconcile_apply 腾出空间，再重试本次写入。
        超长内容改写成「触发条件 + 指向权威文件」的**指针条目**（如
        "涉及生产/集群/DB 时遵守只读铁律，详见 ~/.claude/CLAUDE.md"），正文外置。

        写入侧安全扫描：方向层条目会进每个派出 agent 的 system prompt，因此不可见
        Unicode、提示注入句式（覆盖既有指令 / 套取系统提示 / 伪造对话角色）、凭据
        形态一律拒绝入库。

        kind 四类（决定注入截断优先级 constraint>design>directive>preference）：
        - constraint（禁令/护栏）：一句话、可机检、终身有效。
          如 "所有输出使用中文"、"git 提交绝不自动加 agent 署名"。
        - design（价值排序/设计意图）：缺显式指令时的取舍依据。
          如 "技术决策偏向质量/简洁/健壮/长期可维护，不看重开发成本"。
        - directive（方法论/工作方式）：回答"怎么干"。
          如 "完成即按问题→根因→解法→验证汇报，不攒批次"。
        - preference（格式偏好）：可选，如 "每句一行便于 diff"。

        Args:
            content: 记忆内容（单条 ≤ 400 字，且须放得进本桶字符配额；超长改指针条目）
            kind: constraint / design / directive / preference
            scope: global（全局）/ project（当前项目）/ user（用户级）。
                写 global 前自问：**这条对任意目录的任意会话都成立吗？** 提及具体
                项目/仓库/书稿/某次任务的一律 scope=project——未注册目录会落入本目录
                指纹临时桶（"dir:..."），只被本目录的会话继承，绝不广播成全局记忆。
            supersedes: 可选，被本条置换失效的旧 memory id（偏好被改 = 新条 supersede
                旧条，Zep 失效语义不删除）
            source_refs: 可选，溯源 id 列表（回指 memo/report/meeting，蒸馏提升时用）

        Returns:
            写入结果；超桶配额时返回 success=False + quota 用量 + bucket_entries
            （该桶全部有效条目全文）+ next_action，安全扫描命中时返回拒绝原因
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
    def memory_invalidate(memory_id: str = "", content_match: str = "") -> dict[str, Any]:
        """Invalidate a direction-layer memory — mark it invalid without deleting.

        方向层偏好过时/被推翻时显式失效（Zep 失效语义：置 invalid_at 不删除，
        保留可审计轨迹）。失效后不再进注入，也默认不出现在 memory_list。

        两种定位方式，二选一：**memory_id 精确定位**，或 **content_match 子串定位**
        （手里只有原文时免去先查一次 id——被配额顶回来的那一刻正是这种处境）。
        子串必须唯一命中当前上下文的有效条目：命中 0 条或多条一律不动数据，多条时
        返回候选让你给出更精确的子串。

        Args:
            memory_id: 要失效的方向层记忆 id（与 content_match 二选一）
            content_match: 唯一定位子串，在有效条目正文中精确匹配（与 memory_id 二选一）

        Returns:
            失效后的条目；未命中/命中多条/id 不存在返回错误
        """
        if memory_id and content_match:
            return {
                "success": False,
                "error": "memory_id 与 content_match 二选一，不要同时传。",
            }
        if memory_id:
            return _api_call("POST", f"/api/memories/{memory_id}/invalidate", {})
        if content_match:
            return _api_call(
                "POST", "/api/memories/invalidate", {"content_match": content_match}
            )
        return {
            "success": False,
            "error": "需要 memory_id 或 content_match 之一来定位要失效的条目。",
        }

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

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 800000})
    def memory_reconcile_candidates(
        scope_path: str = "",
        threshold: float = 0.45,
    ) -> dict[str, Any]:
        """按需整理·粗筛：返回情景层候选组 + 方向层清单 + 蒸馏素材 + 操作说明。

        记忆整理 = 会话内按需显式动作（CC 非常驻，无后台整理进程）。本工具只做
        **确定性粗筛（零 LLM）**——OS 无独立 LLM 凭据，判定由你（调用工具的会话内
        agent）完成，工具只负责候选粗筛与操作应用（"agent 算、工具存"）。

        返回四块（project_id 自动按当前上下文解析）：
        - candidate_groups：有效 task_memos 按 scope_path/task 聚簇、簇内 BM25 两两
          相似度超阈配对成的候选组（含组内各条全文 + id）。逐组做 LLM 精判：
          KEEP（都留）/ MERGE（合并）/ INVALIDATE（矛盾失效）/ NOOP（不动）。
        - direction_inventory：全部有效方向层条目全文——逐条做**陈旧检查**（引用的
          功能已退役/版本过时/世界已变 → 提 invalidate）。
        - promotion_candidates：高频跨任务反复出现的簇，蒸馏为方向层条目的素材
          （promote 操作，source_refs 回指源 memo）。
        - operation_guide：四操作语义 + reconcile 三守则（只留高频有用 / 指向权威
          而非复述 / 重写精简优先）+ 量大开 ultracode 提示。

        判完后把确认的操作交给 memory_reconcile_apply 批量应用。

        Args:
            scope_path: 仅整理该路径作用域的 memo（留空=全项目有效 memo）
            threshold: 簇内 BM25 相似度配对阈值（0-1，默认 0.45）

        Returns:
            candidate_groups / promotion_candidates / direction_inventory /
            operation_guide / stats（含 ultracode_hint 当候选组量大时）
        """
        params_dict: dict[str, Any] = {"threshold": threshold}
        if scope_path:
            params_dict["scope_path"] = scope_path
        qs = urllib.parse.urlencode(params_dict)
        return _api_call("GET", f"/api/memory/reconcile/candidates?{qs}")

    @mcp.tool()
    def memory_reconcile_apply(operations: list[dict[str, Any]]) -> dict[str, Any]:
        """按需整理·应用：批量执行 LLM 精判确认后的操作（确定性，幂等）。

        每条操作是一个 dict，按 op 字段分派（未知/缺字段返回 error，不阻断其余）：
        - merge：{op:"merge", content:合并后新内容, memo_ids:[被并各条],
          memo_type?:"summary", scope_path?} —— 建新 memo，把被并各条置 invalid、
          invalidated_by 指向新条（Zep 失效语义不删除）。
        - invalidate：{op:"invalidate", memo_ids:[...]} —— 逐条失效（矛盾/被推翻）。
        - score：{op:"score", memo_id, quality_score:1-10, reason} —— 补质量分，
          reason 入 meta。
        - promote：{op:"promote", content, kind:constraint/design/directive/preference,
          scope?:"project"/global/user, source_refs?:[源 memo id]} —— 蒸馏提升为方向层
          条目；**红线照常生效**（单条 ≤400 字 + 桶字符配额 global 1200 / project
          1500 / user 300，超限该条返回 error 带用量；安全扫描同样生效）。
        - keep / noop：不动（可省略）。

        幂等：对已失效条目重复 invalidate/merge 返回 noop 不报错。应用后自动刷新
        项目 last_reconcile_at（量阈软提示的基线）。

        Args:
            operations: 操作列表，每条一个 dict，按 op 字段分派为
                merge / invalidate / score / promote / keep（各字段见工具说明）。
                一次可混装多种 op；单条出错只返回该条 error，不阻断其余。

        Returns:
            results（逐条 status: applied/noop/error）+ applied_count +
            last_reconcile_at
        """
        return _api_call(
            "POST", "/api/memory/reconcile/apply", {"operations": operations}
        )
