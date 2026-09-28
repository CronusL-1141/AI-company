#!/usr/bin/env python3
"""历史回采：agents 四层 token + tokens_measured_at + tokens_source。默认 dry-run。

规格见 docs/token-attribution-v1-design.md §6 全节 / §7 阶段3。要解决的问题是：计费口径
的用量采集（``usage_sum``）2026-07-28 才上线，此前近两千次派工的 token 全部没落库，而
**它们的 transcript 还都在磁盘上**。取证时 1,957 个候选行的文件 1,957 份全部存活——但
CC 会清理较早的本地会话历史，``transcript_gone`` 这一类**只增不减**。这是整个 v1 里
唯一一件"晚做就永远做不了"的事（§6.2-2），其余各项晚做只是晚做。

回采的对象只有一个：``agents`` 表的五列 + ``tokens_source``（外加 ``model`` 的观测
回填，见下）。四条纪律决定了这个脚本的形状：

1. **绝不触碰 ``workflow_agents.tokens``**（§6.4-1，风险 R3）。那一列是 ``ctx_last``
   口径——末轮上下文水位，与本脚本产出的 ``usage_sum`` 实测差 5~25 倍。把回采值写进
   去会把混口径**永久固化进历史数据**且事后不可分辨，比不回采糟糕得多。所以这里不是
   靠"小心别写"，而是：写列白名单 :data:`WRITABLE_COLUMNS` 是封闭集合，
   ``(workflow_agents, tokens)`` 不在其中；``--apply`` 前后各算一次该列的
   **逐行 sha256 校验和**，不一致就整事务回滚。静态面另有 I14 机检（见
   ``scripts/check_backfill_safety.py``）。
2. **覆盖率按 ``tokens_measured_at`` 分窗**（§6.4-2，对策 6.3-2）。回采会让总覆盖率从
   0.5% 跳到 78%，而"新派工有没有被采到"是**另一回事**——后者才是采集链路健不健康的
   指标。一个数字掩盖另一个数字，所以报告里这两个数**从不合并呈现**。分窗靠的是：活体
   链路写的 ``tokens_measured_at`` 是**逐行微秒级唯一**的停止时刻，而一次回采给上千行
   盖的是**同一个**批次时刻，于是"同一时间戳被 ≥ :data:`BATCH_COHORT_MIN` 行共享"就是
   回采批次的天然签名（批次时刻同时记进 journal，供精确审计）。
3. **只写空列、幂等、dry-run 先行**（§6.4-3）。幂等是结构性的而不是靠标记位：判定的第
   一分支就是"``tokens_measured_at`` 非空 → ``already_measured``"，跑完一次目标列就非
   空了，重跑必然全部落进这一类、零变更。SQL 层另带 ``IS NULL`` 守卫，防的是 dry-run
   与 apply 之间活体系统已经把值写上的竞态。
4. **写不了的行必须分类，不许静默跳过**（Council 纪律① no-data ≠ zero）。原因码见
   :data:`REASONS`，与设计 §3.4 的未归因分类同源。``transcript_gone`` 的行**逐行列出**
   ——那不是噪声，报告本身就是"回采窗口在这些行上已经关闭"的存证。

**model 列的观测回填**（§6.2-4）：transcript 的 ``message.model`` 永远是完整型号，从不
是别名。所以 ``workflow_agents.model`` 里那些还写着别名 ``opus`` 的行（实测 170 行，其中
138 行经 ``os_agent_id`` 能关到存活的 transcript）可以直接读出真实型号——这是**观测回填**，
正是"模型默认值留空、由观测回填"这条刻意决策想要的样子，与"禁止写死型号"不冲突。两条
边界：**别名台账（``MODEL_ALIAS_LEDGER``）的解析结果绝不写进任何行**（那是读侧兜底，不是
观测）；**无 transcript 的行不猜、不动**。``agents.model`` 同理（候选行里 1,913 行该列为
空），与活体 ``SubagentStop`` 路径的行为一致；不想要可以 ``--no-model`` 关掉。

**Job E：workflow 子 agent 的路径重建**。CC 的 SubagentStart 输入里没有子 agent 自己
的 transcript 路径，路径只能等 SubagentStop 或 reaper 补，而这两处都要先读出上下文水位
才落路径——agent 被 kill 在首个回复之前、或尾部 64KB 里没有 assistant 行，路径就永远
空着。这些行的文件多半还在：``<slug>/<session>/subagents/workflows/<wf_id>/agent-<cc_agent_id>.jsonl``。
Job E 只按 ``cc_agent_id`` **精确**寻址（文件名全等、全树唯一、再由会话或 wf_id 至少一把
钥匙印证、没有一把冲突），命中才补 ``transcript_path``（只补空列，已有路径逐行不动）并照
Job A 的口径回采四层 token；文件里没有任何用量行的只补路径、token 留空。已测量的行不动。

**Leader 主会话默认不采**（``--include-leader`` 才做），有两条独立理由，任一条都足够：

* **一份文件被多行共享。** 实测 47 个有路径的 Leader 行只指向 **13 份**主会话
  transcript，最多的一份被 11 行共享（幽灵行的成因见 ``backfill_agent_session_ids.py``）。
  照单全收会把同一份 8.5 亿 token 的用量**重复计入 11 次**。所以即便显式开启，本脚本也
  只给每份文件的**首行**（created_at 最早，与 ``_find_leader`` 的解析规则同源）写值，其
  余行记 ``duplicate_main_transcript``。
* **语义与阶段4 相反。** 主会话 transcript 是**活的、还在长**的文件，阶段4 对它的语义是
  ``snapshot 覆写``；而本脚本是 ``只写空列、写完不再更新``。用后者去碰前者，会把一个会话
  中途的部分值永久冻成"已测量"。主会话采集归阶段4。

用法::

    python3 scripts/backfill_token_usage.py                      # dry-run 全量报告
    python3 scripts/backfill_token_usage.py --db /tmp/copy.db    # 对副本演练
    python3 scripts/backfill_token_usage.py --sample 30          # 抽 30 行供人工核对
    python3 scripts/backfill_token_usage.py --projects-root DIR  # Job E 的寻址根（默认 ~/.claude/projects）
    python3 scripts/backfill_token_usage.py --apply --journal ~/token-backfill.json

``--apply`` 前先备份（journal 是唯一的恢复凭证，目标文件已存在时硬拒覆盖）::

    cp ~/.claude/data/ai-team-os/aiteam.db \\
       ~/aiteam.db.bak-tokenusage-$(date +%Y%m%d%H%M%S)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiteam.clock import naive_utc_now, utc_now  # noqa: E402
from aiteam.services.token_attribution import parse_transcript_usage  # noqa: E402
from aiteam.types import HarnessId  # noqa: E402

DEFAULT_DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"
DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# apply 等写锁的上限。活体 API 在大批落盘时可能长时间持锁；等不到就干净退出（退出码
# EXIT_DB_BUSY），一个字节都没写，稍后重跑即可。
LOCK_TIMEOUT_SECONDS = 5.0

EXIT_ROLLED_BACK = 4  # 越界写入被抓，整事务回滚
EXIT_DB_BUSY = 5  # 写锁没拿到，什么都没发生
EXIT_JOURNAL_FAILED = 6  # journal 落不了盘，整事务回滚（没有凭证就不写）
EXIT_NOT_IDEMPOTENT = 7  # --check-journal：有已写入的行又成了候选

# 本脚本**可能**写到的列的封闭集合。加一列必须同时改这里、改 I14 机检的期望集合，
# 于是"顺手多写一列"在评审时是可见的。(workflow_agents, tokens) 永远不在其中。
# agents.transcript_path 只由 Job E 写，只补空列；apply 前后另有逐行比对兜底。
WRITABLE_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("agents", "input_tokens"),
        ("agents", "output_tokens"),
        ("agents", "cache_creation_tokens"),
        ("agents", "cache_read_tokens"),
        ("agents", "tokens_measured_at"),
        ("agents", "tokens_source"),
        ("agents", "model"),
        ("agents", "transcript_path"),
        ("workflow_agents", "model"),
    }
)

# 这一列是 ctx_last 口径的历史资产，本脚本的产出（usage_sum）与它差 5~25 倍。
# 单列成常量是为了让机检能直接引用，而不是靠读注释。
FORBIDDEN_COLUMN: tuple[str, str] = ("workflow_agents", "tokens")

# 四层 token 的列名，顺序固定（呈现面永远分列，不给合计）。
TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)

# 一次回采给上千行盖同一个时刻；活体链路逐行写各自的停止时刻（微秒级唯一）。
# 于是"被这么多行共享的同一时间戳"= 回采批次签名。阈值取 50：活体不可能在同一微秒
# 停下 50 个 agent，而任何一次真实回采都远多于 50 行。
BATCH_COHORT_MIN = 50

# 完整型号一律以此开头（transcript 实测 claude-opus-5 / claude-opus-4-8[1m] …）；
# 别名是 opus / fable / '' 这类。用"是不是完整型号"判定，比枚举别名健壮——将来多一个
# 别名不需要改这里。
CONCRETE_MODEL_PREFIX = "claude-"

# Job E 的候选行：workflow 扇出的子 agent（= hook_translator.WORKFLOW_AGENT_TYPE，字面量
# 防 import 整个 API 层）。只认 CC 宿主：Codex 的文件是 rollout，形状与寻址都不同。
WORKFLOW_ROLE = "workflow-subagent"
CC_HARNESSES = frozenset({"", HarnessId.CLAUDE_CODE.value})

# workflow 子 agent transcript 的固定落点（相对 projects 根）：
# <slug>/<session>/subagents/workflows/<wf_id>/agent-<cc_agent_id>.jsonl
_WF_DIR_RE = re.compile(r"^wf_[0-9a-z]+(?:-[0-9a-z]+)?$", re.IGNORECASE)

# 文件在这个时长内还有写入，就当 agent 可能还活着（Job A / Job E 共用）。此刻回采写下的
# 是中途值：活体 SubagentStop 之后若再来，会按赋值语义覆写成终值；若再也不来（run 被
# kill、会话中断、回执丢失），中途值就被永久冻成"已测量"，且之后本脚本也不会再碰它。
# 取值依据（2026-09-28 全树实测）：3074 份子 agent transcript 里有 12 份内部写入间隔
# 超过 6h（具名 teammate 空闲数小时后被消息唤醒继续写），最长 28.6h；48h 覆盖全部观测
# 值。代价：当时 Job A 候选里 mtime 落在 6h-48h 的只有 2 行，晚一两天回采而已。
# 这仍是兜底不是保证：静默超过 48h 又复活、且最终 Stop 永远不来的 agent 照样会被冻结。
SETTLE_AFTER_SECONDS = 48 * 3600

REASONS: dict[str, str] = {
    "written": "可写入（dry-run 的候选 / --apply 的实际写入）",
    "already_measured": "tokens_measured_at 已有值 —— 幂等重跑必然全部落在这里，不重测不覆盖",
    "already_set": "目标列已有值且与观测一致",
    "no_transcript_path": "该行从未登记 transcript 路径，无从回采（历史行，新行已覆盖）",
    "transcript_gone": "路径有但文件已不在磁盘 —— 回采窗口已在这些行上关闭，只增不减",
    "transcript_purged": "按 cc_agent_id 只找到 .meta.json，同名 .jsonl 已被 CC 清理 —— 窗口已关闭，只增不减",
    "transcript_not_found": "按 cc_agent_id 找不到任何文件（连 .meta.json 也没有）",
    "no_cc_agent_id": "行上没有 cc_agent_id，无从按文件名寻址",
    "ambiguous_match": "同名文件不止一份、不在 workflows 落点，或所在会话 / wf_id 与行上记录冲突 —— 模糊匹配一律不补",
    "uncorroborated": "行上既无 session_id 也关联不到 workflow_agents，文件名之外没有第二把钥匙印证 —— 不补",
    "agent_live": "文件近期仍有写入，agent 可能还活着 —— 留给活体采集，不把中途值冻成已测量",
    "other_harness": "非 Claude Code 宿主的行 —— 文件是 rollout 不是 transcript，Job A / E 都不碰",
    "unreadable_transcript": "文件在但解析不出任何 usage 快照（空文件/有损坏行）",
    "no_usage_lines": "文件完好但没有任何 assistant 用量行（agent 在首个回复前就结束了）—— 没有数据，不是 0",
    "duplicate_main_transcript": "同一份主会话 transcript 已由更早的行代表 —— 再写就是重复计入",
    "no_observed_model": "transcript 里没读到完整型号（只有合成行 <synthetic> 之类）",
    "already_concrete": "model 已是完整型号，不是别名 —— 观测值不覆盖观测值",
    "transcript_grew": "重算值全面 ≥ 库中值 —— transcript 在测量后仍有写入（agent 仍活着），不改",
    "recompute_mismatch": "重算值与库中值对不上且非增长 —— 真异常，人必须看",
}

# 报告里按这个顺序打印（先给能行动的，再给不能行动的）。
REASON_ORDER = list(REASONS)


@dataclass
class Row:
    """一行的判定结果。``values`` 只在 written 时有意义。"""

    table: str
    row_id: str
    name: str
    reason: str
    values: dict[str, Any] = field(default_factory=dict)
    guard: dict[str, Any] = field(default_factory=dict)
    warn: str = ""
    note: str = ""
    # apply 之后回填：True = 这一行真的写进去了；False = SQL 守卫让路（期间被活体写上）。
    applied: bool | None = None

    def value_repr(self) -> str:
        if not self.values:
            return ""
        parts = []
        for k, v in self.values.items():
            if k == "transcript_path":
                v = ".../" + "/".join(Path(v).parts[-2:])
            parts.append(f"{k.replace('_tokens', '')}={v}")
        return " ".join(parts)


@dataclass
class Job:
    title: str
    table: str
    rows: list[Row] = field(default_factory=list)
    enabled: bool = True

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.reason] = out.get(r.reason, 0) + 1
        return out

    def written(self) -> list[Row]:
        return [r for r in self.rows if r.reason == "written"]

    def by_reason(self, reason: str) -> list[Row]:
        return [r for r in self.rows if r.reason == reason]


# ---------------------------------------------------------------------------
# 读取面
# ---------------------------------------------------------------------------
def open_db(path: Path, *, writable: bool) -> sqlite3.Connection:
    """写模式才用普通连接；只读一律走 ``mode=ro`` URI，物理上写不进去。"""
    if writable:
        con = sqlite3.connect(str(path), timeout=LOCK_TIMEOUT_SECONDS)
    else:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_agents(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """
        select id, name, role, model, created_at, transcript_path,
               input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
               tokens_measured_at, tokens_source, harness
        from agents
        order by created_at
        """
    ).fetchall()


def load_workflow_agents(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """workflow_agents **没有** transcript_path 列 —— 文件只能经 os_agent_id 借道
    agents 行拿到。实测 os_agent_id 全表无重复，所以这个 join 不会扇出。"""
    return con.execute(
        """
        select wa.id, wa.label, wa.model, wa.os_agent_id, wa.cc_agent_id,
               a.transcript_path, a.model as agent_model
        from workflow_agents wa
        left join agents a on a.id = wa.os_agent_id
        order by wa.created_at
        """
    ).fetchall()


def load_pathless_workflow_agents(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Job E 的候选：workflow 子 agent 且 transcript_path 为空。"""
    return con.execute(
        """
        select id, name, model, created_at, session_id, cc_tool_use_id, harness,
               tokens_measured_at
        from agents
        where role = ? and (transcript_path is null or transcript_path = '')
        order by created_at
        """,
        (WORKFLOW_ROLE,),
    ).fetchall()


