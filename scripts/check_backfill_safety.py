#!/usr/bin/env python3
"""I14 红线机检：历史回采脚本的三条硬约束（设计 §6.4）必须始终成立。

``scripts/backfill_token_usage.py`` 会一次性改写两千余行生产数据。它的三条硬约束里，
**第一条错了就无法挽回**：``workflow_agents.tokens`` 是 ctx_last 口径（末轮上下文水位），
本脚本产出的是 usage_sum（用量累加），实测差 5~25 倍。一旦把后者写进前者，混口径就被
**永久固化进历史数据且事后不可分辨**——比不回采糟糕得多（风险 R3）。

所以这条检查是**行为式**而不是文本式的：它真的建一个临时库、真的跑一次 ``--apply``、
真的比对禁改列的逐行指纹。文本扫描挡不住动态拼出来的 SQL，而这里的判据是"跑完之后那
一列有没有变"——任何将来的改动，不管用什么写法，只要碰了那一列就会被抓住。

全程只用 tempfile，一个字节都不碰生产库。

用法::

    python3 scripts/check_backfill_safety.py
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backfill_token_usage.py"
sys.path.insert(0, str(ROOT / "src"))


def load_backfill():
    spec = importlib.util.spec_from_file_location("backfill_token_usage", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_token_usage"] = mod
    spec.loader.exec_module(mod)
    return mod


USAGE = {
    "input_tokens": 10,
    "output_tokens": 500,
    "cache_creation_input_tokens": 2000,
    "cache_read_input_tokens": 90000,
}
SENTINEL_TOKENS = 424242  # 禁改列里放一个显眼的哨兵值


def seed(tmp: Path) -> Path:
    """建一个最小但覆盖各分支的库：有/无 transcript、已测量、别名/完整型号。"""
    transcript = tmp / "agent-cc1.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps({
                "type": "assistant", "requestId": req,
                "message": {"model": "claude-opus-4-8", "usage": USAGE},
            })
            for req in ("r1", "r2")
        ) + "\n",
        encoding="utf-8",
    )

    db = tmp / "probe.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table agents (
            id text primary key, name text, role text, model text, created_at text,
            transcript_path text,
            input_tokens integer, output_tokens integer,
            cache_creation_tokens integer, cache_read_tokens integer,
            tokens_measured_at text, tokens_source text
        );
        create table workflow_agents (
            id text primary key, label text, model text, os_agent_id text,
            cc_agent_id text, created_at text, tokens integer
        );
        """
    )
    con.execute(
        "insert into agents values ('a1','w1','worker',null,'2026-07-20 10:00:00',?,"
        "null,null,null,null,null,null)",
        (str(transcript),),
    )
    con.execute(
        "insert into agents values ('a2','w2','worker',null,'2026-07-20 10:00:00',null,"
        "null,null,null,null,null,null)"
    )
    con.execute(
        "insert into agents values ('a3','w3','worker',null,'2026-07-20 10:00:00',?,"
        "1,2,3,4,'2026-07-25 00:00:00','transcript')",
        (str(transcript),),
    )
    con.execute(
        "insert into workflow_agents values ('wa1','l1','opus','a1','cc1',"
        "'2026-07-20 10:00:00', ?)",
        (SENTINEL_TOKENS,),
    )
    con.execute(
        "insert into workflow_agents values ('wa2','l2','claude-opus-5','a1','cc2',"
        "'2026-07-20 10:00:00', 999)"
    )
    con.commit()
    con.close()
    return db


def run_script(bf, db: Path, *argv: str) -> int:
    saved = sys.argv
    sys.argv = ["backfill", "--db", str(db), "--quiet", *argv]
    try:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return bf.main()
    finally:
        sys.argv = saved


def fingerprint(db: Path, bf) -> dict:
    con = sqlite3.connect(db)
    try:
        return bf.workflow_tokens_fingerprint(con)
    finally:
        con.close()


