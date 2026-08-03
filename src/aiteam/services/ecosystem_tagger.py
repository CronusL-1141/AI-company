"""Ecosystem 三层打标系统 — Layer 1 (topics) + Layer 2 (rules) + Layer 3 (LLM).

设计要点：
- Layer 1：GitHub topics 直接映射，confidence=0.95，source=GITHUB_TOPIC
- Layer 2：name+description+topics 关键词/正则规则匹配，confidence=0.7，source=AUTO_RULE
- Layer 3：LLM 兜底 — 由本服务返回 dispatch_plan，Leader 派发子 agent 后回写
  通过 apply_llm_tags() 接收子 agent 的 JSON 输出 (tags + confidence)，写入 source=AUTO_LLM
- 统一应用：tag_repo() / tag_repos_batch() 自动跑 Layer 1+2，并标识哪些仓需 Layer 3
- 不直接调用 LLM API（节省 token），由 CC 主进程调度子 agent

Layer 3 子 agent prompt 模板见 build_llm_dispatch_plan()。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiteam.services.agent_template_registry import check_subagent_types
from aiteam.services.ecosystem_tag_rules import (
    GITHUB_TOPIC_ALIASES,
    KEYWORD_RULES,
    match_docs_only_by_language,
    match_github_topics,
    match_keyword_rules,
    match_language_tag,
)
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemRepoTag,
    EcosystemTag,
    EcosystemTagSource,
)

logger = logging.getLogger(__name__)


# ============================================================
# Layer 3 LLM 子 agent 派遣常量
# ============================================================

# 单批最大并发子 agent 数量，避免 token 飙
MAX_LLM_CONCURRENCY: int = 20

# Default subagent_type for Layer 3. It must be a type the Agent tool really
# accepts: CC rejects unknown ones outright ("Agent type '<x>' not found"), which
# kills the whole dispatch plan. 'general-purpose' is a CC built-in (present even
# without the OS plugin installed) and carries tools='*' — required here because
# the write-back tool ecosystem_tag_apply_llm_result is explicitly excluded from
# several OS templates.
DEFAULT_LLM_AGENT_TEMPLATE: str = "general-purpose"

# Dispatch charter: orchestrate with Fable, execute with Opus. 'general-purpose'
# has no template frontmatter to carry a model, so pin it here — otherwise the
# sub-agent silently inherits the orchestrator's higher-tier model.
DEFAULT_LLM_AGENT_MODEL: str = "opus"

# Layer 1 / 2 命中数小于此阈值时认为需 Layer 3 兜底
LLM_FALLBACK_TAG_THRESHOLD: int = 2

# Layer 1 / 2 / 3 各自的默认 confidence
CONFIDENCE_TOPIC: float = 0.95
CONFIDENCE_RULE: float = 0.7
CONFIDENCE_LANGUAGE: float = 0.9  # K4: language field is highly reliable
CONFIDENCE_LLM_DEFAULT: float = 0.6

# Layer 3 Sub-agent prompt 模板 (< 500 字)
_LLM_TAGGER_PROMPT_TEMPLATE = """\
你是 ecosystem 仓打标 sub-agent。请阅读下面这个仓的元信息，从允许的标签列表中选出最匹配的 1-5 个标签。

仓信息：
- 全名: {repo_full_name}
- 描述: {description}
- 已有 topics: {topics}
- 主语言: {language}
- 现有标签: {existing_tags}

允许的标签（只能从此列表选）：
{allowed_tags}

输出严格 JSON（不要 markdown 包裹，不要解释）：
{{"tags": [{{"name": "memory_system", "confidence": 0.85}}, ...]}}