def load_workflow_links(con: sqlite3.Connection) -> dict[str, set[str]]:
    """``agents.id`` 或 ``cc_agent_id`` -> 关联到的 wf_id 集合（Job E 的第二把钥匙）。

    两条关联都收：``os_agent_id`` 是常规路径，但实测有 workflow_agents 行的
    ``os_agent_id`` 为空、只有 ``cc_agent_id`` 对得上。键不会撞：前者是 uuid，后者是
    CC 的 agent id。
    """
    links: dict[str, set[str]] = {}
    for wf_id, cc_agent_id, os_agent_id in con.execute(
        "select wf_id, cc_agent_id, os_agent_id from workflow_agents"
    ):
        for key in (os_agent_id, cc_agent_id):
            if key:
                links.setdefault(key, set()).add(wf_id)
    return links


def stored_transcript_paths(con: sqlite3.Connection) -> dict[str, str]:
    """已有 transcript_path 的行 -> 路径。Job E 只补空列，这些值 apply 前后必须逐行不变。"""
    return {
        rid: path
        for rid, path in con.execute(
            "select id, transcript_path from agents "
            "where transcript_path is not null and transcript_path != ''"
        )
    }


def workflow_tokens_fingerprint(con: sqlite3.Connection) -> dict[str, Any]:
    """``workflow_agents.tokens`` 的逐行指纹 —— 硬约束① 的验收凭据。

    只比对合计是不够的：两行一增一减能让 sum 不变。所以取 ``(id, tokens)`` 按 id 排序
    后的 sha256，任何一行被动过都会变。
    """
    h = hashlib.sha256()
    n = 0
    total = 0
    for rid, tokens in con.execute(
        "select id, tokens from workflow_agents order by id"
    ):
        h.update(f"{rid}\x1f{tokens!r}\x1e".encode())
        n += 1
        total += tokens or 0
    return {"rows": n, "sum": total, "sha256": h.hexdigest()}