def check() -> list[str]:
    """返回违规清单，空 = 通过。"""
    bad: list[str] = []
    bf = load_backfill()

    # ── 硬约束① 静态面：禁改列不在写列白名单里 ───────────────────────────
    if bf.FORBIDDEN_COLUMN != ("workflow_agents", "tokens"):
        bad.append(f"FORBIDDEN_COLUMN 被改成了 {bf.FORBIDDEN_COLUMN} —— 禁改列的定义不该动")
    if bf.FORBIDDEN_COLUMN in bf.WRITABLE_COLUMNS:
        bad.append("workflow_agents.tokens 出现在写列白名单里 —— usage_sum 会污染 ctx_last 列")
    wa_writable = {c for t, c in bf.WRITABLE_COLUMNS if t == "workflow_agents"}
    if wa_writable - {"model"}:
        bad.append(f"workflow_agents 上多出可写列 {sorted(wa_writable - {'model'})} —— 只该写 model")
    agents_writable = {c for t, c in bf.WRITABLE_COLUMNS if t == "agents"}
    expected_agents = {
        "input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens",
        "tokens_measured_at", "tokens_source", "model",
    }
    if agents_writable != expected_agents:
        bad.append(
            f"agents 的可写列集合变了：多 {sorted(agents_writable - expected_agents)} / "
            f"少 {sorted(expected_agents - agents_writable)}"
        )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # ── 硬约束③ dry-run 不写 ────────────────────────────────────────
        db = seed(tmp)
        before = fingerprint(db, bf)
        if run_script(bf, db) != 0:
            bad.append("dry-run 返回非 0")
        con = sqlite3.connect(db)
        if con.execute("select count(*) from agents where tokens_measured_at is not null")\
                .fetchone()[0] != 1:
            bad.append("dry-run 写了数据 —— 默认必须只出报告")
        con.close()

        # ── 硬约束③ --apply 必须带 journal，且不覆盖已存在的 journal ──────
        if run_script(bf, db, "--apply") != 2:
            bad.append("--apply 没有 --journal 时未被拒绝 —— journal 是唯一的恢复凭证")
        occupied = tmp / "taken.json"
        occupied.write_text("{}")
        if run_script(bf, db, "--apply", "--journal", str(occupied)) != 2:
            bad.append("已存在的 journal 未被拒绝 —— 二次 apply 会覆盖掉回采前基线")

        # ── 硬约束① 行为面：真跑一次，禁改列逐行未变 ─────────────────────
        journal = tmp / "j.json"
        if run_script(bf, db, "--apply", "--journal", str(journal)) != 0:
            bad.append("--apply 返回非 0")
        after = fingerprint(db, bf)
        if after["sha256"] != before["sha256"]:
            bad.append(
                f"❗ workflow_agents.tokens 被回采改动了："
                f"sum {before['sum']} → {after['sum']}（sha {before['sha256'][:12]} → "
                f"{after['sha256'][:12]}）—— ctx_last 口径已被 usage_sum 污染"
            )
        con = sqlite3.connect(db)
        if con.execute("select tokens from workflow_agents where id='wa1'").fetchone()[0] \
                != SENTINEL_TOKENS:
            bad.append("哨兵行的 tokens 被动了")
        # apply 确实干了活（否则"未变"是因为什么都没做，这条检查就成了空转）
        measured = con.execute(
            "select count(*) from agents where tokens_measured_at is not null"
        ).fetchone()[0]
        if measured != 2:
            bad.append(f"回采未生效（已测量行 {measured}，期望 2）—— 机检可能在空转")
        if con.execute("select model from workflow_agents where id='wa1'").fetchone()[0] \
                != "claude-opus-4-8":
            bad.append("model 观测回填未生效 —— 机检可能在空转")
        con.close()
        if not journal.is_file():
            bad.append("journal 未落盘")
        else:
            data = json.loads(journal.read_text())
            if not data.get("forbidden_column", {}).get("unchanged"):
                bad.append("journal 未记录禁改列未变的凭证")
            if data.get("metric") != "usage_sum":
                bad.append("journal 未标注口径 —— 脱离口径的 token 数值没有意义")

        # ── 硬约束③ 幂等：重跑零变更 ────────────────────────────────────
        saved_out = sys.stdout
        import contextlib
        import io

        buf = io.StringIO()
        sys.argv = ["backfill", "--db", str(db), "--quiet"]
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            bf.main()
        sys.stdout = saved_out
        if "待写入合计：0 行" not in buf.getvalue():
            bad.append("--apply 后重跑仍有待写入行 —— 幂等失守")

        # ── 硬约束① 第二道拦：写入层对越界列当场炸 ───────────────────────
        job = bf.Job("probe", "workflow_agents")
        job.rows.append(bf.Row("workflow_agents", "wa1", "l1", "written", values={"tokens": 1}))
        con = sqlite3.connect(db)
        try:
            bf.apply_job(con, job)
            bad.append("apply_job 接受了越界列写入 —— 白名单 assert 已失效")
        except RuntimeError:
            pass
        finally:
            con.close()

        # ── 硬约束② 覆盖率分窗 ──────────────────────────────────────────
        con = bf.open_db(db, writable=False)
        agents = bf.load_agents(con)
        cohorts = bf.detect_backfill_cohorts(con)
        con.close()
        cov = bf.measure_coverage(agents, cohorts, leader=False)
        if cov.total != 3:
            bad.append(
                f"覆盖率分母 {cov.total} ≠ 3 —— 没有 transcript 的行被移出了分母，"
                "那正是局部冒充全貌（R2）"
            )
        projected = bf.project_coverage(cov, 5)
        if projected.incremental != cov.incremental:
            bad.append("回采改动了'增量采集'那一格 —— 一个数字掩盖了另一个数字（6.3-2）")
        if projected.total != cov.total:
            bad.append("回采改动了分母")
        line = cov.line()
        if not all(k in line for k in ("增量采集", "历史回采", "未归因")):
            bad.append("覆盖率呈现缺少三分之一 —— 未归因必须与已归因同屏")
        if "合计" in line or "总计" in line:
            bad.append("覆盖率行给出了合计 —— 两个口径的数相加没有意义")

    return bad


def main() -> int:
    try:
        bad = check()
    except Exception as exc:  # noqa: BLE001 —— 机检自身炸了要说清楚，不能静默通过
        print(f"❌ 机检执行失败：{type(exc).__name__}: {exc}")
        return 1
    if bad:
        for line in bad:
            print(f"  - {line}")
        return 1
    print(
        "✅ 回采红线通过: workflow_agents.tokens 逐行未变(指纹) / "
        "覆盖率分窗不合并 / dry-run 默认+journal 必需+重跑零变更"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
