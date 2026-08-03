#!/usr/bin/env python3
"""一次性恢复：容器队清理误删的整支队 + 成员行上的四层 token 账。默认 dry-run。

取证见 report ``b168eb34-9c19-40e5-8ecb-624a74ccbc5a`` §3 类① 与 §4 恢复方案。要解决的
问题是：``purge_stale_session_containers`` 随 v1.11.0（2026-07-27）上线时，token 五列还
没落到 ``agents`` 行上（v1.11.1，2026-07-29 才落），保留闸问的是"这支队挂着谁还会去看的
记录"（六张关联表），而 token 账是**行自身的列**、不在任何关联表里，于是判据体系对它天然
失明。2026-08-03 01:49(UTC) 队 ``835c6d0a`` 被当作空壳清掉，连同 6 行 worker 上
**325,075,503 tokens** 的归因账——占当时全库已测量量的 4.74%。

**三个源，各司其职**（全部只读；缺一这个脚本就不该存在）：

* **身份主源** ``aiteam.db.bak-tokenbackfill-20260729115732``（07-29 11:57 +0800）。
  队行、成员行、活动记录逐列取自这里。选它而不是取证报告点名的 07-28 备份，有两条**实测**
  理由，见下面「为什么不用 07-28 那份当主源」。
* **账的凭证** ``token-backfill-journal-20260729.json``。
  ``backfill_token_usage.py --apply`` 的恢复凭证，逐行记了当时写进每一行的四层值、批次
  时刻与 ``model`` 观测值。它回答的是"删除时这些行到底挂没挂账"。
* **第三方复核源** ``aiteam.db.bak-ledgerfix-20260728164849``（07-28 16:48 +0800）。
  换算到目标库的时钟制式后，与身份主源**逐列比对**：一致才继续。它读法特殊——带 WAL 头
  且无 ``-shm``，``mode=ro`` 会以 error 14 打不开，必须 ``immutable=1``（只读、不找 WAL、
  不建任何副文件；对"唯二凭证之一"来说，不产生副文件本身就是一项保护）。

外加 **transcript 本体**：6 份全部仍在磁盘且 mtime 早于 journal（子 agent transcript 完成
即冻结），走**生产同一个** :func:`parse_transcript_usage` 重解析。journal 与重解析互为独立
比对，**两者任何一个字段对不上就中止整个事务**，不做"取其一"的降级。这条校验的前置条件
决定了它必然通过，所以它一旦不通过，说明现实与取证结论不符，那正是必须停下来的时刻。

**为什么不用 07-28 那份当主源**——取证报告 §4.3 那句"备份与实库仅差 ``tokens_source``
一列"比的是 schema，没有比内容，而内容上它与目标库差着两件事：

1. **时钟制式不同。** 07-28 备份 ``PRAGMA user_version = 0``，目标库是
   :data:`MIGRATION_MARKER` —— 它取于 UTC 平移（v1.11.1 / ``migrate_timestamps_utc.py``）
   **之前**，一库的本地墙钟（+0800）。同 id 行对照实测整 8 小时、微秒位一致。原样 INSERT
   会往 UTC 库里塞几百行 +8h 的时间戳，正是 I11 红线说的"库里出现第二个时钟"。旁证：这支
   队的 ``updated_at`` 按 UTC 读是 07-27 01:49:19，purge 发生在 08-03 01:49:14，**恰好**
   卡在 7 天阈值上；按本地读只有 6.67 天，根本不会被删——被删这个事实本身就在说目标库是
   UTC 制。
2. **期间被回填过的列会丢。** 07-28 备份里 6 行 worker 的 ``session_id`` 全是 NULL，而删除
   时已经是 ``abff40af-…``（07-29 ``backfill_agent_session_ids.py`` 补的）。照 07-28 恢复
   等于把那次回填的成果再丢一次。

所以 07-28 那份没有被弃用，而是**降为复核源**：脚本用 ``migrate_timestamps_utc.py`` 自己的
:data:`LOCAL_COLUMNS` 与 ``shift_for``（按当时日期查系统时区库，不写死 −8）把它换算到目标
制式，再与主源逐列比。实测 866 个时间戳单元零偏差；非时间戳列只有 6 处 ``session_id``
NULL→有值，判为"期间回填、取后者"。**两源矛盾一律中止**：双方都非空且不等、或后者反而把
值丢成 NULL，都是"两个快照对同一行的身份说法不一致"，不是脚本该自作主张的事。

六条纪律决定了这个脚本的形状（与 ``backfill_token_usage.py`` 同一套范式，复用而非另造）：

1. **源永远只读，目标默认不写。** 三个源全程只读，journal 与 transcript 不改不移。默认
   dry-run，``--apply`` 才写；``--apply`` 时 ``--journal`` 必填、目标文件已存在即硬拒（二次
   apply 会把凭证覆盖成事后状态）。``--apply`` 前先给目标库做一份前置备份，走 SQLite 自己的
   备份 API 而不是 ``cp``：目标库是 WAL 模式，``cp`` 只复制主文件会得到一份**不含 WAL 内容
   的旧快照**，那种"备份"要到真需要它的时候才发现没用。
2. **写面是封闭集合。** 只 INSERT :data:`RESTORE_TABLES` 三张表、且只限本次 id 集合；
   一律 INSERT，**没有任何 UPDATE/DELETE 路径**。``workflow_agents.tokens``（ctx_last
   口径）永远不在写面内，并沿用回采脚本那份**逐行 sha256 指纹**做前后比对，不一致整事务
   回滚——直接复用 ``backfill_token_usage.workflow_tokens_fingerprint``，指纹算法只此一份，
   不会两边各飘一点。
3. **行与账同一事务落地。** 先插行、后写账之间的任何一次 reap tick 都会把这批"无账 husk"
   再删一遍（新闸 ``_carries_token_ledger`` 是逐队 ``any(...)`` 判定，看的正是这几列）。
   所以 ``BEGIN IMMEDIATE`` 一次性拿写锁，行、账、活动记录一起提交；事务内做完整验收，
   任何一项不达标当场 ``ROLLBACK``。恢复完成后这支队因 6 名成员挂账而命中新闸，自保成立。
4. **``tokens_measured_at`` 写原批次时刻 :data:`BATCH_TS`，绝不写 now()。** 该字段兼任
   两个用途：一次回采给上千行盖同一个值 = **回采批次的天然签名**（``BATCH_COHORT_MIN``
   靠它分窗），而活体链路逐行写各自的微秒级停止时刻 = **采集健康度**。写 now() 会凭空造出
   6 个"新测量"，污染活体覆盖率那一格；写原时刻则让这 6 行归队到既有 1903 行的批次里——
   而这也正是事实：它们本来就是那一批测的。该值来自 07-29 journal，写它的那次回采跑在
   UTC 平移之后，所以它已经是 UTC，不参与任何时钟换算。
5. **时钟制式是显式判据，不是假设。** 目标库与每个源都读 ``PRAGMA user_version``：相等则
   原样取值，源为 0 而目标为 :data:`MIGRATION_MARKER` 则按平移脚本的规则换算，**其余任何
   组合一律中止**。换算复用平移脚本的列清单与位移函数，并逐字复刻它保留亚秒位的做法
   （只搬"到分钟"的前缀，秒与亚秒位原样接回去）——于是恢复回来的行与那些从未被删的兄弟行
   在字节层面是同一种东西。
6. **幂等是结构性的。** 判定的第一分支是"目标 id 已存在 → ``already_present``"，SQL 层
   另带 ``WHERE NOT EXISTS`` 守卫（防 dry-run 与 apply 之间有人先插进去），于是重跑必然
   零变更，而不是靠标记位或"我记得跑过了"。

**schema 适配**：INSERT 按**目标库**的列名显式列出；源里没有的列必须在
:data:`MISSING_COLUMN_DEFAULTS` 里有明文规则，否则当作 schema 漂移直接中止——静默补 NULL
正是"字段悄悄丢了没人知道"的成因。``agents.tokens_source`` 的规则是：**经双源验证有账的
行**补 ``'transcript'``（那是这批账的真实来源），**本就无账的行留 NULL**——实库 697 行未
测量行的 ``tokens_source`` 全为 NULL，给一个没测过的行标上来源等于凭空造一条 provenance。

**范围**：只恢复 :data:`RESTORE_TEAM_ID` 这一支（1 队 + 7 行 agents + 835 条 activities）。
取证报告 §4.1 点名的另外 10 行 Leader husk **不在范围内**：它们无账，恢复后会立刻重新满足
purge 的全部判据、下一个 reap tick 再删一次，只多写 10 条 purge 事件。要它们回来是策略决定
（须同时改判据），不是这个脚本能替谁拍的板。

用法::

    python3 scripts/restore_purged_container_team.py                         # dry-run（默认，实库只读）
    python3 scripts/restore_purged_container_team.py --db /tmp/rehearsal.db  # 对副本演练
    python3 scripts/restore_purged_container_team.py --db /tmp/rehearsal.db \\
        --apply --journal /tmp/restore-journal.json

对实库 ``--apply`` **须用户批准**（发布流水线可中断纪律），并建议先停 API 或确认无并发
reap tick——虽然 ``BEGIN IMMEDIATE`` 已经把这段做成原子的，少一个并发面总是更省心。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aiteam.clock import utc_now  # noqa: E402
from aiteam.services.token_attribution import parse_transcript_usage  # noqa: E402

BACKFILL_SCRIPT = ROOT / "scripts" / "backfill_token_usage.py"
MIGRATE_SCRIPT = ROOT / "scripts" / "migrate_timestamps_utc.py"


def _load_script_module(name: str, path: Path) -> Any:
    """载入同目录的姊妹脚本，复用它们已经定死的判据。

    刻意不抄：禁改列的指纹算法、UTC 平移的列清单与位移函数，如果各自再存一份，将来改一处
    就会长出两套判据，而这两类错**事后都不可分辨**。两个脚本顶层都只有 import 与常量，
    ``main()`` 有 ``__main__`` 守卫，载入无副作用。
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件在仓库里，缺了是环境坏了
        msg = f"姊妹脚本不可载入：{path}"
        raise RuntimeError(msg)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_bf = _load_script_module("backfill_token_usage", BACKFILL_SCRIPT)
