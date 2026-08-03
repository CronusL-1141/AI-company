"""恢复脚本的行为契约 —— 见 report b168eb34-9c19-40e5-8ecb-624a74ccbc5a §4。

这个脚本要往生产库里**插回** 843 行历史数据（1 队 + 7 行 agents + 835 条 activities）连同
3.25 亿 token 的归因账，由缔造者亲手执行。插错的代价与回采脚本对称：写进去的混口径数据
事后不可分辨。所以它的每一条纪律都必须可机检：

* **双源强校验** —— token 值以重解析 transcript 为准、以 07-29 journal 为独立比对，
  两者任何一个字段对不上就中止整个事务，**没有降级开关**。
* **时钟制式是显式判据** —— 源与目标各读 ``PRAGMA user_version``；已知组合才换算，其余
  一律中止。换算复用 ``migrate_timestamps_utc.py`` 的列清单与位移函数，并保留亚秒位。
  这条是演练时抓出来的真问题：07-28 那份备份取于 UTC 平移之前，原样恢复会把 843 行
  +8h 的时间戳塞进 UTC 库（I11 红线"库里不许有第二个时钟"）。
* **第三方复核** —— 另一份快照对同一批行的说法：NULL→有值判为期间回填取后者，双方非空
  且不等（或后者反而丢值）判为矛盾并中止。
* **行与账同一事务** —— 验收不过整事务回滚，目标库一行不留；不存在"先插行、后写账"的
  中间窗口（那个窗口里的任何一次 reap tick 会把无账 husk 再删一遍）。
* **写面封闭 + 幂等结构性** —— 只 INSERT 三张表，``workflow_agents.tokens`` 逐行指纹前后
  不变；``already_present`` 是判定第一分支，SQL 层另有 ``WHERE NOT EXISTS`` 守卫。
* **``tokens_measured_at`` 写原批次时刻而非 now()** —— 该列兼任回采批次签名与活体采集
  健康度，写 now() 会污染覆盖率分窗。

全部用例只跑 tmp_path 里的临时库与临时 transcript，一个字节都不碰生产库与那几份备份。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "restore_purged_container_team.py"
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location("restore_purged_container_team", SCRIPT)
assert _spec and _spec.loader
rp = importlib.util.module_from_spec(_spec)
# 必须先登记进 sys.modules 再 exec：脚本里的 @dataclass 会回查自己的模块命名空间。
sys.modules["restore_purged_container_team"] = rp
_spec.loader.exec_module(rp)

TEAM = rp.RESTORE_TEAM_ID
# 常规用例跑一支**合成队**：真实那支挂着 §3 类① 的期望指纹（7 行 / 835 条 / 3.25 亿），
# 拿它当通用夹具会让每个用例都撞在计数闸上。期望闸自己由 TestSingleTransaction 专门验。
SYNTH_TEAM = "synthetic-team-for-unit-tests"
LEADER = "e4495c71-1721-4c08-83af-45f299242bd7"
W1 = "3603792a-d574-4f8c-b9ee-1d879e0fafff"
W2 = "321ec68a-df2f-48f0-b88a-d77076e1e7e2"

# 与 §3 类① 的真实形状同构：两行 worker 挂账、一行 Leader 无账。
USAGE_A = {
    "input_tokens": 70,
    "output_tokens": 32255,
    "cache_creation_tokens": 117189,
    "cache_read_tokens": 2753368,
}
USAGE_B = {
    "input_tokens": 323,
    "output_tokens": 252416,
    "cache_creation_tokens": 2159530,
    "cache_read_tokens": 46165122,
}

SCHEMA = """
create table teams (
    id text primary key, name text, mode text, project_id text, leader_agent_id text,
    status text, summary text, config text, created_at text, updated_at text, completed_at text
);
create table agents (
    id text primary key, team_id text, name text, role text, model text, status text,
    source text, session_id text, transcript_path text, created_at text, last_active_at text,
    ctx_measured_at text,
    input_tokens integer, output_tokens integer,
    cache_creation_tokens integer, cache_read_tokens integer,
    tokens_measured_at text{tokens_source}
);
create table agent_activities (
    id text primary key, agent_id text, session_id text, tool_name text,
    input_summary text, output_summary text, timestamp text, duration_ms integer,
    status text, error text
);
create table workflow_agents (
    id text primary key, label text, model text, os_agent_id text, cc_agent_id text,
    created_at text, tokens integer
);
"""


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def write_transcript(path: Path, usage: dict[str, int], *, model: str = "claude-opus-5") -> Path:
    """写一份真 jsonl —— 不 mock 解析器。

    同一 requestId 写两行、第二行 output 更大：复刻流式的真实形态（output 是递增快照而非
    增量）。解析器必须按 requestId 取末条。这份夹具的存在就是为了让"校验通过"意味着
    "真解析结果与 journal 一致"，而不是"我以为的解析结果一致"。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for out in (max(usage["output_tokens"] // 2, 1), usage["output_tokens"]):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "requestId": "r1",
                    "message": {
                        "model": model,
                        "usage": {
                            "input_tokens": usage["input_tokens"],
                            "output_tokens": out,
                            "cache_creation_input_tokens": usage["cache_creation_tokens"],
                            "cache_read_input_tokens": usage["cache_read_tokens"],
                        },
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_db(path: Path, *, marker: int, with_tokens_source: bool = True) -> Path:
    con = sqlite3.connect(path)
    con.executescript(
        SCHEMA.format(tokens_source=", tokens_source text" if with_tokens_source else "")
    )
    con.execute(f"pragma user_version = {marker}")
    con.commit()
    con.close()
    return path


def add_team(db: Path, *, team_id: str = SYNTH_TEAM, created: str = "2026-07-24 19:01:03.228316",
             updated: str = "2026-07-27 01:49:19.704237") -> None:
    con = sqlite3.connect(db)
    con.execute(
        "insert into teams values (?,?,?,?,?,?,?,?,?,?,?)",
        (team_id, "session-87500462", "coordinate", "proj-1", LEADER, "completed", "",
         '{"kind": "session"}', created, updated, None),
    )
    con.commit()
    con.close()


def add_agent(
    db: Path,
    aid: str,
    *,
    name: str,
    team_id: str = SYNTH_TEAM,
    role: str = "worker",
    transcript_path: str | None = None,
    session_id: str | None = "sess-1",
    created: str = "2026-07-24 21:17:16.577439",
    last_active: str | None = "2026-07-24 21:28:05.741571",
    ctx_measured: str | None = "2026-07-24 21:26:44.152175",
    model: str = "",
    with_tokens_source: bool = True,
) -> None:
    con = sqlite3.connect(db)
    cols = (
        "id,team_id,name,role,model,status,source,session_id,transcript_path,created_at,"
        "last_active_at,ctx_measured_at,input_tokens,output_tokens,cache_creation_tokens,"
        "cache_read_tokens,tokens_measured_at"
    )
    vals: list[Any] = [
        aid, team_id, name, role, model, "offline", "hook", session_id, transcript_path,
        created, last_active, ctx_measured, None, None, None, None, None,
    ]
    if with_tokens_source:
        cols += ",tokens_source"
        vals.append(None)
    marks = ",".join("?" * len(vals))
    con.execute(f"insert into agents ({cols}) values ({marks})", vals)  # noqa: S608
    con.commit()
    con.close()


def add_activity(db: Path, aid: str, agent_id: str, ts: str = "2026-07-24 21:17:19.712719") -> None:
    con = sqlite3.connect(db)
    con.execute(
        "insert into agent_activities values (?,?,?,?,?,?,?,?,?,?)",
        (aid, agent_id, "sess-1", "Bash", "in", "out", ts, 12, "ok", None),
    )
    con.commit()
    con.close()


def add_wa(db: Path, wid: str, *, tokens: int) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "insert into workflow_agents values (?,?,?,?,?,?,?)",
        (wid, "wf", "opus", None, "cc1", "2026-07-24 19:00:00", tokens),
    )
    con.commit()
    con.close()


def write_journal(path: Path, entries: dict[str, dict[str, Any]]) -> Path:
    """按 backfill_token_usage 的 journal 形状写一份回采凭证。"""
    path.write_text(
        json.dumps(
            {
                "script": "backfill_token_usage.py",
                "batch_ts": rp.BATCH_TS,
                "metric": "usage_sum",
                "jobs": [
                    {
                        "title": "A",
                        "table": "agents",
                        "enabled": True,
                        "written": [
                            {"id": k, "name": k[:8], "values": v, "guard": {}}
                            for k, v in entries.items()
                        ],
                    },
                    {
                        "title": "C",
                        "table": "workflow_agents",
                        "enabled": True,
                        "written": [{"id": "wa1", "name": "wf", "values": {"model": "x"}}],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def ledger_values(usage: dict[str, int], *, model: str | None = "claude-opus-5") -> dict[str, Any]:
    v: dict[str, Any] = dict(usage)
    v["tokens_measured_at"] = rp.BATCH_TS
    v["tokens_source"] = "transcript"
    if model:
        v["model"] = model
    return v


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    """一支两 worker + 一 Leader 的队：源库有、目标库空，形状与真实那支同构。"""
    src = make_db(tmp_path / "source.db", marker=rp.MIGRATION_MARKER)
    tgt = make_db(tmp_path / "target.db", marker=rp.MIGRATION_MARKER)
    add_wa(tgt, "wa1", tokens=424242)

    t1 = write_transcript(tmp_path / "t1.jsonl", USAGE_A)
    t2 = write_transcript(tmp_path / "t2.jsonl", USAGE_B)

    add_team(src)
    add_agent(src, LEADER, name="Leader", role="leader", transcript_path=None, session_id=None,
              ctx_measured=None)
    add_agent(src, W1, name="wb-html-final", transcript_path=str(t1))
    add_agent(src, W2, name="wb-w1a-sandbox", transcript_path=str(t2))
    add_activity(src, "act-1", W1)
    add_activity(src, "act-2", W2)

    journal = write_journal(
        tmp_path / "backfill.json",
        {W1: ledger_values(USAGE_A), W2: ledger_values(USAGE_B)},
    )
    return {
        "src": src, "tgt": tgt, "journal": journal, "t1": t1, "t2": t2, "tmp": tmp_path,
    }


def run(world: dict[str, Any], *extra: str, corroborant: Path | None = None) -> int:
    argv = [
        "--db", str(world["tgt"]),
        "--from-backup", str(world["src"]),
        "--verify-journal", str(world["journal"]),
        "--corroborate-with", str(corroborant or world["tmp"] / "no-such.db"),
        "--team", SYNTH_TEAM,
        *extra,
    ]
    return rp.main(argv)


def fetch(db: Path, sql: str, *params: Any) -> Any:
    con = sqlite3.connect(db)
    row = con.execute(sql, params).fetchone()
    con.close()
    return row


def count(db: Path, table: str) -> int:
    return int(fetch(db, f"select count(*) from {table}")[0])  # noqa: S608


# ===========================================================================
# 双源强校验 —— 两者不一致即中止，没有降级开关
# ===========================================================================
class TestDualSourceVerification:
    def test_matching_sources_produce_the_ledger_overlay(self, world):
        """happy path：重解析与 journal 一致 → overlay 带四层 + 原批次时刻 + 来源标注。"""
        con = rp.open_immutable(world["src"])
        rows = rp.load_source_agents(con, SYNTH_TEAM)
        con.close()
        checks = rp.verify_ledger(rows, rp.load_journal_ledger(world["journal"]))
        assert {c.agent_id for c in checks} == {W1, W2}
        overlay = next(c for c in checks if c.agent_id == W1).overlay
        assert {k: overlay[k] for k in rp.TOKEN_COLUMNS} == USAGE_A
        assert overlay["tokens_measured_at"] == rp.BATCH_TS
        assert overlay["tokens_source"] == "transcript"

    def test_a_single_layer_disagreement_aborts_everything(self, world):
        """一个字段对不上就够了 —— 不是"多数相同即通过"。"""
        bad = dict(USAGE_A, cache_read_tokens=USAGE_A["cache_read_tokens"] + 1)
        write_journal(world["journal"], {W1: ledger_values(bad), W2: ledger_values(USAGE_B)})
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j.json")) == 3
        assert count(world["tgt"], "agents") == 0

    def test_a_missing_transcript_aborts_instead_of_trusting_the_journal(self, world):
        """少一个源 = 双源校验做不成。journal 单独可信也不行，那是"取其一的降级"。"""
        world["t1"].unlink()
        assert run(world) == 3

    def test_an_empty_transcript_is_never_read_as_zero_tokens(self, world):
        """no-data ≠ zero：解析不出快照就中止，绝不落成"用了 0 token"。"""
        world["t1"].write_text("")
        assert run(world) == 3

    def test_a_ledger_without_a_transcript_path_is_unexplainable(self, world, tmp_path):
        """journal 说这行有账，源行却没有路径 —— 无从解释，不恢复。"""
        write_journal(
            world["journal"],
            {W1: ledger_values(USAGE_A), W2: ledger_values(USAGE_B),
             LEADER: ledger_values(USAGE_A)},
        )
        assert run(world) == 3

    def test_a_transcript_without_a_ledger_entry_is_a_source_disagreement(self, world):
        """有路径却在 journal 里查无此行 —— 两源分歧，同样中止。"""
        write_journal(world["journal"], {W2: ledger_values(USAGE_B)})
        assert run(world) == 3

    def test_an_observed_model_mismatch_aborts(self, world):
        """型号也是双源的一部分：journal 记的与重解析出来的必须是同一个。"""
        write_journal(
            world["journal"],
            {W1: ledger_values(USAGE_A, model="claude-opus-4-8"),
             W2: ledger_values(USAGE_B)},
        )
        assert run(world) == 3

    def test_a_row_that_never_had_a_ledger_is_restored_without_one(self, world):
        """Leader husk：身份照恢复，账本来就没有，不许凭空造一个。"""
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j.json")) == 0
        row = fetch(
            world["tgt"],
            "select tokens_measured_at, tokens_source, input_tokens from agents where id=?",
            LEADER,
        )
        assert row == (None, None, None)


# ===========================================================================
# 时钟制式 —— 显式判据，不是假设
# ===========================================================================
class TestClockConvention:
    def test_same_marker_means_no_shift(self):
        assert rp.needs_clock_shift(rp.MIGRATION_MARKER, rp.MIGRATION_MARKER) is False
        assert rp.needs_clock_shift(0, 0) is False

    def test_pre_migration_source_into_migrated_target_is_shifted(self):
        assert rp.needs_clock_shift(0, rp.MIGRATION_MARKER) is True

    def test_any_unknown_pairing_is_refused_rather_than_guessed(self):
        """没有已知换算就停 —— 猜一个方向的代价是几百行永久偏一个时区。"""
        with pytest.raises(rp.ClockConventionError):
            rp.needs_clock_shift(rp.MIGRATION_MARKER, 0)
        with pytest.raises(rp.ClockConventionError):
            rp.needs_clock_shift(0, 99999999)

    def test_the_shift_preserves_sub_minute_bytes(self):
        """平移脚本只搬"到分钟"的前缀，秒与亚秒位原样接回 —— 事件账本的排序靠那几位。"""
        shifted = rp.shift_value("2026-07-25 03:01:03.230396")
        assert shifted.endswith(":03.230396")
        assert shifted == "2026-07-24 19:01:03.230396"

    def test_a_null_timestamp_stays_null(self):
        assert rp.shift_value(None) is None

    def test_restoring_from_a_pre_migration_source_lands_utc_values(self, tmp_path):
        """端到端：源是平移前的库，插进目标库的**每一个**本地墙钟列都必须已经是 UTC。

        这是演练时抓出来的真问题 —— 取证报告只比了 schema，没比时钟制式；照它直接恢复会把
        几百行 +8h 的时间戳写进 UTC 库，且事后与真值长得一模一样。
        """
        src = make_db(tmp_path / "old.db", marker=0)
        tgt = make_db(tmp_path / "new.db", marker=rp.MIGRATION_MARKER)
        add_team(src, created="2026-07-25 03:01:03.228316", updated="2026-07-27 09:49:19.704237")
        add_agent(src, LEADER, name="Leader", role="leader", transcript_path=None,
                  session_id=None, ctx_measured=None, created="2026-07-25 03:01:03.230396",
                  last_active="2026-07-25 03:01:03.232585")
        add_activity(src, "act-1", LEADER, ts="2026-07-25 05:17:19.712719")
        journal = write_journal(tmp_path / "j0.json", {})

        assert rp.main([
            "--db", str(tgt), "--from-backup", str(src), "--verify-journal", str(journal),
            "--corroborate-with", str(tmp_path / "nope.db"), "--team", SYNTH_TEAM,
            "--apply", "--journal", str(tmp_path / "out.json"),
        ]) == 0

        assert fetch(tgt, "select created_at, updated_at from teams where id=?", SYNTH_TEAM) == (
            "2026-07-24 19:01:03.228316", "2026-07-27 01:49:19.704237"
        )
        assert fetch(
            tgt, "select created_at, last_active_at from agents where id=?", LEADER
        ) == ("2026-07-24 19:01:03.230396", "2026-07-24 19:01:03.232585")
        assert fetch(tgt, "select timestamp from agent_activities where id='act-1'")[0] == (
            "2026-07-24 21:17:19.712719"
        )
        # 平移标记本身不跟着搬 —— 目标库的制式由目标库自己声明
        assert fetch(tgt, "pragma user_version")[0] == rp.MIGRATION_MARKER

    def test_a_future_timestamp_trips_the_direction_guard(self, world):
        """换算方向反了会把历史行搬到未来 —— 事务内兜底闸必须抓住。"""
        con = sqlite3.connect(world["tgt"])
        con.row_factory = sqlite3.Row
        add_team(world["tgt"])
        add_agent(world["tgt"], W1, name="wb-html-final", created="2099-01-01 00:00:00.000000")
        problems = rp.verify_after(con, SYNTH_TEAM, [W1], [], None)
        con.close()
        assert any("落在未来" in p for p in problems)


# ===========================================================================
# 第三方复核 —— 另一份快照对同一批行的说法
# ===========================================================================
class TestCorroboration:
    def _corroborant(self, tmp_path: Path, *, session_id: str | None) -> Path:
        db = make_db(tmp_path / "corro.db", marker=rp.MIGRATION_MARKER)
        add_team(db)
        add_agent(db, LEADER, name="Leader", role="leader", transcript_path=None,
                  session_id=None, ctx_measured=None)
        add_agent(db, W1, name="wb-html-final",
                  transcript_path=str(tmp_path / "t1.jsonl"), session_id=session_id)
        add_agent(db, W2, name="wb-w1a-sandbox",
                  transcript_path=str(tmp_path / "t2.jsonl"), session_id="sess-1")
        add_activity(db, "act-1", W1)
        add_activity(db, "act-2", W2)
        return db

    def test_a_null_that_later_became_a_value_is_accepted_and_reported(self, world, capsys):
        """期间被回填：复核源 NULL、主源有值 → 取主源，但必须在报告里看得见。

        这正是"主源必须是更晚那份快照"的实证 —— 照更早那份恢复会把这一列丢掉。
        """
        corro = self._corroborant(world["tmp"], session_id=None)
        assert run(world, corroborant=corro) == 0
        out = capsys.readouterr().out
        assert "期间被回填" in out and "session_id" in out
        assert "零矛盾" in out

    def test_two_snapshots_disagreeing_on_a_value_aborts(self, world):
        """双方都非空且不等 = 两个快照对同一行的身份说法不一致，谁对不是脚本该猜的。"""
        corro = self._corroborant(world["tmp"], session_id="sess-OTHER")
        assert run(world, corroborant=corro, ) == 3

    def test_a_missing_corroborant_is_loudly_flagged_not_silently_skipped(self, world, capsys):
        """跳过复核可以，但不能悄悄跳 —— 那样"没报错"会被读成"复核过了"。"""
        assert run(world) == 0
        assert "复核源不可用" in capsys.readouterr().out

    def test_rows_absent_from_the_corroborant_are_counted_not_fatal(self, world, capsys):
        """复核源可能就是比某一行更早的快照 —— 如实计数，不当成问题。"""
        corro = make_db(world["tmp"] / "thin.db", marker=rp.MIGRATION_MARKER)
        add_team(corro)  # 只有队行，3 行成员 + 2 条活动都比它晚
        assert run(world, corroborant=corro) == 0
        assert "复核源中缺失的行：5" in capsys.readouterr().out


# ===========================================================================
# 写面封闭 + 禁改列
# ===========================================================================
class TestWriteSurface:
    def test_the_write_surface_is_three_tables_and_nothing_else(self):
        assert rp.WRITABLE_TABLES == frozenset({"teams", "agents", "agent_activities"})
        assert rp.FORBIDDEN_COLUMN[0] not in rp.WRITABLE_TABLES

    def test_the_fingerprint_is_the_backfill_scripts_own_function(self):
        """指纹算法只此一份 —— 抄一份出来，将来改一处就会长出两种"未变"的判据。"""
        import backfill_token_usage as bf

        assert rp.workflow_tokens_fingerprint is bf.workflow_tokens_fingerprint
        assert rp.FORBIDDEN_COLUMN == bf.FORBIDDEN_COLUMN

    def test_the_clock_columns_come_from_the_migration_script(self):
        """哪些列是本地墙钟由平移脚本定义 —— 这里不另立一份清单。"""
        import migrate_timestamps_utc as mig

        assert rp.LOCAL_COLUMNS is mig.LOCAL_COLUMNS
        assert rp.MIGRATION_MARKER == mig.MIGRATION_MARKER

    def test_insert_refuses_a_table_outside_the_whitelist(self, world):
        """即使将来有人构造出这样的 plan，写入层也当场炸而不是照写。"""
        plan = rp.RowPlan("workflow_agents", "wa1", "wf", "restorable", {"id": "wa1", "tokens": 1})
        con = sqlite3.connect(world["tgt"])
        with pytest.raises(RuntimeError, match="写表白名单拦截"):
            rp.insert_row(con, plan)
        con.close()
        assert fetch(world["tgt"], "select tokens from workflow_agents where id='wa1'")[0] == 424242

    def test_a_real_apply_leaves_the_forbidden_column_byte_identical(self, world):
        con = sqlite3.connect(world["tgt"])
        before = rp.workflow_tokens_fingerprint(con)
        con.close()
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j.json")) == 0
        con = sqlite3.connect(world["tgt"])
        after = rp.workflow_tokens_fingerprint(con)
        con.close()
        assert after == before
        assert count(world["tgt"], "agents") == 3  # 但这次 apply 确实干了活

    def test_the_journal_carries_the_fingerprint_and_the_clock_verdict(self, world):
        """journal 是唯一凭证：禁改列前后指纹、时钟判定都得在里面。"""
        journal = world["tmp"] / "j.json"
        assert run(world, "--apply", "--journal", str(journal)) == 0
        data = json.loads(journal.read_text())
        assert data["forbidden_column"]["unchanged"] is True
        assert data["metric"] == "usage_sum"
        assert data["clock"]["source_shifted_to_utc"] is False
        assert data["batch_ts"] == rp.BATCH_TS
        assert len(data["restored"]["agent_activities"]) == 2


# ===========================================================================
# 行与账同一事务 —— 验收不过整事务回滚
# ===========================================================================
class TestSingleTransaction:
    def test_a_failed_check_leaves_not_one_row_behind(self, tmp_path):
        """期望指纹对不上 → 回滚。若行已落地而账没落，下一次 reap tick 就会把它们再删一遍。"""
        src = make_db(tmp_path / "s.db", marker=rp.MIGRATION_MARKER)
        tgt = make_db(tmp_path / "t.db", marker=rp.MIGRATION_MARKER)
        add_team(src, team_id=TEAM)
        add_agent(src, LEADER, name="Leader", role="leader", team_id=TEAM,
                  transcript_path=None, session_id=None, ctx_measured=None)
        add_activity(src, "act-1", LEADER)
        journal = write_journal(tmp_path / "j0.json", {})

        rc = rp.main([
            "--db", str(tgt), "--from-backup", str(src), "--verify-journal", str(journal),
            "--corroborate-with", str(tmp_path / "nope.db"),
            "--apply", "--journal", str(tmp_path / "out.json"),
        ])
        assert rc == 4  # 真实那支队是 7 行 / 835 条，这支只有 1 行 / 1 条
        assert count(tgt, "teams") == 0
        assert count(tgt, "agents") == 0
        assert count(tgt, "agent_activities") == 0
        assert not (tmp_path / "out.json").exists()  # 没写成功就不该留下"成功凭证"

    def test_the_expectation_gate_is_skipped_loudly_for_unknown_teams(self, tmp_path, capsys):
        """合成队没有期望指纹 —— 明说跳过，而不是拿空 dict 假装比过了。"""
        src = make_db(tmp_path / "s.db", marker=rp.MIGRATION_MARKER)
        tgt = make_db(tmp_path / "t.db", marker=rp.MIGRATION_MARKER)
        add_team(src, team_id="other-team")
        journal = write_journal(tmp_path / "j0.json", {})
        rc = rp.main([
            "--db", str(tgt), "--from-backup", str(src), "--verify-journal", str(journal),
            "--corroborate-with", str(tmp_path / "nope.db"), "--team", "other-team",
        ])
        assert rc == 0
        assert "没有登记期望指纹" in capsys.readouterr().out


# ===========================================================================
# 幂等 / dry-run / 凭证
# ===========================================================================
class TestIdempotenceAndDryRun:
    def test_dry_run_is_the_default_and_writes_nothing(self, world):
        assert run(world) == 0
        assert count(world["tgt"], "agents") == 0
        assert count(world["tgt"], "teams") == 0

    def test_rerun_after_apply_plans_zero_rows(self, world, capsys):
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j1.json")) == 0
        capsys.readouterr()
        assert run(world) == 0
        out = capsys.readouterr().out
        assert "待恢复合计：0 行" in out
        assert "already_present" in out

    def test_a_second_apply_inserts_nothing(self, world):
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j1.json")) == 0
        before = fetch(world["tgt"], "select count(*) from agents")[0]
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j2.json")) == 0
        assert fetch(world["tgt"], "select count(*) from agents")[0] == before

    def test_the_sql_guard_lets_a_concurrent_insert_win(self, world):
        """判定与写入之间别人先插了 —— 守卫在 SQL 里，并发下也不重复插。"""
        con = rp.open_immutable(world["src"])
        row = con.execute("select * from agents where id=?", (W1,)).fetchone()
        cols = set(rp.table_columns(con, "agents"))
        con.close()
        target = sqlite3.connect(world["tgt"])
        target.row_factory = sqlite3.Row
        values = rp.build_values(
            "agents", row, rp.table_columns(target, "agents"), cols, {}, shift_clock=False
        )
        plan = rp.RowPlan("agents", W1, "wb-html-final", "restorable", values)
        assert rp.insert_row(target, plan) == 1
        assert rp.insert_row(target, plan) == 0  # 第二次一行都不动
        target.close()

    def test_apply_without_a_journal_is_refused(self, world):
        assert run(world, "--apply") == 2

    def test_an_existing_journal_is_never_overwritten(self, world):
        journal = world["tmp"] / "taken.json"
        journal.write_text("{}")
        assert run(world, "--apply", "--journal", str(journal)) == 2
        assert journal.read_text() == "{}"

    def test_the_source_may_never_be_the_target(self, world):
        assert rp.main([
            "--db", str(world["src"]), "--from-backup", str(world["src"]),
            "--verify-journal", str(world["journal"]),
        ]) == 2


# ===========================================================================
# 批次时刻 —— 写原时刻，不写 now()
# ===========================================================================
class TestBatchTimestamp:
    def test_the_original_batch_timestamp_is_written_not_now(self, world):
        """该列兼任回采批次签名与活体采集健康度：写 now() 会凭空造出"新测量"污染分窗。"""
        assert run(world, "--apply", "--journal", str(world["tmp"] / "j.json")) == 0
        stamps = {
            r[0]
            for r in sqlite3.connect(world["tgt"]).execute(
                "select tokens_measured_at from agents where tokens_measured_at is not null"
            )
        }
        assert stamps == {rp.BATCH_TS}

    def test_a_declared_batch_timestamp_is_the_one_that_lands(self, world):
        """批次时刻是参数化的（单测要能换），但写进去的必须就是声明的那一个。"""
        assert run(
            world, "--apply", "--journal", str(world["tmp"] / "j.json"),
            "--batch-ts", "2026-08-03 00:00:00.000000",
        ) == 0
        stamps = {
            r[0]
            for r in sqlite3.connect(world["tgt"]).execute(
                "select tokens_measured_at from agents where tokens_measured_at is not null"
            )
        }
        assert stamps == {"2026-08-03 00:00:00.000000"}

    def test_a_stray_batch_timestamp_is_caught_inside_the_transaction(self, world):
        """事务内闸：库里出现不属于本批次的时刻 → 报问题（调用方据此回滚）。

        防的是"一半行盖了批次戳、另一半被别的路径写了别的戳" —— 那样这批行就再也不是
        一个可分窗的 cohort，而覆盖率分窗正是靠"同一时刻被足够多行共享"认批次的。
        """
        con = sqlite3.connect(world["tgt"])
        con.row_factory = sqlite3.Row
        add_team(world["tgt"])
        add_agent(world["tgt"], W1, name="wb-html-final")
        con.execute(
            "update agents set tokens_measured_at = ? where id = ?",
            ("2026-08-01 12:00:00.000000", W1),
        )
        con.commit()
        problems = rp.verify_after(con, SYNTH_TEAM, [W1], [], None)
        con.close()
        assert any("不是原批次时刻" in p for p in problems)


# ===========================================================================
# schema 适配 —— 缺列必须有明文规则
# ===========================================================================
class TestSchemaAdaptation:
    def test_a_known_missing_column_falls_back_to_its_declared_rule(self, tmp_path):
        """源里没有 tokens_source（它 v1.11.1 才上线）→ 无账行留 NULL，不是补 'transcript'。"""
        src = make_db(tmp_path / "s.db", marker=rp.MIGRATION_MARKER, with_tokens_source=False)
        tgt = make_db(tmp_path / "t.db", marker=rp.MIGRATION_MARKER)
        add_team(src, team_id="other-team")
        add_agent(src, LEADER, name="Leader", role="leader", team_id="other-team",
                  transcript_path=None, session_id=None, ctx_measured=None,
                  with_tokens_source=False)
        journal = write_journal(tmp_path / "j0.json", {})
        rc = rp.main([
            "--db", str(tgt), "--from-backup", str(src), "--verify-journal", str(journal),
            "--corroborate-with", str(tmp_path / "nope.db"), "--team", "other-team",
            "--apply", "--journal", str(tmp_path / "out.json"),
        ])
        assert rc == 0
        assert fetch(tgt, "select tokens_source from agents where id=?", LEADER)[0] is None

    def test_an_unknown_missing_column_is_schema_drift_not_a_silent_null(self, world):
        """没登记过的缺列 = 没人看过的漂移。补 NULL 正是"字段悄悄丢了"的成因。"""
        con = sqlite3.connect(world["tgt"])
        con.execute("alter table agents add column brand_new text")
        con.commit()
        con.close()
        assert run(world) == 5

    def test_a_column_dropped_by_the_target_aborts_rather_than_losing_data(self, tmp_path):
        """目标库比源少一列 → 恢复内容会丢，须人工裁定，不许闷头插。"""
        src = make_db(tmp_path / "s.db", marker=rp.MIGRATION_MARKER)
        tgt = make_db(tmp_path / "t.db", marker=rp.MIGRATION_MARKER, with_tokens_source=False)
        add_team(src, team_id="other-team")
        journal = write_journal(tmp_path / "j0.json", {})
        assert rp.main([
            "--db", str(tgt), "--from-backup", str(src), "--verify-journal", str(journal),
            "--corroborate-with", str(tmp_path / "nope.db"), "--team", "other-team",
        ]) == 5


# ===========================================================================
# 前置备份 —— 走 SQLite 备份 API，且永不覆盖
# ===========================================================================
class TestPreBackup:
    def test_apply_takes_a_consistent_backup_first(self, world):
        backups = world["tmp"] / "baks"
        assert run(
            world, "--apply", "--journal", str(world["tmp"] / "j.json"),
            "--pre-backup-dir", str(backups),
        ) == 0
        made = list(backups.glob("*.bak-restore-*"))
        assert len(made) == 1
        # 前置备份记的是**恢复前**的状态：里面不该有被恢复的行
        assert count(made[0], "agents") == 0
        assert count(world["tgt"], "agents") == 3

    def test_a_second_backup_in_the_same_second_never_overwrites_the_first(self, world):
        """前置备份的全部价值就在于"没被写过" —— 撞名要另起，不许覆盖。"""
        backups = world["tmp"] / "baks"
        run(world, "--apply", "--journal", str(world["tmp"] / "j1.json"),
            "--pre-backup-dir", str(backups))
        run(world, "--apply", "--journal", str(world["tmp"] / "j2.json"),
            "--pre-backup-dir", str(backups))
        made = sorted(backups.glob("*.bak-restore-*"))
        assert len(made) == 2
        assert count(made[0], "agents") == 0  # 第一份仍是恢复前的状态