# ---------------------------------------------------------------------------
# 解析（唯一的磁盘 IO 面）
# ---------------------------------------------------------------------------
class Parser:
    """带缓存与进度的 transcript 解析器。

    缓存是必需的而不是优化：47 个 Leader 行只指向 13 份文件，同一份 35 MB 的文件解析
    11 次纯属浪费。缓存键是路径，值是解析结果（可能是 None）。
    """

    def __init__(self, *, verbose: bool = True) -> None:
        self._cache: dict[str, dict[str, Any] | None] = {}
        self._verbose = verbose
        self.parsed = 0
        self.bytes_read = 0

    def usage(self, path: str) -> dict[str, Any] | None:
        if path in self._cache:
            return self._cache[path]
        try:
            self.bytes_read += os.path.getsize(path)
        except OSError:
            pass
        result = parse_transcript_usage(path)
        self._cache[path] = result
        self.parsed += 1
        if self._verbose and self.parsed % 200 == 0:
            print(
                f"  …已解析 {self.parsed} 份 transcript（{self.bytes_read / 1e6:.0f} MB）",
                file=sys.stderr,
            )
        return result


def transcript_exists(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def still_being_written(path: str | Path, now: float) -> bool:
    """文件在 SETTLE_AFTER_SECONDS 内有写入。stat 失败抛 OSError，由调用方归类。"""
    return now - os.stat(path).st_mtime < SETTLE_AFTER_SECONDS


def classify_no_usage(path: str | Path) -> str:
    """解析器给出 None 的文件再分一层：完好但没有用量行，还是空的 / 有损坏行。

    前者是首个回复前就被 kill 的 agent（"没有数据"），后者才是真读不出来。两者都不写，
    但混在一个桶里，分类账就把 no-data 说成了"损坏"。
    """
    valid = broken = 0
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    json.loads(raw)
                except ValueError:
                    broken += 1
                else:
                    valid += 1
    except OSError:
        return "unreadable_transcript"
    return "no_usage_lines" if valid and not broken else "unreadable_transcript"


# 身份行只看文件开头：CC 每一行都带 sessionId / agentId，首行就有（2026-09-28 全树
# 2071 份 workflow transcript 实测首行全部带这两个字段，且与目录名、文件名全部一致）。
_IDENTITY_SCAN_LINES = 20


def transcript_identity(path: str | Path) -> tuple[str, str] | None:
    """transcript 自己记着的 ``(sessionId, agentId)``；开头几行里读不到就返回 None。"""
    try:
        with open(path, "rb") as fh:
            for i, raw in enumerate(fh):
                if i >= _IDENTITY_SCAN_LINES:
                    break
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row, dict) and (row.get("sessionId") or row.get("agentId")):
                    return str(row.get("sessionId") or ""), str(row.get("agentId") or "")
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Job A —— 子 agent 的四层 token + tokens_measured_at + tokens_source
# ---------------------------------------------------------------------------
def job_subagent_usage(
    agents: list[sqlite3.Row],
    parser: Parser,
    batch_ts: str,
    *,
    with_model: bool = True,
    now: float | None = None,
) -> Job:
    """主回采：``role != 'leader'`` 且 ``tokens_measured_at`` 为空的行。

    分支顺序即幂等的实现：``already_measured`` 排在最前，所以第二次跑的时候所有被写过
    的行在**读到文件之前**就已经出局——重跑既零变更也不重复解析 700 MB。

    文件近期仍有写入的行归 ``agent_live`` 不写（见 :data:`SETTLE_AFTER_SECONDS`）：这一类
    候选里本来就有正在跑的 agent（有路径、还没等到 SubagentStop），写下去的是中途值。
    """
    now = time.time() if now is None else now
    job = Job("A. agents 四层 token + tokens_measured_at + tokens_source（子 agent）", "agents")
    for a in agents:
        if a["role"] == "leader":
            continue
        if (a["harness"] or "") not in CC_HARNESSES:
            # Codex 行的文件是 rollout，不是 CC transcript：不该靠"rollout 里恰好没有
            # assistant 行"挡住，而是按宿主直接不碰。
            job.rows.append(Row("agents", a["id"], a["name"], "other_harness", note=a["harness"]))
            continue
        if a["tokens_measured_at"]:
            job.rows.append(Row("agents", a["id"], a["name"], "already_measured"))
            continue
        path = a["transcript_path"] or ""
        if not path:
            job.rows.append(Row("agents", a["id"], a["name"], "no_transcript_path"))
            continue
        if not transcript_exists(path):
            job.rows.append(
                Row("agents", a["id"], a["name"], "transcript_gone", note=path)
            )
            continue
        try:
            live = still_being_written(path, now)
        except OSError:
            job.rows.append(Row("agents", a["id"], a["name"], "transcript_gone", note=path))
            continue
        if live:
            job.rows.append(Row("agents", a["id"], a["name"], "agent_live", note=path))
            continue
        usage = parser.usage(path)
        if not usage:
            job.rows.append(Row("agents", a["id"], a["name"], classify_no_usage(path), note=path))
            continue
        values: dict[str, Any] = {c: usage[c] for c in TOKEN_COLUMNS}
        values["tokens_measured_at"] = batch_ts
        values["tokens_source"] = "transcript"
        warn = ""
        observed = usage.get("model") or ""
        if with_model and observed and not (a["model"] or ""):
            # 观测回填：库里那一列是空的，transcript 里是完整型号。与活体
            # SubagentStop 路径同一行为（hook_translator 也写这一列）。
            values["model"] = observed
        elif observed and (a["model"] or "") and a["model"] != observed:
            # 已有值与观测不一致：**不动**，只报。覆盖观测值不是本脚本的事。
            warn = f"库中 model={a['model']} 与 transcript 观测 {observed} 不一致（未改动）"
        job.rows.append(
            Row(
                "agents",
                a["id"],
                a["name"],
                "written",
                values=values,
                guard={"tokens_measured_at": None},
                warn=warn,
                note=f"api_calls={usage.get('api_calls')}",
            )
        )
    return job


