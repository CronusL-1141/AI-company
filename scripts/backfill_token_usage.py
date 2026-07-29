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
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiteam.clock import naive_utc_now, utc_now  # noqa: E402
from aiteam.services.token_attribution import parse_transcript_usage  # noqa: E402

DEFAULT_DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"

# 本脚本**可能**写到的列的封闭集合。加一列必须同时改这里、改 I14 机检的期望集合，
# 于是"顺手多写一列"在评审时是可见的。(workflow_agents, tokens) 永远不在其中。
WRITABLE_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("agents", "input_tokens"),
        ("agents", "output_tokens"),
        ("agents", "cache_creation_tokens"),
        ("agents", "cache_read_tokens"),
        ("agents", "tokens_measured_at"),
        ("agents", "tokens_source"),
        ("agents", "model"),
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

REASONS: dict[str, str] = {
    "written": "可写入（dry-run 的候选 / --apply 的实际写入）",
    "already_measured": "tokens_measured_at 已有值 —— 幂等重跑必然全部落在这里，不重测不覆盖",
    "already_set": "目标列已有值且与观测一致",
    "no_transcript_path": "该行从未登记 transcript 路径，无从回采（历史行，新行已覆盖）",
    "transcript_gone": "路径有但文件已不在磁盘 —— 回采窗口已在这些行上关闭，只增不减",
    "unreadable_transcript": "文件在但解析不出任何 usage 快照（空文件/全损坏行）",
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

    def value_repr(self) -> str:
        if not self.values:
            return ""
        parts = []
        for k, v in self.values.items():
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
        con = sqlite3.connect(str(path))
    else:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_agents(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """
        select id, name, role, model, created_at, transcript_path,
               input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
               tokens_measured_at, tokens_source
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


# ---------------------------------------------------------------------------
# Job A —— 子 agent 的四层 token + tokens_measured_at + tokens_source
# ---------------------------------------------------------------------------
def job_subagent_usage(
    agents: list[sqlite3.Row], parser: Parser, batch_ts: str, *, with_model: bool = True
) -> Job:
    """主回采：``role != 'leader'`` 且 ``tokens_measured_at`` 为空的行。

    分支顺序即幂等的实现：``already_measured`` 排在最前，所以第二次跑的时候所有被写过
    的行在**读到文件之前**就已经出局——重跑既零变更也不重复解析 700 MB。
    """
    job = Job("A. agents 四层 token + tokens_measured_at + tokens_source（子 agent）", "agents")
    for a in agents:
        if a["role"] == "leader":
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
        usage = parser.usage(path)
        if not usage:
            job.rows.append(
                Row("agents", a["id"], a["name"], "unreadable_transcript", note=path)
            )
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
def job_workflow_model(rows: list[sqlite3.Row], parser: Parser) -> Job:
    """把 ``workflow_agents.model`` 里的**别名**换成 transcript 观测到的完整型号。

    这是本脚本唯一写 ``workflow_agents`` 的地方，写的是 ``model`` 列。``tokens`` 列不在
    白名单里、不在这个函数里、也过不了 apply 后的指纹比对——三道独立的拦。

    "只写空列"在这里的正确形态不是字面照搬：别名 ``opus`` 是**请求规格**（要什么模型），
    不是观测结果。用观测替换请求规格是补真，方向与"用推断覆盖观测"相反。所以判据是
    **不是完整型号才动**，且 SQL 守卫钉死"仍等于我读到的那个别名"——期间被别人写成完整
    型号就自动让路。
    """
    job = Job("C. workflow_agents.model ← transcript 观测（别名换真名）", "workflow_agents")
    for r in rows:
        current = r["model"] or ""
        label = r["label"] or r["cc_agent_id"] or ""
        if current.startswith(CONCRETE_MODEL_PREFIX):
            job.rows.append(Row("workflow_agents", r["id"], label, "already_concrete"))
            continue
        path = r["transcript_path"] or ""
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
    agents: list[sqlite3.Row], cohorts: set[str], job_a: Job, job_d: Job
) -> None:
    print()
    print("=" * 88)
    print("覆盖率分窗（硬约束②：历史回采与增量采集永远是两个数，不合并）")
    print("-" * 88)
    sub_before = measure_coverage(agents, cohorts, leader=False)
    lead_before = measure_coverage(agents, cohorts, leader=True)
    sub_after = project_coverage(sub_before, len(job_a.written()))
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
        n += cur.rowcount
    return n


def build_journal(
    args: argparse.Namespace,
    batch_ts: str,
    jobs: list[Job],
    fp_before: dict[str, Any],
    fp_after: dict[str, Any],
    written: int,
) -> dict[str, Any]:
    """journal = 唯一的恢复凭证：批次时刻 + 逐行写入内容 + 禁改列的前后指纹。"""
    return {
        "script": "backfill_token_usage.py",
        "db": str(args.db),
        "batch_ts": batch_ts,
        "metric": "usage_sum",
        "generated_at": utc_now().isoformat(),
        "options": {
            "include_leader": args.include_leader,
            "no_model": args.no_model,
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
                    {"id": r.row_id, "name": r.name, "values": r.values, "guard": r.guard}
                    for r in j.written()
                ],
                "transcript_gone": [
                    {"id": r.row_id, "name": r.name, "path": r.note}
                    for r in j.by_reason("transcript_gone")
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
    ap.add_argument("--quiet", action="store_true", help="不打印解析进度")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"库不存在：{args.db}", file=sys.stderr)
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
        cohorts = detect_backfill_cohorts(con)
        fp_before = workflow_tokens_fingerprint(con)

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
        job_c = job_workflow_model(wa_rows, parser)
        job_c.enabled = not args.no_model
        job_d = job_leader_usage(agents, parser, batch_ts, enabled=args.include_leader)
        jobs = [job_a, job_b, job_c, job_d]

        print(f"解析完成：{parser.parsed} 份文件 / {parser.bytes_read / 1e6:.1f} MB",
              file=sys.stderr)

        for job in jobs:
            print_job(job, args.sample, rng)
        if not args.include_leader:
            print()
            print("  Job D 默认不执行（需 --include-leader）。上面只是它的判定预览。")
            print("  两条独立理由：① 47 个 Leader 行只指向 13 份 transcript，照单全收会把")
            print("  同一份用量重复计入最多 11 次；② 主会话是活文件，持续采集归阶段4（snapshot")
            print("  覆写语义），本脚本的'只写空列、写完不再更新'会把中途值永久冻成'已测量'。")

        print_transcript_gone(jobs)
        print_coverage(agents, cohorts, job_a, job_d)

        print()
        print("=" * 88)
        print("硬约束① workflow_agents.tokens 逐行未变（ctx_last 口径不得被 usage_sum 污染）")
        print("-" * 88)
        print(f"  写列白名单：{sorted(f'{t}.{c}' for t, c in WRITABLE_COLUMNS)}")
        print(f"  禁改列：{FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]} —— 不在白名单内，"
              f"apply 层的 assert 与前后指纹比对是第二、三道拦")
        print_fingerprint(fp_before, title="回采前指纹")

        planned = sum(len(j.written()) for j in jobs if j.enabled)
        print()
        print("=" * 88)
        print(f"待写入合计：{planned} 行"
              f"（agents {len(job_a.written()) + (len(job_d.written()) if job_d.enabled else 0)}"
              f" + tokens_source 补标 {len(job_b.written())}"
              f" + workflow_agents.model {len(job_c.written()) if job_c.enabled else 0}）")

        if not args.apply:
            print("dry-run 结束，一个字节都没写。确认无误后由缔造者执行 "
                  "--apply --journal <路径>（先备份）。")
            print("幂等验收：--apply 之后重跑本脚本，待写入应为 0 行"
                  "（全部落进 already_measured / already_set / already_concrete）。")
            return 0

        written = 0
        try:
            for job in jobs:
                written += apply_job(con, job)
            fp_after = workflow_tokens_fingerprint(con)
            if fp_after["sha256"] != fp_before["sha256"]:
                con.rollback()
                print()
                print("❌ 硬约束① 失守：workflow_agents.tokens 指纹变了，整事务已回滚。",
                      file=sys.stderr)
                print_fingerprint(fp_before, title="回采前")
                print_fingerprint(fp_after, title="回采后")
                return 4
            con.commit()
        except Exception:
            con.rollback()
            raise

        fp_final = workflow_tokens_fingerprint(con)
        print()
        print(f"已写入：{written} 行（判定候选 {planned} 行；"
              f"差额 = 期间已被活体写上的行，守卫让路是预期行为）")
        print_fingerprint(fp_before, title="回采前指纹")
        print_fingerprint(fp_final, title="回采后指纹")
        print(f"  ✓ 硬约束① 通过：{FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]} 逐行未变")

        args.journal.parent.mkdir(parents=True, exist_ok=True)
        args.journal.write_text(
            json.dumps(
                build_journal(args, batch_ts, jobs, fp_before, fp_final, written),
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"journal 已落盘：{args.journal}")
        print("重跑本脚本应报告 0 待写入 —— 这就是幂等验收。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
