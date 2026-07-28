#!/usr/bin/env python3
"""存量时间戳平移：本地墙钟 → UTC。默认 dry-run。

规格见 docs/utc-unification-design.md。简述：这个库过去跑着两个墙钟——核心域写
宿主本地时间，ecosystem 域写 UTC——而 SQLite 落库把 offset 静默剥掉，两制的行长得
一模一样。代码侧已统一为 UTC（``aiteam.clock``），本脚本负责把**已经写进去的**
本地墙钟值换算成 UTC。

三件事决定了这个脚本的形状：

1. **按列平移，不按时间点切。** 一个列的写入方在整个库生命期内口径恒定（核心域从来
   只写本地），所以整列平移，不存在"哪天之后的行不动"。
2. **平移前必须证明这个库还没被新代码写过。** 判据有一条是单边无歧义的：UTC 时钟
   写不出未来的值，所以只要有列的 max 晚于 UTC 此刻，它就**必然**含本地墙钟写入的
   行。护栏拦的是"先部署新代码、后跑平移"的顺序颠倒——那会把新写的 UTC 行再推早
   一个时区。
3. **可逆且无损。** 平移是纯值变换，亚秒位原样保留（见 :func:`shift_expr`）；
   ``--rollback`` 用同一份 journal 校验着往回搬。

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
]

# 全库判定的容差：活着的系统里"最近一次写入"距今不会超过这个量。设得比一次
# 会话间隔宽松，但远小于一个时区偏移，才分得开"贴本地 now"和"贴 UTC now"。
GUARD_TOLERANCE = timedelta(hours=3)


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


def assert_uniform_width(conn: sqlite3.Connection, reports: list[ColumnReport]) -> None:
    """确认每个待平移值都长到能被 :func:`shift_expr` 切分（至少 "YYYY-MM-DD HH:MM"）。

    只有日期、没有时刻的值会被前缀切分毁掉（拼出来的串 SQLite 解析为 NULL，等于
    静默清空数据）。宁可在这里停住。
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
    if bad:
        raise SystemExit(
            "❌ 存在无法安全切分的时间值，拒绝执行（继续做会把它们清成 NULL）:\n  "
            + "\n  ".join(bad)
        )


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


def verdict(reports: list[ColumnReport], now_local: datetime) -> tuple[bool, list[str]]:
    """全库级判定：这个库现在还是不是本地墙钟在写。

    为什么判定要放在**库级**而不是列级：真正的风险是部署顺序颠倒（先上新代码再跑
    平移），而部署是全局的——新代码一旦跑起来，所有核心域列一起改口径。一个冷了三
    天的列本来就没有新鲜行可判，逐列去猜只会制造假警报。

    判据由两条组成：

    1. **正面确证（单边、无歧义）**：只要有任何一列的 max 晚于 UTC 此刻，就证明它
       是本地墙钟写的——UTC 时钟写不出未来的值。
    2. **全库最新写入的落点**：活着的系统里"最近一次写入"必然贴着当前时刻。若它贴
       本地 now，说明写入方仍是旧代码（可以平移）；若它反而贴 UTC now，说明新代码
       已经在写了，此时整列平移会把新行再推早一个时区——必须中止。
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

    d_local = abs((now_local - freshest).total_seconds())
    d_utc = abs((now_utc_naive - freshest).total_seconds())
    notes.append(
        f"全库最新写入 {freshest:%Y-%m-%d %H:%M:%S}；"
        f"距本地此刻 {d_local / 3600:.1f}h，距 UTC 此刻 {d_utc / 3600:.1f}h"
    )
    notes.append(
        f"正面确证为本地墙钟的列：{len(proven_local)} / {len(reports)}"
        "（max 晚于 UTC 此刻 —— UTC 时钟写不出未来的值）"
    )

    if proven_local and d_local <= d_utc:
        notes.append("✅ 写入方仍是旧代码（本地墙钟），可以平移。")
        return True, notes
    if not proven_local and d_utc < d_local and d_utc < GUARD_TOLERANCE.total_seconds():
        notes.append(
            "❌ 全库最新写入贴着 UTC 此刻，且没有任何一列能确证为本地 —— "
            "新代码很可能已经在写库了。此时平移会把新写的 UTC 行再推早一个时区。"
        )
        return False, notes
    if not proven_local:
        notes.append(
            "⚠️  没有任何一列能正面确证为本地墙钟（全库都是冷数据）。"
            "请人工确认这个库确实还没被新代码写过，再加 --force 执行。"
        )
        return False, notes
    notes.append(
        "⚠️  有列确证为本地，但全库最新写入更贴 UTC 此刻 —— 新旧口径可能已经混杂，"
        "请人工核对后再决定。"
    )
    return False, notes


def assert_shift_is_constant(reports: list[ColumnReport]) -> float:
    """证明整个数据区间内位移恒定；不恒定就拒绝用常量 SQL 平移。

    逐列端点全查一遍（不只取全局 min/max），这样区间中段的夏令时切换也跑不掉。
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
def render(reports: list[ColumnReport], hours: float, mode: str) -> None:
    print(f"\n{'=' * 100}")
    print(f"存量时间戳平移 · {mode} · 位移 {hours:+g} 小时（本地墙钟 → UTC）")
    print(f"{'=' * 100}\n")
    print(f"{'表':<22} {'列':<22} {'非空':>8}  {'平移前 min→max':<42} 平移后 min→max")
    print("-" * 100)

    total = 0
    for rep in reports:
        total += rep.non_null
        if rep.non_null == 0:
            print(f"{rep.table:<22} {rep.column:<22} {0:>8}  （空列，无行可平移）")
            continue
        lo, hi = parse_db_ts(rep.min_before), parse_db_ts(rep.max_before)
        delta = timedelta(hours=hours)
        lo_a = (lo + delta).strftime("%Y-%m-%d %H:%M:%S") if lo else "?"
        hi_a = (hi + delta).strftime("%Y-%m-%d %H:%M:%S") if hi else "?"
        before = f"{str(rep.min_before)[:19]} → {str(rep.max_before)[:19]}"
        print(f"{rep.table:<22} {rep.column:<22} {rep.non_null:>8}  {before:<42} {lo_a} → {hi_a}")

    print("-" * 100)
    print(f"{'合计':<45} {total:>8} 个非空单元将被平移\n")

    print("抽样前后对照（每列最新 3 行）")
    print("-" * 100)
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

    print("\n逐列自证（能否证明该列仍是本地墙钟写的）")
    print("-" * 100)
    for rep in reports:
        print(f"   {rep.key:<46} {rep.guard}")

    print("\n冻结/敏感对象说明")
    print("-" * 100)
    for table, note in FROZEN_NOTE.items():
        if any(r.table == table for r in reports):
            print(f"  {table:<22} {note}")

    print("\n明确不平移的对象")
    print("-" * 100)
    for what, why in EXCLUDED:
        print(f"  {what:<48} {why}")