_mig = _load_script_module("migrate_timestamps_utc", MIGRATE_SCRIPT)

TOKEN_COLUMNS: tuple[str, ...] = _bf.TOKEN_COLUMNS
FORBIDDEN_COLUMN: tuple[str, str] = _bf.FORBIDDEN_COLUMN
workflow_tokens_fingerprint = _bf.workflow_tokens_fingerprint

# UTC 平移脚本的三件权威判据：哪些列是本地墙钟、位移多少、库上的幂等标记。
LOCAL_COLUMNS: dict[str, tuple[str, ...]] = _mig.LOCAL_COLUMNS
MIGRATION_MARKER: int = _mig.MIGRATION_MARKER
shift_for = _mig.shift_for
parse_db_ts = _mig.parse_db_ts

DATA_DIR = Path.home() / ".claude" / "data" / "ai-team-os"
DEFAULT_DB = DATA_DIR / "aiteam.db"
# 身份主源：07-29 11:57，UTC 平移之后、session_id 回填之后、删除之前的快照。
DEFAULT_SOURCE = Path.home() / "aiteam.db.bak-tokenbackfill-20260729115732"
# 第三方复核源：07-28 16:48，UTC 平移之前（本地墙钟），须换算后再比。
DEFAULT_CORROBORANT = DATA_DIR / "aiteam.db.bak-ledgerfix-20260728164849"
DEFAULT_VERIFY_JOURNAL = Path.home() / "token-backfill-journal-20260729.json"

# 被误删的那支队。整个脚本围着这一个 id 转；--team 只为让单测能拿别的库跑同一条路径。
RESTORE_TEAM_ID = "835c6d0a-6652-4058-b263-ef891ea84579"

# 2026-07-29 那次回采的批次时刻（journal.batch_ts 原样，已是 UTC）。理由见纪律④。
BATCH_TS = "2026-07-29 03:57:32.707912"

# 写面：只 INSERT 这三张表，且只限本次 id 集合。没有 UPDATE / DELETE 路径。
RESTORE_TABLES: tuple[str, ...] = ("teams", "agents", "agent_activities")
WRITABLE_TABLES: frozenset[str] = frozenset(RESTORE_TABLES)

# 目标库有、源里没有的列，必须在这里有明文规则；否则算 schema 漂移，中止。
# 值是"无 overlay 时该填什么"。``agents.tokens_source`` 的 'transcript' 由 overlay 给出
# （只有经双源验证确有账的行才配得上一个来源标注），这里的 None 兜的是无账行。
MISSING_COLUMN_DEFAULTS: dict[tuple[str, str], Any] = {
    ("agents", "tokens_source"): None,
}

MISSING_COLUMN_NOTES: dict[tuple[str, str], str] = {
    ("agents", "tokens_source"): (
        "该列 v1.11.1（07-29）才上线。有账行由 overlay 补 'transcript'，无账行留 NULL —— "
        "与实库 697 行未测量行的惯例一致"
    ),
}


