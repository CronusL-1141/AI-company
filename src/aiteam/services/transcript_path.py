"""transcript 路径派生器 —— 把归因链的前四段从文件路径里读出来。

CC 把每一份 transcript 放在一个**结构化**的位置上，路径本身就编码了归因链：

    ~/.claude/projects/<slug>/<session_id>.jsonl                              主会话
    ~/.claude/projects/<slug>/<session_id>/subagents/agent-<cc_id>.jsonl      直派子 agent
    ~/.claude/projects/<slug>/<session_id>/subagents/workflows/wf_<id>/agent-<cc_id>.jsonl

也就是说 ``project_slug / session_id / wf_id / cc_agent_id`` 四段**不需要新增任何
采集**，解析路径即可得到。这与 ``hook_translator._extract_workflow_run_id`` 已在用
的招式同源——那里只取了 wf_id，本模块把同一条信息用满。

两条必须写死的边界：

* **slug 只作交叉校验，绝不用于判定项目归属。** slug 是 ``re.sub(r'[^a-zA-Z0-9]',
  '-', root_path)`` 的产物，是**有损**的：``~/Desktop/文档`` 与 ``~/Desktop/资料``
  会塌成同一个 ``-Users-x-Desktop---``。生产库实测 6 个 slug 里就有两个是这种非
  ASCII 塌缩形态（525 行 + 191 行）。归属一律以 ``agents.project_id`` 为准，slug
  只用来在回填报告里标出"路径与登记项目对不上"的行供人审。
* **解析不出就返回 None。** 不猜、不兜底、不填默认值——no-data 与 zero 必须分得开
  （Council 纪律①）。

纯函数、无 IO：本模块只看字符串，不碰磁盘。文件是否还在由调用方按需自己判断，
这样解析器可以被单测钉死，也能在没有那些文件的机器上跑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# session 目录名就是 CC 的 session uuid。写成宽松的 36 字符十六进制/连字符形态而不是
# 严格 UUID v4：CC 近期的会话 id 里已经出现 ``019f8b2f-1617-71d1-...`` 这种 v7 风格
# （生产库实测），按 v4 的版本位卡会把它们判死。
_SESSION_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# 子 agent（含 workflow 扇出）的完整形态。wf 段可选。
_SUBAGENT_RE = re.compile(
    r"/projects/(?P<slug>[^/]+)"
    rf"/(?P<session_id>{_SESSION_RE})"
    r"/subagents/"
    r"(?:workflows/(?P<wf_id>wf_[^/]+)/)?"
    r"agent-(?P<cc_agent_id>[^/]+)\.jsonl$"
)

# 主会话：<slug>/<session_id>.jsonl，顶层直挂，没有 subagents 段。
_MAIN_RE = re.compile(
    r"/projects/(?P<slug>[^/]+)"
    rf"/(?P<session_id>{_SESSION_RE})\.jsonl$"
)


@dataclass(frozen=True)
class TranscriptRef:
    """一份 transcript 的路径派生结果。

    ``kind`` 区分主会话与子 agent —— 两者的用量量级差两三个数量级（单个主会话实测
    8.5 亿 vs 子 agent 中位百万级），混在一个榜里子 agent 会被彻底淹没，所以口径上
    必须能分开（设计 §3.3）。
    """

    kind: str  # "main" | "subagent"
    project_slug: str
    session_id: str
    wf_id: str | None  # 仅 workflow 扇出的子 agent 有值
    cc_agent_id: str | None  # 仅子 agent 有值


def parse_transcript_path(path: str | None) -> TranscriptRef | None:
    """从 transcript 路径派生 (slug, session_id, wf_id, cc_agent_id)。

    认三种形态（见模块 docstring）。任何一种都不匹配时返回 None —— 包括空串、
    不含 ``/projects/`` 的路径、以及 session 段不是 uuid 形态的路径。

    Windows 反斜杠先归一成正斜杠再匹配（与 ``_extract_workflow_run_id`` 同一处理）。
    路径**不必**位于 ``~/.claude/projects`` 之下：只要含 ``/projects/<slug>/...``
    的相对形状即可，这样测试与副本目录都能解析。
    """
    if not path:
        return None
    norm = str(path).replace("\\", "/")

    m = _SUBAGENT_RE.search(norm)
    if m:
        return TranscriptRef(
            kind="subagent",
            project_slug=m.group("slug"),
            session_id=m.group("session_id"),
            wf_id=m.group("wf_id"),
            cc_agent_id=m.group("cc_agent_id"),
        )

    m = _MAIN_RE.search(norm)
    if m:
        return TranscriptRef(
            kind="main",
            project_slug=m.group("slug"),
            session_id=m.group("session_id"),
            wf_id=None,
            cc_agent_id=None,
        )

    return None


def derive_session_id(path: str | None) -> str | None:
    """只要 session_id 的窄入口（SubagentStop 顺手写这一列时用）。"""
    ref = parse_transcript_path(path)
    return ref.session_id if ref else None


def slug_matches_root(slug: str, root_path: str | None) -> bool:
    """交叉校验：路径里的 slug 是否与登记项目的 root_path 相符。

    **只回答"对不对得上"，不回答"属于谁"。** slug 是有损映射，多个不同目录可以
    塌成同一个 slug，所以 True 不构成归属证据；False 才有信息量——它说明这一行的
    ``project_id`` 与它 transcript 的实际落点不一致，值得人看一眼（回填报告里以
    ``slug_mismatch`` 标出，但不因此改写任何归属列）。

    slug 的算法**不在这里复写**：函数内延迟 import ``session_probe.project_slug``，
    因为 CC 的命名规则一变就得跟着改，两处各写一份必然漂移（§1.3 已在合成行过滤上
    吃过同样的亏）。延迟 import 是为了不让 services 层在 import 期把 api 层整个拖
    进来。
    """
    if not slug or not root_path:
        return False
    from aiteam.api.session_probe import project_slug

    return slug == project_slug(root_path)