# ---------------------------------------------------------------------------
# Job B —— 已测量行的 tokens_source 补标（顺带做一次独立重算对账）
# ---------------------------------------------------------------------------
def job_source_label(agents: list[sqlite3.Row], parser: Parser) -> Job:
    """``tokens_measured_at`` 非空但 ``tokens_source`` 为空的行（实测 13 行）。

    这些是 D1 活体链路采到的行，``tokens_source`` 那一列比它们晚落地。补标之前先做一件
    更有价值的事：**用本脚本的解析器重算一遍，与活体链路当时写下的四层值逐字段比对**。
    相等才补标——于是这一列的值不是"我假设它来自 transcript"，而是"我重算过，确实是"。
    这同时是设计 §4.4 闸2① 要求的那种零容差同口径对账（解析器是纯函数，容差为零）。

    重算值全面 ≥ 库中值说明 transcript 在测量之后又长了（agent 还活着，比如跑这个脚本
    的这一个）——那不是异常，单独归一类；其余对不上的才是真异常。
    """
    job = Job("B. agents.tokens_source 补标（已测量行，重算比对后才写）", "agents")
    for a in agents:
        if not a["tokens_measured_at"]:
            continue
        if a["tokens_source"]:
            job.rows.append(Row("agents", a["id"], a["name"], "already_set"))
            continue
        path = a["transcript_path"] or ""
        if not path:
            job.rows.append(Row("agents", a["id"], a["name"], "no_transcript_path"))
            continue
        if not transcript_exists(path):
            job.rows.append(Row("agents", a["id"], a["name"], "transcript_gone", note=path))
            continue
        usage = parser.usage(path)
        if not usage:
            job.rows.append(Row("agents", a["id"], a["name"], "unreadable_transcript", note=path))
            continue
        stored = {c: (a[c] or 0) for c in TOKEN_COLUMNS}
        fresh = {c: usage[c] for c in TOKEN_COLUMNS}
        if stored == fresh:
            job.rows.append(
                Row(
                    "agents",
                    a["id"],
                    a["name"],
                    "written",
                    values={"tokens_source": "transcript"},
                    guard={"tokens_source": None},
                    note="重算逐字段相等",
                )
            )
        elif all(fresh[c] >= stored[c] for c in TOKEN_COLUMNS):
            delta = {c: fresh[c] - stored[c] for c in TOKEN_COLUMNS if fresh[c] != stored[c]}
            job.rows.append(
                Row(
                    "agents",
                    a["id"],
                    a["name"],
                    "transcript_grew",
                    note=f"增量 {delta}",
                )
            )
        else:
            job.rows.append(
                Row(
                    "agents",
                    a["id"],
                    a["name"],
                    "recompute_mismatch",
                    warn=f"库中 {stored} vs 重算 {fresh}",
                )
            )
    return job


# ---------------------------------------------------------------------------
# Job C —— workflow_agents.model 的观测回填
# ---------------------------------------------------------------------------
def job_workflow_model(
    rows: list[sqlite3.Row], parser: Parser, rebuilt: dict[str, str] | None = None
) -> Job:
    """把 ``workflow_agents.model`` 里的**别名**换成 transcript 观测到的完整型号。

    这是本脚本唯一写 ``workflow_agents`` 的地方，写的是 ``model`` 列。``tokens`` 列不在
    白名单里、不在这个函数里、也过不了 apply 后的指纹比对——三道独立的拦。

    "只写空列"在这里的正确形态不是字面照搬：别名 ``opus`` 是**请求规格**（要什么模型），
    不是观测结果。用观测替换请求规格是补真，方向与"用推断覆盖观测"相反。所以判据是
    **不是完整型号才动**，且 SQL 守卫钉死"仍等于我读到的那个别名"——期间被别人写成完整
    型号就自动让路。

    ``rebuilt`` 是 Job E 本轮重建出的路径（agent id -> path）。必须在同一轮用上：否则
    apply 之后重跑，Job C 才第一次看见这些路径，"重跑零变更"就破了。
    """
    rebuilt = rebuilt or {}
    job = Job("C. workflow_agents.model ← transcript 观测（别名换真名）", "workflow_agents")
    for r in rows:
        current = r["model"] or ""
        label = r["label"] or r["cc_agent_id"] or ""
        if current.startswith(CONCRETE_MODEL_PREFIX):
            job.rows.append(Row("workflow_agents", r["id"], label, "already_concrete"))
            continue
        path = r["transcript_path"] or rebuilt.get(r["os_agent_id"] or "", "")
        if not path:
            job.rows.append(
                Row("workflow_agents", r["id"], label, "no_transcript_path",
                    note=f"model={current!r} os_agent_id={r['os_agent_id']}")
            )
            continue
        if not transcript_exists(path):
            job.rows.append(Row("workflow_agents", r["id"], label, "transcript_gone", note=path))
            continue
        usage = parser.usage(path)
        observed = (usage or {}).get("model") or ""
        if not observed:
            job.rows.append(
                Row("workflow_agents", r["id"], label, "no_observed_model", note=path)
            )
            continue
        job.rows.append(
            Row(
                "workflow_agents",
                r["id"],
                label,
                "written",
                values={"model": observed},
                guard={"model": current},
                note=f"别名 {current!r} → 观测 {observed}",
            )
        )
    return job


# ---------------------------------------------------------------------------
# Job D —— Leader 主会话（默认关闭）
# ---------------------------------------------------------------------------
def job_leader_usage(
    agents: list[sqlite3.Row], parser: Parser, batch_ts: str, *, enabled: bool
) -> Job:
    """Leader 行的四层 token。**默认不执行**，理由见模块 docstring。

    即便开启也**按文件去重**：同一份主会话 transcript 只由 created_at 最早的那一行代表
    （与 ``_find_leader`` 的解析规则同源），其余记 ``duplicate_main_transcript``。不去重
    的话，实测那份被 11 行共享的 transcript 会让同一份用量重复计入 11 次。
    """
    job = Job(
        "D. agents 四层 token（Leader 主会话，需 --include-leader）", "agents", enabled=enabled
    )
    seen: dict[str, str] = {}  # transcript_path -> 已代表它的 agent_id
    # 两遍扫：已测量行先占文件代表权。代表权必须跟着"谁已经挂了这份文件的账"走，
    # 而不是谁在本轮先被扫到——否则 --apply 一轮之后，代表行落进 already_measured
    # 提前 continue、不再占位，同文件的幽灵行躲过 duplicate 拦截升格为候选，重跑
    # --apply 就会把同一份主会话的用量重复计入（2026-08-03 生产复跑实测抓获：
    # 首轮写 10 行后复跑报 5 行"待写入"，全是已测文件的幽灵行）。
    for a in agents:
        if a["role"] == "leader" and a["tokens_measured_at"] and (a["transcript_path"] or ""):
            seen.setdefault(a["transcript_path"], a["id"])
    for a in agents:  # load_agents 已按 created_at 排序 → 先到者即最早
        if a["role"] != "leader":
            continue
        if a["tokens_measured_at"]:
            job.rows.append(Row("agents", a["id"], a["name"], "already_measured"))
            continue
        path = a["transcript_path"] or ""
        if not path:
            job.rows.append(Row("agents", a["id"], a["name"], "no_transcript_path"))
            continue
        if path in seen:
            job.rows.append(
                Row("agents", a["id"], a["name"], "duplicate_main_transcript",
                    note=f"已由 {seen[path][:8]} 代表：{path}")
            )
            continue
        if not transcript_exists(path):
            job.rows.append(Row("agents", a["id"], a["name"], "transcript_gone", note=path))
            continue
        usage = parser.usage(path)
        if not usage:
            job.rows.append(Row("agents", a["id"], a["name"], "unreadable_transcript", note=path))
            continue
        seen[path] = a["id"]
        values: dict[str, Any] = {c: usage[c] for c in TOKEN_COLUMNS}
        values["tokens_measured_at"] = batch_ts
        values["tokens_source"] = "transcript"
        job.rows.append(
            Row(
                "agents", a["id"], a["name"], "written", values=values,
                guard={"tokens_measured_at": None},
                warn="主会话是活文件，此值是回采时刻的快照；持续采集归阶段4",
                note=f"api_calls={usage.get('api_calls')}",
            )
        )
    return job