要求：
1. confidence 范围 0.0-1.0，仅选自己确信度 >= 0.5 的标签
2. 不要新造标签，不在允许列表里的一律不选
3. 最多 5 个标签
4. 若无任何高置信度匹配，返回 {{"tags": []}}
"""


# ============================================================
# Result 数据结构
# ============================================================

@dataclass
class TagApplyResult:
    """单个仓的打标结果。"""

    repo_id: str
    repo_full_name: str
    layer1_tags: list[str] = field(default_factory=list)  # 通过 GitHub topics 命中的 tag name
    layer2_tags: list[str] = field(default_factory=list)  # 通过规则命中的 tag name
    layer3_tags: list[str] = field(default_factory=list)  # 通过 LLM 命中的 tag name
    skipped_unknown: list[str] = field(default_factory=list)  # 子 agent 输出但字典无对应的 tag
    needs_llm: bool = False  # True 表示 Layer 1+2 命中数低于阈值

    def total_applied(self) -> int:
        return len(self.layer1_tags) + len(self.layer2_tags) + len(self.layer3_tags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "repo_full_name": self.repo_full_name,
            "layer1_tags": self.layer1_tags,
            "layer2_tags": self.layer2_tags,
            "layer3_tags": self.layer3_tags,
            "skipped_unknown": self.skipped_unknown,
            "needs_llm": self.needs_llm,
            "total_applied": self.total_applied(),
        }


@dataclass
class BatchApplyStats:
    """批量打标统计。"""

    repos_processed: int = 0
    layer1_applied: int = 0
    layer2_applied: int = 0
    layer3_applied: int = 0
    repos_needing_llm: int = 0
    repos_failed: int = 0
    by_repo: list[TagApplyResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repos_processed": self.repos_processed,
            "layer1_applied": self.layer1_applied,
            "layer2_applied": self.layer2_applied,
            "layer3_applied": self.layer3_applied,
            "repos_needing_llm": self.repos_needing_llm,
            "repos_failed": self.repos_failed,
            "by_repo": [r.to_dict() for r in self.by_repo],
        }


# ============================================================
# Tagger Service
# ============================================================

class EcosystemTagger:
    """三层打标服务，对接 StorageRepository。

    用法：
      tagger = EcosystemTagger(repo)
      result = await tagger.tag_repo(repo_profile)
      stats = await tagger.tag_repos_batch(profiles)
      plan = tagger.build_llm_dispatch_plan(repos_needing_llm)
      await tagger.apply_llm_tags(repo_id, [{"name": "memory_system", "confidence": 0.85}], agent_id)
    """

    def __init__(
        self,
        repo: StorageRepository,
        project_id: str = "",
    ) -> None:
        """初始化打标服务。

        Args:
            repo: 数据访问层。
            project_id: 可选项目作用域；空时透传 repo._project_scope。
                所有 repo-tag 关联写入都会附带此项目 id。
        """
        self._repo = repo
        self._project_id = project_id or repo._project_scope or ""
        self._tag_cache: dict[str, EcosystemTag] | None = None  # name -> tag

    async def _load_tag_dict(self) -> dict[str, EcosystemTag]:
        """加载并缓存全部标签字典 (按 name 索引)。"""
        if self._tag_cache is None:
            tags = await self._repo.list_tags(limit=500)
            self._tag_cache = {t.name: t for t in tags}
        return self._tag_cache

    def invalidate_cache(self) -> None:
        """清空标签缓存 — 当上游手动新增标签后调用。"""
        self._tag_cache = None

    async def _resolve_tag_name_to_id(self, name: str) -> str | None:
        """将 tag name 解析为 tag id，未注册则返回 None。"""
        cache = await self._load_tag_dict()
        tag = cache.get(name)
        return tag.id if tag else None

    # --------------------------------------------------------
    # Public single-repo entry
    # --------------------------------------------------------

    async def tag_repo(
        self,
        *,
        repo_id: str,
        repo_full_name: str,
        name: str,
        description: str | None,
        topics: list[str],
        owner: str = "",
        language: str | None = None,
        agent_id: str | None = "ecosystem-tagger",
        replace_auto: bool = False,
    ) -> TagApplyResult:
        """对单个仓执行 Layer 1 + 2 自动打标，写入 EcosystemRepoTag。

        Layer 3（LLM）不在此方法触发；仅返回 needs_llm 标记由调用方决定是否派遣。

        K4: 增加 language 字段直接映射 (CONFIDENCE_LANGUAGE=0.9)，并在主语言为
        Jupyter Notebook / TeX / HTML 等文档型时推断 docs_only 标签。

        K4 follow-up: ``replace_auto=True`` 时先删除该仓 source ∈
        {github_topic, auto_rule} 的旧标签后再写入；保留 manual / auto_llm。
        用于规则升级后清理陈旧自动标签。
        """
        result = TagApplyResult(repo_id=repo_id, repo_full_name=repo_full_name)
        cache = await self._load_tag_dict()

        if replace_auto:
            await self._repo.delete_repo_tags_by_sources(
                repo_id=repo_id,
                sources=[
                    EcosystemTagSource.GITHUB_TOPIC.value,
                    EcosystemTagSource.AUTO_RULE.value,
                ],
                project_id=self._project_id or None,
            )

        # Layer 1: GitHub topics
        layer1 = match_github_topics(topics)
        for tag_name in layer1:
            if tag_name not in cache:
                result.skipped_unknown.append(tag_name)
                continue
            await self._repo.add_repo_tag(
                EcosystemRepoTag(
                    project_id=self._project_id or None,
                    repo_id=repo_id,
                    tag_id=cache[tag_name].id,
                    confidence=CONFIDENCE_TOPIC,
                    source=EcosystemTagSource.GITHUB_TOPIC,
                    agent_id=agent_id,
                ),
                project_id=self._project_id or None,
            )
            result.layer1_tags.append(tag_name)

        # Layer 2a: language field -> tech_stack tag (high confidence)
        lang_match = match_language_tag(language)
        for tag_name in lang_match:
            if tag_name in layer1:
                continue
            if tag_name not in cache:
                result.skipped_unknown.append(tag_name)
                continue
            await self._repo.add_repo_tag(
                EcosystemRepoTag(
                    project_id=self._project_id or None,
                    repo_id=repo_id,
                    tag_id=cache[tag_name].id,
                    confidence=CONFIDENCE_LANGUAGE,
                    source=EcosystemTagSource.AUTO_RULE,
                    agent_id=agent_id,
                ),
                project_id=self._project_id or None,
            )
            result.layer2_tags.append(tag_name)

        # Layer 2b: docs_only inference from language
        if match_docs_only_by_language(language) and "docs_only" not in result.layer2_tags:
            if "docs_only" in cache:
                await self._repo.add_repo_tag(
                    EcosystemRepoTag(
                        project_id=self._project_id or None,
                        repo_id=repo_id,
                        tag_id=cache["docs_only"].id,
                        confidence=CONFIDENCE_LANGUAGE,
                        source=EcosystemTagSource.AUTO_RULE,
                        agent_id=agent_id,
                    ),
                    project_id=self._project_id or None,
                )
                result.layer2_tags.append("docs_only")

        # Layer 2c: keyword rules — 跳过 Layer 1+2a/b 已命中的 tag
        layer2 = match_keyword_rules(name=name, description=description, topics=topics, owner=owner)
        already = set(layer1) | set(result.layer2_tags)
        for tag_name in layer2:
            if tag_name in already:
                continue
            if tag_name not in cache:
                result.skipped_unknown.append(tag_name)
                continue
            await self._repo.add_repo_tag(
                EcosystemRepoTag(
                    project_id=self._project_id or None,
                    repo_id=repo_id,
                    tag_id=cache[tag_name].id,
                    confidence=CONFIDENCE_RULE,
                    source=EcosystemTagSource.AUTO_RULE,
                    agent_id=agent_id,
                ),
                project_id=self._project_id or None,
            )
            result.layer2_tags.append(tag_name)

        if result.total_applied() < LLM_FALLBACK_TAG_THRESHOLD:
            result.needs_llm = True
        return result

    async def tag_repos_batch(
        self,
        repos: list[dict[str, Any]],
        *,
        agent_id: str | None = "ecosystem-tagger",
        replace_auto: bool = False,
    ) -> BatchApplyStats:
        """批量打标。

        repos 元素必须包含: id, repo_full_name, name, description, topics, owner, language。

        ``replace_auto=True`` 透传给单仓 tag_repo，用于规则升级后批量清理旧自动标签。
        """
        stats = BatchApplyStats()
        for r in repos:
            try:
                tr = await self.tag_repo(
                    repo_id=r["id"],
                    repo_full_name=r["repo_full_name"],
                    name=r.get("name", ""),
                    description=r.get("description"),
                    topics=r.get("topics") or [],
                    owner=r.get("owner", ""),
                    language=r.get("language"),
                    agent_id=agent_id,
                    replace_auto=replace_auto,
                )
                stats.by_repo.append(tr)
                stats.layer1_applied += len(tr.layer1_tags)
                stats.layer2_applied += len(tr.layer2_tags)
                if tr.needs_llm:
                    stats.repos_needing_llm += 1
            except Exception as exc:
                logger.warning("tag_repo failed for %s: %s", r.get("repo_full_name", "?"), exc)
                stats.repos_failed += 1
            stats.repos_processed += 1
        return stats

    # --------------------------------------------------------
    # Layer 3: LLM dispatch_plan + result apply
    # --------------------------------------------------------

    async def build_llm_dispatch_plan(
        self,
        repos: list[dict[str, Any]],
        *,
        agent_template: str = DEFAULT_LLM_AGENT_TEMPLATE,
        max_concurrency: int = MAX_LLM_CONCURRENCY,
    ) -> dict[str, Any]:
        """为需要 Layer 3 LLM 兜底的仓生成派遣计划。

        Leader 收到后按 plan["dispatch"] 列表逐项 spawn sub-agent，
        sub-agent 完成后调用 ecosystem_tag_apply_llm_result（MCP 工具）回写结果。

        agent_template 会对照模板真相源核一次：命中即通过，未命中**原样放行**并在
        plan 顶层 template_warnings 里报警——模板是加速器不是白名单，CC 内置类型
        无法枚举，是否改用就近模板由读计划的人定夺。
        """
        cache = await self._load_tag_dict()
        allowed_tags = sorted(cache.keys())
        allowed_brief = "\n".join(f"- {n} ({cache[n].category.value})" for n in allowed_tags)

        # 截断到 max_concurrency
        batch = repos[:max_concurrency]
        skipped = max(len(repos) - max_concurrency, 0)

        dispatch: list[dict[str, Any]] = []
        for r in batch:
            existing_tags_rows = await self._repo.list_repo_tags(repo_id=r["id"])
            existing_names: list[str] = []
            for row in existing_tags_rows:
                t = await self._repo.get_tag(row.tag_id)
                if t:
                    existing_names.append(t.name)

            prompt = _LLM_TAGGER_PROMPT_TEMPLATE.format(
                repo_full_name=r.get("repo_full_name", "?"),
                description=(r.get("description") or "(无)")[:300],
                topics=", ".join(r.get("topics") or []) or "(无)",
                language=r.get("language") or "(未知)",
                existing_tags=", ".join(existing_names) or "(无)",
                allowed_tags=allowed_brief,
            )
            dispatch.append({
                "repo_id": r["id"],
                "repo_full_name": r.get("repo_full_name"),
                "launch_call": {
                    "tool": "Agent",
                    "params": {
                        # No team_name: CC v2.1.219 deprecates and ignores it —
                        # the session owns a single implicit team and OS-side
                        # attribution is handled by SubagentStart auto-enrolment.
                        "subagent_type": agent_template,
                        "model": DEFAULT_LLM_AGENT_MODEL,
                        "description": f"标签兜底-{r.get('repo_full_name', '?')}",
                        "prompt": prompt,
                    },
                },
            })

        # Report-only: the dispatch above already carries the caller's own template.
        template_warnings = check_subagent_types(
            [(agent_template, f"全部 {len(dispatch)} 个 dispatch 项")]
        )
        instructions = (
            "Leader 顺序调用每个 dispatch[i].launch_call 派发 sub-agent。"
            "Sub-agent 输出 JSON 后通过 MCP 工具 ecosystem_tag_apply_llm_result"
            " 提交：repo_id + tags 数组。"
        )
        for w in template_warnings:
            instructions += f"\n⚠️ {w['message']}"

        return {
            "agent_template": agent_template,
            "max_concurrency": max_concurrency,
            "total_requested": len(repos),
            "dispatched": len(dispatch),
            "skipped_due_to_limit": skipped,
            "dispatch": dispatch,
            "template_warnings": template_warnings,
            "instructions": instructions,
        }

    async def apply_llm_tags(
        self,
        *,
        repo_id: str,
        repo_full_name: str,
        llm_output_tags: list[dict[str, Any]],
        agent_id: str | None = None,
    ) -> TagApplyResult:
        """接收 Layer 3 sub-agent 输出，写入 EcosystemRepoTag (source=AUTO_LLM)。

        llm_output_tags: list of {"name": str, "confidence": float}
        非字典里的 tag 一律忽略并记入 skipped_unknown。
        """
        cache = await self._load_tag_dict()
        result = TagApplyResult(repo_id=repo_id, repo_full_name=repo_full_name)

        for entry in llm_output_tags:
            name = (entry.get("name") or "").strip()
            try:
                conf = float(entry.get("confidence", CONFIDENCE_LLM_DEFAULT))
            except (TypeError, ValueError):
                conf = CONFIDENCE_LLM_DEFAULT
            conf = max(0.0, min(1.0, conf))
            if not name:
                continue
            if name not in cache:
                result.skipped_unknown.append(name)
                continue
            await self._repo.add_repo_tag(
                EcosystemRepoTag(
                    project_id=self._project_id or None,
                    repo_id=repo_id,
                    tag_id=cache[name].id,
                    confidence=conf,
                    source=EcosystemTagSource.AUTO_LLM,
                    agent_id=agent_id,
                ),
                project_id=self._project_id or None,
            )
            result.layer3_tags.append(name)
        return result

    # --------------------------------------------------------
    # Manual tagging
    # --------------------------------------------------------

    async def manual_tag(
        self,
        *,
        repo_id: str,
        tag_name: str,
        confidence: float = 1.0,
        agent_id: str | None = None,
    ) -> EcosystemRepoTag | None:
        """手动打标 — source=MANUAL，confidence 默认 1.0。"""
        cache = await self._load_tag_dict()
        tag = cache.get(tag_name)
        if tag is None:
            return None
        rt = EcosystemRepoTag(
            project_id=self._project_id or None,
            repo_id=repo_id,
            tag_id=tag.id,
            confidence=max(0.0, min(1.0, confidence)),
            source=EcosystemTagSource.MANUAL,
            agent_id=agent_id,
        )
        return await self._repo.add_repo_tag(
            rt, project_id=self._project_id or None
        )

    async def remove_tag(self, repo_id: str, tag_name: str) -> bool:
        cache = await self._load_tag_dict()
        tag = cache.get(tag_name)
        if tag is None:
            return False
        return await self._repo.remove_repo_tag(
            repo_id, tag.id, project_id=self._project_id or None
        )


# ============================================================
# 暴露常量供测试访问
# ============================================================

__all__ = [
    "BatchApplyStats",
    "CONFIDENCE_LLM_DEFAULT",
    "CONFIDENCE_RULE",
    "CONFIDENCE_TOPIC",
    "EcosystemTagger",
    "GITHUB_TOPIC_ALIASES",
    "KEYWORD_RULES",
    "LLM_FALLBACK_TAG_THRESHOLD",
    "MAX_LLM_CONCURRENCY",
    "TagApplyResult",
]
