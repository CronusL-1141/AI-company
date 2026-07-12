"""统一检索 — 知识层 P1b（docs/knowledge-layer-design.md）。

三臂 RRF（k=60，GBrain 同款常数、中性权重平等竞争）：
- BM25 臂：memo/report/task 全文（复用 retriever 的中文 bigram+英文词分词）
- 图谱臂：查询含 OS 原生 ID 时走 knowledge_links 扇出，可达文档入列
- 精确臂：ID 前缀命中（兼容 run 缩写如 wf_cbad7348）/ 标题子串直通

语料量级（memo 数百/report 个位/task 数十）支持每查询实时全量拉取，
无索引缓存；过千再谈增量索引。纯 Python 零新依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiteam.api.link_extract import extract_refs
from aiteam.memory.retriever import _bm25_scores, _tokenize_bm25
from aiteam.storage.repository import StorageRepository

RRF_K = 60  # GBrain 同款；各臂中性权重
MAX_SNIPPET = 160


@dataclass
class SearchDoc:
    kind: str  # task / task_memo / report
    id: str
    title: str
    text: str
    project_id: str = ""
    ref_ids: set[str] = field(default_factory=set)  # 文中出现的 OS ID（图谱臂用）

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


async def gather_docs(
    repo: StorageRepository, project_id: str | None = None
) -> list[SearchDoc]:
    """汇聚三源语料。project_id 给定时只取该项目。"""
    docs: list[SearchDoc] = []
    projects = await repo.list_projects()
    if project_id:
        projects = [p for p in projects if p.id == project_id]

    for proj in projects:
        tasks = await repo.list_tasks_by_project(proj.id)
        for task in tasks:
            base = f"{task.title}\n{task.description or ''}"
            docs.append(
                SearchDoc(
                    kind="task",
                    id=task.id,
                    title=task.title,
                    text=base,
                    project_id=proj.id,
                    ref_ids={task.id},
                )
            )
            # 记忆 v2：直查 task_memos 表（默认过滤失效条目），id 用真 memo id。
            for memo in await repo.list_task_memos(task.id):
                content = memo.content
                if not content:
                    continue
                docs.append(
                    SearchDoc(
                        kind="task_memo",
                        id=memo.id,
                        title=f"[{memo.memo_type}] {task.title}",
                        text=content,
                        project_id=proj.id,
                        ref_ids={r.to_id for r in extract_refs(content)} | {task.id},
                    )
                )
        try:
            metas = await repo.list_reports(project_id=proj.id)
        except Exception:  # noqa: BLE001
            metas = []
        for meta in metas:
            report = await repo.get_report(meta.id)
            if report is None:
                continue
            docs.append(
                SearchDoc(
                    kind="report",
                    id=report.id,
                    title=f"[报告] {report.topic}",
                    text=f"{report.topic}\n{report.content or ''}",
                    project_id=proj.id,
                    ref_ids={r.to_id for r in extract_refs(report.content or "")}
                    | {report.id},
                )
            )
    return docs


def _rrf_fuse(arms: list[list[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion：score += 1/(k+rank)，rank 从 1 起。"""
    scores: dict[str, float] = {}
    for ranked in arms:
        for rank, key in enumerate(ranked, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return scores


def _snippet(text: str, query_tokens: list[str]) -> str:
    """截取首个命中 token 附近的片段；无命中取开头。"""
    low = text.lower()
    pos = -1
    for t in query_tokens:
        if len(t) < 2:
            continue
        p = low.find(t.lower())
        if p != -1 and (pos == -1 or p < pos):
            pos = p
    if pos == -1:
        return text[:MAX_SNIPPET].strip()
    start = max(0, pos - 40)
    return text[start : start + MAX_SNIPPET].strip()


async def unified_search(
    repo: StorageRepository,
    query: str,
    limit: int = 10,
    project_id: str | None = None,
) -> list[dict]:
    """三臂 RRF 统一检索。返回 [{kind,id,title,snippet,score,project_id}]。"""
    query = (query or "").strip()
    if not query:
        return []
    docs = await gather_docs(repo, project_id)
    if not docs:
        return []
    by_key = {d.key: d for d in docs}

    arms: list[list[str]] = []

    # ── BM25 臂（中文 bigram + 英文词，复用 retriever 纯 Python 实现）──
    q_tokens = _tokenize_bm25(query)
    if q_tokens:
        corpus = [_tokenize_bm25(f"{d.title}\n{d.text}") for d in docs]
        scores = _bm25_scores(corpus, q_tokens)
        ranked = [
            d.key
            for d, s in sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
            if s > 0
        ]
        if ranked:
            arms.append(ranked[:50])

    # ── 图谱臂：查询含 OS ID → 直接引用 + knowledge_links 扇出可达 ──
    query_refs = {r.to_id for r in extract_refs(query)}
    if query_refs:
        graph_hits: list[str] = []
        expanded = set(query_refs)
        for ref in list(query_refs):
            try:
                for node in await repo.knowledge_link_fanout(
                    "run" if ref.startswith("wf_") else "task", ref, depth=2
                ):
                    expanded.add(node["id"])
            except Exception:  # noqa: BLE001
                pass
        for d in docs:
            if d.ref_ids & expanded:
                graph_hits.append(d.key)
        if graph_hits:
            arms.append(graph_hits[:50])

    # ── 精确臂：ID 前缀（兼容 run 缩写）/ 标题子串直通 ──
    q_low = query.lower()
    exact_hits = [
        d.key
        for d in docs
        if q_low in d.title.lower()
        or d.id.lower().startswith(q_low)
        or any(rid.lower().startswith(q_low) for rid in d.ref_ids if len(q_low) >= 4)
    ]
    if exact_hits:
        arms.append(exact_hits[:50])

    if not arms:
        return []
    fused = _rrf_fuse(arms)
    top = sorted(fused.items(), key=lambda x: x[1], reverse=True)[
        : max(1, min(limit, 50))
    ]
    return [
        {
            "kind": by_key[k].kind,
            "id": by_key[k].id,
            "title": by_key[k].title,
            "snippet": _snippet(by_key[k].text, q_tokens),
            "score": round(s, 5),
            "project_id": by_key[k].project_id,
        }
        for k, s in top
        if k in by_key
    ]