@dataclass(frozen=True)
class TeamExpectation:
    """这支队"恢复得对不对"的期望指纹 —— 取证报告 §3 类① 的数字，逐项硬比。

    只对已知的队生效。别的队（单测用的合成库）没有期望值，计数闸会显式声明跳过，
    而不是拿一个空 dict 假装比过了。
    """

    agents: int
    measured_agents: int
    activities: int
    layers: dict[str, int]

    @property
    def token_total(self) -> int:
        return sum(self.layers.values())


EXPECTATIONS: dict[str, TeamExpectation] = {
    RESTORE_TEAM_ID: TeamExpectation(
        agents=7,  # 6 worker + 1 Leader
        measured_agents=6,  # Leader 行本就无账
        activities=835,
        layers={
            "input_tokens": 3_912,
            "output_tokens": 1_043_862,
            "cache_creation_tokens": 17_684_918,
            "cache_read_tokens": 306_342_811,
        },
    ),
}


class VerificationError(RuntimeError):
    """双源校验 / 第三方复核不通过 —— 现实与取证结论不符，整个恢复中止。"""


class SchemaDriftError(RuntimeError):
    """源与目标库的列集合出现未登记的差异 —— 静默补 NULL 会悄悄丢字段。"""


class ClockConventionError(RuntimeError):
    """源与目标库的时钟制式对不上且无已知换算 —— 混口径进库事后不可分辨。"""


# ---------------------------------------------------------------------------
# 连接（源只读是物理性的，不靠自觉）
# ---------------------------------------------------------------------------
def open_immutable(path: Path) -> sqlite3.Connection:
    """源专用：``immutable=1``。

    这些快照里有的带 WAL 头且无 ``-shm``，``mode=ro`` 会以 error 14 打不开；
    ``immutable=1`` 告诉 SQLite 这份文件不会变，于是它既不找 WAL 也不建任何副文件。
    """
    con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def open_target(path: Path, *, writable: bool) -> sqlite3.Connection:
    """目标库：dry-run 一律 ``mode=ro``（物理上写不进去），``--apply`` 才要普通连接。

    ``isolation_level=None`` 关掉 sqlite3 的隐式事务管理 —— 本脚本要自己发
    ``BEGIN IMMEDIATE``，纪律③ 要的是"行与账在同一个事务里"，不是"驱动看着办"。
    """
    if writable:
        con = sqlite3.connect(str(path), isolation_level=None)
    else:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(r["name"]) for r in con.execute(f"pragma table_info({table})")]


def clock_marker(con: sqlite3.Connection) -> int:
    """库上的 UTC 平移幂等标记 —— 一个库到底是哪个时钟制式，问它本人。"""
    return int(con.execute("pragma user_version").fetchone()[0])


def needs_clock_shift(source_marker: int, target_marker: int) -> bool:
    """源要不要换算才能进目标库。除已知的两种组合外，一律拒绝执行。

    * 两边标记相同 → 同制式，原样取值；
    * 源 0 / 目标 :data:`MIGRATION_MARKER` → 源是平移前的本地墙钟，按平移脚本换算；
    * 其余（含"源已平移而目标没有"）→ 没有已知的正确做法，中止比猜一个方向安全得多。
    """
    if source_marker == target_marker:
        return False
    if source_marker == 0 and target_marker == MIGRATION_MARKER:
        return True
    msg = (
        f"源库 user_version={source_marker} 与目标库 user_version={target_marker} 之间"
        f"没有已知的时钟换算（已知：相等 或 0→{MIGRATION_MARKER}）—— 中止"
    )
    raise ClockConventionError(msg)


def shift_value(raw: Any) -> Any:
    """本地墙钟 → UTC，逐字复刻 ``migrate_timestamps_utc.shift_expr`` 的分钟前缀语义。

    平移脚本只搬"到分钟"的前缀、秒与亚秒位原样接回去（``datetime(col,'-8 hours')`` 会把
    结果截到整秒，等于顺手抹掉微秒位，而事件账本按 timestamp 排序时同一秒内的先后就靠
    那几位）。这里必须一模一样，恢复回来的行才与从未被删的兄弟行在字节层面同构。

    位移量走 ``shift_for``：按**当时的日期**查系统时区库，而不是写死 −8。
    """
    if raw is None:
        return None
    text = str(raw)
    head = parse_db_ts(text[:16] + ":00")
    if head is None:
        msg = f"时间戳无法解析，不敢换算：{text!r}"
        raise ClockConventionError(msg)
    moved = head + shift_for(head)
    return moved.strftime("%Y-%m-%d %H:%M") + text[16:]


def normalize_clock(table: str, values: dict[str, Any], *, shift: bool) -> dict[str, Any]:
    """把一行里属于本地墙钟的列换算成 UTC。列清单取自平移脚本，不另立一份。"""
    if not shift:
        return values
    out = dict(values)
    for col in LOCAL_COLUMNS.get(table, ()):
        if col in out and out[col] is not None:
            out[col] = shift_value(out[col])
    return out


# ---------------------------------------------------------------------------
# 身份主源
# ---------------------------------------------------------------------------
def load_source_team(con: sqlite3.Connection, team_id: str) -> sqlite3.Row | None:
    return con.execute("select * from teams where id = ?", (team_id,)).fetchone()


def load_source_agents(con: sqlite3.Connection, team_id: str) -> list[sqlite3.Row]:
    return con.execute(
        "select * from agents where team_id = ? order by created_at", (team_id,)
    ).fetchall()


def load_source_activities(con: sqlite3.Connection, agent_ids: list[str]) -> list[sqlite3.Row]:
    if not agent_ids:
        return []
    marks = ",".join("?" * len(agent_ids))
    return con.execute(
        f"select * from agent_activities where agent_id in ({marks}) order by timestamp",  # noqa: S608 - 占位符个数由 id 数量决定，值全部参数化
        agent_ids,
    ).fetchall()


