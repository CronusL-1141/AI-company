"""AI Team OS — Memory reconcile 粗筛（记忆系统 v2 P2）.

设计 §4 的第一步「粗筛（零 LLM）」：把有效 task_memos 按 scope_path/task 聚簇，
簇内用内建 BM25 计两两相似度，超阈的配成候选组交给调用工具的 CC 会话内 agent
做 LLM 精判（KEEP/MERGE/INVALIDATE/NOOP）。

核心架构约束——OS 无独立 LLM 凭据：本模块只做确定性的候选粗筛，判定由 agent 完成
（参照 ecosystem apply_shallow_summary 的"agent 算、工具存"模式）。纯 Python，
复用 retriever 的 BM25，无第三方依赖。簇内配对是数秒级 CPU 计算，API 请求里放到
一次性子解释器算（cluster_edges_isolated），不占 API 进程的 GIL。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from aiteam.memory.retriever import _bm25_score_matrix, _tokenize_bm25
from aiteam.types import TaskMemo

logger = logging.getLogger(__name__)

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
    # 逐条作 query 打分：rows[i][j] = i 作 query 时 j 的原始 BM25 分（全簇只建一次索引）
    rows = _bm25_score_matrix(tokenized)

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


def candidate_clusters(memos: list[TaskMemo]) -> list[tuple[str, list[TaskMemo]]]:
    """按 cluster_key 聚簇，只留成员 ≥ 2 的簇（按首次出现排序）。"""
    clusters: dict[str, list[TaskMemo]] = {}
    for m in memos:
        clusters.setdefault(_cluster_key(m), []).append(m)
    return [(key, members) for key, members in clusters.items() if len(members) >= 2]


def cluster_edges(
    cluster_contents: list[list[str]], threshold: float
) -> list[list[tuple[int, int, float]]]:
    """每簇正文 → 簇内超阈边。粗筛里唯一的重计算，只吃字符串（子进程与进程内共用）。"""
    return [
        _pairwise_edges([_tokenize_bm25(content) for content in contents], threshold)
        for contents in cluster_contents
    ]


def build_candidate_groups(
    memos: list[TaskMemo],
    threshold: float = DEFAULT_SIM_THRESHOLD,
) -> list[dict[str, Any]]:
    """情景层候选粗筛：聚簇 → 簇内 BM25 两两配对 → 连通分量成候选组。

    纯 CPU 计算，3000 条的项目要算数秒：请求处理里配对走 cluster_edges_isolated。

    Args:
        memos: 有效 task_memos（调用方已过滤 invalid_at IS NULL）。
        threshold: 对称归一化 BM25 相似度阈值。

    Returns:
        候选组列表，每组含 cluster_key/scope_path/成员全文/平均相似度/
        跨任务数/promotion_candidate 标记。空簇或无配对不产出。
    """
    clusters = candidate_clusters(memos)
    contents = [[m.content for m in members] for _key, members in clusters]
    return assemble_candidate_groups(clusters, cluster_edges(contents, threshold))


# One pairing child's wall-clock budget. Production corpus (3000 memos, largest
# cluster 461): 2.5s idle, 21-23s with 20 busy processes on 10 cores, 45s with 40.
# Past this the machine is overloaded, the MCP caller (30s client timeout) has long
# given up, and an error asking for a later retry beats more CPU.
PAIRING_CHILD_TIMEOUT_SECONDS = 120


class PairingChildError(RuntimeError):
    """配对子进程失败（起不来、超时被杀、非零退出、输出不对）；消息带 stderr 尾巴。"""


def cluster_edges_isolated(
    cluster_contents: list[list[str]], threshold: float
) -> tuple[list[list[tuple[int, int, float]]], str]:
    """同 cluster_edges，放到一次性子解释器里算。阻塞，须在 worker 线程里调用。

    配对是纯 Python CPU 计算，放在 API 进程的线程里也要抢同一把 GIL：3000 条语料
    算的那几秒里 hook POST 从 10ms 涨到 0.65s（2026-09-28 实测）。子进程有自己的
    GIL，API 这边只剩等管道。API 进程被杀时子进程变孤儿，算完写管道失败即退出。

    Returns:
        (每簇的边, pairing)。pairing 为 "child"；没有可配对的簇时不起子进程，为
        "none"；找不到解释器时退回进程内计算，为 "in-process"（记 WARNING）。

    Raises:
        PairingChildError: 其余一切子进程失败。不退回进程内：起不了进程、超时、被
        信号杀多半是机器过载或 API 正在退出，这时再往 API 进程塞秒级计算是反方向。
    """
    if not cluster_contents:
        return [], "none"
    try:
        return _cluster_edges_in_child(cluster_contents, threshold), "child"
    except FileNotFoundError as exc:
        logger.warning("reconcile pairing: no interpreter to start (%s), computing in-process", exc)
        return cluster_edges(cluster_contents, threshold), "in-process"


# The child imports this module from the parent's own source tree (argv[1]), so both
# sides always run the same code whatever the child's sys.path would find first.
_CHILD_BOOT = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from aiteam.memory.reconcile import _cluster_edges_child_main; "
    "_cluster_edges_child_main()"
)


def _stderr_tail(stderr: bytes | None) -> str:
    return (stderr or b"").decode(errors="replace")[-500:].strip()


def _cluster_edges_in_child(
    cluster_contents: list[list[str]], threshold: float
) -> list[list[tuple[int, int, float]]]:
    """在一次性子进程里跑 cluster_edges：stdin 进 JSON，stdout 出 JSON，算完即退。

    JSON 的浮点按 repr 往返，逐位不变。解释器不存在抛 FileNotFoundError，其余失败
    抛 PairingChildError；超时由 subprocess.run 杀掉并回收子进程。
    """
    if not sys.executable:
        raise FileNotFoundError("sys.executable is empty")
    package_root = str(Path(__file__).resolve().parents[2])
    request = json.dumps({"threshold": threshold, "clusters": cluster_contents})
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_BOOT, package_root],
            input=request.encode("ascii"),
            capture_output=True,
            check=False,
            timeout=PAIRING_CHILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PairingChildError(
            f"pairing child killed after {exc.timeout}s; stderr: {_stderr_tail(exc.stderr)}"
        ) from exc
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PairingChildError(f"pairing child could not start: {exc}") from exc
    if proc.returncode != 0:
        raise PairingChildError(
            f"pairing child exited {proc.returncode}; stderr: {_stderr_tail(proc.stderr)}"
        )
    try:
        result = json.loads(proc.stdout)
        if not isinstance(result, list) or len(result) != len(cluster_contents):
            raise ValueError(f"expected {len(cluster_contents)} clusters")
        return [[(i, j, sim) for i, j, sim in edges] for edges in result]
    except (ValueError, TypeError) as exc:
        raise PairingChildError(f"pairing child returned a malformed result: {exc}") from exc


def _cluster_edges_child_main() -> None:
    request = json.loads(sys.stdin.buffer.read())
    json.dump(cluster_edges(request["clusters"], request["threshold"]), sys.stdout)


def assemble_candidate_groups(
    clusters: list[tuple[str, list[TaskMemo]]],
    edges_per_cluster: list[list[tuple[int, int, float]]],
) -> list[dict[str, Any]]:
    """簇内边 → 连通分量成候选组，按 promotion 与平均相似度排序。"""
    groups: list[dict[str, Any]] = []
    for (key, members), edges in zip(clusters, edges_per_cluster, strict=True):
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
        "source_refs:[源 memo id]}（红线照常生效：单条 ≤400 字，且须放得进桶字符"
        "配额 global 1200 / project 1500 / user 300——存储上限就是注入预算）。",
        "score": "为 summary/decision 型 memo 补质量分："
        "{op:'score', memo_id, quality_score:1-10, reason}。",
    },
    "ultracode_hint": "候选组数量大时，开 ultracode 用 Workflow 并发精判各组，回收后统一 apply。",
    "guards": [
        "apply 须持有不带 peek 调 candidates 取得的整理权（reconcile_lease；CC 会话与 "
        "HTTP MCP 连接自动识别，其他调用方回传 lease_id）：整批全部成功即释放，有报错"
        "保留供重试；判完无改动提交空批释放。",
        "apply 只动当前项目的 memo：含别的项目 memo id 的那条操作整条报错不执行。",
        "direction_inventory 里 scope=global/user 的条目所有项目共享：提失效或改写前先交"
        "缔造者过目，memory_invalidate 失效与 memory_add(supersedes=…) 置换都须带 "
        "confirm_shared_scope=true。",
    ],
}
