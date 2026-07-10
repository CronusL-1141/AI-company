"""跨域引用抽取器 — 知识层 P1a（docs/knowledge-layer-design.md）。

从 memo/report 文本抽取 OS 原生 ID 引用，纯正则零 LLM（GBrain「约定即边」
路线，实体换成 OS 的 ID——语言无关，中文内容零影响）。挂在 API 路由写入
路径（task_memo_add / report_save 双入口的汇聚点），失败静默绝不阻塞主写入。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 命中点上下文窗口（证据快照 ±120 字；link_type 推断 ±60 字）
_CONTEXT_CHARS = 120
_TYPE_WINDOW = 60

# wf_id：wf_ + 8 hex，可带 -xxx 后缀（观测层 run 编号格式）
_WF_RE = re.compile(r"\bwf_[0-9a-f]{8}(?:-[0-9a-f]{2,4})?\b")
# 标准 UUID（任务/报告 id）
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
# CC 记忆链接 [[slug]]
_MEMORY_RE = re.compile(r"\[\[([a-z0-9][a-z0-9-]{2,60})\]\]")
# commit 短 hash：7-10 hex——高误报风险，必须有 commit 语境词才算
_COMMIT_RE = re.compile(r"\b[0-9a-f]{7,10}\b")
_COMMIT_CUES = re.compile(r"commit|提交|修复|修|fix|feat|refactor|release|版本", re.I)
# fixes 语义线索（±60 字窗口内出现则边类型升级为 fixes）
_FIX_CUES = re.compile(r"修复|修好|根治|根因|fix(?:ed|es)?\b|已修|修掉", re.I)


@dataclass
class ExtractedRef:
    to_kind: str  # run / task / commit / memory
    to_id: str
    link_type: str  # references / fixes
    context: str  # 命中点证据快照


def _window(text: str, start: int, end: int, radius: int) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def extract_refs(text: str) -> list[ExtractedRef]:
    """从文本抽取全部跨域引用（同一 (kind,id) 去重，保首个命中的上下文）。"""
    if not text:
        return []
    refs: dict[tuple[str, str], ExtractedRef] = {}

    def _add(kind: str, ref_id: str, m_start: int, m_end: int) -> None:
        key = (kind, ref_id)
        if key in refs:
            return
        type_win = _window(text, m_start, m_end, _TYPE_WINDOW)
        link_type = "fixes" if _FIX_CUES.search(type_win) else "references"
        refs[key] = ExtractedRef(
            to_kind=kind,
            to_id=ref_id,
            link_type=link_type,
            context=_window(text, m_start, m_end, _CONTEXT_CHARS).strip(),
        )

    for m in _WF_RE.finditer(text):
        _add("run", m.group(0), m.start(), m.end())
    for m in _UUID_RE.finditer(text):
        _add("task", m.group(0), m.start(), m.end())
    for m in _MEMORY_RE.finditer(text):
        _add("memory", m.group(1), m.start(), m.end())
    for m in _COMMIT_RE.finditer(text):
        # 已被 wf_/uuid 命中的片段跳过；无 commit 语境词跳过（防裸 hex 误抽）
        frag = m.group(0)
        if any(frag in k[1] for k in refs):
            continue
        cue_win = _window(text, m.start(), m.end(), _TYPE_WINDOW)
        if not _COMMIT_CUES.search(cue_win):
            continue
        _add("commit", frag, m.start(), m.end())

    return list(refs.values())
