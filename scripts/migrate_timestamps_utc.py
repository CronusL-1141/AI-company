#!/usr/bin/env python3
"""存量时间戳平移：本地墙钟 → UTC。默认 dry-run。

规格见 docs/utc-unification-design.md。简述：这个库过去跑着两个墙钟——核心域写
宿主本地时间，ecosystem 域写 UTC——而 SQLite 落库把 offset 静默剥掉，两制的行长得
一模一样。代码侧已统一为 UTC（``aiteam.clock``），本脚本负责把**已经写进去的**
本地墙钟值换算成 UTC。

四件事决定了这个脚本的形状：

1. **按列平移 + 逐行排除。** 一个列的写入方在库的绝大部分生命期内口径恒定（核心域
   一直写本地），但新代码合入后**任何新起的进程都写 UTC**，所以列内会混入少数已经
   是 UTC 的行。整列平移的同时必须把这些行逐行挑出来排除——它们已经是目标态，再减
   一个时区就永久错 16 小时。检测在执行时刻对全库现算（见 :func:`detect_contamination`），
   不吃任何固定行号清单：污染量随时间增长，昨天的清单今天就是错的。
2. **判不了的行不许静默平移。** 能被证据判定的行（口径确定）自动处理；判不了的逐行
   进 journal 的 undecidable 清单，必须人审后加 ``--ack-undecidable`` 才放行。
3. **只许平移一次。** 成功 ``--apply`` 后在库上打幂等标记（``PRAGMA user_version``）；
   再次 ``--apply`` 硬拒。``--journal`` 目标文件已存在同样硬拒——那是唯一的恢复凭证，
   被覆盖就等于销毁。
4. **可逆且无损。** 平移是纯值变换，亚秒位原样保留（见 :func:`shift_expr`）；
   ``--rollback`` 按 journal 记录的位移量与排除清单往回搬，排除行两次都不动。

用法::

    python3 scripts/migrate_timestamps_utc.py                    # dry-run 全表报告
    python3 scripts/migrate_timestamps_utc.py --db /tmp/copy.db  # 对副本演练
    python3 scripts/migrate_timestamps_utc.py --apply --journal ~/utc-journal.json
    python3 scripts/migrate_timestamps_utc.py --rollback --journal ~/utc-journal.json

``--apply`` / ``--rollback`` 由缔造者亲自执行，执行前先备份::

    cp ~/.claude/data/ai-team-os/aiteam.db ~/aiteam.db.bak-utc-$(date +%Y%m%d%H%M%S)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"

# ---------------------------------------------------------------------------
# 待平移列清单 —— 与 docs/utc-unification-design.md §3.1 逐条对应。
#
# 判定依据是写入方代码（核心域一律 datetime.now() 宿主本地），并由生产库实测复核：
# 各表 max(值) 均等于取证时刻的**本地**墙钟，而 ecosystem 域各表 max 落在 UTC 侧，
# 两域分布无交叠。ecosystem_* 与 pipeline_stage_history 本就写 UTC，**不在此列**。
# ---------------------------------------------------------------------------
LOCAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": ("created_at", "updated_at"),
    "phases": ("created_at", "updated_at"),
    "teams": ("created_at", "updated_at", "completed_at"),
    "agents": ("created_at", "last_active_at", "ctx_measured_at", "tokens_measured_at"),
    "tasks": ("created_at", "started_at", "completed_at"),
    "task_memos": ("created_at", "invalid_at"),
    "memories": ("created_at", "accessed_at", "invalid_at"),
    "events": ("timestamp",),
    "meetings": ("created_at", "concluded_at"),
    "meeting_messages": ("timestamp",),
    "agent_activities": ("timestamp",),
    "scheduled_tasks": ("created_at", "last_run_at", "next_run_at"),
    "cross_messages": ("created_at", "read_at"),
    "wake_sessions": ("started_at", "finished_at"),
    "leader_briefings": ("created_at", "resolved_at"),
    "channel_messages": ("created_at",),
    "reports": ("created_at",),
    "workflow_runs": (
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "last_activity_at",
    ),
    "workflow_agents": (
        "created_at",
        "updated_at",
        "started_at",
        "queued_at",
        "last_activity_at",
    ),
    "knowledge_links": ("created_at",),
}

# 每张表的"出生列"：插入时一次写定、此后再不更新。它是**唯一**能用 rowid（物理写入
# 序）做单调性检测的列——rowid 递增即写入序递增，值相对前缀最大值倒退约一个时区就是
# UTC 写入的指纹。其余列都会被 UPDATE 原地改写，rowid 与值的时序脱钩，只能靠跨列预言
# 机判定。哪列属于哪类**不是启发式，是写入方代码的事实**，改 ORM 时必须同步这里。
ROW_BIRTH_COLUMN: dict[str, str] = {
    "projects": "created_at",
    "phases": "created_at",
    "teams": "created_at",
    "agents": "created_at",
    "tasks": "created_at",
    "task_memos": "created_at",
    "memories": "created_at",
    "events": "timestamp",
    "meetings": "created_at",
    "meeting_messages": "timestamp",
    "agent_activities": "timestamp",
    "scheduled_tasks": "created_at",
    "cross_messages": "created_at",
    "wake_sessions": "started_at",
    "leader_briefings": "created_at",
    "channel_messages": "created_at",
    "reports": "created_at",
    "workflow_runs": "created_at",
    "workflow_agents": "created_at",
    "knowledge_links": "created_at",
}

# 保证"不早于出生列"的列 —— 它们记的是"这一行后来发生了什么"，所以同行的
# ``出生列 <= 本列`` 是**结构性不变量**，可以双向当预言机用（既约束本列，也给出生列
# 一个上界）。名单是白名单不是黑名单，因为反例是实打实存在的：
#   · scheduled_tasks.next_run_at 记的是未来时刻；
#   · workflow_runs/agents 的 started_at / queued_at / completed_at 来自 CC 状态文件的
#     epoch，实测最早到 2026-06-23，比该行落库的 created_at(2026-07-06) 早好几周。
# 把这些列塞进来，预言机会对着一条假不变量下判断。
AFTER_BIRTH_COLUMNS: set[tuple[str, str]] = {
    ("projects", "updated_at"),
    ("phases", "updated_at"),
    ("teams", "updated_at"),
    ("teams", "completed_at"),
    ("agents", "last_active_at"),
    ("agents", "ctx_measured_at"),
    ("agents", "tokens_measured_at"),
    ("tasks", "started_at"),
    ("tasks", "completed_at"),
    ("task_memos", "invalid_at"),
    ("memories", "accessed_at"),
    ("memories", "invalid_at"),
    ("meetings", "concluded_at"),
    ("scheduled_tasks", "last_run_at"),
    ("cross_messages", "read_at"),
    ("wake_sessions", "finished_at"),
    ("leader_briefings", "resolved_at"),
    ("workflow_runs", "updated_at"),
    ("workflow_runs", "last_activity_at"),
    ("workflow_agents", "updated_at"),
    ("workflow_agents", "last_activity_at"),
}

# 冻结/敏感对象——照样平移（列本身不是冻结对象），但单独留档并在报告里点名。
# tasks.config 里的旧 memo JSON 才是冻结档案，本脚本一个字节都不碰它。
FROZEN_NOTE = {
    "tasks": "tasks.config.memo 是冻结档案（CLAUDE.md），本脚本只动 DATETIME 列，不碰 JSON",
    "task_memos": "记忆 v2 升表后的活数据；旧 JSON 档案留在 tasks.config 内，不动",
    "events": "只可追加账本（无修改面）；本次是值换算，不增删行",
    "memories": "方向层记忆；invalid_at 是失效轴，一并平移以免失效判定错位",
}

# 明确不动的对象及理由 —— 报告里原样打印，免得下一个人重新调查一遍。
EXCLUDED = [
    ("ecosystem_* (15 张表 / 36 列)", "本就以 UTC 写入，已是目标态"),
    ("pipeline_stage_history.transitioned_at", "同上，ecosystem 侧 UTC"),
    ("ecosystem_repo_profiles.pushed_at / last_commit_at", "值来自 GitHub API，本就是真 UTC"),
    ("governance_lease.expires_at / updated_at", "VARCHAR，存的就是带 +00:00 的 isoformat"),
    ("loop_states.updated_at", "退役 cron 引擎遗留死表，全仓零写入方"),
    ("events.data 内嵌时间戳", "事件载荷是 hook 当时上报了什么的原样记录，改它等于篡改证词"),
    ("tasks.config.memo 内嵌时间戳", "冻结档案（CLAUDE.md 明令不清理不写入）"),
    ("reports.content / task_memos.content 正文内的时间", "是人写的文字，不是数据"),
    ("检测判定为已是 UTC 的行", "新代码写入的行已在目标态，逐行排除（见报告「逐行口径判定」栏）"),
]

# 全库判定的容差：活着的系统里"最近一次写入"距今不会超过这个量。设得比一次
# 会话间隔宽松，但远小于一个时区偏移，才分得开"贴本地 now"和"贴 UTC now"。
GUARD_TOLERANCE = timedelta(hours=3)

# 时区指纹容差：一个值要被认定为"同一瞬刻的另一种口径表达"，它与预期位置的偏差必须
# 小于这个量。给到分钟级而不是小时级是有实测依据的——生产库 task_memos.created_at 里
# 存在 7.1~8.7 小时的**合法**乱序（记忆 v2 升表时的批量回填），小时级容差会把它们
# 误认成时区指纹。
SIGNATURE_TOLERANCE = timedelta(minutes=5)

# 口径见证取样：只看最近这么多行。用中位数而不是 max —— max 天生只反映极少数最新行，
# 少数派混入永远进不了 max，这正是旧护栏对"混口径"结构性失明的根因。
WITNESS_TAIL = 200

# 幂等标记：成功平移后写进 PRAGMA user_version。选它是因为侵入最小——不建表、不加列、
# 不产生任何业务可见对象，且 SQLAlchemy / 本仓 storage 层从不读写它（实测生产库为 0）。
MIGRATION_MARKER = 20260728


# ---------------------------------------------------------------------------
def shift_for(naive_local: datetime) -> timedelta:
    """把一个 naive 的本地墙钟值换算成 UTC 所需的位移（UTC+8 上是 −8h）。

    用系统时区库按**当时的日期**求偏移，而不是写死 −8：写死会在有夏令时的机器上
    把半年的数据搬错。Asia/Shanghai 1991 年后无夏令时，所以本机实际恒为 −8——但
    这一点由 :func:`assert_shift_is_constant` 去**证明**，而不是假设。
    """
    return naive_local.astimezone(UTC).replace(tzinfo=None) - naive_local


def parse_db_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class Contamination:
    """一列的逐行口径判定结果。

    ``excluded`` 是**已经是 UTC**的行（新代码写的），平移与回滚都必须绕开它们；
    ``undecidable`` 是证据不足以判定口径的行，交人审。
    """

    def __init__(
        self,
        key: str,
        suspects: int = 0,
        excluded: list[int] | None = None,
        undecidable: list[dict] | None = None,
        method: str = "未检测",
    ) -> None:
        self.key = key
        self.suspects = suspects
        self.excluded: list[int] = excluded or []
        self.undecidable: list[dict] = undecidable or []
        self.method = method

    @property
    def excluded_set(self) -> set[int]:
        return set(self.excluded)

    def as_journal(self) -> dict:
        return {
            "suspects": self.suspects,
            "method": self.method,
            "excluded_rowids": self.excluded,
            "undecidable": self.undecidable,
        }


class ColumnReport:
    def __init__(self, table: str, column: str) -> None:
        self.table = table
        self.column = column
        self.rows = 0
        self.non_null = 0
        self.min_before: str | None = None
        self.max_before: str | None = None
        self.samples: list[str] = []
        self.guard: str = "n/a"
        self.guard_ok = True
        # 追加型列（出生列）才有 rowid 单调性可用，也才有资格当口径见证。
        self.is_birth = ROW_BIRTH_COLUMN.get(table) == column
        self.tail_median: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"

    def as_journal(self) -> dict:
        return {
            "table": self.table,
            "column": self.column,
            "non_null": self.non_null,
            "min_before": self.min_before,
            "max_before": self.max_before,
            "samples_before": self.samples,
            "guard": self.guard,
        }


def shift_expr(column: str, hours: float) -> str:
    """位移表达式 —— **保留亚秒精度**。

    直接 ``datetime(col, '-8 hours')`` 会把结果截到整秒，等于顺手抹掉 32 万行的
    微秒位。而事件账本按 timestamp 排序，同一秒内的先后就靠那几位。

    因为位移量必然是整分钟（现存所有时区的偏移都是整分钟，含 +05:30 / +05:45），
    所以只搬"到分钟"的前缀，秒与亚秒位原样接回去：

        "2026-07-28 17:01:25.068573"
         └─ 前 16 位 ─┘└─ 第 17 位起原样保留 ─┘
    """
    minutes = round(hours * 60)
    sign = "+" if minutes >= 0 else "-"
    shifted_prefix = (
        f"datetime(substr({column}, 1, 16) || ':00', '{sign}{abs(minutes)} minutes')"
    )
    return f"substr({shifted_prefix}, 1, 16) || substr({column}, 17)"


def assert_uniform_width(
    conn: sqlite3.Connection, reports: list[ColumnReport], hours: float = -8.0
) -> None:
    """确认每个待平移值都能被 :func:`shift_expr` 安全切分。

    两道检查，缺一不可：

    * 宽度：短于 ``YYYY-MM-DD HH:MM`` 的值一定被前缀切分毁掉；
    * **直接判据**：拿真正要执行的表达式跑一遍，凡是"原值非空但结果为 NULL"的行都
      会被静默清空。宽度够并不等于能解析（纪元浮点串、非法月日、斜杠格式都长于 16
      位却照样清成 NULL），所以宽度检查是必要不充分条件，真正管用的是这一条。
    """
    bad: list[str] = []
    for rep in reports:
        if rep.non_null == 0:
            continue
        n = conn.execute(
            f"select count(*) from {rep.table} "
            f"where {rep.column} is not null and length({rep.column}) < 16"
        ).fetchone()[0]
        if n:
            bad.append(f"{rep.key}: {n} 行的值短于 'YYYY-MM-DD HH:MM'")
        n = conn.execute(
            f"select count(*) from {rep.table} where {rep.column} is not null "
            f"and ({shift_expr(rep.column, hours)}) is null"
        ).fetchone()[0]
        if n:
            bad.append(f"{rep.key}: {n} 行经位移表达式后变成 NULL（值形态非法）")
    if bad:
        raise SystemExit(
            "❌ 存在无法安全切分的时间值，拒绝执行（继续做会把它们清成 NULL）:\n  "
            + "\n  ".join(bad)
        )


# ---------------------------------------------------------------------------
# 混口径检测
#
# 背景（实测，非推演）：UTC 新代码已合入 master 且 aiteam 是 editable 安装，任何在
# 合并之后新起的进程（新会话的 MCP server、hook 子进程、CLI、被自动拉活的 API）写的
# 都是 UTC。于是同一列里会同时存在两种口径的行，而且污染量随时间单调增长——所以检测
# 必须在执行时刻现算。
#
# 判据的骨架是一条硬约束：一个 UTC 写入的值 v，其真实时刻是 v + offset，而这个真实
# 时刻必须落在 [T0, now] 之内（T0 = 新代码最早可能运行的时刻）。反过来说，
#
#     只有落在 [T0 − offset, now − offset] 这条**风险带**里的值才有可能是 UTC 写的。
#
# 带外的值一律**证明**为本地口径，照常平移。带内的值再逐行判：
#
# * 追加型列（出生列）：rowid 即写入序。取该行前面所有带外行的最大值当下界、后面第一
#   个带外行的值当上界，把 v 与 v+offset 两种读法分别套进去——只有一种读法自洽时判定
#   成立，两种都自洽或都不自洽则进 undecidable。
# * 原地更新列：拿同行的出生列当锚，用"更新不可能早于创建"这条不变量判。
# ---------------------------------------------------------------------------
def risk_band(t0: datetime, now_local: datetime, forward_hours: float) -> tuple[datetime, datetime]:
    """风险带：只有落在这里的**原始值**才可能是 UTC 写入的，带外一律证明为本地。

    上沿贴的是**执行时刻**而不是库内最新写入——一次 UTC 写入完全可能发生在最后一条
    本地行之后，拿库内 max 收口会漏掉它。代价是对着一份放久了的快照跑 dry-run，带宽
    会随墙钟一起长、候选行跟着变多；这不是误报，是"拖得越久越难判"的如实反映。
    """
    offset = timedelta(hours=-forward_hours)
    edges = (t0 - offset, now_local - offset)
    return min(edges), max(edges)


def _scan(conn: sqlite3.Connection, table: str, column: str) -> list[tuple[int, datetime]]:
    rows = conn.execute(
        f"select rowid, {column} from {table} where {column} is not null order by rowid"
    ).fetchall()
    out: list[tuple[int, datetime]] = []
    for rid, raw in rows:
        parsed = parse_db_ts(raw)
        if parsed is not None:
            out.append((int(rid), parsed))
    return out


def detect_utc_writer_since(
    conn: sqlite3.Connection, reports: list[ColumnReport], offset: timedelta
) -> tuple[datetime | None, list[dict]]:
    """取证：库里最早的一次 UTC 写入发生在什么时候（本地墙钟表达）。

    只在追加型列上找**时区指纹式乱序**——值相对写入序前缀的最大值倒退了恰好一个
    offset（东半球；西半球则是超前于后继行）。容差按分钟给，见 :data:`SIGNATURE_TOLERANCE`。

    返回 (T0, 证据行)。T0 为 None 表示全库没有任何 UTC 写入的痕迹。
    """
    evidence: list[dict] = []
    for rep in reports:
        if not rep.is_birth or rep.non_null == 0:
            continue
        rows = _scan(conn, rep.table, rep.column)
        if offset > timedelta(0):
            # 东半球：UTC 值比同刻的本地值早，表现为 rowid 序上的倒退。
            running: datetime | None = None
            for rid, v in rows:
                if running is not None and abs((running - v) - offset) <= SIGNATURE_TOLERANCE:
                    evidence.append(
                        {
                            "table": rep.table,
                            "column": rep.column,
                            "rowid": rid,
                            "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                            "real_time": (v + offset).strftime("%Y-%m-%d %H:%M:%S.%f"),
                        }
                    )
                if running is None or v > running:
                    running = v
        else:
            # 西半球（含 UTC 本身不会走到这里）：UTC 值反而更晚，表现为超前于后继行。
            following: datetime | None = None
            for rid, v in reversed(rows):
                if following is not None and abs((v - following) + offset) <= SIGNATURE_TOLERANCE:
                    evidence.append(
                        {
                            "table": rep.table,
                            "column": rep.column,
                            "rowid": rid,
                            "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                            "real_time": (v + offset).strftime("%Y-%m-%d %H:%M:%S.%f"),
                        }
                    )
                if following is None or v < following:
                    following = v
    if not evidence:
        return None, []
    t0 = min(datetime.strptime(e["real_time"], "%Y-%m-%d %H:%M:%S.%f") for e in evidence)
    return t0, evidence


def _classify_birth_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    band: tuple[datetime, datetime],
    offset: timedelta,
) -> tuple[list[int], list[dict], int]:
    """追加型列的逐行判定：用 rowid 邻居 + 同行后继列把两种读法卡到只剩一种。

    两个证据源：

    * **rowid 邻居**（下界取前面所有带外行的最大值，上界取后面第一个带外行）——rowid
      就是物理写入序，一行的真实时刻必然夹在两者之间。
    * **同行后继列**（:data:`AFTER_BIRTH_COLUMNS`）——出生时刻不可能晚于这一行后来
      被更新的时刻。只采信落在风险带**之外**的后继列值（那些已被证明是本地口径），
      所以不存在循环依赖。
    """
    lo, hi = band
    siblings = [
        c
        for c in LOCAL_COLUMNS.get(table, ())
        if (table, c) in AFTER_BIRTH_COLUMNS
        and c in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    ]
    picked = ", ".join([column, *siblings])
    raw_rows = conn.execute(
        f"select rowid, {picked} from {table} where {column} is not null order by rowid"
    ).fetchall()
    rows: list[tuple[int, datetime]] = []
    ceiling: dict[int, datetime] = {}
    for record in raw_rows:
        parsed = parse_db_ts(record[1])
        if parsed is None:
            continue
        rid = int(record[0])
        rows.append((rid, parsed))
        for value in record[2:]:
            sib = parse_db_ts(value)
            # 带内的后继列自己口径未定，不能拿来当证据。
            if sib is None or lo <= sib <= hi:
                continue
            if rid not in ceiling or sib < ceiling[rid]:
                ceiling[rid] = sib

    suspect = [lo <= v <= hi for _, v in rows]
    n = len(rows)

    prev_clean: list[datetime | None] = [None] * n
    running: datetime | None = None
    for i, (_, v) in enumerate(rows):
        prev_clean[i] = running
        if not suspect[i] and (running is None or v > running):
            running = v

    next_clean: list[datetime | None] = [None] * n
    following: datetime | None = None
    for i in range(n - 1, -1, -1):
        next_clean[i] = following
        if not suspect[i]:
            following = rows[i][1]

    excluded: list[int] = []
    undecidable: list[dict] = []
    suspects = 0
    for i in range(n):
        if not suspect[i]:
            continue
        suspects += 1
        rid, v = rows[i]
        low = prev_clean[i]
        high = next_clean[i]
        roof = ceiling.get(rid)
        if roof is not None and (high is None or roof < high):
            high = roof

        def fits(moment: datetime, low: datetime | None = low, high: datetime | None = high) -> bool:
            if low is not None and moment < low - SIGNATURE_TOLERANCE:
                return False
            return not (high is not None and moment > high + SIGNATURE_TOLERANCE)

        local_ok, utc_ok = fits(v), fits(v + offset)
        if utc_ok and not local_ok:
            excluded.append(rid)
        elif local_ok and not utc_ok:
            continue
        else:
            undecidable.append(
                {
                    "rowid": rid,
                    "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "reason": "两种读法都自洽" if local_ok else "两种读法都与邻居冲突",
                }
            )
    return excluded, undecidable, suspects


def _classify_updated_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    birth_column: str,
    birth_excluded: set[int],
    birth_pending: set[int],
    band: tuple[datetime, datetime],
    offset: timedelta,
) -> tuple[list[int], list[dict], int]:
    """原地更新列的逐行判定：跨列一致性预言机。

    rowid 对这类列没有意义（值会被 UPDATE 原地改写，写入序与值的时序脱钩），唯一可用
    的是不变量 **同一行的更新时刻不可能早于它的创建时刻**：拿同行的出生列当锚，把两种
    读法套进去——更新值比创建时刻早了将近一个 offset，就只能是 UTC 口径。

    这条不变量只对 :data:`AFTER_BIRTH_COLUMNS` 成立，其余列（未来时刻、外部 epoch
    来源）没有可用预言机，带内行一律交人审。
    """
    lo, hi = band
    excluded: list[int] = []
    undecidable: list[dict] = []
    suspects = 0
    usable = (table, column) in AFTER_BIRTH_COLUMNS
    rows = conn.execute(
        f"select rowid, {column}, {birth_column} from {table} where {column} is not null"
    ).fetchall()
    for raw_rid, raw, birth_raw in rows:
        v = parse_db_ts(raw)
        if v is None or not (lo <= v <= hi):
            continue
        suspects += 1
        rid = int(raw_rid)
        if not usable:
            undecidable.append(
                {
                    "rowid": rid,
                    "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "reason": "该列可能早于创建时刻（未来时刻或外部 epoch 来源），无可用不变量",
                }
            )
            continue
        anchor = parse_db_ts(birth_raw)
        if anchor is None:
            undecidable.append({"rowid": rid, "value": str(raw), "reason": "锚列为空，无从对照"})
            continue
        if rid in birth_pending:
            undecidable.append(
                {
                    "rowid": rid,
                    "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "anchor": f"{birth_column}={anchor:%Y-%m-%d %H:%M:%S}",
                    "reason": "锚列自己的口径就待人审，不能拿来定案",
                }
            )
            continue
        # 锚列本身若被判为 UTC，它的真实创建时刻是 anchor + offset。
        born = anchor + offset if rid in birth_excluded else anchor
        local_ok = v >= born - SIGNATURE_TOLERANCE
        utc_ok = (v + offset) >= born - SIGNATURE_TOLERANCE
        if utc_ok and not local_ok:
            excluded.append(rid)
        elif local_ok and not utc_ok:
            continue
        else:
            undecidable.append(
                {
                    "rowid": rid,
                    "value": v.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "anchor": f"{birth_column}={anchor:%Y-%m-%d %H:%M:%S}",
                    "reason": "两种读法都不违反「更新不早于创建」" if local_ok else "两种读法都与锚冲突",
                }
            )
    return excluded, undecidable, suspects


def detect_contamination(
    conn: sqlite3.Connection,
    reports: list[ColumnReport],
    forward_hours: float,
    now_local: datetime,
    since: datetime | None = None,
) -> tuple[dict[str, Contamination], datetime | None, list[dict]]:
    """全库逐行口径检测。执行时刻现算，不吃任何固定行号清单。

    返回 (逐列结果, T0, 取证行)。``since`` 给定时覆盖自动取证的 T0（运维手册里
    应当填新代码合入的那一刻——那是 UTC 写入方最早可能出现的时间）。
    """
    offset = timedelta(hours=-forward_hours)
    found: dict[str, Contamination] = {r.key: Contamination(key=r.key) for r in reports}

    t0, evidence = detect_utc_writer_since(conn, reports, offset)
    if since is not None:
        t0 = since if t0 is None else min(t0, since)
    if t0 is None:
        for c in found.values():
            c.method = "无 UTC 写入痕迹（风险带为空）"
        return found, None, evidence

    band = risk_band(t0, now_local, forward_hours)

    for rep in reports:
        if rep.non_null == 0:
            found[rep.key].method = "空列"
            continue
        birth_column = ROW_BIRTH_COLUMN.get(rep.table)
        if rep.is_birth:
            excluded, undecidable, suspects = _classify_birth_column(
                conn, rep.table, rep.column, band, offset
            )
            method = "rowid 单调性 + 同行后继列（追加型列）"
        elif birth_column is None:
            found[rep.key].method = "无出生列可锚定，全部候选行交人审"
            continue
        else:
            birth = found[f"{rep.table}.{birth_column}"]
            excluded, undecidable, suspects = _classify_updated_column(
                conn,
                rep.table,
                rep.column,
                birth_column,
                birth.excluded_set,
                {u["rowid"] for u in birth.undecidable},
                band,
                offset,
            )
            method = f"跨列一致性（锚 {birth_column}）"
        entry = found[rep.key]
        entry.suspects = suspects
        entry.excluded = sorted(excluded)
        entry.undecidable = undecidable
        entry.method = method

    # 取证行必定是 UTC 行；即便邻居判定因为数据稀疏没能定案，也要并进排除集。
    for e in evidence:
        entry = found.get(f"{e['table']}.{e['column']}")
        if entry is None:
            continue
        if e["rowid"] not in entry.excluded:
            entry.excluded = sorted([*entry.excluded, e["rowid"]])
        entry.undecidable = [u for u in entry.undecidable if u["rowid"] != e["rowid"]]
    return found, t0, evidence


def all_undecidable(contamination: dict[str, Contamination]) -> list[dict]:
    out: list[dict] = []
    for key, entry in contamination.items():
        for row in entry.undecidable:
            out.append({"column": key, **row})
    return out


# ---------------------------------------------------------------------------
def collect(conn: sqlite3.Connection, now_local: datetime) -> list[ColumnReport]:
    reports: list[ColumnReport] = []
    existing = {
        r[0] for r in conn.execute("select name from sqlite_master where type='table'")
    }
    now_utc_naive = now_local.astimezone(UTC).replace(tzinfo=None)

    for table, columns in LOCAL_COLUMNS.items():
        if table not in existing:
            print(f"⚠️  表 {table} 不在库中，跳过", file=sys.stderr)
            continue
        table_rows = conn.execute(f"select count(*) from {table}").fetchone()[0]
        cols_present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            rep = ColumnReport(table, column)
            rep.rows = table_rows
            if column not in cols_present:
                print(f"⚠️  列 {table}.{column} 不在库中，跳过", file=sys.stderr)
                continue
            cnt, lo, hi = conn.execute(
                f"select count({column}), min({column}), max({column}) from {table}"
            ).fetchone()
            rep.non_null = cnt
            rep.min_before, rep.max_before = lo, hi
            rep.samples = [
                str(r[0])
                for r in conn.execute(
                    f"select {column} from {table} where {column} is not null "
                    f"order by {column} desc limit 3"
                )
            ]
            if rep.is_birth and cnt:
                tail = [
                    parsed
                    for r in conn.execute(
                        f"select {column} from {table} where {column} is not null "
                        f"order by rowid desc limit {WITNESS_TAIL}"
                    )
                    if (parsed := parse_db_ts(str(r[0]))) is not None
                ]
                if tail:
                    rep.tail_median = sorted(tail)[len(tail) // 2]

            # ---- 逐列自证（单边判据，见 verdict() 的说明）----
            newest = parse_db_ts(hi)
            if newest is None:
                rep.guard = "空列（无行可判）"
            elif newest > now_utc_naive:
                # UTC 时钟不可能写出未来的值，所以这一列**必然**含本地墙钟写入的行。
                # 这是正面确证，不是启发式。
                rep.guard = f"确证本地（最新行 {str(hi)[:19]} 晚于 UTC 此刻 {now_utc_naive:%H:%M:%S}）"
            else:
                age = now_local - newest
                rep.guard = f"冷数据（最新行距今 {age.days} 天 {age.seconds // 3600} 小时，无法自证）"
            reports.append(rep)
    return reports


def verdict(
    reports: list[ColumnReport],
    now_local: datetime,
    contamination: dict[str, Contamination] | None = None,
    ack_undecidable: bool = False,
) -> tuple[bool, list[str]]:
    """全库级判定：这个库现在还是不是本地墙钟在写，混进来的 UTC 行有没有被识别。

    判定放在**库级**而不是列级：真正的风险是部署顺序颠倒，而部署是全局的。一个冷了
    三天的列本来就没有新鲜行可判，逐列去猜只会对冷数据制造假警报。

    口径见证用**最新鲜的那个追加型列的尾部中位数**，两点讲究：

    * 只有追加型列（出生列）有资格作证——它的 rowid 就是写入序，"最近这批写入"这个
      概念对它才成立；原地更新列的 max 可能来自任何一行的任何一次更新。
    * 用中位数而不是 max。max 天生只反映极少数最新行：库已整体平移成 UTC、旧代码又
      补写了一行本地值时，那一行就足以让 max 贴住本地此刻——旧护栏正是这样把"已平移
      的库"判成"可以平移"的。中位数要求**多数**近期写入是同一口径，少数派掀不翻它。

    正面确证仍然是单边、无歧义的：UTC 时钟写不出未来的值，所以见证中位数晚于 UTC
    此刻就**必然**是本地墙钟写的。

    第二层判定是混口径：检测到的 UTC 行必须已被逐行识别并排除；判不了口径的行必须
    经人审（``--ack-undecidable``）才放行——**绝不静默平移**。
    """
    now_utc_naive = now_local.astimezone(UTC).replace(tzinfo=None)
    notes: list[str] = []

    proven_local = [r for r in reports if r.guard.startswith("确证本地")]
    freshest = max(
        (parse_db_ts(r.max_before) for r in reports if r.max_before),
        default=None,
    )

    if freshest is None:
        notes.append("库内没有任何非空时间值 —— 无需平移，也无从判定。")
        return True, notes

    witness = None
    for rep in reports:
        if rep.tail_median is None:
            continue
        if witness is None or rep.tail_median > witness.tail_median:
            witness = rep

    notes.append(
        f"全库最新写入 {freshest:%Y-%m-%d %H:%M:%S}；"
        f"距本地此刻 {abs((now_local - freshest).total_seconds()) / 3600:.1f}h，"
        f"距 UTC 此刻 {abs((now_utc_naive - freshest).total_seconds()) / 3600:.1f}h"
    )
    notes.append(
        f"正面确证为本地墙钟的列：{len(proven_local)} / {len(reports)}"
        "（max 晚于 UTC 此刻 —— UTC 时钟写不出未来的值）"
    )

    passed = True
    if witness is None:
        notes.append("⚠️  没有任何追加型列可作口径见证（库内无 rowid 可信的列）。请人工确认后加 --force。")
        passed = False
    else:
        median = witness.tail_median
        assert median is not None
        d_local = abs((now_local - median).total_seconds())
        d_utc = abs((now_utc_naive - median).total_seconds())
        notes.append(
            f"口径见证 {witness.key}：最近 {WITNESS_TAIL} 行的中位数 {median:%Y-%m-%d %H:%M:%S}"
            f"（距本地此刻 {d_local / 3600:.1f}h，距 UTC 此刻 {d_utc / 3600:.1f}h）"
        )
        if median > now_utc_naive + SIGNATURE_TOLERANCE:
            notes.append("✅ 写入方仍是旧代码（本地墙钟），可以平移。")
        elif d_utc <= GUARD_TOLERANCE.total_seconds() and d_utc < d_local:
            notes.append(
                "❌ 近期写入的多数已经贴着 UTC 此刻 —— 新代码很可能已经在写库了，"
                "或这个库已经平移过。此时再平移会把这些 UTC 行推早一个时区。"
            )
            passed = False
        else:
            notes.append(
                "⚠️  见证列早于 UTC 此刻且不贴任何一侧（全库都是冷数据），无法自证。"
                "请人工确认这个库确实还没被新代码写过，再加 --force 执行。"
            )
            passed = False

    if contamination is not None:
        total_excluded = sum(len(c.excluded) for c in contamination.values())
        dirty = {k: c for k, c in contamination.items() if c.excluded}
        if dirty:
            notes.append(
                f"混口径检测：{len(dirty)} 列共 {total_excluded} 行已判定为 UTC 口径，"
                "将被逐行排除、不参与平移。"
            )
            for key, entry in sorted(dirty.items()):
                notes.append(f"    · {key}: {len(entry.excluded)} 行（{entry.method}）")
        else:
            notes.append("混口径检测：未发现任何已是 UTC 口径的行。")
        pending = all_undecidable(contamination)
        if pending:
            if ack_undecidable:
                notes.append(
                    f"⚠️  {len(pending)} 行口径无法判定，已由 --ack-undecidable 确认按本地口径平移"
                    "（逐行清单见 journal 与上方「逐行口径判定」栏）。"
                )
            else:
                notes.append(
                    f"❌ {len(pending)} 行口径无法判定 —— 绝不静默平移。请逐行人审上方清单，"
                    "确认它们确实是本地墙钟写的，再加 --ack-undecidable 执行。"
                )
                passed = False
        else:
            notes.append("口径无法判定的行：0（无需 --ack-undecidable）。")

    return passed, notes


def assert_shift_is_constant(reports: list[ColumnReport]) -> float:
    """证明整个数据区间内位移恒定；不恒定就拒绝用常量 SQL 平移。

    逐列端点全查一遍（不只取全局 min/max）。注意这是端点采样：若某列首尾处于同一
    夏令时状态而**中段**跨过切换点，这里查不出来。对 Asia/Shanghai（1991 年后无
    夏令时）无实际影响，脚本被搬到有 DST 的宿主上时必须改成逐行换算。
    """
    stamps = [
        parsed
        for rep in reports
        for raw in (rep.min_before, rep.max_before)
        if raw and (parsed := parse_db_ts(raw))
    ]
    if not stamps:
        return 0.0
    shifts = {shift_for(s).total_seconds() / 3600.0 for s in stamps}
    if len(shifts) != 1:
        raise SystemExit(
            f"❌ 数据区间内本地偏移不恒定（观测到 {sorted(shifts)} 小时）——"
            "本机时区存在夏令时，不能用常量小时数平移。需改用逐行换算。"
        )
    return shifts.pop()


# ---------------------------------------------------------------------------
def read_marker(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def write_marker(conn: sqlite3.Connection, value: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(value)}")


def _rowids(contamination: dict[str, Contamination] | None, key: str) -> set[int]:
    if not contamination or key not in contamination:
        return set()
    return contamination[key].excluded_set


# ---------------------------------------------------------------------------
def render(
    reports: list[ColumnReport],
    hours: float,
    mode: str,
    contamination: dict[str, Contamination] | None = None,
    since: datetime | None = None,
    band: tuple[datetime, datetime] | None = None,
) -> None:
    print(f"\n{'=' * 108}")
    print(f"存量时间戳平移 · {mode} · 位移 {hours:+g} 小时（本地墙钟 → UTC）")
    print(f"{'=' * 108}\n")
    print(f"{'表':<20} {'列':<20} {'非空':>8} {'排除':>6}  {'平移前 min→max':<42} 平移后 min→max")
    print("-" * 108)

    total = 0
    total_excluded = 0
    for rep in reports:
        skipped = len(_rowids(contamination, rep.key))
        total_excluded += skipped
        total += max(rep.non_null - skipped, 0)
        if rep.non_null == 0:
            print(f"{rep.table:<20} {rep.column:<20} {0:>8} {0:>6}  （空列，无行可平移）")
            continue
        lo, hi = parse_db_ts(rep.min_before), parse_db_ts(rep.max_before)
        delta = timedelta(hours=hours)
        lo_a = (lo + delta).strftime("%Y-%m-%d %H:%M:%S") if lo else "?"
        hi_a = (hi + delta).strftime("%Y-%m-%d %H:%M:%S") if hi else "?"
        before = f"{str(rep.min_before)[:19]} → {str(rep.max_before)[:19]}"
        print(
            f"{rep.table:<20} {rep.column:<20} {rep.non_null:>8} {skipped:>6}  "
            f"{before:<42} {lo_a} → {hi_a}"
        )

    print("-" * 108)
    print(f"{'合计':<43} {total:>8} 个非空单元将被平移，{total_excluded} 个单元被排除\n")

    print("抽样前后对照（每列最新 3 行）")
    print("-" * 108)
    for rep in reports:
        if not rep.samples:
            continue
        pairs = []
        for s in rep.samples:
            d = parse_db_ts(s)
            pairs.append(
                f"{str(s)[:19]} → {(d + timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S') if d else '?'}"
            )
        print(f"  {rep.key:<46} {' | '.join(pairs)}")

    if contamination is not None:
        print("\n逐行口径判定（混口径检测：哪些行已经是 UTC，不参与平移）")
        print("-" * 108)
        print(
            "  取证基准 T0（最早的 UTC 写入时刻，本地墙钟）："
            + (f"{since:%Y-%m-%d %H:%M:%S}" if since else "无痕迹（风险带为空，全库单一口径）")
        )
        if band is not None:
            print(
                f"  风险带（原始值落在这个区间才可能是 UTC 写的）：{band[0]:%Y-%m-%d %H:%M:%S}"
                f" … {band[1]:%Y-%m-%d %H:%M:%S}；带外一律证明为本地"
            )
        dirty = False
        for rep in reports:
            entry = contamination.get(rep.key)
            if entry is None or (not entry.excluded and not entry.undecidable):
                continue
            dirty = True
            shown = ", ".join(str(r) for r in entry.excluded[:20])
            more = f" …共 {len(entry.excluded)} 行" if len(entry.excluded) > 20 else ""
            print(
                f"  {rep.key:<40} 候选 {entry.suspects:>6} 行 · 排除 {len(entry.excluded):>4} 行"
                f" · 待人审 {len(entry.undecidable):>4} 行  [{entry.method}]"
            )
            if entry.excluded:
                print(f"      排除 rowid: {shown}{more}")
            for row in entry.undecidable[:20]:
                print(f"      ⚠️ 待人审 rowid={row['rowid']} 值={row['value']} —— {row['reason']}")
            if len(entry.undecidable) > 20:
                print(f"      ⚠️ …另有 {len(entry.undecidable) - 20} 行待人审（完整清单见 journal）")
        if not dirty:
            print("  所有列均为单一口径，无行需要排除。")

    print("\n逐列自证（能否证明该列仍是本地墙钟写的）")
    print("-" * 108)
    for rep in reports:
        print(f"   {rep.key:<46} {rep.guard}")

    print("\n冻结/敏感对象说明")
    print("-" * 108)
    for table, note in FROZEN_NOTE.items():
        if any(r.table == table for r in reports):
            print(f"  {table:<22} {note}")

    print("\n明确不平移的对象")
    print("-" * 108)
    for what, why in EXCLUDED:
        print(f"  {what:<48} {why}")


# ---------------------------------------------------------------------------
def execute(
    conn: sqlite3.Connection,
    reports: list[ColumnReport],
    hours: float,
    contamination: dict[str, Contamination] | None = None,
) -> int:
    """按列平移，逐行绕开已是 UTC 的行。

    平移后立刻复核非空行数：位移表达式对畸形值会返回 NULL（静默清空），
    而 ``cur.rowcount`` 只数匹配行、数不出这件事。对不上就抛异常——此时事务尚未
    提交，连接一关整批回退，不存在半平移状态。
    """
    assert_uniform_width(conn, reports, hours)
    touched = 0
    for rep in reports:
        if rep.non_null == 0:
            continue
        skip = _rowids(contamination, rep.key)
        where = f"{rep.column} is not null"
        if skip:
            conn.execute("drop table if exists temp._utc_skip")
            conn.execute("create temp table _utc_skip (rid integer primary key)")
            conn.executemany(
                "insert into _utc_skip (rid) values (?)", [(int(r),) for r in sorted(skip)]
            )
            where += " and rowid not in (select rid from _utc_skip)"
        cur = conn.execute(
            f"update {rep.table} set {rep.column} = {shift_expr(rep.column, hours)} where {where}"
        )
        after = conn.execute(
            f"select count({rep.column}) from {rep.table}"
        ).fetchone()[0]
        if after != rep.non_null:
            raise SystemExit(
                f"❌ {rep.key} 平移后非空行数 {after} ≠ 平移前 {rep.non_null} —— "
                "位移把值清成了 NULL。事务未提交，库保持原状。"
            )
        touched += cur.rowcount
        note = f"（另有 {len(skip)} 行已是 UTC，已排除）" if skip else ""
        print(f"  {rep.key:<46} {cur.rowcount:>8} 行{note}")
    conn.execute("drop table if exists temp._utc_skip")
    conn.commit()
    return touched


def verify_rollback(conn: sqlite3.Connection, journal: dict) -> int:
    """回滚后逐列复核：行数与 min/max 必须回到 journal 记录的平移前状态。"""
    problems = 0
    for entry in journal["columns"]:
        table, column = entry["table"], entry["column"]
        cnt, lo, hi = conn.execute(
            f"select count({column}), min({column}), max({column}) from {table}"
        ).fetchone()
        if cnt != entry["non_null"]:
            print(f"  ❌ {table}.{column} 行数 {cnt} ≠ 平移前 {entry['non_null']}")
            problems += 1
        elif str(lo)[:19] != str(entry["min_before"])[:19] or str(hi)[:19] != str(
            entry["max_before"]
        )[:19]:
            print(
                f"  ❌ {table}.{column} 区间 {str(lo)[:19]}→{str(hi)[:19]} "
                f"≠ 平移前 {str(entry['min_before'])[:19]}→{str(entry['max_before'])[:19]}"
            )
            problems += 1
        else:
            print(f"  ✅ {table}.{column} 已回到平移前状态")
    return problems


def contamination_from_journal(journal: dict) -> dict[str, Contamination]:
    """回滚时的排除清单**只认 journal**——平移之后库里已看不出谁是谁了。"""
    out: dict[str, Contamination] = {}
    for key, entry in (journal.get("detection", {}).get("columns", {}) or {}).items():
        out[key] = Contamination(
            key=key,
            suspects=entry.get("suspects", 0),
            excluded=list(entry.get("excluded_rowids", [])),
            undecidable=list(entry.get("undecidable", [])),
            method=entry.get("method", ""),
        )
    return out


def build_journal(
    db: Path,
    reports: list[ColumnReport],
    forward: float,
    contamination: dict[str, Contamination],
    since: datetime | None,
    evidence: list[dict],
    mode: str,
    band: tuple[datetime, datetime] | None = None,
) -> dict:
    return {
        "db": str(db),
        "mode": mode,
        "executed_at": datetime.now(tz=UTC).isoformat(),
        "shift_hours": forward,
        "marker": MIGRATION_MARKER,
        "detection": {
            "utc_writer_since": since.strftime("%Y-%m-%d %H:%M:%S.%f") if since else None,
            "risk_band": [b.strftime("%Y-%m-%d %H:%M:%S.%f") for b in band] if band else None,
            "evidence": evidence,
            "undecidable_total": len(all_undecidable(contamination)),
            "columns": {k: v.as_journal() for k, v in contamination.items()},
        },
        "columns": [r.as_journal() for r in reports],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 文件路径")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认 dry-run）")
    ap.add_argument("--rollback", action="store_true", help="反向平移（+offset），需 --journal")
    ap.add_argument(
        "--journal",
        default="",
        help="留档 JSON 路径（--apply / --rollback 必需；dry-run 给了就把完整清单落盘供人审）",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="护栏报警时仍然执行 —— 只有在人已确认该列口径后才可用；不能越过待人审行与幂等标记",
    )
    ap.add_argument(
        "--ack-undecidable",
        action="store_true",
        help="确认已逐行人审 undecidable 清单，按本地口径平移它们",
    )
    ap.add_argument(
        "--utc-writer-since",
        default="",
        help="新代码最早可能写库的本地时刻（YYYY-MM-DD HH:MM:SS）；不给则由库内取证自动推断",
    )
    args = ap.parse_args()

    db = Path(args.db).expanduser()
    if not db.is_file():
        print(f"❌ 找不到数据库：{db}", file=sys.stderr)
        return 2
    if args.apply and args.rollback:
        print("❌ --apply 与 --rollback 不能同时给", file=sys.stderr)
        return 2
    if (args.apply or args.rollback) and not args.journal:
        print(
            "❌ --apply / --rollback 必须给 --journal —— 那是唯一的恢复凭证（含逐行排除清单）",
            file=sys.stderr,
        )
        return 2

    journal_path = Path(args.journal).expanduser() if args.journal else None
    # ---- journal 防覆盖：二次 apply 会把"平移前基线"覆盖成"平移后状态"，
    #      随后的 --rollback 就会对着被污染的基线报「复核全过」。凭证只许写一次。
    if journal_path is not None and not args.rollback and journal_path.exists():
        print(
            f"❌ journal 目标文件已存在：{journal_path}\n"
            "   覆盖它等于销毁唯一的恢复凭证。请换一个文件名。",
            file=sys.stderr,
        )
        return 2

    since = None
    if args.utc_writer_since:
        since = parse_db_ts(args.utc_writer_since)
        if since is None:
            print(f"❌ --utc-writer-since 无法解析：{args.utc_writer_since}", file=sys.stderr)
            return 2

    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA busy_timeout = 30000")
    marker = read_marker(conn)

    # ---- 幂等标记：平移只许成功一次 ----
    if args.apply and marker == MIGRATION_MARKER:
        print(
            f"❌ 这个库已经平移过了（PRAGMA user_version = {MIGRATION_MARKER}）。\n"
            "   再平移一次会把全库再减一个时区。要重来先 --rollback。",
            file=sys.stderr,
        )
        return 3
    if args.apply and marker != 0:
        print(
            f"❌ PRAGMA user_version = {marker}，被别的用途占用了 —— 幂等标记无处安放，拒绝执行。",
            file=sys.stderr,
        )
        return 3
    if args.rollback and marker != MIGRATION_MARKER:
        print(
            f"❌ 这个库没有平移标记（user_version = {marker}，期望 {MIGRATION_MARKER}）—— "
            "它没被本脚本平移过，回滚只会把数据推晚一个时区。",
            file=sys.stderr,
        )
        return 3

    now_local = datetime.now().astimezone()
    reports = collect(conn, now_local.replace(tzinfo=None))
    if not reports:
        print("❌ 没有可处理的列", file=sys.stderr)
        return 2

    forward = assert_shift_is_constant(reports)
    if forward == 0.0:
        print("ℹ️  本机本地时区即 UTC，无需平移。")
        return 0

    # ---------------- rollback ----------------
    if args.rollback:
        assert journal_path is not None
        if not journal_path.is_file():
            print(f"❌ 找不到 journal：{journal_path}", file=sys.stderr)
            return 2
        journal = json.loads(journal_path.read_text())
        if journal.get("db") != str(db) and not args.force:
            print(
                f"❌ journal 记的是 {journal.get('db')}，当前库是 {db} —— 张冠李戴的回滚会搬错数据。",
                file=sys.stderr,
            )
            return 2
        # 位移量取 journal 记录的值而不是按当前宿主时区现算：换机器/换时区回滚才不会搬错。
        hours = -float(journal.get("shift_hours", forward))
        skip = contamination_from_journal(journal)
        skipped_total = sum(len(c.excluded) for c in skip.values())
        print(f"回滚：按 journal {journal_path} 反向平移 {hours:+g} 小时")
        if skipped_total:
            print(f"      平移时排除过的 {skipped_total} 行同样不动（它们本来就是 UTC）")
        touched = execute(conn, reports, hours, skip)
        print(f"\n共回滚 {touched} 个单元。逐列复核：")
        problems = verify_rollback(conn, journal)
        if not problems:
            write_marker(conn, 0)
            conn.commit()
            print("\n已清除平移标记（PRAGMA user_version = 0）")
        print("\n" + ("❌ 回滚复核有出入，请人工介入" if problems else "✅ 回滚复核全过"))
        return 1 if problems else 0

    contamination, detected_since, evidence = detect_contamination(
        conn, reports, forward, now_local.replace(tzinfo=None), since
    )
    hours = forward

    mode = "APPLY（真的写库）" if args.apply else "DRY-RUN（不写库）"
    band = (
        risk_band(detected_since, now_local.replace(tzinfo=None), forward)
        if detected_since is not None
        else None
    )
    render(reports, hours, mode, contamination, detected_since, band)

    passed, notes = verdict(
        reports, now_local.replace(tzinfo=None), contamination, args.ack_undecidable
    )
    pending = all_undecidable(contamination)
    hard_block = bool(pending) and not args.ack_undecidable

    print("\n全库护栏判定")
    print("-" * 108)
    if marker:
        print(f"  ⚠️  PRAGMA user_version = {marker}（{MIGRATION_MARKER} 表示已平移过）")
    for note in notes:
        print(f"  {note}")
    if not passed:
        print(f"\n{'!' * 108}")
        print("护栏拦截 —— 正确顺序是：① 尽量停掉写入方 ② 备份 ③ 平移（新代码写的行会被逐行排除）④ 重启 API。")
        if hard_block:
            print("其中「待人审行」这一条 --force 越不过去：请逐行核对后加 --ack-undecidable。")
        else:
            print("若已人工确认口径无误，可加 --force 强制执行。")
        print(f"{'!' * 108}")

    if journal_path is not None and not args.apply:
        journal_path.write_text(
            json.dumps(
                build_journal(db, reports, forward, contamination, detected_since, evidence, "dry-run", band),
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"\n完整逐行清单已写入 {journal_path}（dry-run 留档，供人审）")

    if not args.apply:
        print("\n（dry-run，未写库。确认无误后由缔造者执行 --apply，执行前先备份。）")
        return 0

    if hard_block or (not passed and not args.force):
        return 1

    assert journal_path is not None
    journal_path.write_text(
        json.dumps(
            build_journal(db, reports, forward, contamination, detected_since, evidence, "apply", band),
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"\n留档已写入 {journal_path}")

    print("\n执行中：")
    touched = execute(conn, reports, hours, contamination)
    write_marker(conn, MIGRATION_MARKER)
    conn.commit()
    print(f"\n✅ 已平移 {touched} 个单元，幂等标记已写入（user_version = {MIGRATION_MARKER}）。")
    print("请复核几张表的 max(时间列)：现在应当等于 UTC 此刻，而不是本地此刻。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
