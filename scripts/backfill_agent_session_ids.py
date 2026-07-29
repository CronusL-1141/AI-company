#!/usr/bin/env python3
"""历史回填：agents.session_id + Leader 行 transcript_path。默认 dry-run。

规格见 docs/token-attribution-v1-design.md §2.6 / §7 阶段1。要解决的问题是：归因链
上「session → agent」这一跳在库里等于不存在（取证时 2,568 行只有 2 行 session_id
非空），而这一跳**根本不需要新采集**——它编码在 transcript 路径里：

    ~/.claude/projects/<slug>/<session_id>/subagents/[workflows/<wf>/]agent-<cc>.jsonl

三件事决定了这个脚本的形状：

1. **只写空列，从不覆盖非空值。** 幂等因此是结构性的而不是靠标记位：跑完一次目标列
   就非空了，再跑天然零变更。已有值与派生值冲突的行**一行都不动**，只在报告里点名
   （``conflict``）——覆盖等于用推断抹掉观测。
2. **写不了的行必须分类，不许静默跳过。** 每一行要么被写，要么带一个原因码进报告
   （no-data ≠ zero，Council 纪律①）。原因码与设计 §3.4 的未归因分类同源。
3. **改变活体行为的部分单独开关。** 回填 Leader 的 session_id 会改变
   ``_find_leader`` / ``_on_session_start`` 的解析结果（它们取同会话 leader 里
   **created_at 最早**的一行），这是活体行为变更而不是纯补数，所以默认**不做**，
   要做得显式加 ``--leader-session-id``，且 dry-run 会逐个会话打印"将改成哪一行"。

前置条件（务必先读报告的「风险」栏）：``agents`` 的登记去重有一条按
``session_id + name`` 的旁路（``repository.find_agent_by_session``，``limit(1)`` 且
无 ORDER BY，实际取最早那行）。回填让历史行重新进入这条旁路的可见范围，于是未来
一次**同名同会话**的新派工可能被判成"复用旧行"而就地覆盖旧行的 token 列。取证时刻
待回填的 1,923 行里 ``(session, name)`` 全部唯一、零现存碰撞，但通用名（``Explore``
这类）未来重名是可能的。脚本会实测这一点：一旦发现会新造碰撞，``--apply`` 硬拒，
除非显式 ``--ack-collisions``。

用法::

    python3 scripts/backfill_agent_session_ids.py                     # dry-run 全量报告
    python3 scripts/backfill_agent_session_ids.py --db /tmp/copy.db   # 对副本演练
    python3 scripts/backfill_agent_session_ids.py --sample 30         # 抽 30 行供人工核对
    python3 scripts/backfill_agent_session_ids.py --apply             # 缔造者亲自执行

``--apply`` 前先备份::

    cp ~/.claude/data/ai-team-os/aiteam.db \
       ~/aiteam.db.bak-sessionid-$(date +%Y%m%d%H%M%S)
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiteam.api.session_probe import project_slug  # noqa: E402
from aiteam.services.transcript_path import (  # noqa: E402
    parse_transcript_path,
    slug_matches_root,
)

DEFAULT_DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# 原因码 —— 与设计 §3.4 的未归因分类同源。"写不了"不是一种状态，是好几种，
# 处置方式各不相同，所以报告必须分列而不是给一个总数。
REASONS = {
    "written": "可写入（本次 dry-run 的候选 / --apply 的实际写入）",
    "already_set": "目标列已有值且与派生值一致 —— 幂等重跑必然全部落在这里",
    "conflict": "目标列已有值但与派生值不同 —— 不动，留待人判",
    "no_transcript_path": "该行从未登记 transcript 路径，无从派生",
    "unparseable_path": "登记了路径但不符合 CC 的目录形态，解析器拒绝猜",
    "no_session_id": "Leader 行既无 session_id 也无 team.config.owner_session_id",
    "no_root_path": "解析不到项目 root_path，拼不出 slug",
    "transcript_gone": "路径拼得出但文件已不在磁盘上 —— 回采窗口已关闭，只增不减",
    "slug_mismatch": "文件在磁盘上但落在另一个 slug 下 —— 归属存疑，人看一眼",
}


@dataclass
class Row:
    """一行的判定结果。``value`` 只在 written 时有意义。

    ``warn`` 与 ``note`` 分开：前者是"这行有事"（报告里带 ⚠，人必须看），后者只是
    补充上下文。混成一个字段会让真警告淹在噪声里。
    """

    agent_id: str
    name: str
    reason: str
    column: str
    value: str = ""
    warn: str = ""
    note: str = ""


@dataclass
class Job:
    title: str
    rows: list[Row] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.reason] = out.get(r.reason, 0) + 1
        return out

    def written(self) -> list[Row]:
        return [r for r in self.rows if r.reason == "written"]


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
        select a.id, a.name, a.role, a.status, a.created_at,
               a.session_id, a.transcript_path,
               t.name as team_name, t.config as team_config,
               coalesce(p1.root_path, p2.root_path) as root_path
        from agents a
        left join teams t on t.id = a.team_id
        left join projects p1 on p1.id = a.project_id
        left join projects p2 on p2.id = t.project_id
        order by a.created_at
        """
    ).fetchall()


