"""Ecosystem lifecycle trigger service (v1.5.0-C).

Implements Stage 1/2/3 漏斗 trigger primitives described in
``docs/v1.5.0-progressive-deep-review-design.md`` §2.2 / §2.3 / §2.4.

Design principle (用户决策 A): **ecosystem 不重做工作流，只触发现有系统**.
- Stage 2 辩论 → 走现有 meeting/debate_start (本服务返回会议 payload，
  由 MCP 工具/Leader 调 ``debate_start``)。
- Stage 3 集成 → 走现有 task_create (本服务返回 task payload，由 MCP 工具
  调 ``/api/projects/{id}/tasks``)。

Service operations
------------------
1. ``request_deep_review_batch`` — Stage 1 启动: 按 tags 拉候选 → 每仓建
   ``EcosystemDeepReview`` row + 推进 stage_status=QUEUED → 返回 backend-architect
   sub-agent 的 ``DispatchIntent`` 列表 (architecture_md 写回路径)。
2. ``apply_architecture_md`` — agent 回写 architecture markdown 后推进
   ``stage_status=architecture_done``，记 ``architecture_completed_at``。
3. ``trigger_debate`` — Stage 2 启动: 候选 review 必须处于 architecture_done。
   返回 ``DebateDispatchIntent`` (含 debate_start 参数 + 关联 review_ids)，
   由 MCP 工具/Leader 调 ``debate_start`` 后再调 ``link_debate_meeting`` 写回。
4. ``link_debate_meeting`` — 关联会议 id 到所有相关 review，便于 hook 反向写回。
5. ``apply_debate_result`` — Stage 2 结论回写: agent 写 risks/learnings/integration
   后推进 ``stage_status=debated``。
6. ``mark_as_reference`` — Stage 3 reference 路径: 加 lifecycle:reference tag +
   推进 ``stage_status=referenced``。
7. ``start_integration`` — Stage 3 integrate 路径: 返回 ``TaskDispatchIntent``
   (task_create 参数)，由 MCP 工具调 ``/api/projects/{id}/tasks`` 后再调
   ``link_integration_task`` 写回。

Dispatch pattern
----------------
Service 不 spawn agent / 不创建会议 / 不创建任务，而是返回 *Intent* dataclass
让上层 (MCP 工具或 Leader) 实际触发，与 Stage 0 worker (``DispatchIntent``)
保持一致，便于单元测试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemDeepReview,
    EcosystemRepoProfile,
    EcosystemRepoTag,
    EcosystemStageStatus,
    EcosystemTag,
    EcosystemTagCategory,
    EcosystemTagSource,
)

logger = logging.getLogger(__name__)


# ============================================================
# Constants
# ============================================================

# Stage 1 timeout per design §3.1.
STAGE1_TIMEOUT_SECONDS = 1800  # 30 min

# Lifecycle seed tag names (设计稿 §4.5 — 5 个 lifecycle tags)
LIFECYCLE_TAG_REFERENCE = "reference"
LIFECYCLE_TAG_INTEGRATED = "integrated"
LIFECYCLE_TAG_EVALUATING = "evaluating"
LIFECYCLE_TAG_DELETED = "deleted"
LIFECYCLE_TAG_PRIVATE_NOW = "private_now"

LIFECYCLE_TAGS: tuple[str, ...] = (
    LIFECYCLE_TAG_REFERENCE,
    LIFECYCLE_TAG_INTEGRATED,
    LIFECYCLE_TAG_EVALUATING,
    LIFECYCLE_TAG_DELETED,
    LIFECYCLE_TAG_PRIVATE_NOW,
)

# Lifecycle tag descriptions for ensure-on-demand seeding.
_LIFECYCLE_TAG_DESCRIPTIONS: dict[str, str] = {
    LIFECYCLE_TAG_REFERENCE: "已被项目作为架构参考",
    LIFECYCLE_TAG_INTEGRATED: "已被项目集成实施",
    LIFECYCLE_TAG_EVALUATING: "正在评估辩论中",
    LIFECYCLE_TAG_DELETED: "GitHub 端已删除",
    LIFECYCLE_TAG_PRIVATE_NOW: "GitHub 端已设为私密",
}

# Keywords that hint a meeting is ecosystem-related (反向写入 hook 用).
ECOSYSTEM_KEYWORDS: tuple[str, ...] = (
    "ecosystem",
    "生态",
    "生态库",
    "生态仓",
    "deep review",
    "深扫",
    "deep_review",
)


# ============================================================
# Stage 1 architecture-analysis sub-agent prompt
# ============================================================

ARCHITECTURE_AGENT_PROMPT = """\
你是 ecosystem Stage 1 architecture-analysis sub-agent (backend-architect)。

