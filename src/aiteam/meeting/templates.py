"""Meeting template definitions with round structure and keyword matching."""

from __future__ import annotations

# Trigger keywords for auto-selecting meeting template from topic text.
# Keys match TEMPLATE_ROUNDS keys. Values are keyword lists.
TEMPLATE_KEYWORDS: dict[str, list[str]] = {
    "brainstorm": ["brainstorm", "头脑风暴", "idea", "创意", "发散", "探索", "可能性"],
    "decision": ["decision", "决策", "选择", "方案对比", "trade-off", "选型", "approve"],
    "review": ["review", "评审", "code review", "PR", "验收", "审查", "quality"],
    "retrospective": ["retro", "复盘", "回顾", "总结", "经验教训", "改进"],
    "standup": ["standup", "站会", "同步", "daily", "进度", "状态更新"],
    "debate": ["debate", "辩论", "分歧", "争议", "disagreement", "正反方"],
    "lean_coffee": ["lean coffee", "开放讨论", "自由议题", "open discussion"],
    "council": ["council", "评审委员会", "多角度", "multi-perspective", "专家评审", "架构评审", "方案评估"],
}


def recommend_template(topic: str) -> tuple[str, str]:
    """Recommend a meeting template based on topic text.

    Returns (template_name, reason). Falls back to 'brainstorm' if no match.
    """
    topic_lower = topic.lower()
    scores: dict[str, int] = {}
    for tpl, keywords in TEMPLATE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in topic_lower)
        if score > 0:
            scores[tpl] = score

    if not scores:
        return "brainstorm", "no keyword match, defaulting to brainstorm"

    best = max(scores, key=scores.get)  # type: ignore[arg-type]
    return best, f"matched {scores[best]} keyword(s) for '{best}'"