# ---------------------------------------------------------------------------
# Job E —— workflow 子 agent 的路径重建 + 四层 token
# ---------------------------------------------------------------------------
@dataclass
class TranscriptIndex:
    """projects 根下全部子 agent 文件，按 cc_agent_id 归档。一次树遍历服务全部候选行。"""

    root: Path
    files: dict[str, list[Path]] = field(default_factory=dict)  # cc id -> 全部同名 .jsonl
    metas: set[str] = field(default_factory=set)  # 还剩 .meta.json 的 cc id

    @classmethod
    def build(cls, root: Path) -> TranscriptIndex:
        index = cls(root)
        for path in root.glob("*/*/subagents/**/agent-*.jsonl"):
            index.files.setdefault(path.name[len("agent-"): -len(".jsonl")], []).append(path)
        for path in root.glob("*/*/subagents/**/agent-*.meta.json"):
            index.metas.add(path.name[len("agent-"): -len(".meta.json")])
        return index

    def workflow_location(self, path: Path) -> tuple[str, str] | None:
        """``(session_id, wf_id)``，路径不是 workflow 落点形状就返回 None。"""
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            return None
        if (
            len(parts) != 6
            or parts[2:4] != ("subagents", "workflows")
            or not _WF_DIR_RE.match(parts[4])
        ):
            return None
        return parts[1], parts[4]


def job_workflow_path_rebuild(
    candidates: list[sqlite3.Row],
    links: dict[str, set[str]],
    index: TranscriptIndex | None,
    parser: Parser,
    batch_ts: str,
    *,
    with_model: bool = True,
    now: float | None = None,
) -> Job:
    """给没有 transcript_path 的 workflow 子 agent 行按 ``cc_agent_id`` 精确找回文件。

    精确 = 四条同时成立，任一不成立就归类不写：

    1. 全树只有**一份** ``agent-<cc_agent_id>.jsonl``，且它在 workflow 落点
       ``<slug>/<session>/subagents/workflows/<wf_id>/`` 下；
    2. 行上有 session_id 时，文件所在会话目录必须就是它；
    3. 行关联得到 workflow_agents 时，文件所在 wf 目录必须是**唯一**关联的那个 wf_id；
    4. 2、3 至少有一条可用 —— 文件名之外必须有第二把钥匙印证。

    另有一道内容级否决：文件开头自带的 ``sessionId`` / ``agentId`` 读得到时，必须与行上的
    session_id / cc_agent_id 相等，否则按歧义处理。它让"全树唯一 + 目录名"不再是唯一承重点。

    命中后：只补空列（SQL 守卫同时钉 ``transcript_path`` 与 ``tokens_measured_at`` 为空），
    四层 token 与 Job A 同一个解析器、同一个批次时刻。文件里一条用量行都没有（agent 在
    首个回复前就被 kill）时只补路径，token 留空 —— 那是"没有数据"，不是"用了 0"。
    分支顺序即幂等：写过的行下一轮已不在候选里（路径非空）。
    """
    job = Job("E. workflow 子 agent 路径重建 + 四层 token（按 cc_agent_id 精确寻址）", "agents")
    now = time.time() if now is None else now
    for a in candidates:
        name = a["name"]
        if (a["harness"] or "") not in CC_HARNESSES:
            job.rows.append(Row("agents", a["id"], name, "other_harness", note=a["harness"]))
            continue
        if a["tokens_measured_at"]:
            job.rows.append(Row("agents", a["id"], name, "already_measured"))
            continue
        cc_id = a["cc_tool_use_id"] or ""
        if not cc_id:
            job.rows.append(Row("agents", a["id"], name, "no_cc_agent_id"))
            continue
        assert index is not None  # 有候选才建索引，走到这里必然已建
        hits = index.files.get(cc_id, [])
        if not hits:
            reason = "transcript_purged" if cc_id in index.metas else "transcript_not_found"
            job.rows.append(Row("agents", a["id"], name, reason, note=cc_id))
            continue
        if len(hits) > 1:
            job.rows.append(
                Row("agents", a["id"], name, "ambiguous_match",
                    note=f"{len(hits)} 份同名文件：" + " | ".join(str(p) for p in hits))
            )
            continue
        path = hits[0]
        location = index.workflow_location(path)
        if location is None:
            job.rows.append(
                Row("agents", a["id"], name, "ambiguous_match", note=f"不在 workflow 落点：{path}")
            )
            continue
        file_session, file_wf = location
        session = a["session_id"] or ""
        linked = links.get(a["id"], set()) | links.get(cc_id, set())
        if session and session != file_session:
            job.rows.append(
                Row("agents", a["id"], name, "ambiguous_match",
                    note=f"会话不符：行 {session} / 文件 {file_session}")
            )
            continue
        if len(linked) > 1 or (linked and file_wf not in linked):
            job.rows.append(
                Row("agents", a["id"], name, "ambiguous_match",
                    note=f"wf_id 不符：行关联 {sorted(linked)} / 文件 {file_wf}")
            )
            continue
        if not session and not linked:
            job.rows.append(Row("agents", a["id"], name, "uncorroborated", note=str(path)))
            continue
        identity = transcript_identity(path)
        if identity is not None:
            file_sid, file_aid = identity
            if (file_aid and file_aid != cc_id) or (file_sid and session and file_sid != session):
                job.rows.append(
                    Row("agents", a["id"], name, "ambiguous_match",
                        note=f"文件内身份不符：文件记 session={file_sid} agent={file_aid}")
                )
                continue
        try:
            live = still_being_written(path, now)
        except OSError:
            job.rows.append(Row("agents", a["id"], name, "transcript_not_found", note=str(path)))
            continue
        if live:
            job.rows.append(Row("agents", a["id"], name, "agent_live", note=str(path)))
            continue

        keys = "session+wf" if session and linked else ("session" if session else "wf")
        if identity is not None:
            keys += "+content"
        keys += f" 会话={file_session[:8]} wf={file_wf}"
        values: dict[str, Any] = {"transcript_path": str(path)}
        usage = parser.usage(str(path))
        if usage:
            values.update({c: usage[c] for c in TOKEN_COLUMNS})
            values["tokens_measured_at"] = batch_ts
            values["tokens_source"] = "transcript"
            observed = usage.get("model") or ""
            if with_model and observed and not (a["model"] or ""):
                values["model"] = observed
            note = f"印证键={keys} api_calls={usage.get('api_calls')}"
        else:
            note = f"印证键={keys} 文件无用量行，只补路径（no-data ≠ zero，token 留空）"
        job.rows.append(
            Row(
                "agents",
                a["id"],
                name,
                "written",
                values=values,
                guard={"transcript_path": None, "tokens_measured_at": None},
                note=note,
            )
        )
    return job


def job_e_rebuilt_paths(job: Job) -> dict[str, str]:
    """Job E 本轮要写的路径（agent id -> path），交给 Job C 同轮使用。"""
    return {r.row_id: r.values["transcript_path"] for r in job.written()}