# ---------------------------------------------------------------------------
# 账的凭证：回采 journal 里逐行记下的写入值
# ---------------------------------------------------------------------------
def load_journal_ledger(path: Path) -> dict[str, dict[str, Any]]:
    """从回采 journal 取出 ``agents`` 表的逐行写入值，返回 ``id -> values``。

    只认 ``table == 'agents'`` 且真带四层值的 job：同一份 journal 里还有
    ``workflow_agents.model`` 与 ``tokens_source`` 补标的记录，混进来会让"这一行有没有账"
    的判断变糊。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    ledger: dict[str, dict[str, Any]] = {}
    for job in data.get("jobs", []):
        if job.get("table") != "agents" or not job.get("enabled"):
            continue
        for row in job.get("written", []):
            values = row.get("values") or {}
            if not any(c in values for c in TOKEN_COLUMNS):
                continue
            ledger[str(row["id"])] = values
    return ledger


# ---------------------------------------------------------------------------
# 双源强校验（纪律：两者不一致即中止整个事务）
# ---------------------------------------------------------------------------
@dataclass
class LedgerCheck:
    """一行的双源校验结果 —— 成功才有 ``overlay``，失败在 :func:`verify_ledger` 里就抛了。"""

    agent_id: str
    name: str
    transcript_path: str
    layers: dict[str, int]
    model: str
    api_calls: int
    overlay: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.layers.values())


def verify_ledger(
    agent_rows: list[sqlite3.Row],
    journal_ledger: dict[str, dict[str, Any]],
    *,
    batch_ts: str = BATCH_TS,
) -> list[LedgerCheck]:
    """重解析 transcript 与 journal 逐字段对账，返回有账行的 overlay。

    四类中止条件，每一类都对应一种"现实与取证结论不符"：

    * 行有 ``transcript_path`` 但文件已不在磁盘 → 少一个源，双源校验做不成；
    * 文件在但解析不出快照 → 同上（no-data ≠ zero，绝不落成"用了 0 token"）；
    * 有账行在 journal 里找不到 / 四层对不上 / 观测型号对不上 → 两源分歧；
    * journal 有账但源行没有 ``transcript_path`` → 一笔无从解释的账。

    没有 ``--force`` 之类的降级开关：这条校验的前置条件（子 agent transcript 完成即冻结、
    mtime 全部早于 journal）决定了它必然通过，所以它不通过时唯一正确的动作就是停下来。
    """
    checks: list[LedgerCheck] = []
    for row in agent_rows:
        aid = str(row["id"])
        name = str(row["name"])
        path = str(row["transcript_path"] or "")
        journal_values = journal_ledger.get(aid)

        if not path:
            if journal_values:
                msg = (
                    f"{name}({aid[:8]}) 在 journal 里有账，但源行没有 transcript_path —— "
                    f"一笔无从解释的账，不恢复"
                )
                raise VerificationError(msg)
            continue  # 无路径无账（Leader husk）：身份照恢复，账本来就没有

        if journal_values is None:
            msg = f"{name}({aid[:8]}) 有 transcript_path 但 journal 里没有它的账 —— 两源分歧"
            raise VerificationError(msg)

        usage = parse_transcript_usage(path)
        if usage is None:
            msg = (
                f"{name}({aid[:8]}) 的 transcript 解析不出快照（文件缺失或全损坏）：{path} —— "
                f"双源校验缺一源，中止"
            )
            raise VerificationError(msg)

        fresh = {c: int(usage[c]) for c in TOKEN_COLUMNS}
        recorded = {c: int(journal_values.get(c, 0)) for c in TOKEN_COLUMNS}
        if fresh != recorded:
            diff = {c: (recorded[c], fresh[c]) for c in TOKEN_COLUMNS if recorded[c] != fresh[c]}
            msg = (
                f"{name}({aid[:8]}) 双源不一致（journal vs 重解析）：{diff} —— "
                f"整个恢复中止，不做取其一的降级"
            )
            raise VerificationError(msg)

        observed = str(usage.get("model") or "")
        journal_model = str(journal_values.get("model") or "")
        if journal_model and journal_model != observed:
            msg = (
                f"{name}({aid[:8]}) 观测型号不一致：journal={journal_model!r} vs "
                f"重解析={observed!r} —— 整个恢复中止"
            )
            raise VerificationError(msg)

        overlay: dict[str, Any] = dict(fresh)
        overlay["tokens_measured_at"] = batch_ts
        overlay["tokens_source"] = "transcript"
        if observed and not str(row["model"] or ""):
            # 观测回填：源行那一列是空的，transcript 里是完整型号。与当时回采写下的值
            # 同源同值（上面刚比过），恢复的就是"删除那一刻行上真实的样子"。
            overlay["model"] = observed

        checks.append(
            LedgerCheck(
                agent_id=aid,
                name=name,
                transcript_path=path,
                layers=fresh,
                model=observed,
                api_calls=int(usage.get("api_calls") or 0),
                overlay=overlay,
            )
        )
    return checks


# ---------------------------------------------------------------------------
# 第三方复核（另一份快照对同一批行的说法）
# ---------------------------------------------------------------------------
@dataclass
class Corroboration:
    """复核源与主源的一处出入。``agree`` 不逐条记录，只计数。"""

    table: str
    row_id: str
    column: str
    primary: Any
    secondary: Any
    verdict: str  # filled_later / contradiction


@dataclass
class CorroborationReport:
    source: str
    shifted: bool
    cells_compared: int = 0
    rows_missing: int = 0
    findings: list[Corroboration] = field(default_factory=list)

    @property
    def contradictions(self) -> list[Corroboration]:
        return [f for f in self.findings if f.verdict == "contradiction"]

    @property
    def filled_later(self) -> list[Corroboration]:
        return [f for f in self.findings if f.verdict == "filled_later"]


def corroborate_rows(
    report: CorroborationReport,
    con: sqlite3.Connection,
    table: str,
    primary_rows: list[sqlite3.Row],
    *,
    id_col: str = "id",
) -> None:
    """把复核源换算到目标制式后与主源逐列比，结果记进 ``report``。

    三条判据，覆盖"两个快照对同一行说法不同"的全部形态：

    * 两边相等 → 一致（只计数）；
    * 复核源为 NULL、主源有值 → **期间被回填**，取主源（主源是更晚的快照）。记录下来，
      因为它同时说明"照复核源恢复会丢这一列"——这正是主源必须是更晚那份的实证；
    * 其余（双方非空且不等 / 主源反而丢了值）→ **矛盾**，中止。谁对谁错不是脚本该猜的。

    复核源里没有的行不算问题：它可能就是比那一行更早的快照，只计数并如实报出。
    """
    secondary_cols = set(table_columns(con, table))
    for row in primary_rows:
        rid = str(row[id_col])
        other = con.execute(
            f"select * from {table} where {id_col} = ?",  # noqa: S608 - 表名来自白名单常量，列名来自本文件常量
            (rid,),
        ).fetchone()
        if other is None:
            report.rows_missing += 1
            continue
        shared = [c for c in row.keys() if c in secondary_cols]
        normalized = normalize_clock(
            table, {c: other[c] for c in shared}, shift=report.shifted
        )
        for col in shared:
            report.cells_compared += 1
            mine, theirs = row[col], normalized[col]
            if mine == theirs:
                continue
            verdict = "filled_later" if theirs is None and mine is not None else "contradiction"
            report.findings.append(Corroboration(table, rid, col, mine, theirs, verdict))


# ---------------------------------------------------------------------------
# 计划（幂等的第一分支：目标 id 已存在 → already_present）
# ---------------------------------------------------------------------------
@dataclass
class RowPlan:
    table: str
    row_id: str
    label: str
    reason: str  # restorable / already_present
    values: dict[str, Any] = field(default_factory=dict)


def build_values(
    table: str,
    source_row: sqlite3.Row,
    target_cols: list[str],
    source_cols: set[str],
    overlay: dict[str, Any],
    *,
    shift_clock: bool,
) -> dict[str, Any]:
    """按**目标库**的列名逐列取值：源 → 时钟换算 → overlay 覆盖。

    "源里没有的列"不许退化成"补 NULL 拉倒"：必须在 :data:`MISSING_COLUMN_DEFAULTS` 里
    登记过，否则说明源与目标库之间出现了没人看过的 schema 漂移，中止比猜一个值安全得多。

    overlay 最后落，且**不参与时钟换算** —— 它的 ``tokens_measured_at`` 来自 07-29 的
    journal，那次回采跑在 UTC 平移之后，本来就是 UTC。
    """
    values: dict[str, Any] = {}
    for col in target_cols:
        if col in source_cols:
            values[col] = source_row[col]
        elif (table, col) in MISSING_COLUMN_DEFAULTS:
            values[col] = MISSING_COLUMN_DEFAULTS[(table, col)]
        else:
            msg = (
                f"{table}.{col} 在目标库有、源里没有，且没有明文缺列规则 —— "
                f"schema 漂移，中止（补 NULL 会悄悄丢字段）"
            )
            raise SchemaDriftError(msg)
    values = normalize_clock(table, values, shift=shift_clock)
    for col, val in overlay.items():
        if col not in target_cols:
            msg = f"overlay 想写 {table}.{col}，但目标库没有这一列 —— 中止"
            raise SchemaDriftError(msg)
        values[col] = val
    return values


def existing_ids(con: sqlite3.Connection, table: str, ids: list[str]) -> set[str]:
    """目标库里已经在位的 id —— 幂等判定的唯一依据（分批查，避开 SQLite 变量上限）。"""
    found: set[str] = set()
    chunk = 400
    for i in range(0, len(ids), chunk):
        part = ids[i : i + chunk]
        marks = ",".join("?" * len(part))
        rows = con.execute(
            f"select id from {table} where id in ({marks})",  # noqa: S608 - 表名来自本文件常量，值全部参数化
            part,
        )
        found.update(str(r["id"]) for r in rows)
    return found


def plan_table(
    target_con: sqlite3.Connection,
    table: str,
    source_rows: list[sqlite3.Row],
    source_cols: set[str],
    *,
    overlays: dict[str, dict[str, Any]] | None = None,
    label_col: str = "name",
    shift_clock: bool = False,
) -> list[RowPlan]:
    if table not in WRITABLE_TABLES:
        msg = f"写表白名单拦截：{table} 不在 RESTORE_TABLES 中"
        raise RuntimeError(msg)
    target_cols = table_columns(target_con, table)
    dropped = source_cols - set(target_cols)
    if dropped:
        msg = (
            f"{table} 在源里有而目标库没有的列：{sorted(dropped)} —— "
            f"目标库删过列，恢复内容会丢失，须人工裁定后再跑"
        )
        raise SchemaDriftError(msg)

    ids = [str(r["id"]) for r in source_rows]
    present = existing_ids(target_con, table, ids)
    plans: list[RowPlan] = []
    for row in source_rows:
        rid = str(row["id"])
        label = str(row[label_col]) if label_col in source_cols else rid[:8]
        if rid in present:
            plans.append(RowPlan(table, rid, label, "already_present"))
            continue
        values = build_values(
            table, row, target_cols, source_cols, (overlays or {}).get(rid, {}),
            shift_clock=shift_clock,
        )
        plans.append(RowPlan(table, rid, label, "restorable", values))
    return plans


# ---------------------------------------------------------------------------
# 写入（单事务 + 事务内验收 + 不达标即回滚）
# ---------------------------------------------------------------------------
def insert_row(con: sqlite3.Connection, plan: RowPlan) -> int:
    """带 ``WHERE NOT EXISTS`` 守卫的插入 —— 守卫在 SQL 里而不只在判定层。

    判定与写入之间目标库随时可能被别人写：守卫让"期间已经被插进去"的行自动让路，
    重跑因此必然零变更，而不是靠"我记得跑过了"。
    """
    if plan.table not in WRITABLE_TABLES:
        msg = f"写表白名单拦截：{plan.table} 不在 RESTORE_TABLES 中"
        raise RuntimeError(msg)
    cols = list(plan.values)
    marks = ",".join("?" * len(cols))
    sql = (
        f"insert into {plan.table} ({', '.join(cols)}) "  # noqa: S608 - 表名来自白名单常量，列名来自目标库 pragma
        f"select {marks} where not exists (select 1 from {plan.table} where id = ?)"
    )
    cur = con.execute(sql, [*plan.values.values(), plan.row_id])
    return int(cur.rowcount or 0)


def verify_after(
    con: sqlite3.Connection,
    team_id: str,
    agent_ids: list[str],
    activity_ids: list[str],
    expectation: TeamExpectation | None,
    *,
    batch_ts: str = BATCH_TS,
) -> list[str]:
    """事务内验收，返回问题清单（空 = 通过）。任何一条不达标就回滚。"""
    problems: list[str] = []

    if not con.execute("select 1 from teams where id = ?", (team_id,)).fetchone():
        problems.append("队行不在位")

    marks = ",".join("?" * len(agent_ids)) if agent_ids else "''"
    present_agents = int(
        con.execute(
            f"select count(*) from agents where id in ({marks})",  # noqa: S608 - 占位符个数由 id 数量决定
            agent_ids,
        ).fetchone()[0]
    )
    if present_agents != len(agent_ids):
        problems.append(f"成员行 {present_agents}/{len(agent_ids)} 在位")

    layer_sql = ", ".join(f"coalesce(sum({c}), 0)" for c in TOKEN_COLUMNS)
    layer_row = con.execute(
        f"select {layer_sql}, count(tokens_measured_at) from agents where id in ({marks})",  # noqa: S608 - 列名来自本文件常量
        agent_ids,
    ).fetchone()
    layers = {c: int(layer_row[i]) for i, c in enumerate(TOKEN_COLUMNS)}
    measured = int(layer_row[len(TOKEN_COLUMNS)])

    activity_present = 0
    chunk = 400
    for i in range(0, len(activity_ids), chunk):
        part = activity_ids[i : i + chunk]
        amarks = ",".join("?" * len(part))
        activity_present += int(
            con.execute(
                f"select count(*) from agent_activities where id in ({amarks})",  # noqa: S608 - 占位符个数由 id 数量决定
                part,
            ).fetchone()[0]
        )
    if activity_present != len(activity_ids):
        problems.append(f"活动记录 {activity_present}/{len(activity_ids)} 在位")

    stray_ts = int(
        con.execute(
            f"select count(*) from agents where id in ({marks}) "  # noqa: S608 - 占位符个数由 id 数量决定
            f"and tokens_measured_at is not null and tokens_measured_at != ?",
            [*agent_ids, batch_ts],
        ).fetchone()[0]
    )
    if stray_ts:
        problems.append(f"{stray_ts} 行的 tokens_measured_at 不是原批次时刻 {batch_ts}")

    # 时钟制式的兜底闸：被恢复的行全是历史行，任何一个"未来"时间戳都说明换算走反了。
    now_text = utc_now().strftime("%Y-%m-%d %H:%M:%S.%f")
    future = int(
        con.execute(
            f"select count(*) from agents where id in ({marks}) and created_at > ?",  # noqa: S608 - 占位符个数由 id 数量决定
            [*agent_ids, now_text],
        ).fetchone()[0]
    )
    if future:
        problems.append(f"{future} 行的 created_at 落在未来 —— 时钟换算方向反了")

    if expectation is None:
        return problems

    if len(agent_ids) != expectation.agents:
        problems.append(f"成员行数 {len(agent_ids)} ≠ 期望 {expectation.agents}")
    if len(activity_ids) != expectation.activities:
        problems.append(f"活动记录数 {len(activity_ids)} ≠ 期望 {expectation.activities}")
    if measured != expectation.measured_agents:
        problems.append(f"挂账行数 {measured} ≠ 期望 {expectation.measured_agents}")
    for col, want in expectation.layers.items():
        if layers[col] != want:
            problems.append(f"{col} 合计 {layers[col]:,} ≠ 期望 {want:,}")
    if sum(layers.values()) != expectation.token_total:
        problems.append(f"四层合计 {sum(layers.values()):,} ≠ 期望 {expectation.token_total:,}")
    return problems


def make_pre_backup(db: Path, dest_dir: Path) -> Path:
    """``--apply`` 前的前置备份 —— 走 SQLite 备份 API 而不是 ``cp``。

    目标库是 WAL 模式：``cp`` 只拿主文件会得到一份**不含 WAL 内容**的旧快照，而这种"备份"
    要到真需要它的那一刻才会发现是空的。备份 API 读的是一致性快照，WAL 里的内容一并落进去。
    """
    # 走 aiteam.clock 的唯一时钟（I11 红线：库里不许有第二个时钟）。
    stamp = utc_now().strftime("%Y%m%d%H%M%S")
    dest = dest_dir / f"{db.name}.bak-restore-{stamp}"
    # 同秒内重跑要另起文件名而不是覆盖 —— 前置备份的全部价值就在于"没被写过"。
    serial = 1
    while dest.exists():
        serial += 1
        dest = dest_dir / f"{db.name}.bak-restore-{stamp}-{serial}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def print_checks(checks: list[LedgerCheck]) -> None:
    print()
    print("=" * 92)
    print("双源强校验：重解析 transcript（生产 parse_transcript_usage） vs 07-29 回采 journal")
    print("-" * 92)
    if not checks:
        print("  这支队没有任何挂账行 —— 只恢复身份，不涉及账。")
        return
    for c in checks:
        layers = " ".join(f"{k.replace('_tokens', '')}={v:,}" for k, v in c.layers.items())
        print(f"  ✓ {c.agent_id[:8]}  {c.name[:24]:<24} {layers}")
        print(f"      合计 {c.total:,} / api_calls={c.api_calls} / model={c.model or '(无输出)'}")
    print(f"  ── {len(checks)} 行两源逐字段一致，合计 {sum(c.total for c in checks):,} tokens")


def print_corroboration(report: CorroborationReport | None) -> None:
    print()
    print("=" * 92)
    print("第三方复核：另一份快照对同一批行的说法")
    print("-" * 92)
    if report is None:
        print("  ⚠ 复核源不可用 —— 本次身份列与时钟换算仅由计算保证，未经独立快照复核。")
        return
    print(f"  复核源：{report.source}")
    print(f"  时钟换算：{'已按平移脚本换算到 UTC 后比对' if report.shifted else '同制式，原样比对'}")
    print(f"  比对单元：{report.cells_compared} 个；复核源中缺失的行：{report.rows_missing}")
    if report.filled_later:
        print(f"  ── 期间被回填 {len(report.filled_later)} 处（复核源 NULL → 主源有值，取主源）")
        for f in report.filled_later[:10]:
            print(f"     · [{f.table}] {f.row_id[:8]} {f.column} = {f.primary!r}")
        if len(report.filled_later) > 10:
            print(f"       …… 另有 {len(report.filled_later) - 10} 处")
    if not report.contradictions:
        print("  ✓ 零矛盾 —— 两份快照对这批行的身份说法一致")


def print_plan(plans: list[RowPlan]) -> None:
    print()
    print("=" * 92)
    print("恢复计划（幂等第一分支：目标 id 已存在 → already_present）")
    print("-" * 92)
    for table in RESTORE_TABLES:
        rows = [p for p in plans if p.table == table]
        if not rows:
            continue
        restorable = [p for p in rows if p.reason == "restorable"]
        present = [p for p in rows if p.reason == "already_present"]
        print(
            f"  {table:<18} 待恢复 {len(restorable):5d} │ already_present {len(present):5d} │ "
            f"合计 {len(rows):5d}"
        )
    print()
    for p in plans:
        if p.table == "agent_activities":
            continue  # 835 条活动记录逐条打印没有信息量，计数已在上面
        mark = "+" if p.reason == "restorable" else "="
        print(f"    {mark} [{p.table}] {p.row_id[:8]}  {p.label[:32]}  ({p.reason})")


def build_journal(
    args: argparse.Namespace,
    plans: list[RowPlan],
    checks: list[LedgerCheck],
    corroboration: CorroborationReport | None,
    clock: dict[str, Any],
    fp_before: dict[str, Any],
    fp_after: dict[str, Any],
    inserted: int,
    pre_backup: Path | None,
) -> dict[str, Any]:
    """journal = 这次恢复的唯一凭证：插了哪些 id、值是什么、各源证据、禁改列前后指纹。"""
    return {
        "script": "restore_purged_container_team.py",
        "db": str(args.db),
        "team_id": args.team,
        "metric": "usage_sum",
        "batch_ts": args.batch_ts,
        "generated_at": utc_now().isoformat(),
        "sources": {
            "identity": str(args.from_backup),
            "verify_journal": str(args.verify_journal),
            "corroborant": str(args.corroborate_with) if corroboration else "",
            "transcripts": [c.transcript_path for c in checks],
        },
        "clock": clock,
        "pre_backup": str(pre_backup) if pre_backup else "",
        "forbidden_column": {
            "column": f"{FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]}",
            "before": fp_before,
            "after": fp_after,
            "unchanged": fp_before["sha256"] == fp_after["sha256"],
        },
        "dual_source_verification": [
            {
                "id": c.agent_id,
                "name": c.name,
                "transcript": c.transcript_path,
                "layers": c.layers,
                "total": c.total,
                "api_calls": c.api_calls,
                "model": c.model,
            }
            for c in checks
        ],
        "corroboration": (
            {
                "source": corroboration.source,
                "clock_shifted": corroboration.shifted,
                "cells_compared": corroboration.cells_compared,
                "rows_missing": corroboration.rows_missing,
                "filled_later": [
                    {"table": f.table, "id": f.row_id, "column": f.column, "value": f.primary}
                    for f in corroboration.filled_later
                ],
                "contradictions": [
                    {
                        "table": f.table,
                        "id": f.row_id,
                        "column": f.column,
                        "primary": f.primary,
                        "secondary": f.secondary,
                    }
                    for f in corroboration.contradictions
                ],
            }
            if corroboration
            else None
        ),
        "rows_inserted": inserted,
        "restored": {
            table: [
                {"id": p.row_id, "name": p.label, "values": p.values}
                if table != "agent_activities"
                else {"id": p.row_id}
                for p in plans
                if p.table == table and p.reason == "restorable"
            ]
            for table in RESTORE_TABLES
        },
        "already_present": {
            table: [p.row_id for p in plans if p.table == table and p.reason == "already_present"]
            for table in RESTORE_TABLES
        },
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="目标库（默认生产库）")
    ap.add_argument(
        "--from-backup", type=Path, default=DEFAULT_SOURCE,
        help="身份主源快照，全程 immutable=1 只读",
    )
    ap.add_argument(
        "--corroborate-with", type=Path, default=DEFAULT_CORROBORANT,
        help="第三方复核源快照（只读）；不存在则跳过复核并高声告警",
    )
    ap.add_argument(
        "--verify-journal", type=Path, default=DEFAULT_VERIFY_JOURNAL,
        help="07-29 回采 journal，作为 token 值的独立比对源（只读）",
    )
    ap.add_argument("--team", default=RESTORE_TEAM_ID, help="要恢复的队 id")
    ap.add_argument("--batch-ts", default=BATCH_TS, help="写入 tokens_measured_at 的批次时刻")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只出报告）")
    ap.add_argument("--journal", type=Path, help="--apply 必给：恢复凭证落点（已存在则硬拒）")
    ap.add_argument(
        "--pre-backup-dir", type=Path, help="前置备份落点目录（默认与目标库同目录）",
    )
    return ap


def _check_args(args: argparse.Namespace) -> int:
    """入口处的硬拒清单。返回非 0 即退出码。"""
    if not args.db.exists():
        print(f"目标库不存在：{args.db}", file=sys.stderr)
        return 2
    if not args.from_backup.exists():
        print(f"身份主源不存在：{args.from_backup}", file=sys.stderr)
        return 2
    if not args.verify_journal.exists():
        print(f"回采 journal 不存在：{args.verify_journal}", file=sys.stderr)
        return 2
    if args.db.resolve() == args.from_backup.resolve():
        print("❌ 目标库与身份主源是同一个文件 —— 源必须只读，绝不允许被写", file=sys.stderr)
        return 2
    if args.apply and args.journal is None:
        print("❌ --apply 必须给 --journal —— 那是这次恢复的唯一凭证", file=sys.stderr)
        return 2
    if args.journal is not None and args.journal.exists():
        print(
            f"❌ journal 目标文件已存在：{args.journal}\n"
            "   二次 apply 会把它覆盖成恢复后的状态，凭证就没了。换个文件名。",
            file=sys.stderr,
        )
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bad = _check_args(args)
    if bad:
        return bad

    expectation = EXPECTATIONS.get(args.team)

    print(f"目标库：{args.db}")
    print(f"模式：{'APPLY（写入）' if args.apply else 'DRY-RUN（只读，mode=ro）'}")
    print(f"身份主源（immutable=1 只读）：{args.from_backup}")
    print(f"账的凭证（只读）：{args.verify_journal}")
    print(f"恢复范围：team {args.team}（整支队；报告 §4.1 点名的另 10 行 husk 不在范围内）")
    print(f"批次时刻：{args.batch_ts}（原批次，**不是** now() —— 该列兼任回采签名与采集健康度）")
    if expectation is None:
        print("⚠ 该队没有登记期望指纹，计数/合计闸本次跳过（只有已取证的队才有硬期望）")

    src = open_immutable(args.from_backup)
    tgt = open_target(args.db, writable=args.apply)
    corro_con: sqlite3.Connection | None = None
    try:
        target_marker = clock_marker(tgt)
        source_marker = clock_marker(src)
        shift_source = needs_clock_shift(source_marker, target_marker)
        print()
        print("=" * 92)
        print("时钟制式（PRAGMA user_version；混口径事后不可分辨，所以是显式判据不是假设）")
        print("-" * 92)
        print(f"  目标库 = {target_marker} │ 身份主源 = {source_marker} │ "
              f"换算：{'本地墙钟 → UTC（按平移脚本的列清单与位移函数）' if shift_source else '不需要，同制式'}")

        team_row = load_source_team(src, args.team)
        if team_row is None:
            print(f"❌ 身份主源里没有 team {args.team}", file=sys.stderr)
            return 2
        agent_rows = load_source_agents(src, args.team)
        agent_ids = [str(r["id"]) for r in agent_rows]
        activity_rows = load_source_activities(src, agent_ids)
        activity_ids = [str(r["id"]) for r in activity_rows]

        team_cols = set(table_columns(src, "teams"))
        agent_cols = set(table_columns(src, "agents"))
        activity_cols = set(table_columns(src, "agent_activities"))

        print()
        print(f"主源中读到：1 队 / {len(agent_rows)} 行 agents / {len(activity_rows)} 条 activities")
        for table, cols in (
            ("teams", team_cols), ("agents", agent_cols), ("agent_activities", activity_cols)
        ):
            for col in sorted(set(table_columns(tgt, table)) - cols):
                note = MISSING_COLUMN_NOTES.get(
                    (table, col), "（无说明 —— 下面会当作 schema 漂移中止）"
                )
                print(f"  schema 差异：目标库有而主源没有 {table}.{col} —— {note}")

        # 第三方复核 —— 主源之外另一份快照怎么说
        corroboration: CorroborationReport | None = None
        if args.corroborate_with and args.corroborate_with.exists():
            corro_con = open_immutable(args.corroborate_with)
            corro_marker = clock_marker(corro_con)
            corroboration = CorroborationReport(
                source=str(args.corroborate_with),
                shifted=needs_clock_shift(corro_marker, target_marker),
            )
            corroborate_rows(corroboration, corro_con, "teams", [team_row])
            corroborate_rows(corroboration, corro_con, "agents", agent_rows)
            corroborate_rows(corroboration, corro_con, "agent_activities", activity_rows)
        print_corroboration(corroboration)
        if corroboration and corroboration.contradictions:
            lines = "\n".join(
                f"   - [{f.table}] {f.row_id[:8]} {f.column}: 主源={f.primary!r} vs "
                f"复核源={f.secondary!r}"
                for f in corroboration.contradictions[:20]
            )
            msg = f"两份快照对 {len(corroboration.contradictions)} 处身份列说法不一致：\n{lines}"
            raise VerificationError(msg)

        journal_ledger = load_journal_ledger(args.verify_journal)
        checks = verify_ledger(agent_rows, journal_ledger, batch_ts=args.batch_ts)
        print_checks(checks)

        overlays = {c.agent_id: c.overlay for c in checks}
        plans = plan_table(tgt, "teams", [team_row], team_cols, shift_clock=shift_source)
        plans += plan_table(
            tgt, "agents", agent_rows, agent_cols, overlays=overlays, shift_clock=shift_source
        )
        plans += plan_table(
            tgt, "agent_activities", activity_rows, activity_cols,
            label_col="tool_name", shift_clock=shift_source,
        )
        print_plan(plans)

        fp_before = workflow_tokens_fingerprint(tgt)
        print()
        print("=" * 92)
        print("硬约束：写面是封闭集合，只 INSERT 且只限本次 id")
        print("-" * 92)
        print(f"  写表白名单：{sorted(WRITABLE_TABLES)}（无 UPDATE / DELETE 路径）")
        print(
            f"  禁改列：{FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]} —— 不在写面内，"
            f"并用回采脚本同一份逐行指纹做前后比对"
        )
        print(
            f"  恢复前指纹：{fp_before['rows']} 行 / sum={fp_before['sum']:,} / "
            f"sha256={fp_before['sha256'][:16]}…"
        )

        restorable = [p for p in plans if p.reason == "restorable"]
        print()
        print("=" * 92)
        print(f"待恢复合计：{len(restorable)} 行")

        clock_note = {
            "target_user_version": target_marker,
            "source_user_version": source_marker,
            "source_shifted_to_utc": shift_source,
            "shifted_columns": {t: list(LOCAL_COLUMNS.get(t, ())) for t in RESTORE_TABLES}
            if shift_source
            else {},
        }

        if not args.apply:
            print("dry-run 结束，一个字节都没写。确认无误后由缔造者批准 --apply --journal <路径>。")
            print("幂等验收：--apply 之后重跑本脚本，待恢复应为 0 行（全部 already_present）。")
            return 0

        pre_backup = make_pre_backup(args.db, args.pre_backup_dir or args.db.parent)
        print(f"前置备份已生成（SQLite 备份 API，含 WAL 内容）：{pre_backup}")

        inserted = 0
        tgt.execute("BEGIN IMMEDIATE")
        try:
            for plan in restorable:
                inserted += insert_row(tgt, plan)
            problems = verify_after(
                tgt, args.team, agent_ids, activity_ids, expectation, batch_ts=args.batch_ts
            )
            fp_after = workflow_tokens_fingerprint(tgt)
            if fp_after["sha256"] != fp_before["sha256"]:
                problems.append(
                    f"{FORBIDDEN_COLUMN[0]}.{FORBIDDEN_COLUMN[1]} 指纹变了 —— ctx_last 口径被动过"
                )
            if problems:
                tgt.execute("ROLLBACK")
                print()
                print("❌ 事务内验收未过，已整事务回滚，目标库一字未改：", file=sys.stderr)
                for p in problems:
                    print(f"   - {p}", file=sys.stderr)
                return 4
            tgt.execute("COMMIT")
        except Exception:
            tgt.execute("ROLLBACK")
            raise

        fp_final = workflow_tokens_fingerprint(tgt)
        print()
        print(
            f"已恢复：{inserted} 行（计划 {len(restorable)} 行；"
            f"差额 = 期间已被别人插入的行，守卫让路是预期行为）"
        )
        print("  ✓ 行与账同一事务落地 —— 中间不存在「无账 husk」窗口，reap tick 无从下手")
        print(f"  ✓ 禁改列逐行未变：sum={fp_final['sum']:,} / sha256={fp_final['sha256'][:16]}…")
        if expectation is not None:
            print(
                f"  ✓ 期望指纹全中：{expectation.agents} 行 agents / "
                f"{expectation.activities} 条 activities / 四层合计 {expectation.token_total:,}"
            )

        args.journal.parent.mkdir(parents=True, exist_ok=True)
        args.journal.write_text(
            json.dumps(
                build_journal(
                    args, plans, checks, corroboration, clock_note,
                    fp_before, fp_final, inserted, pre_backup,
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"journal 已落盘：{args.journal}")
        print("重跑本脚本应报告 0 行待恢复 —— 这就是幂等验收。")
        return 0
    except VerificationError as exc:
        print()
        print(f"❌ 校验失败，恢复中止（目标库一字未改）：{exc}", file=sys.stderr)
        return 3
    except SchemaDriftError as exc:
        print()
        print(f"❌ schema 漂移，恢复中止（目标库一字未改）：{exc}", file=sys.stderr)
        return 5
    except ClockConventionError as exc:
        print()
        print(f"❌ 时钟制式不可换算，恢复中止（目标库一字未改）：{exc}", file=sys.stderr)
        return 6
    finally:
        src.close()
        tgt.close()
        if corro_con is not None:
            corro_con.close()


if __name__ == "__main__":
    raise SystemExit(main())
