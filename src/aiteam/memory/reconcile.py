"""AI Team OS — Memory reconcile 粗筛（记忆系统 v2 P2）.

设计 §4 的第一步「粗筛（零 LLM）」：把有效 task_memos 按 scope_path/task 聚簇，
簇内用内建 BM25 计两两相似度，超阈的配成候选组交给调用工具的 CC 会话内 agent
做 LLM 精判（KEEP/MERGE/INVALIDATE/NOOP）。

核心架构约束——OS 无独立 LLM 凭据：本模块只做确定性的候选粗筛，判定由 agent 完成
（参照 ecosystem apply_shallow_summary 的"agent 算、工具存"模式）。纯 Python，
复用 retriever 的 BM25，无第三方依赖。
"""

from __future__ import annotations

from typing import Any

from aiteam.memory.retriever import _bm25_scores, _tokenize_bm25
from aiteam.types import TaskMemo

# 两两相似度阈值：sim = 对称归一化 BM25（0..1），≥ 阈值即配对成候选边。
DEFAULT_SIM_THRESHOLD = 0.45
# promotion（蒸馏提升）候选门槛：成员数或跨任务数达标即标记为方向层提升素材。
_PROMOTION_MIN_MEMBERS = 3
_PROMOTION_MIN_TASKS = 2


def _cluster_key(memo: TaskMemo) -> str:
    """聚簇键：优先 scope_path（②路径作用域），为空回退到同任务。

    scope_path 非空的 memo 按路径作用域跨任务聚簇；无 scope_path 的只在同一
    任务内聚簇，避免把全项目无标注 memo 塞进一个巨簇。
    """
    sp = (memo.scope_path or "").strip()
    return f"scope:{sp}" if sp else f"task:{memo.task_id}"


def _pairwise_edges(
    tokenized: list[list[str]], threshold: float
) -> list[tuple[int, int, float]]:
    """簇内两两 BM25 相似度，返回超阈的对称边 (i, j, sim)。

    BM25 本身非对称（query vs doc）。以每条 memo 为 query 对全簇打分，用
    self-score 归一化得 sim(i→j)=score[j]/score[i]，再取双向 min 保守对称化。
    """
    n = len(tokenized)
    if n < 2:
        return []
    # 逐条作 query 打分：rows[i][j] = i 作 query 时 j 的原始 BM25 分
    rows: list[list[float]] = []
    for i in range(n):
        q = tokenized[i]
        rows.append(_bm25_scores(tokenized, q) if q else [0.0] * n)

    edges: list[tuple[int, int, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            self_i = rows[i][i]
            self_j = rows[j][j]
            sim_ij = rows[i][j] / self_i if self_i > 0 else 0.0
            sim_ji = rows[j][i] / self_j if self_j > 0 else 0.0
            sim = min(sim_ij, sim_ji)  # 保守：双向都相似才算相似
            if sim >= threshold:
                edges.append((i, j, sim))
    return edges


def _connected_components(n: int, edges: list[tuple[int, int, float]]) -> list[list[int]]:
    """并查集：把候选边并成连通分量（候选组），只保留 size ≥ 2 的组。"""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _sim in edges:
        parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)
    return [members for members in groups.values() if len(members) >= 2]


def _memo_brief(memo: TaskMemo) -> dict[str, Any]:
    """候选组内单条 memo 的对外视图（供 agent 精判，含全文）。"""
    return {
        "id": memo.id,
        "task_id": memo.task_id,
        "memo_type": memo.memo_type,
        "content": memo.content,
        "scope_path": memo.scope_path or "",
        "quality_score": memo.quality_score,
        "created_at": memo.created_at.isoformat() if memo.created_at else None,
    }


def build_candidate_groups(
    memos: list[TaskMemo],
    threshold: float = DEFAULT_SIM_THRESHOLD,
) -> list[dict[str, Any]]:
    """情景层候选粗筛：聚簇 → 簇内 BM25 两两配对 → 连通分量成候选组。

    Args:
        memos: 有效 task_memos（调用方已过滤 invalid_at IS NULL）。
        threshold: 对称归一化 BM25 相似度阈值。

    Returns:
        候选组列表，每组含 cluster_key/scope_path/成员全文/平均相似度/
        跨任务数/promotion_candidate 标记。空簇或无配对不产出。
    """
    # 1) 按 cluster_key 聚簇
    clusters: dict[str, list[TaskMemo]] = {}
    for m in memos:
        clusters.setdefault(_cluster_key(m), []).append(m)

    groups: list[dict[str, Any]] = []
    for key, members in clusters.items():
        if len(members) < 2:
            continue
        tokenized = [_tokenize_bm25(m.content) for m in members]
        edges = _pairwise_edges(tokenized, threshold)
        if not edges:
            continue
        # 每对相似度用于组内平均相似度展示
        sim_lookup = {(i, j): s for i, j, s in edges}
        for comp in _connected_components(len(members), edges):
            comp_set = set(comp)
            comp_sims = [
                s for (i, j), s in sim_lookup.items() if i in comp_set and j in comp_set
            ]
            comp_members = [members[idx] for idx in comp]
            distinct_tasks = {m.task_id for m in comp_members}
            promotion = (
                len(comp_members) >= _PROMOTION_MIN_MEMBERS
                or len(distinct_tasks) >= _PROMOTION_MIN_TASKS
            )
            groups.append(
                {
                    "cluster_key": key,
                    "scope_path": comp_members[0].scope_path or "",
                    "member_count": len(comp_members),
                    "distinct_tasks": len(distinct_tasks),
                    "avg_similarity": round(
                        sum(comp_sims) / len(comp_sims), 4
                    )
                    if comp_sims
                    else 0.0,
                    "promotion_candidate": promotion,
                    "members": [_memo_brief(m) for m in comp_members],
                }
            )

    # 高相似 + 跨任务的组排前，便于 agent 优先处理
    groups.sort(
        key=lambda g: (g["promotion_candidate"], g["avg_similarity"]), reverse=True
    )
    return groups


# 操作说明常量（响应附带，告知调用 agent 四操作语义 + 三守则）。
OPERATION_GUIDE: dict[str, Any] = {
    "operations": {
        "KEEP": "两条都保留（无冗余/各有信息）——无需在 apply 提交任何操作。",
        "MERGE": "合并：提交 {op:'merge', content:合并后新内容, memo_ids:[被并各条]}；"
        "工具建新 memo 并把被并各条置 invalid + invalidated_by 指向新条（Zep 失效语义）。",
        "INVALIDATE": "矛盾/被推翻：提交 {op:'invalidate', memo_ids:[...]} 置其失效（不删除）。",
        "NOOP": "本组不动——无需提交操作。",
    },
    "reconcile_principles": [
        "只保留对几乎每个未来任务都有用的条目（低价值的整理时失效）。",
        "指向权威文件/工具而非复述其内容（超长内容降级为指针条目）。",
        "优先重写精简而非追加（MERGE 出更短更准的新条，别堆叠）。",
    ],
    "distill_and_score": {
        "promote": "跨 memo 反复出现的结论/用户纠正 → 提升为方向层条目："
        "{op:'promote', content, kind:constraint/design/directive/preference, "
        "source_refs:[源 memo id]}（红线照常生效：单条 ≤400 字、每桶 ≤40 条）。",
        "score": "为 summary/decision 型 memo 补质量分："
        "{op:'score', memo_id, quality_score:1-10, reason}。",
    },
    "ultracode_hint": "候选组数量大时，开 ultracode 用 Workflow 并发精判各组，回收后统一 apply。",
}