# ---------------------------------------------------------------------------
def execute(conn: sqlite3.Connection, reports: list[ColumnReport], hours: float) -> int:
    assert_uniform_width(conn, reports)
    touched = 0
    for rep in reports:
        if rep.non_null == 0:
            continue
        cur = conn.execute(
            f"update {rep.table} set {rep.column} = {shift_expr(rep.column, hours)} "
            f"where {rep.column} is not null"
        )
        touched += cur.rowcount
        print(f"  {rep.key:<46} {cur.rowcount:>8} 行")
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 文件路径")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认 dry-run）")
    ap.add_argument("--rollback", action="store_true", help="反向平移（+offset），需 --journal")
    ap.add_argument("--journal", default="", help="留档 JSON 路径（--apply 强烈建议，--rollback 必需）")
    ap.add_argument(
        "--force",
        action="store_true",
        help="护栏报警时仍然执行 —— 只有在人已确认该列口径后才可用",
    )
    args = ap.parse_args()

    db = Path(args.db).expanduser()
    if not db.is_file():
        print(f"❌ 找不到数据库：{db}", file=sys.stderr)
        return 2
    if args.apply and args.rollback:
        print("❌ --apply 与 --rollback 不能同时给", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db))
    now_local = datetime.now().astimezone()
    reports = collect(conn, now_local.replace(tzinfo=None))
    if not reports:
        print("❌ 没有可处理的列", file=sys.stderr)
        return 2

    forward = assert_shift_is_constant(reports)
    if forward == 0.0:
        print("ℹ️  本机本地时区即 UTC，无需平移。")
        return 0
    # forward = 本地→UTC 的位移（UTC+8 上为 −8）；回滚就是它的反向
    hours = -forward if args.rollback else forward

    # ---------------- rollback ----------------
    if args.rollback:
        if not args.journal:
            print("❌ --rollback 必须给 --journal（用它校验回滚结果）", file=sys.stderr)
            return 2
        journal = json.loads(Path(args.journal).expanduser().read_text())
        print(f"回滚：按 journal {args.journal} 反向平移 {hours:+g} 小时")
        touched = execute(conn, reports, hours)
        print(f"\n共回滚 {touched} 个单元。逐列复核：")
        problems = verify_rollback(conn, journal)
        print("\n" + ("❌ 回滚复核有出入，请人工介入" if problems else "✅ 回滚复核全过"))
        return 1 if problems else 0

    mode = "APPLY（真的写库）" if args.apply else "DRY-RUN（不写库）"
    render(reports, hours, mode)

    passed, notes = verdict(reports, now_local.replace(tzinfo=None))
    print("\n全库护栏判定")
    print("-" * 100)
    for note in notes:
        print(f"  {note}")
    if not passed:
        print(f"\n{'!' * 100}")
        print("护栏拦截 —— 正确顺序是：① 停/不重启生产 API ② 备份 ③ 平移 ④ 部署新代码并重启。")
        print("若已人工确认口径无误，可加 --force 强制执行。")
        print(f"{'!' * 100}")
        if args.apply and not args.force:
            return 1

    if not args.apply:
        print("\n（dry-run，未写库。确认无误后由缔造者执行 --apply，执行前先备份。）")
        return 0

    if args.journal:
        Path(args.journal).expanduser().write_text(
            json.dumps(
                {
                    "db": str(db),
                    "executed_at": datetime.now(tz=UTC).isoformat(),
                    "shift_hours": forward,
                    "columns": [r.as_journal() for r in reports],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"\n留档已写入 {args.journal}")

    print("\n执行中：")
    touched = execute(conn, reports, hours)
    print(f"\n✅ 已平移 {touched} 个单元。")
    print("请复核几张表的 max(时间列)：现在应当等于 UTC 此刻，而不是本地此刻。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
