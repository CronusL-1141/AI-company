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


def parse_transcript_usage(path: str | Path) -> dict[str, Any] | None:
    """Sum a subagent transcript's billed token usage.

    **算法要点（做错就会严重虚高）**：一次 API 调用会产出**多条** assistant 行
    （每个 content block 一条），实测 79 行只对应 17 个唯一 ``requestId``。同一
    ``requestId`` 内 input/cache 恒定不变，而 ``output_tokens`` 随流式**递增**
    （3 → 3 → … → 583）。所以必须**按 requestId 分组、每组取最后一条快照、再跨组
    累加**。逐行裸加实测会把 input 从 12,185 抬到 72,556、cache_read 从 1,289,742
    抬到 5,105,773。

    Returns None when the file is absent — "没有数据" 和 "用了 0 token" 是两回事，
    绝不能把前者写成后者（Council 纪律：no-data ≠ zero）。
    """
    p = Path(path)
    if not p.exists():
        return None

    # requestId -> 该次调用的最后一条 usage 快照
    snapshots: dict[str, dict[str, int]] = {}
    order: list[str] = []
    model = ""

    try:
        with p.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue  # 单行损坏不该毁掉整份归因
                if row.get("type") != "assistant":
                    continue
                message = row.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                # compact 合成行的 model 是 "<synthetic>"，不是任何真实型号。
                # 子 agent transcript 一般不含合成行，所以此前没被咬到；主会话
                # transcript 一上来就有 —— 实测一份 35.1 MB 的主会话解析回来的
                # model 正是 "<synthetic>"（§1.3）。跳过逻辑与 session_probe
                # .read_session_model 同源同常量。
                raw_model = message.get("model")
                if raw_model and str(raw_model) != SYNTHETIC_MODEL:
                    model = str(raw_model)
                # 无 requestId 时退回行号，保证每行自成一组（不会被误合并）
                req = str(row.get("requestId") or f"_line{lineno}")
                if req not in snapshots:
                    order.append(req)
                snapshots[req] = {
                    out: int(usage.get(src) or 0) for src, out in _USAGE_MAP.items()
                }
    except OSError:
        logger.debug("token attribution: transcript unreadable %s", p, exc_info=True)
        return None

    if not snapshots:
        return None

    totals = {name: 0 for name in _USAGE_MAP.values()}
    for req in order:
        for name, value in snapshots[req].items():
            totals[name] += value

    totals["api_calls"] = len(snapshots)
    totals["total_tokens"] = (
        totals["input_tokens"]
        + totals["output_tokens"]
        + totals["cache_creation_tokens"]
        + totals["cache_read_tokens"]
    )
    totals["model"] = model
    # 记下型号是怎么来的：transcript 定真 vs 别名兜底，事后可审计。
    totals["model_source"] = "transcript" if model else "unknown"
    return totals
