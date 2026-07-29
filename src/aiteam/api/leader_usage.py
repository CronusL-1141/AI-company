"""Leader 主会话 token 用量采集 —— 归因 v1 阶段 4。

子 agent 侧的用量已由 SubagentStop 落 agents 五列，主会话侧则是**整片空白**
（实测 117 个 leader 行的 token 五列与 transcript_path 全为空）。缺口本身好补：
主会话 transcript 就在 ``~/.claude/projects/<slug>/<session_id>.jsonl``，
:func:`aiteam.services.token_attribution.parse_transcript_usage` 不加改动就能解析。

真正会把数字变成谎言的是**怎么落库**。三条语义各自对应一个坑，实现时不许简化：

1. **快照覆写，绝不累加。** 主会话 transcript 是**累计文件** —— 每次解析得到的
   都是"从会话开始到此刻"的总量，不是这一轮的增量。所以落库只能是覆写；照抄子
   agent 那种"stop 时写一次"的累加心智，一个会话有多少轮就会虚高多少倍。
   本模块所有写入都经 :meth:`UsageSnapshot.as_agent_updates`，那里全是赋值，
   没有任何一处 ``+=`` —— 这不是巧合，是唯一允许的形态。
2. **节流。** ``Stop`` 每轮对话都触发，而全量解析随会话线性变贵（实测 45.1 MB /
   0.18 s）。于是 ``Stop`` 走 :meth:`SessionUsageMeter.should_measure` 的门；
   ``SessionEnd`` / ``PostCompact`` / ``SessionStart`` 带 ``force=True`` 强制定格。
3. **合成行过滤。** compact 会在 transcript 里留下 ``model="<synthetic>"`` 的
   assistant 行。实测这些行的 usage 四字段**全是 0**（不会虚高 token），但
   ``parse_transcript_usage`` 取 model 时不跳过它们，于是主会话解析出来的 model
   就是 ``<synthetic>``（本机 54d51683 那份实测复现，真实模型是 claude-fable-5）。
   本模块在**调用侧**用 ``session_probe.SYNTHETIC_MODEL`` 兜一层，见 :func:`_resolve_model`。

compact 行为实测（阶段 4 顺手考古，回答设计 §8 未决问题②）：compact **原地追加**
一条 ``system/compact_boundary`` + 一条 ``user/isCompactSummary``，**不截断、不替换、
不归档** —— 45.1 MB 的单份 transcript 里躺着 7 次 compact，19 天前的行原样还在。
所以 ``PostCompact`` 的强制定格不是"抢救即将消失的数据"，而是①把 compact 这一刻
定格；②覆盖"compact/resume 后会话另起文件"的交接点（另起的文件会重放一条同样的
compact_boundary，但重放的 assistant 行 usage 全 0，两份文件各自解析不会重复计数）。

**呈现约束（仅在此标注，落地属阶段 5）**：Leader 主会话的量级远超全部子 agent 之和
（单会话实测 8.5 亿 vs 子 agent 合计），二者混进同一个排行榜会让子 agent 的归因结果
被彻底淹没。因此 Leader 行（``role='leader'``）与子 agent 行**必须分列呈现、默认不
合并**，且两者都是 ``usage_sum`` 口径 —— 与 ``workflow_agents.tokens`` 的 ``ctx_last``
口径不可相加（设计 §0.2 / §3.3）。本模块刻意**不产出任何合计字段**，四层永远分列。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiteam.api import session_probe
from aiteam.clock import from_timestamp, utc_now
from aiteam.config import settings
from aiteam.services import token_attribution

# 进程内节流状态的上限。一个 API 进程可以横跨很多会话，字典本身很小（每会话两个
# 时间戳），但无上限的字典在长命进程里就是慢性泄漏。超限时整体丢弃：节流状态丢了
# 最坏结果是下一次事件多解析一次，语义无损（覆写是幂等的）。
_MAX_TRACKED_SESSIONS = 512


def _projects_dir() -> Path:
    """CC 主会话 transcript 的根目录 —— 约定只在 session_probe 留一份。"""
    return session_probe._claude_projects_dir()  # noqa: SLF001


def locate_main_transcript(
    payload_path: str,
    *,
    cwd: str = "",
    session_id: str = "",
) -> Path | None:
    """Resolve the main-session transcript for a hook payload, or None.

    payload 里的路径优先，但**必须先确认它真的在磁盘上**：hook 侧对超长路径会追加
    ``...(truncated)``（send_event._trim_payload），超大载荷还会整体剥离到必留字段，
    两种情况拿到的都是不可用的路径。兜底走 ``<slug>/<session_id>.jsonl`` 这条既有
    约定（``session_probe.session_last_active`` 用的是同一条），这样 ``PostCompact``
    这种载荷最容易被裁剪的事件也能定位到文件。
    """
    candidate = (payload_path or "").strip()
    if candidate:
        path = Path(candidate)
        if path.is_file():
            return path
    if cwd and session_id:
        path = _projects_dir() / session_probe.project_slug(cwd) / f"{session_id}.jsonl"
        if path.is_file():
            return path
    return None


def _resolve_model(raw: str, transcript: Path) -> str:
    """Drop the compact placeholder model, falling back to the last real one.

    TODO(阶段 0): ``parse_transcript_usage`` 一旦复用 session_probe 的合成行过滤
    （设计 §1.3 / §7 阶段 0），这里的调用侧兜底就退化成一层冗余保险，可连同本注释
    一并收敛。在那之前主会话采集不能裸信 usage['model'] —— 主会话 100% 有合成行。
    """
    if raw and raw != session_probe.SYNTHETIC_MODEL:
        return raw
    return session_probe.read_session_model(str(transcript))


@dataclass(frozen=True)
class UsageSnapshot:
    """一次主会话用量测量的结果 —— 是**当前累计值的快照**，不是增量。

    四层分列，刻意**没有合计字段**：任何"总量"实际上是"缓存读取量"的同义词
    （实测 cache_read 占 95.6%），单独呈现会让跨模型、跨路径的比较系统性失真
    （设计 §1.2）。要合计请到阶段 2 的 TokenAttribution 里连同口径与分母一起拿。
    """

    session_id: str
    transcript_path: str
    measured_at: datetime
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    api_calls: int
    model: str
    forced: bool

    def as_agent_updates(self) -> dict[str, Any]:
        """Fields to write onto the leader row — every one is an assignment.

        覆写语义的唯一落点。这里出现任何形如 ``old + new`` 的写法都是把累计文件当
        增量文件读，会让数值随轮次线性虚高（设计 §3.3 陷阱①、R4）。
        """
        updates: dict[str, Any] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "tokens_measured_at": self.measured_at,
            # Leader 行此前 0/117 有 transcript_path；采到用量的同时把来源路径一并
            # 落库，覆盖率与回采才有据可查（设计 §2.6 回填项）。
            "transcript_path": self.transcript_path,
        }
        if self.model:
            updates["model"] = self.model
        return updates

    def as_summary(self) -> dict[str, Any]:
        """Compact form for hook responses / logs（同样不含合计）。"""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "api_calls": self.api_calls,
            "model": self.model,
            "forced": self.forced,
            "metric": "usage_sum",
        }


@dataclass
class _LastMeasure:
    """上一次**解析尝试**的时点与当时的文件 mtime。"""

    measured_at: datetime
    mtime: datetime | None


class SessionUsageMeter:
    """Throttled, snapshot-overwrite usage probe for main-session transcripts.

    进程内保持每会话的上次测量指纹，据此决定这一轮 ``Stop`` 要不要真去解析文件。
    状态丢失（进程重启、超限清空）不影响正确性：覆写是幂等的，最坏是多解析一次。
    """

    def __init__(
        self,
        *,
        min_interval_seconds: int | None = None,
        mtime_advance_seconds: int | None = None,
    ) -> None:
        self._min_interval = timedelta(
            seconds=(
                settings.LEADER_USAGE_MIN_INTERVAL_SECONDS
                if min_interval_seconds is None
                else min_interval_seconds
            )
        )
        self._mtime_advance = timedelta(
            seconds=(
                settings.LEADER_USAGE_MTIME_ADVANCE_SECONDS
                if mtime_advance_seconds is None
                else mtime_advance_seconds
            )
        )
        self._last: dict[str, _LastMeasure] = {}

    def should_measure(
        self,
        session_id: str,
        *,
        mtime: datetime | None,
        now: datetime,
        force: bool = False,
    ) -> bool:
        """Whether this event is allowed to pay for a full transcript parse.

        判据顺序本身就是语义，不要重排：

        1. ``force``（SessionStart / SessionEnd / PostCompact）永远直通 —— 强制
           定格不受任何窗口约束，否则"定格"就名不副实。
        2. 本会话还没测过 -> 测（首测建立基线，Leader 行才会从空变有值）。
        3. **文件一个字节都没动 -> 不测。** 同一份文件重复解析必然得到同一份快照，
           解析器是纯函数，这一趟纯属白花。
        4. 距上次解析已超过 ``min_interval`` -> 测。
        5. transcript 落盘时间比上次测量时推进了超过 ``mtime_advance`` -> 测。
           这一条兜的是"一轮跑了很久"的长任务：窗口还没到，但这段时间里写进去的
           量级可能已经很可观。
        """
        if force:
            return True
        last = self._last.get(session_id)
        if last is None:
            return True
        if mtime is not None and last.mtime is not None and mtime <= last.mtime:
            return False
        if now - last.measured_at >= self._min_interval:
            return True
        return bool(
            mtime is not None
            and last.mtime is not None
            and (mtime - last.mtime) >= self._mtime_advance
        )

    def capture(
        self,
        session_id: str,
        transcript: Path,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> UsageSnapshot | None:
        """Parse the transcript and return a snapshot, or None when skipped.

        None 有三种含义，调用方都只需"什么都不写"：被节流挡下 / 文件读不到 /
        文件里还没有任何 usage 行。**绝不把这三种写成 0** —— no-data ≠ zero。
        """
        now = now or utc_now()
        try:
            mtime = from_timestamp(transcript.stat().st_mtime)
        except OSError:
            return None
        if not self.should_measure(session_id, mtime=mtime, now=now, force=force):
            return None

        usage = token_attribution.parse_transcript_usage(transcript)
        # 记在解析**之后**、返回之前：被节流的是"解析"这个动作，所以哪怕这次解析
        # 一无所获（新会话还没有 assistant 行），也要记时点，否则每一轮 Stop 都会
        # 重新扫一遍同一个文件。
        self._remember(session_id, measured_at=now, mtime=mtime)
        if not usage:
            return None
        return UsageSnapshot(
            session_id=session_id,
            transcript_path=str(transcript),
            measured_at=now,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
            api_calls=int(usage.get("api_calls") or 0),
            model=_resolve_model(str(usage.get("model") or ""), transcript),
            forced=force,
        )

    def _remember(self, session_id: str, *, measured_at: datetime, mtime: datetime | None) -> None:
        if len(self._last) >= _MAX_TRACKED_SESSIONS:
            self._last.clear()
        self._last[session_id] = _LastMeasure(measured_at=measured_at, mtime=mtime)
