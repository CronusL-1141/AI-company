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
CODEX_SENTINEL_TOKENS = 313131  # Codex 形状那一行的禁改列哨兵

# Codex 侧的 rollout 是**另一种文件**，不是 CC transcript：没有 ``type:"assistant"``
# 行，token 账记在 ``event_msg / token_count`` 的 ``total_token_usage`` 里，且那是一个
# **累计快照**（不是每次调用的增量）。把它当 CC transcript 解析，最省事的错法是"解析
# 不出 usage 就当 0 写进去"——那会给一个真实烧了 19,309 token 的会话盖上一个
# ``tokens_source='transcript'`` 的 0，而事后与"真的是 0"不可分辨。
# 数字取自真实语料（P-1 夹具 0.145 面），形状照抄，不带 rate_limits 那一段。
CODEX_TOTAL_USAGE = {
    "input_tokens": 19067,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 242,
    "reasoning_output_tokens": 66,
    "total_tokens": 19309,
}
CODEX_SESSION_ID = "019f8b21-a6ae-7533-b326-2d260efbf40b"  # v7
CODEX_CC_TOOL_USE_ID = "019f8b4d-1b95-7763-9625-e9b0691b5a3e"  # v7


def write_codex_rollout(path: Path) -> None:
    """一份最小但形状真实的 Codex rollout。

    三种行类型足够：``session_meta``（带 v7 会话 id 与 cli_version）、``response_item``
    （工具调用，带 turn_id）、``event_msg/token_count``（累计用量快照）。CC 回采器认的
    ``type:"assistant"`` 一条也没有——这正是要钉住的那件事。
    """
    lines = [
        {
            "timestamp": "2026-07-22T09:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": CODEX_SESSION_ID,
                "timestamp": "2026-07-22T09:00:00.000Z",
                "cwd": "/workspace/probe",
                "cli_version": "0.145.0-alpha.30",
                "source": "vscode",
                "thread_source": "subagent",
            },
        },
        {
            "timestamp": "2026-07-22T09:00:05.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "call_probe",
                "name": "exec",
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "019f8b21-b000-7000-8000-000000000001"
                },
            },
        },
        {
            "timestamp": "2026-07-22T09:00:09.000Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": dict(CODEX_TOTAL_USAGE),
                    "last_token_usage": dict(CODEX_TOTAL_USAGE),
                    "model_context_window": 258400,
                },
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


def seed(tmp: Path) -> Path:
    """建一个最小但覆盖各分支的库：有/无 transcript、已测量、别名/完整型号、Codex 形状。

    Codex 那一行（``cx1`` / ``wa3``）是这个探针库里唯一一条**不是 CC 形状**的行：
    ``session_id`` 与 ``cc_tool_use_id`` 同为 v7、``transcript_path`` 指向一份 rollout
    而不是 CC transcript。它在这里的职责是当一根探针——回采器只要开始把 rollout 当
    transcript 处理，这一行就会以"被写了一个 0"或"禁改列被动了"的形态当场露头。
    """
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
    rollout = tmp / f"rollout-2026-07-22T09-00-00-{CODEX_SESSION_ID}.jsonl"
    write_codex_rollout(rollout)

    db = tmp / "probe.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        create table agents (
            id text primary key, name text, role text, model text, created_at text,
            transcript_path text,
            input_tokens integer, output_tokens integer,
            cache_creation_tokens integer, cache_read_tokens integer,
            tokens_measured_at text, tokens_source text,
            session_id text, cc_tool_use_id text, harness text
        );
        create table workflow_agents (
            id text primary key, label text, model text, os_agent_id text,
            cc_agent_id text, created_at text, tokens integer
        );
        """
    )
    con.execute(
        "insert into agents values ('a1','w1','worker',null,'2026-07-20 10:00:00',?,"
        "null,null,null,null,null,null,null,null,null)",
        (str(transcript),),
    )
    con.execute(
        "insert into agents values ('a2','w2','worker',null,'2026-07-20 10:00:00',null,"
        "null,null,null,null,null,null,null,null,null)"
    )
    con.execute(
        "insert into agents values ('a3','w3','worker',null,'2026-07-20 10:00:00',?,"
        "1,2,3,4,'2026-07-25 00:00:00','transcript',null,null,null)",
        (str(transcript),),
    )
    con.execute(
        "insert into agents values ('cx1','wcx','worker',null,'2026-07-22 09:00:00',?,"
        "null,null,null,null,null,null,?,?,'codex')",
        (str(rollout), CODEX_SESSION_ID, CODEX_CC_TOOL_USE_ID),
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
    con.execute(
        "insert into workflow_agents values ('wa3','lcx','opus','cx1','cc3',"
        "'2026-07-22 09:00:00', ?)",
        (CODEX_SENTINEL_TOKENS,),
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

        # ── Codex 形状行：rollout 不是 transcript，一个字节都不该被回采写进去 ──
        # 三条一起看才拦得住："没写 token"可能是因为整个 job 空转，所以上面先钉了
        # "CC 行确实被写了"；这里再钉 Codex 行**在同一次 apply 里**没被写。
        cx = con.execute(
            "select input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
            " tokens_measured_at, tokens_source from agents where id='cx1'"
        ).fetchone()
        if any(v is not None for v in cx):
            bad.append(
                f"❗ Codex 形状行被 CC 回采器写了：{cx} —— rollout 里没有 type='assistant' "
                f"行，把它当 transcript 解析只会解出 0；带着 tokens_source='transcript' 落库的"
                f"那个 0，事后与'真的是 0'不可分辨。修法是给 backfill_token_usage.py 加"
                f" harness 谓词，不是放宽这条断言"
            )
        if con.execute("select tokens from workflow_agents where id='wa3'").fetchone()[0] \
                != CODEX_SENTINEL_TOKENS:
            bad.append("Codex 形状行的禁改列 tokens 被动了 —— 借道 os_agent_id 也不行")
        if con.execute("select model from workflow_agents where id='wa3'").fetchone()[0] \
                != "opus":
            bad.append(
                "Codex 形状行的 model 别名被 rollout 观测覆盖了 —— rollout 的 model 字段"
                "不是 CC transcript 的 message.model，两者不是同一个口径"
            )
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
        # 分母 4 = a1/a2/a3 + Codex 形状的 cx1。Codex 行采不到，但它**在分母里**：
        # 采不到的行被移出分母，覆盖率就会恒等于 100%，那正是局部冒充全貌（R2）。
        if cov.total != 4:
            bad.append(
                f"覆盖率分母 {cov.total} ≠ 4 —— 没有 transcript 的行（或 Codex 形状的行）"
                "被移出了分母，那正是局部冒充全貌（R2）"
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