TEMPLATE_ROUNDS: dict[str, dict] = {
    "brainstorm": {
        "total_rounds": 4,
        "description": "头脑风暴——发散思维，产生尽可能多的创意和方案",
        "rounds": [
            {
                "number": 1,
                "name": "独立发散",
                "rule": "每人独立提出想法，不评判他人。发言格式：[想法标题] + [简要描述] + [预期价值]",
            },
            {
                "number": 2,
                "name": "交叉启发",
                "rule": "必须引用至少1个他人想法，在其基础上衍生。发言格式：[引用原想法] → [衍生想法] + [组合理由]",
            },
            {
                "number": 3,
                "name": "评估筛选",
                "rule": "每Agent投3票（可投给自己或他人的想法）。发言格式：[投票: 想法A, 想法B, 想法C] + [各投票理由]",
            },
            {
                "number": 4,
                "name": "汇总共识",
                "rule": "主持人汇总，各Agent确认或补充。产出：Top-N想法清单 + 每个想法的初步行动建议",
            },
        ],
    },
    "decision": {
        "total_rounds": 3,
        "description": "决策会议——在多个备选方案中做出明确选择",
        "rounds": [
            {
                "number": 1,
                "name": "方案陈述",
                "rule": "只陈述事实和优势，不攻击其他方案。发言格式：[方案名称] + [事实依据] + [优势清单] + [适用条件]",
            },
            {
                "number": 2,
                "name": "交叉质询",
                "rule": "对非自己代言的方案提问/质疑，方案代言人需回应。发言格式：[质疑对象] + [风险点] + [证据/理由]",
            },
            {
                "number": 3,
                "name": "决策收敛",
                "rule": "贡献者给出推荐排序，决策者宣布最终决定。发言格式（贡献者）：[推荐排序] + [关键理由]；发言格式（决策者）：[最终决定] + [决定理由] + [风险缓解]",
            },
        ],
    },
    "review": {
        "total_rounds": 3,
        "description": "评审会议——评估交付物质量，发现问题并提出改进建议",
        "rounds": [
            {
                "number": 1,
                "name": "方案陈述",
                "rule": "汇报人展示交付物，评审者只听不评（可提澄清问题）。发言格式：[概述] + [核心设计] + [已知局限] + [期望反馈]",
            },
            {
                "number": 2,
                "name": "独立评审",
                "rule": "评审者独立发言，不互相参考。问题分级：Critical/Major/Minor/Suggestion，每个问题附带改进建议。发言格式：[总体评价] + 各级问题列表",
            },
            {
                "number": 3,
                "name": "回应裁定",
                "rule": "汇报人对每个问题回应（接受/部分接受/不接受+理由）。裁定结果：APPROVED / CONDITIONALLY_APPROVED / REVISION_REQUIRED",
            },
        ],
    },
    "retrospective": {
        "total_rounds": 3,
        "description": "复盘会议——总结经验，提取教训，持续改进",
        "rounds": [
            {
                "number": 1,
                "name": "4Ls回顾",
                "rule": "每人从4个维度各提供至少1条反馈，描述事实不指责个人。发言格式：[Loved/满意] + [Learned/学到] + [Lacked/不足] + [Longed For/期望]",
            },
            {
                "number": 2,
                "name": "行动方向",
                "rule": "基于Round 1的反馈，从5个维度提出改进建议。发言格式：[Keep Doing继续保持] + [More Of多做] + [Less Of少做] + [Start Doing开始做] + [Stop Doing停止做]",
            },
            {
                "number": 3,
                "name": "承诺计划",
                "rule": "投票选出Top-3改进项，每项指定负责人和验证方式。产出：改进承诺卡（改进内容 + 负责人 + 具体行动 + 验证标准）",
            },
        ],
    },
    "standup": {
        "total_rounds": 1,
        "description": "站会——快速信息同步，识别阻塞，对齐优先级",
        "rounds": [
            {
                "number": 1,
                "name": "三问",
                "rule": "每人严格限1条消息，只说三件事不展开讨论。发言格式：[已完成] + [计划做] + [阻塞项（无则写'无'）]",
            },
        ],
    },
    "debate": {
        "total_rounds": 3,
        "description": "辩论模式——存在明显分歧时进行结构化深入讨论",
        "rounds": [
            {
                "number": 1,
                "name": "立场声明",
                "rule": "各方明确阐述立场和核心论点，提供证据支撑",
            },
            {
                "number": 2,
                "name": "交叉质询",
                "rule": "各方对对方论点提出质疑，必须引用对方原文回应，不能曲解。允许承认对方论点的合理性（立场更新机制）",
            },
            {
                "number": 3,
                "name": "收敛裁定",
                "rule": "当立场趋同或达到最大轮次时，由决策者综合各方论点做出裁定",
            },
        ],
    },
    "lean_coffee": {
        "total_rounds": 3,
        "description": "Lean Coffee——民主议程，没有预设议程的开放式讨论",
        "rounds": [
            {
                "number": 1,
                "name": "议题收集",
                "rule": "每个Agent提出想讨论的议题（每人1条消息），不评论他人议题",
            },
            {
                "number": 2,
                "name": "投票排序",
                "rule": "对所有议题投票，每人3票，得票最高的优先讨论",
            },
            {
                "number": 3,
                "name": "时间盒讨论",
                "rule": "按得票顺序逐一讨论，每个议题时间到后投票继续/跳过。从每个议题中提取行动项",
            },
        ],
    },
    "council": {
        "total_rounds": 3,
        "description": "Council review — multi-perspective expert evaluation of proposals or architectures",
        "rounds": [
            {
                "number": 1,
                "name": "Expert perspectives",
                "rule": "Each participant evaluates from their professional angle "
                "(security, performance, maintainability, UX, cost, etc.). "
                "Format: [Perspective] + [Strengths] + [Risks] + [Score 1-5]",
            },
            {
                "number": 2,
                "name": "Cross-examination",
                "rule": "Challenge the highest-risk items from Round 1. "
                "Propose mitigations or alternatives. "
                "Format: [Risk addressed] + [Mitigation proposal] + [Revised score]",
            },
            {
                "number": 3,
                "name": "Verdict",
                "rule": "Each expert gives final verdict: APPROVE / CONDITIONAL / REJECT. "
                "Decision requires majority APPROVE. "
                "Output: [Verdict] + [Conditions if any] + [Action items]",
            },
        ],
    },
}