## 目标仓
- repo_full_name: {repo_full_name}
- repo_id: {repo_id}
- deep_review_id: {deep_review_id}
- stars: {stars}
- shallow_summary:
{shallow_summary}

## 研究目标
{research_goal}

## 任务（800-1500 字中文 markdown）
git clone --depth=1 https://github.com/{repo_full_name} /tmp/ecosystem-arch/{repo_id}
分析仓的架构关键点，回答：
1. **架构概览** — 核心分层、模块划分、入口在哪
2. **关键文件清单** — 5-10 个最有研究价值的文件 + 一句话说明
3. **核心模块** — 每个核心模块的职责、对外接口、依赖
4. **集成切入点** — 我们若要借鉴/集成，从哪入手风险最小

## 完成后调用
ecosystem_apply_architecture_md(
    deep_review_id="{deep_review_id}",
    architecture_md="<800-1500 字中文 markdown>",
)

## 约束
- 用 --depth=1 浅克隆节省空间。
- 不要 commit / push 该仓代码到我们项目。
- 不能完成时 (clone 失败 / 编码错)：调 ecosystem_apply_architecture_md 时
  把 architecture_md 留空，加 error_message 字段，OS 会走 failed 分类。
"""


# ============================================================
# Result types — Intent dataclasses
# ============================================================


@dataclass
class DeepReviewBatchIntent:
    """Stage 1 batch dispatch intent.

    每个 entry 对应一个候选仓的 backend-architect agent dispatch。
    """

    repo_id: str
    repo_full_name: str
    deep_review_id: str
    prompt: str
    timeout_seconds: int = STAGE1_TIMEOUT_SECONDS
    project_id: str | None = None


@dataclass
class DebateDispatchIntent:
    """Stage 2 debate dispatch intent.

    Returned by ``trigger_debate`` so the caller (MCP 工具/Leader) 可调用现有
    ``debate_start`` MCP 工具或 ``meeting_create``。包含建议参与者和讨论 topic
    模板，但具体角色/参会者由 Leader 在调用时决定。
    """

    review_ids: list[str]
    repo_full_names: list[str]
    research_goal: str
    suggested_topic: str
    suggested_advocate: str = "backend-architect"
    suggested_critic: str = "code-reviewer"
    suggested_judge: str = "team-lead"
    project_id: str | None = None


@dataclass
class TaskDispatchIntent:
    """Stage 3 integration task dispatch intent.

    Returned by ``start_integration`` so the caller can POST to
    ``/api/projects/{project_id}/tasks``. 上层调完任务后调
    ``link_integration_task`` 把 task.id 写回 review。
    """

    review_id: str
    repo_id: str
    repo_full_name: str
    title: str
    description: str
    priority: str = "high"
    horizon: str = "mid"
    tags: list[str] = field(default_factory=list)
    project_id: str | None = None


@dataclass
class WritebackHint:
    """Reverse-writeback hint emitted when a meeting topic looks ecosystem-related.

    The hook (``hooks/meeting_ecosystem_writeback.py``) reads concluded meeting
    topics and, when they hint at an ecosystem debate, emits this so Leader
    knows which review_ids should receive ``apply_debate_result`` calls.
    """

    meeting_id: str
    topic: str
    matched_keywords: list[str]
    review_ids: list[str]


# ============================================================
# Service
# ============================================================


class EcosystemLifecycleService:
    """Stage 1/2/3 trigger primitives + agent writeback handlers."""

    def __init__(
        self,
        repo: StorageRepository,
        *,
        project_id: str = "",
    ) -> None:
        """初始化生命周期服务。

        Args:
            repo: 数据访问层。
            project_id: 显式项目作用域；空时回退到 ``repo._project_scope``。
                所有写回操作都限定于该项目。
        """
        self._repo = repo
        self._project_id = project_id or repo._project_scope or ""

    # ------------------------------------------------------------------
    # Stage 1 — request_deep_review_batch + apply_architecture_md
    # ------------------------------------------------------------------

    async def request_deep_review_batch(
        self,
        *,
        tags: list[str] | None = None,
        min_stars: int | None = None,
        limit: int = 20,
        research_goal: str = "",
    ) -> list[DeepReviewBatchIntent]:
        """Stage 1 候选派遣：拉 active+shallow_done+tags 的仓 → 创建 review row。

        Args:
            tags: 必填候选筛选标签列表（AND 语义）。空则报错。
            min_stars: 项目设置的 min_stars 默认；显式传则覆盖。
            limit: 最多拉多少候选 (默认 20)。
            research_goal: 研究目标短语，注入 sub-agent prompt。

        Returns:
            每个候选仓一个 ``DeepReviewBatchIntent``，调用方据此 spawn
            backend-architect agent。

        Raises:
            ValueError: tags 为空时。
        """
        if not tags:
            raise ValueError("tags 不能为空：Stage 1 必须按标签筛选候选")

        # 解析 min_stars 默认 (项目 settings)
        if min_stars is None and self._project_id:
            settings = await self._repo.get_ecosystem_project_settings(
                self._project_id
            )
            if settings is not None:
                min_stars = settings.min_stars
        if min_stars is None:
            min_stars = 1000

        # 拉候选 — 必须 active + shallow_done + 包含所有 tags
        profiles, _ = await self._repo.search_ecosystem_profiles_extended(
            min_stars=min_stars,
            tags=list(tags),
            tag_match_mode="all",
            limit=limit,
            offset=0,
            project_id=self._project_id or None,
        )

        intents: list[DeepReviewBatchIntent] = []
        for p in profiles:
            if not p.is_active or p.is_deleted or p.is_private_now:
                continue
            # 必须有 shallow_summary 才进入 Stage 1
            if not p.shallow_summary:
                continue
            # 同仓已有未完成 architecture review 则跳过
            existing = await self._repo.list_deep_reviews_by_stage(
                EcosystemStageStatus.ARCHITECTURE_DONE,
                repo_id=p.id,
                project_id=self._project_id or None,
            )
            if existing:
                continue
            # D5 收敛：在飞判据只看权威轴 stage_status（status 已停写 running，
            # 旧 status∈{QUEUED,RUNNING} 条件对新行恒假会导致重复派遣）。
            # 两种在飞形态：
            #   ① stage=queued —— Stage 0 浅扫仍在飞；
            #   ② stage=shallow_done 且 architecture_md 为空且 claimed_by 非空
            #      —— 本方法此前派出的 Stage 1 架构评审在飞（建行即带
            #      claimed_by，stage 推进时由 choke point 自动释放）。
            in_flight = False
            current = await self._repo.list_deep_reviews(
                repo_id=p.id, project_id=self._project_id or None
            )
            for row in current:
                if row.stage_status == EcosystemStageStatus.QUEUED:
                    in_flight = True
                    break
                if (
                    row.stage_status == EcosystemStageStatus.SHALLOW_DONE
                    and row.architecture_md == ""
                    and row.claimed_by
                ):
                    in_flight = True
                    break
            if in_flight:
                continue

            # D5：不传 status（由 create_deep_review 按 stage 派生为 completed，
            # 与回填 F1 映射一致），建行原子携带 claimed_by 表达 Stage 1 在飞，
            # 同时堵住 claim_next_review_repo 对在飞行的双认领。
            now = datetime.now(tz=UTC)
            review = EcosystemDeepReview(
                project_id=self._project_id or None,
                repo_id=p.id,
                stage_status=EcosystemStageStatus.SHALLOW_DONE,
                claimed_at=now,
            )
            review.claimed_by = f"lifecycle:{review.id[:8]}"
            await self._repo.create_deep_review(
                review, project_id=self._project_id or None
            )

            prompt = self._build_architecture_prompt(
                profile=p,
                deep_review_id=review.id,
                research_goal=research_goal,
            )
            # D5: 停写 status=RUNNING —— status 为派生只读视图。
            await self._repo.update_deep_review(
                review.id,
                _project_id=self._project_id or None,
                started_at=now,
                dispatch_prompt=prompt,
            )

            intents.append(
                DeepReviewBatchIntent(
                    repo_id=p.id,
                    repo_full_name=p.repo_full_name,
                    deep_review_id=review.id,
                    prompt=prompt,
                    project_id=self._project_id or None,
                )
            )

        logger.info(
            "ecosystem_lifecycle.request_deep_review_batch tags=%s dispatched=%d",
            tags,
            len(intents),
        )
        return intents

    async def apply_architecture_md(
        self,
        deep_review_id: str,
        *,
        architecture_md: str,
        agent_id: str | None = None,
    ) -> EcosystemDeepReview | None:
        """Stage 1 写回：agent 提交 architecture_md → 推进 architecture_done。

        Args:
            deep_review_id: 目标 review id。
            architecture_md: 800-1500 字中文 markdown。空字符串触发错误。
            agent_id: 可选 — 写入 review.agent_id 便于追溯。

        Returns:
            更新后的 review，找不到 → None。

        Raises:
            ValueError: architecture_md 为空时。
        """
        if not architecture_md or not architecture_md.strip():
            raise ValueError("architecture_md 不能为空")

        review = await self._repo.get_deep_review(
            deep_review_id, project_id=self._project_id or None
        )
        if review is None:
            return None

        # D5: 不再旁写 status=COMPLETED / completed_at —— 随后的
        # update_deep_review_stage(ARCHITECTURE_DONE) 派生补齐。
        update_kwargs: dict = {
            "_project_id": self._project_id or None,
            "architecture_md": architecture_md.strip(),
        }
        if agent_id:
            update_kwargs["agent_id"] = agent_id

        await self._repo.update_deep_review(deep_review_id, **update_kwargs)
        return await self._repo.update_deep_review_stage(
            deep_review_id,
            EcosystemStageStatus.ARCHITECTURE_DONE,
            project_id=self._project_id or None,
        )

    # ------------------------------------------------------------------
    # Stage 2 — trigger_debate / link_debate_meeting / apply_debate_result
    # ------------------------------------------------------------------

    async def trigger_debate(
        self,
        *,
        repo_ids: list[str],
        research_goal: str,
        suggested_advocate: str = "backend-architect",
        suggested_critic: str = "code-reviewer",
        suggested_judge: str = "team-lead",
    ) -> DebateDispatchIntent:
        """Stage 2 触发：拉 architecture_done 的 review，生成 debate dispatch payload。

        Args:
            repo_ids: 选中的 finalist 仓 id 列表 (1-5 个为宜)。
            research_goal: 研究目标，用于会议 topic。
            suggested_advocate / critic / judge: 推荐角色，调用方可覆盖。

        Returns:
            ``DebateDispatchIntent``，调用方据此调 ``debate_start`` MCP 工具
            或直接 POST 到 ``/api/teams/{team}/meetings``。

        Raises:
            ValueError: repo_ids 为空，或没有任何对应的 architecture_done review。
        """
        if not repo_ids:
            raise ValueError("repo_ids 不能为空：Stage 2 必须指定 finalists")

        review_ids: list[str] = []
        repo_full_names: list[str] = []
        for repo_id in repo_ids:
            reviews = await self._repo.list_deep_reviews_by_stage(
                EcosystemStageStatus.ARCHITECTURE_DONE,
                repo_id=repo_id,
                project_id=self._project_id or None,
            )
            if not reviews:
                logger.warning(
                    "ecosystem_lifecycle.trigger_debate skipping repo_id=%s "
                    "(no architecture_done review)",
                    repo_id,
                )
                continue
            # 取最新一条 architecture_done review
            review = reviews[0]
            review_ids.append(review.id)
            profile = await self._repo.get_ecosystem_profile_by_id(
                repo_id, project_id=self._project_id or None
            )
            if profile is not None:
                repo_full_names.append(profile.repo_full_name)
            else:
                repo_full_names.append(f"<unknown:{repo_id[:8]}>")

        if not review_ids:
            raise ValueError(
                "没有任何 repo_ids 对应 architecture_done review，"
                "无法触发辩论。请先完成 Stage 1。"
            )

        topic = self._build_debate_topic(
            repo_full_names=repo_full_names, research_goal=research_goal
        )
        return DebateDispatchIntent(
            review_ids=review_ids,
            repo_full_names=repo_full_names,
            research_goal=research_goal,
            suggested_topic=topic,
            suggested_advocate=suggested_advocate,
            suggested_critic=suggested_critic,
            suggested_judge=suggested_judge,
            project_id=self._project_id or None,
        )

    async def link_debate_meeting(
        self,
        *,
        review_ids: list[str],
        meeting_id: str,
    ) -> int:
        """会议创建后回写 meeting_id 到所有相关 review 行。

        与 trigger_debate 配套：trigger_debate 返回 review_ids → 调用方调
        debate_start 创建会议 → 把 meeting.id + review_ids 一并传给本方法。

        Args:
            review_ids: 关联的 review id 列表。
            meeting_id: 创建出的 meeting id。

        Returns:
            实际更新的 review 行数。
        """
        if not review_ids or not meeting_id:
            return 0
        updated = 0
        for review_id in review_ids:
            row = await self._repo.update_deep_review(
                review_id,
                _project_id=self._project_id or None,
                debate_meeting_id=meeting_id,
            )
            if row is not None:
                updated += 1
        return updated

    async def apply_debate_result(
        self,
        deep_review_id: str,
        *,
        risks_md: str = "",
        learnings_md: str = "",
        integration_md: str = "",
        integration_recommendation: str = "",
        agent_id: str | None = None,
    ) -> EcosystemDeepReview | None:
        """Stage 2 写回：辩论结论 → 推进 stage_status=debated。

        Args:
            deep_review_id: 目标 review id。
            risks_md: 风险点 markdown。
            learnings_md: 借鉴点 markdown。
            integration_md: 集成建议 markdown。
            integration_recommendation: enum 字符串 (integrate/reference/learn/skip)。
            agent_id: 可选——写入 review.agent_id。

        Returns:
            更新后的 review，找不到 → None。

        Raises:
            ValueError: 三个 *_md 全空 (至少一个非空才算有效结论)。
        """
        non_empty = [s for s in (risks_md, learnings_md, integration_md) if s.strip()]
        if not non_empty:
            raise ValueError(
                "risks_md / learnings_md / integration_md 至少需提供一个非空"
            )

        review = await self._repo.get_deep_review(
            deep_review_id, project_id=self._project_id or None
        )
        if review is None:
            return None

        update_kwargs: dict = {
            "_project_id": self._project_id or None,
        }
        if risks_md.strip():
            update_kwargs["risks_md"] = risks_md.strip()
        if learnings_md.strip():
            update_kwargs["learnings_md"] = learnings_md.strip()
        if integration_md.strip():
            update_kwargs["integration_md"] = integration_md.strip()
        if agent_id:
            update_kwargs["agent_id"] = agent_id

        # 处理 integration_recommendation
        from aiteam.types import IntegrationRecommendation

        if integration_recommendation:
            try:
                update_kwargs["integration_recommendation"] = (
                    IntegrationRecommendation(integration_recommendation.lower())
                )
            except ValueError:
                logger.warning(
                    "apply_debate_result: invalid integration_recommendation=%r",
                    integration_recommendation,
                )

        await self._repo.update_deep_review(deep_review_id, **update_kwargs)
        return await self._repo.update_deep_review_stage(
            deep_review_id,
            EcosystemStageStatus.DEBATED,
            project_id=self._project_id or None,
        )

    # ------------------------------------------------------------------
    # Stage 3 — mark_as_reference / start_integration
    # ------------------------------------------------------------------

    async def mark_as_reference(
        self,
        deep_review_id: str,
        *,
        agent_id: str | None = None,
        confidence: float = 1.0,
    ) -> EcosystemDeepReview | None:
        """Stage 3 reference：加 lifecycle:reference tag + 推进 referenced。

        Args:
            deep_review_id: 目标 review id。
            agent_id: 可选 — 写入 EcosystemRepoTag.agent_id。
            confidence: tag 置信度 (默认 1.0 — manual 决策)。

        Returns:
            更新后的 review，找不到 → None。
        """
        review = await self._repo.get_deep_review(
            deep_review_id, project_id=self._project_id or None
        )
        if review is None:
            return None

        await self._ensure_lifecycle_tag_applied(
            repo_id=review.repo_id,
            tag_name=LIFECYCLE_TAG_REFERENCE,
            agent_id=agent_id,
            confidence=confidence,
        )
        return await self._repo.update_deep_review_stage(
            deep_review_id,
            EcosystemStageStatus.REFERENCED,
            project_id=self._project_id or None,
        )

    async def start_integration(
        self,
        deep_review_id: str,
        *,
        title: str = "",
        description: str = "",
        priority: str = "high",
        horizon: str = "mid",
        extra_tags: list[str] | None = None,
    ) -> TaskDispatchIntent:
        """Stage 3 integrate：返回 task_create payload，由调用方实际创建任务。

        本方法只构建 ``TaskDispatchIntent`` (不直接调 task_create) 并推进
        review.stage_status=INTEGRATED。调用方负责调 ``/api/projects/{id}/tasks``
        创建任务后再调 ``link_integration_task`` 把 task.id 写回 review。

        Args:
            deep_review_id: 目标 review id。
            title: 任务标题，空时自动生成 ``Integrate {repo_full_name}: {summary}``。
            description: 任务描述，空时自动汇总 review 的 architecture/integration_md。
            priority / horizon: 任务优先级与时间维度。
            extra_tags: 追加 tag 列表（默认会附加 ``ecosystem-integration``）。

        Returns:
            ``TaskDispatchIntent``，调用方按字段 POST 到任务 API。

        Raises:
            ValueError: review 不存在或未到 debated 状态。
        """
        review = await self._repo.get_deep_review(
            deep_review_id, project_id=self._project_id or None
        )
        if review is None:
            raise ValueError(f"deep_review id={deep_review_id} 不存在")
        if review.stage_status not in (
            EcosystemStageStatus.DEBATED,
            EcosystemStageStatus.ARCHITECTURE_DONE,
            EcosystemStageStatus.REFERENCED,
        ):
            raise ValueError(
                f"deep_review stage_status={review.stage_status.value}，"
                "需先到 architecture_done / debated / referenced 才能集成"
            )

        profile = await self._repo.get_ecosystem_profile_by_id(
            review.repo_id, project_id=self._project_id or None
        )
        if profile is None:
            raise ValueError(f"repo profile id={review.repo_id} 不存在")

        # 标 lifecycle:integrated tag
        await self._ensure_lifecycle_tag_applied(
            repo_id=review.repo_id,
            tag_name=LIFECYCLE_TAG_INTEGRATED,
            agent_id=None,
            confidence=1.0,
        )

        # 构造 task payload
        repo_label = profile.repo_full_name
        if not title:
            short_summary = (
                profile.shallow_summary or profile.description or ""
            ).strip()[:60]
            title = f"Integrate {repo_label}: {short_summary}".strip()
        if not description:
            description = self._build_integration_description(profile, review)

        tags = ["ecosystem-integration", f"repo:{repo_label}"]
        if extra_tags:
            tags.extend(t for t in extra_tags if t and t not in tags)

        # 推进 stage_status → integrated (task_id 由 link_integration_task 后续写)
        await self._repo.update_deep_review_stage(
            deep_review_id,
            EcosystemStageStatus.INTEGRATED,
            project_id=self._project_id or None,
        )

        return TaskDispatchIntent(
            review_id=deep_review_id,
            repo_id=review.repo_id,
            repo_full_name=repo_label,
            title=title,
            description=description,
            priority=priority,
            horizon=horizon,
            tags=tags,
            project_id=self._project_id or None,
        )

    async def link_integration_task(
        self,
        *,
        deep_review_id: str,
        task_id: str,
    ) -> EcosystemDeepReview | None:
        """配套 start_integration：任务创建后回写 task_id。

        Args:
            deep_review_id: 目标 review id。
            task_id: 创建出的 task id。

        Returns:
            更新后的 review，找不到 → None。
        """
        if not task_id:
            return None
        return await self._repo.update_deep_review_stage(
            deep_review_id,
            EcosystemStageStatus.INTEGRATED,
            integration_task_id=task_id,
            project_id=self._project_id or None,
        )

    # ------------------------------------------------------------------
    # Reverse-writeback hint detection
    # ------------------------------------------------------------------

    async def detect_meeting_writeback(
        self,
        *,
        meeting_id: str,
        topic: str,
    ) -> WritebackHint | None:
        """会议结束时检测是否涉及生态库 → 提醒 Leader 反向写回。

        Topic 含 ECOSYSTEM_KEYWORDS 任一关键词 → 拉所有 debate_meeting_id=
        meeting_id 的 review，返回一个 ``WritebackHint`` 让 hook/Leader
        知道该让 agent 调 apply_debate_result。

        Args:
            meeting_id: 刚结束的会议 id。
            topic: 会议 topic 文本。

        Returns:
            ``WritebackHint`` 或 None（如果 topic 不命中关键词）。
        """
        if not topic:
            return None
        topic_lower = topic.lower()
        matched = [kw for kw in ECOSYSTEM_KEYWORDS if kw.lower() in topic_lower]
        if not matched:
            return None

        # 拉所有该会议关联的 review
        reviews = await self._repo.list_deep_reviews(
            limit=100, project_id=self._project_id or None
        )
        review_ids = [r.id for r in reviews if r.debate_meeting_id == meeting_id]

        return WritebackHint(
            meeting_id=meeting_id,
            topic=topic,
            matched_keywords=matched,
            review_ids=review_ids,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_architecture_prompt(
        *,
        profile: EcosystemRepoProfile,
        deep_review_id: str,
        research_goal: str,
    ) -> str:
        """渲染 Stage 1 sub-agent prompt。"""
        return ARCHITECTURE_AGENT_PROMPT.format(
            repo_full_name=profile.repo_full_name,
            repo_id=profile.id,
            deep_review_id=deep_review_id,
            stars=profile.stars,
            shallow_summary=(profile.shallow_summary or "(暂无浅扫总结)")[:1500],
            research_goal=research_goal or "(未指定特定研究目标)",
        )

    @staticmethod
    def _build_debate_topic(
        *,
        repo_full_names: list[str],
        research_goal: str,
    ) -> str:
        """合成会议 topic 字符串。"""
        head = research_goal or "生态库选型辩论"
        candidates = ", ".join(repo_full_names[:5])
        if len(repo_full_names) > 5:
            candidates += f"... (共 {len(repo_full_names)} 个)"
        return f"[Ecosystem Stage 2] {head} — 候选: {candidates}"

    @staticmethod
    def _build_integration_description(
        profile: EcosystemRepoProfile,
        review: EcosystemDeepReview,
    ) -> str:
        """合成集成任务描述（汇总 architecture / integration md）。"""
        parts: list[str] = [
            "## 集成目标",
            f"将 [{profile.repo_full_name}](https://github.com/{profile.repo_full_name}) "
            "集成到本项目。",
            "",
        ]
        if profile.shallow_summary:
            parts.append("## 仓概览（浅扫）")
            parts.append(profile.shallow_summary[:500])
            parts.append("")
        if review.architecture_md:
            parts.append("## 架构概览")
            parts.append(review.architecture_md[:1500])
            parts.append("")
        if review.integration_md:
            parts.append("## 集成建议（来自 Stage 2 辩论）")
            parts.append(review.integration_md[:1500])
            parts.append("")
        if review.risks_md:
            parts.append("## 已知风险")
            parts.append(review.risks_md[:800])
            parts.append("")
        parts.append("## 关联")
        parts.append(f"- ecosystem deep_review_id: `{review.id}`")
        parts.append(f"- ecosystem repo_id: `{review.repo_id}`")
        if review.debate_meeting_id:
            parts.append(f"- 辩论会议: `{review.debate_meeting_id}`")
        return "\n".join(parts)

    async def _ensure_lifecycle_tag(self, tag_name: str) -> EcosystemTag:
        """按需 seed lifecycle tag（positioning 类）。已存在则返回。"""
        existing = await self._repo.get_tag_by_name(tag_name)
        if existing is not None:
            return existing
        new_tag = EcosystemTag(
            name=tag_name,
            category=EcosystemTagCategory.POSITIONING,
            description=_LIFECYCLE_TAG_DESCRIPTIONS.get(tag_name, ""),
        )
        await self._repo.upsert_tag(new_tag)
        seeded = await self._repo.get_tag_by_name(tag_name)
        return seeded or new_tag

    async def _ensure_lifecycle_tag_applied(
        self,
        *,
        repo_id: str,
        tag_name: str,
        agent_id: str | None,
        confidence: float,
    ) -> EcosystemRepoTag:
        """加 lifecycle:* tag 到 repo（已存在则更新 source/agent/confidence）。"""
        tag = await self._ensure_lifecycle_tag(tag_name)
        repo_tag = EcosystemRepoTag(
            project_id=self._project_id or None,
            repo_id=repo_id,
            tag_id=tag.id,
            confidence=max(0.0, min(1.0, confidence)),
            source=EcosystemTagSource.LIFECYCLE,
            agent_id=agent_id,
        )
        return await self._repo.add_repo_tag(
            repo_tag, project_id=self._project_id or None
        )


__all__ = [
    "EcosystemLifecycleService",
    "DeepReviewBatchIntent",
    "DebateDispatchIntent",
    "TaskDispatchIntent",
    "WritebackHint",
    "ARCHITECTURE_AGENT_PROMPT",
    "STAGE1_TIMEOUT_SECONDS",
    "LIFECYCLE_TAG_REFERENCE",
    "LIFECYCLE_TAG_INTEGRATED",
    "LIFECYCLE_TAG_EVALUATING",
    "LIFECYCLE_TAG_DELETED",
    "LIFECYCLE_TAG_PRIVATE_NOW",
    "LIFECYCLE_TAGS",
    "ECOSYSTEM_KEYWORDS",
]