def index_main_transcripts() -> dict[str, list[str]]:
    """磁盘上全部主会话 transcript：session_id -> [slug, ...]（可能多个）。"""
    index: dict[str, list[str]] = {}
    if not PROJECTS_DIR.is_dir():
        return index
    for slug_dir in PROJECTS_DIR.iterdir():
        if not slug_dir.is_dir():
            continue
        try:
            entries = list(slug_dir.iterdir())
        except OSError:
            continue
        for f in entries:
            if f.suffix == ".jsonl" and f.is_file():
                index.setdefault(f.stem, []).append(slug_dir.name)
    return index


def owner_session_of(row: sqlite3.Row) -> str:
    """Leader 行的会话身份：自身列优先，其次队 config 里出生时盖的 owner_session_id。

    容器队的 ``config.owner_session_id`` 是建队那一刻盖上的（见
    ``hook_translator._find_or_create_session_team``），比队名前缀可靠——队名只留了
    session 的前 8 位，理论上会撞。队名前缀**不**作为兜底：8 位前缀不是身份。
    """
    if row["session_id"]:
        return str(row["session_id"])
    try:
        cfg = json.loads(row["team_config"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    return str(cfg.get("owner_session_id") or "")


# ---------------------------------------------------------------------------
# 三个 Job
# ---------------------------------------------------------------------------
def job_subagent_session_id(agents: list[sqlite3.Row]) -> Job:
    """Job A：子 agent 的 session_id ← transcript 路径派生。"""
    job = Job("A. agents.session_id ← transcript_path 派生（子 agent）")
    for a in agents:
        if a["role"] == "leader":
            continue
        path = a["transcript_path"] or ""
        if not path:
            job.rows.append(Row(a["id"], a["name"], "no_transcript_path", "session_id"))
            continue
        ref = parse_transcript_path(path)
        if ref is None:
            job.rows.append(
                Row(a["id"], a["name"], "unparseable_path", "session_id", note=path)
            )
            continue
        warn = ""
        if a["root_path"] and not slug_matches_root(ref.project_slug, a["root_path"]):
            # 归属**不**因此改写：slug 是有损映射，对不上只说明值得人看一眼。
            warn = f"slug={ref.project_slug} 与登记项目 root_path 对不上"
        current = a["session_id"] or ""
        if current == ref.session_id:
            job.rows.append(
                Row(a["id"], a["name"], "already_set", "session_id", ref.session_id, warn)
            )
        elif current:
            job.rows.append(
                Row(a["id"], a["name"], "conflict", "session_id", ref.session_id,
                    warn=f"库中已有 {current}，派生得 {ref.session_id}")
            )
        else:
            job.rows.append(
                Row(a["id"], a["name"], "written", "session_id", ref.session_id, warn)
            )
    return job


def job_leader_transcript_path(
    agents: list[sqlite3.Row], disk: dict[str, list[str]]
) -> Job:
    """Job B：Leader 行的 transcript_path ← 按 slug + session_id 反查主会话文件。"""
    job = Job("B. agents.transcript_path ← session_probe 反查（Leader 行）")
    for a in agents:
        if a["role"] != "leader":
            continue
        sid = owner_session_of(a)
        if not sid:
            job.rows.append(Row(a["id"], a["name"], "no_session_id", "transcript_path"))
            continue
        if not a["root_path"]:
            job.rows.append(Row(a["id"], a["name"], "no_root_path", "transcript_path", note=sid))
            continue
        expected_slug = project_slug(a["root_path"])
        on_disk = disk.get(sid, [])
        if not on_disk:
            job.rows.append(
                Row(a["id"], a["name"], "transcript_gone", "transcript_path", note=sid)
            )
            continue
        if expected_slug in on_disk:
            slug, reason, warn = expected_slug, "written", ""
        else:
            # 文件在，但落在别的 slug 下。以**文件真相**为准写路径（回采要的是文件），
            # 但单独标一类原因码，且绝不因此改写 project_id —— 归属只认登记列。
            slug, reason = on_disk[0], "slug_mismatch"
            warn = f"期望 {expected_slug}，实际落在 {'/'.join(on_disk)}"
        path = str(PROJECTS_DIR / slug / f"{sid}.jsonl")
        current = a["transcript_path"] or ""
        if current == path:
            job.rows.append(
                Row(a["id"], a["name"], "already_set", "transcript_path", path, warn)
            )
        elif current:
            job.rows.append(
                Row(a["id"], a["name"], "conflict", "transcript_path", path,
                    warn=f"库中已有 {current}")
            )
        else:
            job.rows.append(Row(a["id"], a["name"], reason, "transcript_path", path, warn))
    return job


def job_leader_session_id(agents: list[sqlite3.Row]) -> Job:
    """Job C：Leader 行的 session_id ← team.config.owner_session_id。**默认不执行。**

    这一项与 A/B 不同：它会改变**活体解析结果**。``_on_session_start`` 复用
    Leader 的判据是"同 session_id 的 leader 行里 created_at 最早的一条"，所以一旦
    同一会话的多个幽灵行都补上 session_id，下次会话恢复复用的将是**最早**那行，
    而不是当前这行。设计上这正是"一会话一 Leader 行"想要的收敛方向（幽灵行的成因
    正是身份被抹后每次恢复都新建），但它是行为变更，必须由人拍板。
    """
    job = Job("C. agents.session_id ← team.config.owner_session_id（Leader 行，需 --leader-session-id）")
    for a in agents:
        if a["role"] != "leader":
            continue
        sid = owner_session_of(a)
        if not sid:
            job.rows.append(Row(a["id"], a["name"], "no_session_id", "session_id"))
            continue
        current = a["session_id"] or ""
        if current == sid:
            job.rows.append(Row(a["id"], a["name"], "already_set", "session_id", sid))
        elif current:
            job.rows.append(
                Row(a["id"], a["name"], "conflict", "session_id", sid,
                    warn=f"库中已有 {current}")
            )
        else:
            job.rows.append(
                Row(a["id"], a["name"], "written", "session_id", sid,
                    note=f"team={a['team_name']}")
            )
    return job


# ---------------------------------------------------------------------------
# 风险检测
# ---------------------------------------------------------------------------
def detect_name_collisions(
    agents: list[sqlite3.Row], job_a: Job, job_c: Job, include_c: bool
) -> list[str]:
    """回填后是否出现 ``(session_id, name)`` 重复 —— 登记去重旁路的踩雷点。

    ``repository.find_agent_by_session(session_id, name)`` 是 ``limit(1)`` 且无
    ORDER BY，取到的是最早那行。同一 (session, name) 下有多行时，未来一次同名新派工
    会被判成"复用"并就地覆盖旧行的 token 列——正好毁掉本项目要建的账。
    """
    planned: dict[str, str] = {}
    for job, on in ((job_a, True), (job_c, include_c)):
        if not on:
            continue
        for r in job.written():
            if r.column == "session_id":
                planned[r.agent_id] = r.value

    buckets: dict[tuple[str, str], list[str]] = {}
    for a in agents:
        sid = planned.get(a["id"]) or (a["session_id"] or "")
        if not sid:
            continue
        # workflow 扇出走独立注册路径（按 cc_agent_id 去重，不按名字），不受这条旁路影响
        if a["role"] == "workflow-subagent":
            continue
        buckets.setdefault((sid, a["name"]), []).append(a["id"])

    return [
        f"(session={sid[:8]}, name={name!r}) → {len(ids)} 行"
        for (sid, name), ids in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        if len(ids) > 1
    ]


def describe_leader_resolution_shift(agents: list[sqlite3.Row], job_c: Job) -> list[str]:
    """Job C 会把每个会话的 Leader 解析结果改成哪一行 —— 逐会话打印，供人拍板。"""
    by_session: dict[str, list[sqlite3.Row]] = {}
    planned = {r.agent_id: r.value for r in job_c.written()}
    for a in agents:
        if a["role"] != "leader":
            continue
        sid = planned.get(a["id"]) or (a["session_id"] or "")
        if sid:
            by_session.setdefault(sid, []).append(a)
    lines = []
    for sid, rows in sorted(by_session.items(), key=lambda kv: -len(kv[1])):
        rows.sort(key=lambda r: r["created_at"] or "")
        winner = rows[0]
        lines.append(
            f"session {sid[:8]}: {len(rows)} 个 Leader 行 → 解析将命中最早的 "
            f"{winner['id'][:8]}（{winner['created_at']}, team={winner['team_name']}, "
            f"status={winner['status']}）"
        )
    return lines


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def print_job(job: Job, sample_n: int, rng: random.Random) -> None:
    print()
    print("=" * 78)
    print(job.title)
    print("-" * 78)
    counts = job.counts()
    total = sum(counts.values())
    for reason, desc in REASONS.items():
        if reason in counts:
            print(f"  {counts[reason]:6d}  {reason:<20} {desc}")
    print(f"  {total:6d}  合计")

    writable = [r for r in job.rows if r.reason in ("written", "slug_mismatch")]
    if not writable:
        return
    picks = writable if len(writable) <= sample_n else rng.sample(writable, sample_n)
    picks.sort(key=lambda r: r.agent_id)
    print()
    print(f"  ── 抽样 {len(picks)}/{len(writable)} 行（供人工核对）──")
    for r in picks:
        flag = "" if r.reason == "written" else f"  [{r.reason}]"
        extra = f"  ({r.note})" if r.note else ""
        warn = f"  ⚠ {r.warn}" if r.warn else ""
        print(f"    {r.agent_id[:8]}  {r.name[:28]:<28} {r.column}={r.value}{flag}{extra}{warn}")


def apply_job(con: sqlite3.Connection, job: Job) -> int:
    """只写空列：SQL 自带 ``(col IS NULL OR col='')`` 守卫，与判定层双保险。

    判定已经过滤过一次，这里再守一次是因为 dry-run 与 apply 之间库可能已经变了
    （活体系统随时在写）。守卫在 SQL 里意味着并发下也不会覆盖别人刚写的值。
    """
    n = 0
    for r in job.rows:
        if r.reason not in ("written", "slug_mismatch"):
            continue
        cur = con.execute(
            f"update agents set {r.column} = ? "  # noqa: S608 — 列名来自本文件常量，非外部输入
            f"where id = ? and ({r.column} is null or {r.column} = '')",
            (r.value, r.agent_id),
        )
        n += cur.rowcount
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="目标库（默认生产库）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只出报告）")
    ap.add_argument("--sample", type=int, default=30, help="每个 Job 抽样打印的行数")
    ap.add_argument("--seed", type=int, default=0, help="抽样随机种子（默认 0，可复现）")
    ap.add_argument(
        "--leader-session-id", action="store_true",
        help="额外执行 Job C（回填 Leader 的 session_id）—— 会改变活体 Leader 解析结果",
    )
    ap.add_argument(
        "--ack-collisions", action="store_true",
        help="已人审 (session,name) 碰撞清单，允许带碰撞写入",
    )
    args = ap.parse_args()

    if not args.db.exists():
        print(f"库不存在：{args.db}", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    con = open_db(args.db, writable=args.apply)
    try:
        agents = load_agents(con)
        disk = index_main_transcripts()

        job_a = job_subagent_session_id(agents)
        job_b = job_leader_transcript_path(agents, disk)
        job_c = job_leader_session_id(agents)

        print(f"库：{args.db}")
        print(f"模式：{'APPLY（写入）' if args.apply else 'DRY-RUN（只读，mode=ro）'}")
        print(f"agents 总行数：{len(agents)}；磁盘主会话 transcript：{sum(len(v) for v in disk.values())} 份")
        print("口径：只写空列，从不覆盖非空值；重跑必然零变更（全部落进 already_set）")

        print_job(job_a, args.sample, rng)
        print_job(job_b, args.sample, rng)
        print_job(job_c, args.sample, rng)
        if not args.leader_session_id:
            print()
            print("  Job C 默认不执行（需 --leader-session-id）。上面只是它的判定预览。")

        print()
        print("=" * 78)
        print("风险栏")
        print("-" * 78)
        collisions = detect_name_collisions(agents, job_a, job_c, args.leader_session_id)
        if collisions:
            print("  ⚠ 回填后出现 (session_id, name) 重复 —— 未来同名派工可能被判成"
                  "「复用旧行」并覆盖其 token 列：")
            for line in collisions[:20]:
                print(f"      {line}")
            if len(collisions) > 20:
                print(f"      …… 另有 {len(collisions) - 20} 组")
        else:
            print("  ✓ 回填后 (session_id, name) 全部唯一，登记去重旁路不会踩到历史行")

        shift = describe_leader_resolution_shift(agents, job_c if args.leader_session_id else Job(""))
        if args.leader_session_id:
            print()
            print("  Job C 将改变以下会话的 Leader 解析结果：")
            for line in shift:
                print(f"      {line}")

        jobs = [job_a, job_b] + ([job_c] if args.leader_session_id else [])
        planned = sum(len([r for r in j.rows if r.reason in ("written", "slug_mismatch")]) for j in jobs)
        print()
        print("=" * 78)
        print(f"待写入合计：{planned} 行")

        if not args.apply:
            print("dry-run 结束，一个字节都没写。确认无误后由缔造者执行 --apply（先备份）。")
            return 0

        if collisions and not args.ack_collisions:
            print("拒绝写入：存在 (session,name) 碰撞且未 --ack-collisions。", file=sys.stderr)
            return 3

        written = sum(apply_job(con, j) for j in jobs)
        con.commit()
        print(f"已写入：{written} 行（判定候选 {planned} 行；差额=期间已被活体写上的行）")
        print("重跑本脚本应报告 0 待写入 —— 这就是幂等验收。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
