"""覆盖率的分母定义与未归因分类 —— token 用量归因 v1 §4.1 / §3.4。

**为什么单独一个模块**：这两样东西必须能被 storage 层直接引用，而同域的
``services/token_attribution.py`` 为了共用合成行常量 import 了 ``api.session_probe``
（阶段 0 刻意为之，见那边注释），顺着 api 层一路 import 回 ``storage.repository``
形成环。分母的定义不该为了住在"看起来更对"的文件里而变成延迟导入 —— 它是这套
东西里最需要一眼看见的一段。

本模块只依赖 ``pathlib`` 与 ``aiteam.types``，任何时候都能被安全导入。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from aiteam.types import UnattributedReason

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


def classify_unattributed(
    transcript_path: str | None,
    *,
    file_probe: Callable[[str], bool] = transcript_exists,
) -> str:
    """一行没测到用量的派工，属于 §3.4 四类未归因里的哪一类。

    只回答"为什么没测到"，不回答"要不要测" —— 调用方已经确认这一行的
    ``tokens_measured_at`` 是 NULL 才会问到这里。

    三个返回值的处置完全不同，这正是这张表存在的理由（§3.4：让"覆盖率 78%"变成
    一句可行动的话）。``NOT_YET_MEASURED`` 跑一次回采就能补上；另外两个补不上，
    而且 ``TRANSCRIPT_GONE`` 随时间只增不减 —— 回采的窗口正在关闭（R6）。把它们
    合并成一个"未归因"数字，看的人就无从判断该不该现在动手。
    """
    if not transcript_path:
        return UnattributedReason.NO_TRANSCRIPT_PATH.value
    if not file_probe(transcript_path):
        return UnattributedReason.TRANSCRIPT_GONE.value
    return UnattributedReason.NOT_YET_MEASURED.value