def print_job_e_summary(job: Job) -> None:
    """Job E 的回收账：救回多少行、多少 token（四层分列，不给合计）、救不回的按原因分。"""
    written = job.written()
    with_tokens = [r for r in written if "tokens_measured_at" in r.values]
    print()
    print("  ── Job E 回收账 ──")
    print(f"    补路径 + 四层 token：{len(with_tokens)} 行")
    print(f"    只补路径（文件无用量行）：{len(written) - len(with_tokens)} 行")
    for col in TOKEN_COLUMNS:
        print(f"      {col:<22} {sum(r.values[col] for r in with_tokens):>14,}")
    lost = {
        reason: n for reason, n in job.counts().items()
        if reason not in ("written", "already_measured")
    }
    if lost:
        print("    救不回 / 不补：" + "，".join(f"{k} {v}" for k, v in lost.items()))


# ---------------------------------------------------------------------------
# 覆盖率分窗（硬约束②）
# ---------------------------------------------------------------------------
def detect_backfill_cohorts(con: sqlite3.Connection) -> set[str]:
    """已存在于库中的回采批次时刻 —— "同一时间戳被 ≥ BATCH_COHORT_MIN 行共享"。"""
    return {
        str(ts)
        for ts, n in con.execute(
            "select tokens_measured_at, count(*) from agents "
            "where tokens_measured_at is not null group by tokens_measured_at"
        )
        if n >= BATCH_COHORT_MIN
    }


@dataclass
class Coverage:
    """一个分母下的覆盖率三分：增量采集 / 历史回采 / 未归因。

    三个数**从不合并成一个**。§6.3-2 说得很直接：回采后总覆盖率跳到 78%，但"新派工的
    采集率"是另一回事，后者才是判断采集链路健不健康的指标——一个数字会掩盖另一个数字。
    """

    label: str
    total: int
    incremental: int  # 活体链路采到的（逐行唯一时间戳）
    backfilled: int  # 回采批次采到的（共享批次时间戳）

    @property
    def measured(self) -> int:
        return self.incremental + self.backfilled

    def line(self) -> str:
        def pct(n: int) -> str:
            return f"{n / self.total * 100:5.1f}%" if self.total else "  n/a"

        return (
            f"  {self.label:<26} 分母 {self.total:5d} │ "
            f"增量采集 {self.incremental:5d} {pct(self.incremental)} │ "
            f"历史回采 {self.backfilled:5d} {pct(self.backfilled)} │ "
            f"未归因 {self.total - self.measured:5d} {pct(self.total - self.measured)}"
        )


def measure_coverage(
    agents: list[sqlite3.Row], cohorts: set[str], *, leader: bool
) -> Coverage:
    """§4.1：分母是**该 scope 的全部派工行**，含没有 transcript 的行。

    不得以"没路径所以不算"为由把行移出分母——那正是让局部冒充全貌（风险 R2）。
    分母按行是否存在算，**不按 tokens_measured_at**，否则未测量的行会从分母里消失、
    覆盖率恒等于 100%。
    """
    rows = [a for a in agents if (a["role"] == "leader") == leader]
    incremental = backfilled = 0
    for a in rows:
        ts = a["tokens_measured_at"]
        if not ts:
            continue
        if str(ts) in cohorts:
            backfilled += 1
        else:
            incremental += 1
    return Coverage(
        "Leader 主会话" if leader else "子 agent（派工）", len(rows), incremental, backfilled
    )


def project_coverage(before: Coverage, newly_written: int) -> Coverage:
    """回采**只加历史回采那一格**，增量采集那一格逐字不动 —— 这正是要给人看的。"""
    return Coverage(before.label, before.total, before.incremental, before.backfilled + newly_written)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def print_job(job: Job, sample_n: int, rng: random.Random) -> None:
    print()
    print("=" * 88)
    print(job.title + ("" if job.enabled else "   〔本次不执行，以下仅为判定预览〕"))
    print("-" * 88)
    counts = job.counts()
    for reason in REASON_ORDER:
        if reason in counts:
            print(f"  {counts[reason]:6d}  {reason:<26} {REASONS[reason]}")
    print(f"  {sum(counts.values()):6d}  合计")

    written = job.written()
    if written:
        picks = written if len(written) <= sample_n else rng.sample(written, sample_n)
        picks.sort(key=lambda r: r.row_id)
        print()
        print(f"  ── 抽样 {len(picks)}/{len(written)} 行（供人工核对）──")
        for r in picks:
            note = f"  ({r.note})" if r.note else ""
            warn = f"  ⚠ {r.warn}" if r.warn else ""
            print(f"    {r.row_id[:8]}  {r.name[:26]:<26} {r.value_repr()}{note}{warn}")

    # 有 warn 的行单独再列一次：抽样可能抽不到它们，而它们恰恰是要人看的。
    warned = [r for r in job.rows if r.warn and r.reason != "written"]
    warned += [r for r in written if r.warn]
    if warned:
        print()
        print(f"  ── 需人工过目 {len(warned)} 行 ──")
        for r in warned[:20]:
            print(f"    ⚠ {r.row_id[:8]}  {r.name[:26]:<26} [{r.reason}] {r.warn or r.note}")
        if len(warned) > 20:
            print(f"      …… 另有 {len(warned) - 20} 行")


def print_transcript_gone(jobs: list[Job]) -> None:
    """``transcript_gone`` 逐行列出 —— §3.4 要求，且这类只增不减。

    这不是噪声：报告本身就是"回采窗口已在这些行上永久关闭"的存证。今天列出来的每一行，
    都是将来任何人再想回采都拿不到的那一行。
    """
    print()
    print("=" * 88)
    print("transcript_gone 逐行清单（§3.4：只增不减，报告即窗口关闭的存证）")
    print("-" * 88)
    gone = [(j, r) for j in jobs for r in j.by_reason("transcript_gone")]
    if not gone:
        print("  ✓ 0 行 —— 本次回采窗口完全敞开，登记过的 transcript 一份不少地还在磁盘上。")
        return
    print(f"  {len(gone)} 行的 transcript 已不在磁盘，这些用量永久不可回采：")
    for job, r in gone:
        print(f"    [{job.table}] {r.row_id[:8]}  {r.name[:26]:<26} {r.note}")


def print_coverage(
    agents: list[sqlite3.Row], cohorts: set[str], job_a: Job, job_d: Job, job_e: Job
) -> None:
    print()
    print("=" * 88)
    print("覆盖率分窗（硬约束②：历史回采与增量采集永远是两个数，不合并）")
    print("-" * 88)
    sub_before = measure_coverage(agents, cohorts, leader=False)
    lead_before = measure_coverage(agents, cohorts, leader=True)
    # Job E 只补路径的行没有 tokens_measured_at，不算已测量。
    e_measured = sum(1 for r in job_e.written() if "tokens_measured_at" in r.values)
    sub_after = project_coverage(sub_before, len(job_a.written()) + e_measured)
    lead_after = project_coverage(lead_before, len(job_d.written()) if job_d.enabled else 0)

    print("  回采前：")
    print(sub_before.line())
    print(lead_before.line())
    print("  回采后（预计）：")
    print(sub_after.line())
    print(lead_after.line())
    print()
    print("  读法：'增量采集'那一格**回采前后逐字不变** —— 它才是采集链路健不健康的指标；")
    print("        回采只抬高'历史回采'一格。两格相加没有意义，本报告也不给这个和。")
    print("  分母口径：agents 表按 role 分的全部行，**含没有 transcript 的行**（§4.1）。")


