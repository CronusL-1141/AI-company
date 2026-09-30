"""覆盖率的分母定义与未归因分类 —— token 用量归因 v1 §4.1 / §3.4。

**为什么单独一个模块**：这两样东西必须能被 storage 层直接引用，而同域的
``services/token_attribution.py`` 为了共用合成行常量 import 了 ``api.session_probe``
（阶段 0 刻意为之，见那边注释），顺着 api 层一路 import 回 ``storage.repository``
形成环。分母的定义不该为了住在"看起来更对"的文件里而变成延迟导入 —— 它是这套
东西里最需要一眼看见的一段。

本模块只依赖标准库、``aiteam.types`` 与 ``services.codex_activity``（后者只再依赖
``aiteam.clock``），任何时候都能被安全导入。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from aiteam.services.codex_activity import codex_home
from aiteam.types import AgentStatus, HarnessId, UnattributedReason

# ---------------------------------------------------------------------------
# 分母（§4.1）
# ---------------------------------------------------------------------------
# 分母是唯一能被悄悄做假的地方（R2）：把没数据的行移出分母，覆盖率就恒等于 100%，
# 数字会变好看而且完全说得通。所以它只在这里定义一次，由 repository 的聚合方法
# 引用，并有单测逐条钉住取值。
#
# 判据：``agents.role`` 不等于这个字面量即算一次派工。全库实测 2,451 行，其中
# 2,343 行（96%）是 workflow 派生的 ``workflow-subagent``。
#
# **刻意不采纳** ``api/agent_reuse.py`` 的 ``_EXCLUDED_ROLES``（那里把
# ``workflow-subagent`` 一并排除）：那是"谁能被复用"的治理口径，排除的是不由人
# 管理的行。而在归因口径里，workflow 派工**就是派工** —— 它烧掉的 token 一点不比
# 直派的少。把占 96% 的群体移出分母，覆盖率立刻从 78% 跳到很好看的数字，而那正是
# R2 点名要防的形态。
LEADER_ROLE = "leader"

# workflow 扇出的子 agent 行的 role（= hook_translator.WORKFLOW_AGENT_TYPE）。agent->workflow
# 这一跳的分母只数 workflow 派工，要靠它认出哪些行是。
WORKFLOW_SUBAGENT_ROLE = "workflow-subagent"


# ---------------------------------------------------------------------------
# 未归因分类（§3.4）
# ---------------------------------------------------------------------------


def transcript_exists(path: str) -> bool:
    """transcript 文件今天还在不在磁盘上。

    单独成函数是为了让测试能替换它：未归因分类要区分"救得回"与"救不回"，而这个
    区分完全取决于文件系统的即时状态 —— 不测这条分支等于不测这张表。
    """
    if not path:
        return False
    try:
        return Path(path).is_file()
    except OSError:
        return False


# 行在这段时间内还有活动就算在飞：与 reaper 把不活跃 agent 判离线的 30 分钟同尺度。
IN_FLIGHT_WINDOW = timedelta(minutes=30)


def is_in_flight(status: str | None, last_active_at: datetime | None, now: datetime | None) -> bool:
    """这一行的派工还在跑吗：busy，或 :data:`IN_FLIGHT_WINDOW` 内还有活动记录。

    未归因分类与维护脚本共用这一个判据：在飞的行任何结论都还下不得。
    """
    if status == AgentStatus.BUSY:
        return True
    return last_active_at is not None and now is not None and now - last_active_at < IN_FLIGHT_WINDOW


def in_codex_archive(path: str) -> bool:
    """rollout 不在登记的位置，但同名文件在 Codex 的归档目录里。

    Codex 归档会话时把 rollout 从 ``sessions/年/月/日/`` 挪进平铺的
    ``archived_sessions/``，文件名不变，所以按文件名查一次即可。
    """
    if not path:
        return False
    try:
        return (codex_home() / "archived_sessions" / Path(path).name).is_file()
    except OSError:
        return False


def classify_unattributed(
    transcript_path: str | None,
    *,
    harness: str | None = None,
    status: str | None = None,
    last_active_at: datetime | None = None,
    now: datetime | None = None,
    unmeasurable: str | None = None,
    file_probe: Callable[[str], bool] = transcript_exists,
    archive_probe: Callable[[str], bool] = in_codex_archive,
) -> str:
    """一行没测到用量的派工，属于哪一类未归因。

    只回答"为什么没测到"，不回答"要不要测" —— 调用方已经确认这一行的
    ``tokens_measured_at`` 是 NULL 才会问到这里。

    各返回值的处置完全不同，这正是这张表存在的理由（让"覆盖率 78%"变成一句可行动
    的话）：``NOT_YET_MEASURED`` 与 ``TRANSCRIPT_ARCHIVED`` 跑一次回采就能补上（CC 行用
    backfill_token_usage.py，Codex 行用 backfill_codex_usage.py）；``IN_FLIGHT`` 还没到
    该测的时候；其余补不上，而且 ``TRANSCRIPT_GONE`` 随时间只增不减。

    判定顺序：

    1. 本库没有解析器的宿主：``HARNESS_UNSUPPORTED``（claude-code 与 codex 都有解析器）；
    2. 行是 busy、或 :data:`IN_FLIGHT_WINDOW` 内还有活动，即在飞。活的证据压过行上已
       写下的结论：结论是某一刻看文件得出的，行又动起来了就说明它还在长。
       SubagentStart 不带 transcript 路径，在飞的行此时没有路径是常态，不能判成
       "从未登记"；
    3. 行上已有"没有可计用量"的结论（``usage_unmeasurable``，如 api_error_only、
       no_usage_lines；Codex 行由解析器确认 rollout 里没有用量事件时写下）；
    4. Codex 行：解析器按线程 id 在 sessions 与 archived_sessions 里找原件，所以没有
       登记路径不等于找不到，与文件在一样算 ``NOT_YET_MEASURED``；登记的路径上没有、
       归档目录里有同名文件是 ``TRANSCRIPT_ARCHIVED``（回采会改指归档原件）；两处都
       没有才是灭失；
    5. 其余：没路径、文件不在，最后才是"可以去回采"。
    """
    if harness not in (None, HarnessId.CLAUDE_CODE, HarnessId.CODEX):
        return UnattributedReason.HARNESS_UNSUPPORTED.value
    if is_in_flight(status, last_active_at, now):
        return UnattributedReason.IN_FLIGHT.value
    if unmeasurable:
        return str(unmeasurable)
    if harness == HarnessId.CODEX:
        if not transcript_path or file_probe(transcript_path):
            return UnattributedReason.NOT_YET_MEASURED.value
        if archive_probe(transcript_path):
            return UnattributedReason.TRANSCRIPT_ARCHIVED.value
        return UnattributedReason.TRANSCRIPT_GONE.value
    if not transcript_path:
        return UnattributedReason.NO_TRANSCRIPT_PATH.value
    if not file_probe(transcript_path):
        return UnattributedReason.TRANSCRIPT_GONE.value
    return UnattributedReason.NOT_YET_MEASURED.value
