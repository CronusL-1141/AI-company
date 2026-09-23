"""子 agent token 归因 — 从 transcript 采计费口径的用量。

agents 表此前只有"上下文水位"口径（ctx_tokens/ctx_pct），**没有任何计费口径的
采集**；workflow 侧 workflow_agents.tokens 也大面积为空（实测 model='opus' 的 170
行全部 token=0/NULL，且持续发生到最近，不是早期遗留）。

根因不是采集坏了，而是**数据源只有请求规格**：同一份 workflow JSON 里 82 个 agent
只写了"要什么模型"（model 原样是别名 ``opus``、tokens 为 null），只有 24 个带回
遥测，OS 原样落库。

但这些 agent 的 **transcript 完整存在**，而 transcript 的 ``message.model``
**永远是完整型号**（实测 ``claude-opus-5`` / ``claude-opus-4-8``），从不是别名。
所以真实型号与 token 都可以从 transcript 无损回采 —— 这正是"模型字段由观测回填"
该有的样子。别名映射表只用来给 transcript 已灭失的行兜底，**只在读侧解析，绝不
回写 model 字段**（2026-07-07 铁律：未知就空着，不写死型号）。

累加算法是本模块唯一容易做错的地方，单独说明见 :func:`parse_transcript_usage`。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

# 合成行标记的**唯一**定义在 session_probe —— 它是最早正确处理这件事的地方
# （read_session_model 一直显式跳过）。这里刻意 import 而不是再抄一份常量：
# 一个字面量抄成两份，就会在其中一份忘记跳过时长出两种"模型识别"行为，而这
# 恰好就是本函数被 §1.3 点名的那个缺陷。
from aiteam.api.session_probe import SYNTHETIC_MODEL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 别名映射：append-only 台账，只读侧兜底
# ---------------------------------------------------------------------------
# 别名（opus / sonnet / haiku）是**浮动**的：同一个 "opus" 在不同时期指向不同型号。
# 因此映射必须带生效区间与证据来源，且永远只追加、不修改历史条目 —— 否则事后
# 无法复原"当时那条记录到底跑的是什么"。``effective_until=None`` 表示仍然生效。
#
# 用法边界：仅在 transcript 已灭失、无法定真时用于**展示/统计**的兜底解析。
# 任何情况下都不把解析结果写回 agents.model / workflow_agents.model。
MODEL_ALIAS_LEDGER: list[dict[str, Any]] = [
    {
        "alias": "opus",
        "resolved": "claude-opus-4-8",
        "effective_from": "2026-07-01",
        "effective_until": "2026-07-27",
        "evidence": "workflow transcript 实测：同期 agent-*.jsonl 的 message.model 恒为 claude-opus-4-8",
    },
    {
        "alias": "opus",
        "resolved": "claude-opus-5",
        "effective_from": "2026-07-28",
        "effective_until": None,
        "evidence": "本机 subagent transcript 实测：message.model = claude-opus-5",
    },
]


def resolve_model_alias(alias: str, at: str) -> dict[str, Any] | None:
    """Resolve a floating alias to the model it named on date ``at`` (YYYY-MM-DD).

    Returns the ledger entry (so callers keep the evidence trail), or None when
    the alias is unknown or the date predates every recorded window — guessing
    outside a recorded window is exactly how a wrong model gets baked in.
    """
    for entry in MODEL_ALIAS_LEDGER:
        if entry["alias"] != alias:
            continue
        if at < entry["effective_from"]:
            continue
        until = entry["effective_until"]
        if until is not None and at > until:
            continue
        return entry
    return None


# ---------------------------------------------------------------------------
# transcript 解析
# ---------------------------------------------------------------------------

_USAGE_MAP = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
}


# 行预筛（json.loads 之前的廉价关卡）。一行要计入用量，必须同时满足
# ``type == "assistant"`` 与 ``message.usage`` 是对象，于是它的原文里**必然**出现
# ``"assistant"`` 与 ``"usage"`` 这两个 JSON 字符串字面量（与键值间有无空格无关，
# 所以对紧凑与带空格两种写法都成立）。两者缺一就不可能计数，可以不解析直接跳过：
# 实测一份 490MB 主会话 17.4 万行里只剩 4 万行进 json.loads，解析耗时减半。
# 唯一的前提是写入方不把这两个 ASCII 单词写成 ``\u`` 转义 —— JSON.stringify 与
# json.dumps 都不会这么做；嵌在工具输出里的同名单词是转义过的 ``\"usage\"``，
# 也不会误中（误中也只是多解析一行，结果不变）。
_PREFILTER_TOKENS = (b'"usage"', b'"assistant"')

# 游标锚点长度：增量推进前核对游标之前这么多字节是否还是上次读到的内容。
_ANCHOR_BYTES = 64


def _usage_row(raw: bytes) -> tuple[str, tuple[int, int, int, int], str] | None:
    """One JSONL line -> ``(requestId, usage snapshot, real model)``, or None.

    整份解析与增量游标共用的**唯一**行级判定，两条路径不可能各长各的。返回的
    requestId 可能为空串（由调用方按字节偏移兜底成独立分组）；model 已剔除合成行
    占位符，空串表示本行不更新型号。
    """
    if not all(token in raw for token in _PREFILTER_TOKENS):
        return None
    try:
        line = raw.decode("utf-8").strip()
        if not line:
            return None
        row = json.loads(line)
    except ValueError:  # JSONDecodeError 与 UnicodeDecodeError 都是它的子类
        return None  # 单行损坏不该毁掉整份归因
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return None
    message = row.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    # compact 合成行的 model 是 "<synthetic>"，不是任何真实型号。子 agent
    # transcript 一般不含合成行，所以此前没被咬到；主会话 transcript 一上来就有
    # —— 实测一份 35.1 MB 的主会话解析回来的 model 正是 "<synthetic>"（§1.3）。
    # 跳过逻辑与 session_probe.read_session_model 同源同常量。
    raw_model = message.get("model")
    model = str(raw_model) if raw_model and str(raw_model) != SYNTHETIC_MODEL else ""
    snapshot = tuple(int(usage.get(src) or 0) for src in _USAGE_MAP)
    return str(row.get("requestId") or ""), snapshot, model  # type: ignore[return-value]


class TranscriptUsageCursor:
    """Incremental twin of :func:`parse_transcript_usage` for append-only transcripts.

    主会话 transcript 只追加不截断（compact 也是原地追加边界行），每次都从头解析
    意味着成本随会话线性增长 —— 490MB 的会话单次约 2s。游标记住上次读到哪里，下一
    次只解析新增字节，成本只与新增量成正比。状态：

    * ``(st_dev, st_ino)``：文件身份。换了文件（另起文件、被替换）就整份重解析；
    * 偏移：**最后一个完整换行之后**的绝对字节偏移。末尾没有换行的半行（写入方写
      到一半）不消费，下次从它的行首重读；
    * 每个 requestId 的末快照 + 四层累计：同一 requestId 的后续行（流式递增的
      output）跨两次推进也能正确替换，而不是相加；
    * 偏移前 64 字节的锚点：推进前核对，内容变了就整份重解析。

    判据的局限：文件被**原地改写**、长度没有变短、且锚点那 64 字节恰好没变时检测不
    到，会在旧状态上继续累加。CC 从不原地改写 transcript，这里不为它付出每次重读全
    文的代价。游标只活在进程内存里：API 重启后第一次推进就是一次整份解析，这本身
    就是兜底 —— 落库是按赋值覆写的累计快照，从零重算得到的是同一个数。

    非线程安全：同一个游标同时只能有一个调用方（调用方负责串行化）。
    """

    __slots__ = ("_ident", "_offset", "_anchor", "_snapshots", "_totals", "_model")

    def __init__(self) -> None:
        self._reset(None)

    def _reset(self, ident: tuple[int, int] | None) -> None:
        self._ident = ident
        self._offset = 0
        self._anchor = b""
        # requestId（或 _off<字节偏移>）-> 该次调用的最后一条 usage 快照
        self._snapshots: dict[str, tuple[int, int, int, int]] = {}
        self._totals = [0, 0, 0, 0]
        self._model = ""

    @property
    def offset(self) -> int:
        """Absolute byte offset just past the last fully consumed line."""
        return self._offset

    @property
    def tracked_requests(self) -> int:
        return len(self._snapshots)

    def advance(self, path: str | Path, *, final: bool = False) -> dict[str, Any] | None:
        """Consume whatever was appended since the last call; return the running totals.

        ``final=True`` 表示调用方不会再来（一次性整份解析）：末尾没有换行的最后一行
        也照常解析，与逐行文本读取的旧语义一致。增量调用保持 ``False``。

        Raises OSError when the file cannot be opened or read —— 调用方据此区分
        "读不到" 与 "还没有用量行"（两者都不能写成 0）。
        """
        with Path(path).open("rb") as fh:
            st = os.fstat(fh.fileno())
            ident = (st.st_dev, st.st_ino)
            if ident != self._ident or st.st_size < self._offset or not self._anchor_holds(fh):
                self._reset(ident)
            fh.seek(self._offset)
            offset = self._offset
            for raw in fh:
                if not raw.endswith(b"\n") and not final:
                    break  # 写到一半的行：不消费，下次从它的行首重读
                self._ingest(raw, offset)
                offset += len(raw)
                self._offset = offset
            start = max(0, self._offset - _ANCHOR_BYTES)
            fh.seek(start)
            self._anchor = fh.read(self._offset - start)
        return self._result()

    def _anchor_holds(self, fh: Any) -> bool:
        if not self._anchor:
            return True
        fh.seek(self._offset - len(self._anchor))
        return fh.read(len(self._anchor)) == self._anchor

    def _ingest(self, raw: bytes, offset: int) -> None:
        parsed = _usage_row(raw)
        if parsed is None:
            return
        request_id, snapshot, model = parsed
        # 无 requestId 时退回**绝对字节偏移**，保证每行自成一组。不用行号：增量
        # 推进分批读，行号要么得额外持久化，要么在两批之间撞键、把两次调用误合并。
        key = request_id or f"_off{offset}"
        previous = self._snapshots.get(key)
        totals = self._totals
        if previous is not None:
            for i, value in enumerate(previous):
                totals[i] -= value
        for i, value in enumerate(snapshot):
            totals[i] += value
        self._snapshots[key] = snapshot
        if model:
            self._model = model

    def _result(self) -> dict[str, Any] | None:
        if not self._snapshots:
            return None
        totals: dict[str, Any] = dict(zip(_USAGE_MAP.values(), self._totals, strict=True))
        totals["api_calls"] = len(self._snapshots)
        totals["total_tokens"] = (
            totals["input_tokens"]
            + totals["output_tokens"]
            + totals["cache_creation_tokens"]
            + totals["cache_read_tokens"]
        )
        totals["model"] = self._model
        # 记下型号是怎么来的：transcript 定真 vs 别名兜底，事后可审计。
        totals["model_source"] = "transcript" if self._model else "unknown"
        return totals


def parse_transcript_usage(path: str | Path) -> dict[str, Any] | None:
    """Sum a transcript's billed token usage in one pass.

    **算法要点（做错就会严重虚高）**：一次 API 调用会产出**多条** assistant 行
    （每个 content block 一条），实测 79 行只对应 17 个唯一 ``requestId``。同一
    ``requestId`` 内 input/cache 恒定不变，而 ``output_tokens`` 随流式**递增**
    （3 → 3 → … → 583）。所以必须**按 requestId 分组、每组取最后一条快照、再跨组
    累加**。逐行裸加实测会把 input 从 12,185 抬到 72,556、cache_read 从 1,289,742
    抬到 5,105,773。

    实现就是一次性的 :class:`TranscriptUsageCursor`：整份解析与增量推进共用同一个
    行级判定（含行预筛），两者对同一份内容逐字段相等。

    Returns None when the file is absent — "没有数据" 和 "用了 0 token" 是两回事，
    绝不能把前者写成后者（数据纪律：no-data ≠ zero）。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        return TranscriptUsageCursor().advance(p, final=True)
    except OSError:
        logger.debug("token attribution: transcript unreadable %s", p, exc_info=True)
        return None