def print_fingerprint(fp: dict[str, Any], *, title: str) -> None:
    print(f"  {title}: {fp['rows']} 行 / sum={fp['sum']:,} / sha256={fp['sha256'][:16]}…")


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def apply_job(con: sqlite3.Connection, job: Job) -> int:
    """逐行写入，带 SQL 级守卫。

    守卫在 SQL 里而不只在判定层，是因为 dry-run 与 apply 之间库随时可能被活体系统改写：
    ``tokens_measured_at IS NULL`` 让并发下也不会覆盖别人刚写的观测值；
    ``model = <我读到的那个别名>`` 让期间被写成完整型号的行自动让路。
    """
    if not job.enabled:
        return 0
    n = 0
    for r in job.written():
        for col in r.values:
            if (job.table, col) not in WRITABLE_COLUMNS:
                raise RuntimeError(
                    f"写列白名单拦截：({job.table}, {col}) 不在 WRITABLE_COLUMNS 中"
                )
        assign = ", ".join(f"{c} = ?" for c in r.values)
        params: list[Any] = list(r.values.values())
        where = ["id = ?"]
        params.append(r.row_id)
        for col, expected in r.guard.items():
            if expected is None:
                where.append(f"({col} is null or {col} = '')")
            else:
                where.append(f"{col} = ?")
                params.append(expected)
        cur = con.execute(
            f"update {job.table} set {assign} where {' and '.join(where)}",  # noqa: S608 — 表名/列名全部来自本文件常量与白名单，非外部输入
            params,
        )
        r.applied = cur.rowcount == 1
        n += cur.rowcount
    return n


