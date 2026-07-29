"""单次派工实测 —— `/usage` 页 ④ 单次实测卡的**唯一**数据来源（设计 §5.2 ④）。

这张卡显式豁免于覆盖率闸：它是单次实测定真，不是全量台账，两条链解耦。但豁免有
代价，而代价就落在本模块的存在方式上 —— **卡片数据只能来自单次 transcript 解析，
禁止从聚合视图取数**。否则豁免就成了绕过闸门的后门：一个没有分母的数字，只要挂上
"单次实测"四个字就能上页面。

因此这里刻意不碰 ``agents`` 表的 token 五列（那是回采/采集写进去的**台账**值），
而是现场读那一份 transcript 重算。两者数值通常一致，但来源不同 —— 这张卡承诺的是
"我刚刚亲自数了一遍"，台账值给不了这个承诺。

数值算法不在本模块：一律走 :func:`aiteam.services.token_attribution.parse_transcript_usage`
这一份实现（按 requestId 分组取末条快照再跨组累加，逐行裸加会虚高 5 倍）。本模块只
额外做两件呈现层的事：摘录首尾文本、按 transcript 时间戳量耗时。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from aiteam.services import token_attribution
from aiteam.types import TOKEN_LAYERS, TokenMetric

logger = logging.getLogger(__name__)

# 摘要截断长度。卡片是"对外样例专用通道"，要的是能一眼看懂这次派工在干什么，
# 不是把整段 prompt 搬上页面。
EXCERPT_LIMIT = 600


def _text_of(message: Any) -> str:
    """把一条 CC 消息的 content 压成纯文本；非文本块（tool_use / image）跳过。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p).strip()


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= EXCERPT_LIMIT else text[:EXCERPT_LIMIT] + "…"


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def read_transcript_sample(path: str | Path) -> dict[str, Any] | None:
    """摘录一份 transcript 的首条输入、末条产出与首尾时间跨度。

    返回 None 表示文件不在 —— "没有数据"与"用了 0"是两回事，绝不能把前者写成后者。
    """
    p = Path(path)
    if not p.exists():
        return None

    first_input = ""
    last_output = ""
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    try:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue  # 单行损坏不该毁掉整份摘要
                ts = _parse_ts(row.get("timestamp"))
                if ts is not None:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                kind = row.get("type")
                if kind == "user" and not first_input:
                    # 首条 user = 派工时递进去的那段 prompt（后续 user 行多是工具结果回灌）
                    first_input = _text_of(row.get("message"))
                elif kind == "assistant":
                    text = _text_of(row.get("message"))
                    if text:
                        last_output = text
    except OSError:
        logger.debug("usage probe: transcript unreadable %s", p, exc_info=True)
        return None

    duration_ms: int | None = None
    if first_ts is not None and last_ts is not None and last_ts >= first_ts:
        duration_ms = int((last_ts - first_ts).total_seconds() * 1000)

    return {
        "prompt_excerpt": _clip(first_input),
        "result_excerpt": _clip(last_output),
        "duration_ms": duration_ms,
        "started_at": first_ts.isoformat() if first_ts else None,
        "ended_at": last_ts.isoformat() if last_ts else None,
    }


def probe_dispatch(transcript_path: str) -> dict[str, Any] | None:
    """现场解析一份 transcript，返回单次实测卡所需的全部字段。

    **刻意丢弃 parse 返回的 ``total_tokens``**：四层实测 95.6% 是 cache_read，任何
    "总量"都是"缓存读取量"的同义词，把它放上呈现面就是让人拿着一个会系统性带偏
    比较的数字去比较（§1.2）。要合计由看的人自己加，并自己承担解释责任。
    """
    usage = token_attribution.parse_transcript_usage(transcript_path)
    if usage is None:
        return None
    sample = read_transcript_sample(transcript_path) or {}

    payload: dict[str, Any] = {name: int(usage.get(name) or 0) for name in TOKEN_LAYERS}
    payload.update(
        {
            # 口径与聚合面同源：本卡与 /usage 的归因卡都是 usage_sum，可以对照着看。
            "metric": TokenMetric.USAGE_SUM.value,
            "api_calls": int(usage.get("api_calls") or 0),
            # 型号是**观测得来**的，不是配置里读的：transcript 里永远是完整型号，
            # 模板里的 `model: opus` 是层级别名，两者不可互相替代。
            "model": str(usage.get("model") or ""),
            "model_source": str(usage.get("model_source") or "unknown"),
            "transcript_path": transcript_path,
        }
    )
    payload.update(sample)
    return payload