def script_provenance() -> dict[str, Any]:
    """这份脚本本身的指纹：worktree 合并后会删，事后只有它能说清 journal 是哪版代码写的。"""
    here = Path(__file__).resolve()
    out: dict[str, Any] = {"sha256": hashlib.sha256(here.read_bytes()).hexdigest()}
    try:
        head = subprocess.run(
            ["git", "-C", str(here.parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        dirty = subprocess.run(
            ["git", "-C", str(here.parent), "status", "--porcelain", "--", here.name],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if head.returncode == 0:
            out["git_head"] = head.stdout.strip()
            out["script_uncommitted"] = bool(dirty.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def check_journal_overlap(journal: dict[str, Any], jobs: list[Job]) -> dict[str, list[str]]:
    """幂等验收：journal 里真写进去的行，现在又成了哪个 Job 的候选（应当一行都没有）。

    看的是"同一行被写两次"，不是待写入总数：新静止下来的 agent 会让 Job A 冒出新候选，
    那不是幂等失守。旧 journal 没有 ``applied`` 字段时按"全部已写"处理。
    """
    applied: dict[str, set[str]] = {}
    for j in journal.get("jobs", []):
        key = str(j.get("title", "")).split(".", 1)[0]
        applied[key] = {w["id"] for w in j.get("written", []) if w.get("applied", True)}
    overlap: dict[str, list[str]] = {}
    for job in jobs:
        if not job.enabled:
            continue
        key = job.title.split(".", 1)[0]
        again = sorted({r.row_id for r in job.written()} & applied.get(key, set()))
        if again:
            overlap[key] = again
    return overlap


def build_journal(
    args: argparse.Namespace,
    batch_ts: str,
    jobs: list[Job],
    fp_before: dict[str, Any],
    fp_after: dict[str, Any],
    written: int,
) -> dict[str, Any]:
    """journal = 唯一的恢复凭证：批次时刻 + 逐行写入内容 + 禁改列的前后指纹。

    逐行带 ``applied``：判定之后活体可能先把某行写上，SQL 守卫让路、这行实际没写。按
    journal 恢复时只能动 ``applied`` 为真的行，否则会抹掉活体的测量值。
    """
    return {
        "script": "backfill_token_usage.py",
        "script_provenance": script_provenance(),
        "db": str(args.db),
        "batch_ts": batch_ts,
        "metric": "usage_sum",
        "generated_at": utc_now().isoformat(),
        "options": {
            "include_leader": args.include_leader,
            "no_model": args.no_model,
            "projects_root": str(args.projects_root),
        },
        "forbidden_column": {
            "column": f"{FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]}",
            "before": fp_before,
            "after": fp_after,
            "unchanged": fp_before["sha256"] == fp_after["sha256"],
        },
        "rows_written": written,
        "jobs": [
            {
                "title": j.title,
                "table": j.table,
                "enabled": j.enabled,
                "counts": j.counts(),
                "written": [
                    {"id": r.row_id, "name": r.name, "values": r.values, "guard": r.guard,
                     "note": r.note, "applied": r.applied}
                    for r in j.written()
                ],
                "transcript_gone": [
                    {"id": r.row_id, "name": r.name, "path": r.note}
                    for r in j.by_reason("transcript_gone")
                ],
                # Job E 的窗口关闭存证：与 transcript_gone 同理，只增不减。
                "transcript_purged": [
                    {"id": r.row_id, "name": r.name, "cc_agent_id": r.note}
                    for r in j.by_reason("transcript_purged")
                ],
            }
            for j in jobs
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="目标库（默认生产库）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只出报告）")
    ap.add_argument("--journal", type=Path, help="--apply 必给：恢复凭证落点（已存在则硬拒）")
    ap.add_argument("--sample", type=int, default=30, help="每个 Job 抽样打印的行数")
    ap.add_argument("--seed", type=int, default=0, help="抽样随机种子（默认 0，可复现）")
    ap.add_argument(
        "--include-leader", action="store_true",
        help="额外执行 Job D（Leader 主会话回采）—— 语义与阶段4 的 snapshot 覆写相反，默认不做",
    )
    ap.add_argument(
        "--no-model", action="store_true",
        help="不做 model 观测回填（Job C 全禁 + Job A 不写 model 列）",
    )
    ap.add_argument(
        "--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT,
        help="Job E 寻址 transcript 的根目录（默认 ~/.claude/projects）",
    )
    ap.add_argument(
        "--check-journal", type=Path,
        help="幂等验收（只读）：报告 journal 里已写入的行有没有又成为候选，有则退出码 7",
    )
    ap.add_argument("--quiet", action="store_true", help="不打印解析进度")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"库不存在：{args.db}", file=sys.stderr)
        return 2
    if args.apply and args.check_journal is not None:
        print("❌ --check-journal 是只读验收，不能与 --apply 同用", file=sys.stderr)
        return 2
    if args.apply and args.journal is None:
        print("❌ --apply 必须给 --journal —— 那是唯一的恢复凭证", file=sys.stderr)
        return 2
    if args.journal is not None and args.journal.exists():
        print(
            f"❌ journal 目标文件已存在：{args.journal}\n"
            "   二次 apply 会把它覆盖成回采后的状态，恢复凭证就没了。换个文件名。",
            file=sys.stderr,
        )
        return 2

    rng = random.Random(args.seed)
    # 批次时刻：一次回采的全部行共享同一个值，这既是事实（就是这一刻测的），也是
    # 硬约束② 分窗的签名。走 aiteam.clock 的唯一时钟（I11 红线：库里不许有第二个
    # 时钟）；naive_utc_now 的落盘形态与 UtcDateTime 一致，两边天然对齐。
    batch_ts = naive_utc_now().isoformat(sep=" ")

    con = open_db(args.db, writable=args.apply)
    try:
        agents = load_agents(con)
        wa_rows = load_workflow_agents(con)
        e_candidates = load_pathless_workflow_agents(con)
        e_links = load_workflow_links(con) if e_candidates else {}
        cohorts = detect_backfill_cohorts(con)
        # 只供报告展示；apply 的比对基线在写锁之内另取，见下。
        fp_loaded = workflow_tokens_fingerprint(con)

        print(f"库：{args.db}")
        print(f"模式：{'APPLY（写入）' if args.apply else 'DRY-RUN（只读，mode=ro）'}")
        print("口径：usage_sum（按 requestId 分组取末条快照再跨组累加）—— 与 "
              "workflow_agents.tokens 的 ctx_last 口径实测差 5~25 倍，两者永不相加")
        print(f"批次时刻：{batch_ts}（本次全部写入行共享此值，即分窗签名）")
        print(f"agents {len(agents)} 行；workflow_agents {len(wa_rows)} 行；"
              f"已识别历史回采批次 {len(cohorts)} 个")
        print()
        print("开始解析 transcript……", file=sys.stderr)

        parser = Parser(verbose=not args.quiet)
        job_a = job_subagent_usage(agents, parser, batch_ts, with_model=not args.no_model)
        job_b = job_source_label(agents, parser)
        # 有候选才遍历 projects 树（上千份文件），没有候选的库一个目录都不打开。
        e_index = TranscriptIndex.build(args.projects_root) if e_candidates else None
        job_e = job_workflow_path_rebuild(
            e_candidates, e_links, e_index, parser, batch_ts, with_model=not args.no_model
        )
        # Job C 必须在 Job E 之后判定：同一轮就用上重建出的路径，否则重跑会冒出新写入。
        job_c = job_workflow_model(wa_rows, parser, job_e_rebuilt_paths(job_e))
        job_c.enabled = not args.no_model
        job_d = job_leader_usage(agents, parser, batch_ts, enabled=args.include_leader)
        jobs = [job_a, job_b, job_c, job_d, job_e]

        print(f"解析完成：{parser.parsed} 份文件 / {parser.bytes_read / 1e6:.1f} MB",
              file=sys.stderr)

        for job in jobs:
            print_job(job, args.sample, rng)
        print_job_e_summary(job_e)
        if not args.include_leader:
            print()
            print("  Job D 默认不执行（需 --include-leader）。上面只是它的判定预览。")
            print("  两条独立理由：① 47 个 Leader 行只指向 13 份 transcript，照单全收会把")
            print("  同一份用量重复计入最多 11 次；② 主会话是活文件，持续采集归阶段4（snapshot")
            print("  覆写语义），本脚本的'只写空列、写完不再更新'会把中途值永久冻成'已测量'。")

        print_transcript_gone(jobs)
        print_coverage(agents, cohorts, job_a, job_d, job_e)

        print()
        print("=" * 88)
        print("硬约束① workflow_agents.tokens 逐行未变（ctx_last 口径不得被 usage_sum 污染）")
        print("-" * 88)
        print(f"  写列白名单：{sorted(f'{t}.{c}' for t, c in WRITABLE_COLUMNS)}")
        print(f"  禁改列：{FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]} —— 不在白名单内，"
              f"apply 层的 assert 与前后指纹比对是第二、三道拦")
        print_fingerprint(fp_loaded, title="载入时指纹")

        planned = sum(len(j.written()) for j in jobs if j.enabled)
        print()
        print("=" * 88)
        print(f"待写入合计：{planned} 行"
              f"（agents {len(job_a.written()) + (len(job_d.written()) if job_d.enabled else 0)}"
              f" + tokens_source 补标 {len(job_b.written())}"
              f" + workflow_agents.model {len(job_c.written()) if job_c.enabled else 0}"
              f" + 路径重建 {len(job_e.written())}）")
        print("待写入分项：" + " ".join(
            f"{j.title.split('.', 1)[0]}={len(j.written()) if j.enabled else 0}" for j in jobs
        ))

        if not args.apply:
            print("dry-run 结束，一个字节都没写。确认无误后由缔造者执行 "
                  "--apply --journal <路径>（先备份）。")
            print("幂等验收看分项不看总数：--apply 之后用 --check-journal <journal> 重跑，"
                  "它只问'写过的行有没有又成为候选'。新静止下来的 agent 会让 Job A 冒出新候选，"
                  "那是新候选，不是幂等失守。")
            if args.check_journal is not None:
                overlap = check_journal_overlap(
                    json.loads(args.check_journal.read_text(encoding="utf-8")), jobs
                )
                print()
                print("=" * 88)
                print(f"幂等验收（对照 {args.check_journal}）")
                print("-" * 88)
                if overlap:
                    for key, ids in overlap.items():
                        print(f"  ❌ Job {key}：{len(ids)} 行已写入却又成了候选：{ids[:5]}")
                    return EXIT_NOT_IDEMPOTENT
                print("  ✓ journal 里写入的行一行都没有再成为候选")
            return 0

        written = 0
        try:
            con.execute("begin immediate")
        except sqlite3.OperationalError as exc:
            print(f"❌ {LOCK_TIMEOUT_SECONDS:.0f}s 内没拿到写锁（{exc}）：活体 API 正在大批"
                  "落盘。一个字节都没写，稍后重跑即可。", file=sys.stderr)
            return EXIT_DB_BUSY
        partial = args.journal.with_name(args.journal.name + ".partial")
        try:
            # 先拿写锁再拍已有路径的快照：快照与写入同在一个事务里，活体写入插不进来，
            # 比对出的差异只可能是本脚本自己造成的。
            # 两份基线都在写锁之内重取：载入时那份指纹之后，活体 workflow 还在更新
            # tokens（live tail 每轮都写），拿它比对会把活体写入误判成本脚本越界而回滚。
            fp_before = workflow_tokens_fingerprint(con)
            paths_before = stored_transcript_paths(con)
            for job in jobs:
                written += apply_job(con, job)
            paths_after = stored_transcript_paths(con)
            touched = sorted(rid for rid, p in paths_before.items() if paths_after.get(rid) != p)
            if touched:
                con.rollback()
                print()
                print(f"❌ 已有 transcript_path 被改动 {len(touched)} 行（Job E 只许补空列），"
                      f"整事务已回滚：{touched[:5]}", file=sys.stderr)
                return EXIT_ROLLED_BACK
            fp_after = workflow_tokens_fingerprint(con)
            if fp_after["sha256"] != fp_before["sha256"]:
                con.rollback()
                print()
                print("❌ 硬约束① 失守：workflow_agents.tokens 指纹变了，整事务已回滚。",
                      file=sys.stderr)
                print_fingerprint(fp_before, title="回采前")
                print_fingerprint(fp_after, title="回采后")
                return EXIT_ROLLED_BACK
            # journal 先落盘、再提交：凭证写不出来就不写库。提交后才改成正式文件名，
            # 于是正式文件名存在 = 这批写入确已提交。
            try:
                args.journal.parent.mkdir(parents=True, exist_ok=True)
                partial.write_text(
                    json.dumps(
                        build_journal(args, batch_ts, jobs, fp_before, fp_after, written),
                        ensure_ascii=False, indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                con.rollback()
                print(f"❌ journal 落不了盘（{exc}），整事务已回滚：没有恢复凭证就不写库。",
                      file=sys.stderr)
                return EXIT_JOURNAL_FAILED
            con.commit()
        except Exception:
            con.rollback()
            partial.unlink(missing_ok=True)
            raise

        # 回采后指纹同样用事务内那份（fp_after）：提交之后活体写入随时可能再动这一列，
        # 事后重取会让 journal 的 unchanged 凭证误报。
        print()
        print(f"已写入：{written} 行（判定候选 {planned} 行；"
              f"差额 = 期间已被活体写上的行，守卫让路是预期行为）")
        print_fingerprint(fp_before, title="回采前指纹")
        print_fingerprint(fp_after, title="回采后指纹")
        print(f"  ✓ 硬约束① 通过：{FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]} 逐行未变")

        try:
            os.replace(partial, args.journal)
        except OSError as exc:
            print(f"⚠ 写入已提交，但 journal 改名失败（{exc}）：凭证在 {partial}，请手动改名。",
                  file=sys.stderr)
            return 0
        print(f"journal 已落盘：{args.journal}")
        print(f"幂等验收：用 --check-journal {args.journal} 重跑，应报告没有行再成为候选。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
