"""历史回采脚本的行为契约 —— 见 docs/token-attribution-v1-design.md §6 / §7 阶段3。

这个脚本会一次性改写两千余行生产数据、由缔造者亲手执行，所以设计里那三条"硬约束"
（§6.4）不能只写在注释里，必须逐条可机检：

* **硬约束① 绝不触碰 ``workflow_agents.tokens``** —— 那是 ctx_last 口径（末轮上下文
  水位），与本脚本产出的 usage_sum 实测差 5~25 倍。写进去 = 混口径永久固化进历史且
  事后不可分辨（风险 R3）。三道拦：写列白名单、apply 层 assert、前后逐行 sha256 指纹。
* **硬约束② 覆盖率按 tokens_measured_at 分窗** —— 回采会把总覆盖率从 0.5% 抬到 78%，
  但"新派工有没有被采到"是另一回事。一个数字掩盖另一个数字（对策 6.3-2），所以两个数
  从不合并。
* **硬约束③ dry-run 先行、只写空列、重跑零变更** —— 幂等是结构性的（``already_measured``
  是判定的第一分支），不是靠标记位。

外加两条本脚本自己的纪律：**写不了的行必须带原因码**（no-data ≠ zero），**model 只做
观测回填**（别名台账的解析结果绝不落库，无 transcript 的行不猜不动）。

全部用例只跑 tmp_path 里的临时库与临时 transcript，一个字节都不碰生产库。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_token_usage.py"
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

_spec = importlib.util.spec_from_file_location("backfill_token_usage", SCRIPT)
assert _spec and _spec.loader
bf = importlib.util.module_from_spec(_spec)
# 必须先登记进 sys.modules 再 exec：脚本里的 @dataclass 会回查自己的模块命名空间。
sys.modules["backfill_token_usage"] = bf
_spec.loader.exec_module(bf)

SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"
SLUG = "-Users-dev-Desktop-AI-team-OS"


# ---------------------------------------------------------------------------
# 夹具：真 transcript 文件 + 最小两表库
# ---------------------------------------------------------------------------
def write_transcript(
    path: Path, calls: list[tuple[str, dict[str, int]]], *, model: str = "claude-opus-5"
) -> Path:
    """写一份真 jsonl —— 不 mock 解析器。

    每个 (requestId, usage) 写**两行**，第二行的 output_tokens 更大：这复刻了流式的真实
    形态（同一 requestId 内 output 是递增快照而非增量）。解析器必须按 requestId 取末条，
    逐行裸加会严重虚高。这份夹具的存在就是为了让"回采值 = 真解析结果"而不是"= 我以为的
    解析结果"。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for req, usage in calls:
        half = dict(usage, output_tokens=max(usage["output_tokens"] // 2, 1))
        for u in (half, usage):
            lines.append(
                json.dumps(
                    {
                        "type": "assistant",
                        "requestId": req,
                        "message": {"model": model, "usage": {
                            "input_tokens": u["input_tokens"],
                            "output_tokens": u["output_tokens"],
                            "cache_creation_input_tokens": u["cache_creation_tokens"],
                            "cache_read_input_tokens": u["cache_read_tokens"],
                        }},
                    }
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


USAGE_ONE = {
    "input_tokens": 10,
    "output_tokens": 500,
    "cache_creation_tokens": 2000,
    "cache_read_tokens": 90000,
}
# 一份两次调用的 transcript 的期望累加值（跨 requestId 相加）。
EXPECTED_TWO_CALLS = {k: v * 2 for k, v in USAGE_ONE.items()}


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def dbfile(tmp_path: Path) -> Path:
    """最小两表库，列名与生产一致。"""
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
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
    con.commit()
    con.close()
    return p


def add_agent(
    dbfile: Path,
    aid: str,
    *,
    name: str = "worker",
    role: str = "worker",
    model: str | None = None,
    transcript_path: str | None = None,
    created_at: str = "2026-07-20 10:00:00",
    measured_at: str | None = None,
    source: str | None = None,
    tokens: dict[str, int] | None = None,
) -> None:
    t = tokens or {}
    con = sqlite3.connect(dbfile)
    con.execute(
        "insert into agents values (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            aid, name, role, model, created_at, transcript_path,
            t.get("input_tokens"), t.get("output_tokens"),
            t.get("cache_creation_tokens"), t.get("cache_read_tokens"),
            measured_at, source,
        ),
    )
    con.commit()
    con.close()


def add_wa(
    dbfile: Path, wid: str, *, model: str, os_agent_id: str | None = None,
    tokens: int | None = 12345, label: str = "wf-x",
) -> None:
    con = sqlite3.connect(dbfile)
    con.execute(
        "insert into workflow_agents values (?,?,?,?,?,?,?)",
        (wid, label, model, os_agent_id, "cc1", "2026-07-20 10:00:00", tokens),
    )
    con.commit()
    con.close()


def load(dbfile: Path):
    con = bf.open_db(dbfile, writable=False)
    rows = bf.load_agents(con)
    con.close()
    return rows


def run(monkeypatch, dbfile: Path, *extra: str) -> int:
    monkeypatch.setattr(sys, "argv", ["backfill", "--db", str(dbfile), "--quiet", *extra])
    return bf.main()


def fetch(dbfile: Path, sql: str, *params):
    con = sqlite3.connect(dbfile)
    row = con.execute(sql, params).fetchone()
    con.close()
    return row


BATCH = "2026-07-29 03:00:00.000000"


# ===========================================================================
# 硬约束① —— workflow_agents.tokens 逐行未变
# ===========================================================================
class TestHardConstraint1ForbiddenColumn:
    def test_the_forbidden_column_is_not_in_the_write_whitelist(self):
        """白名单是封闭集合，禁改列不在其中 —— 这是第一道拦，也是最便宜的一道。"""
        assert bf.FORBIDDEN_COLUMN == ("workflow_agents", "tokens")
        assert bf.FORBIDDEN_COLUMN not in bf.WRITABLE_COLUMNS

    def test_only_model_is_writable_on_workflow_agents(self):
        """本脚本碰 workflow_agents 只为把别名换成观测型号，别的列一概不写。"""
        wa_cols = {c for t, c in bf.WRITABLE_COLUMNS if t == "workflow_agents"}
        assert wa_cols == {"model"}

    def test_apply_refuses_a_job_that_targets_the_forbidden_column(self, dbfile):
        """第二道拦：即使将来有人构造出这样的 Job，写入层也当场炸而不是照写。"""
        add_wa(dbfile, "wa1", model="opus", tokens=999)
        job = bf.Job("恶意", "workflow_agents")
        job.rows.append(bf.Row("workflow_agents", "wa1", "x", "written", values={"tokens": 1}))
        con = sqlite3.connect(dbfile)
        with pytest.raises(RuntimeError, match="写列白名单拦截"):
            bf.apply_job(con, job)
        con.close()
        assert fetch(dbfile, "select tokens from workflow_agents where id='wa1'")[0] == 999

    def test_fingerprint_catches_compensating_edits_that_preserve_the_sum(self, dbfile):
        """第三道拦必须比"对合计"强：两行一增一减能让 sum 纹丝不动。"""
        add_wa(dbfile, "wa1", model="opus", tokens=100)
        add_wa(dbfile, "wa2", model="opus", tokens=200)
        con = sqlite3.connect(dbfile)
        before = bf.workflow_tokens_fingerprint(con)
        con.execute("update workflow_agents set tokens=200 where id='wa1'")
        con.execute("update workflow_agents set tokens=100 where id='wa2'")
        con.commit()
        after = bf.workflow_tokens_fingerprint(con)
        con.close()
        assert before["sum"] == after["sum"]  # 合计骗得过
        assert before["sha256"] != after["sha256"]  # 逐行指纹骗不过

    def test_a_real_apply_leaves_the_column_byte_identical(self, dbfile, tmp_path, monkeypatch):
        """端到端：真跑一次回采，禁改列逐行未变（§7 阶段3 验收条目）。"""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE), ("r2", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="opus", os_agent_id="a1", tokens=777)
        con = sqlite3.connect(dbfile)
        before = bf.workflow_tokens_fingerprint(con)
        con.close()

        assert run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json")) == 0

        con = sqlite3.connect(dbfile)
        after = bf.workflow_tokens_fingerprint(con)
        con.close()
        assert after == before
        # 但 model 列确实被观测回填了 —— 证明这次 apply 不是什么都没干
        assert fetch(dbfile, "select model from workflow_agents where id='wa1'")[0] == "claude-opus-5"

    def test_journal_records_the_fingerprint_both_sides(self, dbfile, tmp_path, monkeypatch):
        """journal 是唯一的恢复凭证，禁改列的前后指纹必须在里面。"""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="opus", os_agent_id="a1", tokens=777)
        journal = tmp_path / "j.json"
        run(monkeypatch, dbfile, "--apply", "--journal", str(journal))
        data = json.loads(journal.read_text())
        assert data["forbidden_column"]["column"] == "workflow_agents.tokens"
        assert data["forbidden_column"]["unchanged"] is True
        assert data["metric"] == "usage_sum"  # 口径随凭证走，不靠事后回忆


# ===========================================================================
# 硬约束② —— 覆盖率分窗
# ===========================================================================
class TestHardConstraint2CoverageWindowing:
    def test_denominator_keeps_rows_that_have_no_transcript(self, dbfile):
        """§4.1：不得以"没路径所以不算"把行移出分母 —— 那正是局部冒充全貌（R2）。"""
        add_agent(dbfile, "a1", transcript_path=None)
        add_agent(dbfile, "a2", transcript_path="/nope.jsonl")
        cov = bf.measure_coverage(load(dbfile), set(), leader=False)
        assert cov.total == 2
        assert cov.measured == 0

    def test_leader_rows_are_counted_under_their_own_denominator(self, dbfile):
        """主会话量级比子 agent 大两三个数量级，混一个分母子 agent 会被淹没（§3.3）。"""
        add_agent(dbfile, "a1")
        add_agent(dbfile, "L1", role="leader")
        assert bf.measure_coverage(load(dbfile), set(), leader=False).total == 1
        assert bf.measure_coverage(load(dbfile), set(), leader=True).total == 1

    def test_a_shared_timestamp_is_what_marks_a_backfill_cohort(self, dbfile):
        """活体链路逐行写各自的停止时刻（微秒级唯一）；回采给上千行盖同一个值。"""
        for i in range(bf.BATCH_COHORT_MIN):
            add_agent(dbfile, f"b{i}", measured_at=BATCH)
        add_agent(dbfile, "live", measured_at="2026-07-28 11:20:20.521255")
        con = bf.open_db(dbfile, writable=False)
        cohorts = bf.detect_backfill_cohorts(con)
        con.close()
        assert cohorts == {BATCH}

    def test_a_handful_of_rows_sharing_a_timestamp_is_not_a_cohort(self, dbfile):
        """阈值存在的意义：偶然同刻的两三行不该被误判成一次回采。"""
        for i in range(3):
            add_agent(dbfile, f"b{i}", measured_at=BATCH)
        con = bf.open_db(dbfile, writable=False)
        assert bf.detect_backfill_cohorts(con) == set()
        con.close()

    def test_backfill_never_moves_the_incremental_number(self, dbfile):
        """对策 6.3-2 的核心：回采只抬"历史回采"一格，"增量采集"逐字不动。"""
        add_agent(dbfile, "live", measured_at="2026-07-28 11:20:20.521255")
        for i in range(5):
            add_agent(dbfile, f"todo{i}")
        before = bf.measure_coverage(load(dbfile), set(), leader=False)
        after = bf.project_coverage(before, 5)
        assert (before.incremental, before.backfilled) == (1, 0)
        assert (after.incremental, after.backfilled) == (1, 5)
        assert after.total == before.total  # 分母不因回采而变

    def test_the_rendered_line_shows_three_numbers_and_no_sum(self, dbfile):
        """两格相加没有意义，报告也不给这个和 —— 混口径的下一步就是混分母。"""
        add_agent(dbfile, "live", measured_at="2026-07-28 11:20:20.521255")
        add_agent(dbfile, "todo")
        line = bf.measure_coverage(load(dbfile), set(), leader=False).line()
        assert "增量采集" in line and "历史回采" in line and "未归因" in line
        assert "合计" not in line and "总计" not in line


# ===========================================================================
# 硬约束③ —— dry-run / 只写空列 / 幂等
# ===========================================================================
class TestHardConstraint3DryRunAndIdempotence:
    def test_dry_run_is_the_default_and_writes_nothing(self, dbfile, tmp_path, monkeypatch):
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        assert run(monkeypatch, dbfile) == 0
        assert fetch(dbfile, "select tokens_measured_at from agents where id='a1'")[0] is None

    def test_apply_writes_the_parsed_four_layers_and_the_source_tag(
        self, dbfile, tmp_path, monkeypatch
    ):
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE), ("r2", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json"))
        row = fetch(
            dbfile,
            "select input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,"
            " tokens_source from agents where id='a1'",
        )
        assert tuple(row[:4]) == tuple(EXPECTED_TWO_CALLS[c] for c in bf.TOKEN_COLUMNS)
        assert row[4] == "transcript"

    def test_rerun_after_apply_plans_zero_writes(self, dbfile, tmp_path, monkeypatch, capsys):
        """§7 阶段3 验收：``--apply`` 后重跑零变更。"""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json"))
        capsys.readouterr()
        assert run(monkeypatch, dbfile) == 0
        assert "待写入合计：0 行" in capsys.readouterr().out

    def test_a_measured_row_is_never_re_measured(self, dbfile, tmp_path):
        """幂等的实现位置：``already_measured`` 是判定的第一分支，早于读文件。"""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp), measured_at=BATCH, source="transcript")
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        assert [r.reason for r in job.rows] == ["already_measured"]
        assert job.written() == []

    def test_sql_guard_lets_a_concurrent_writer_win(self, dbfile, tmp_path):
        """dry-run 与 apply 之间活体系统可能已把值写上 —— 守卫在 SQL 里，并发下也不覆盖。"""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        assert len(job.written()) == 1
        # 判定之后、写入之前，活体链路先写上了
        con = sqlite3.connect(dbfile)
        con.execute("update agents set tokens_measured_at='2026-07-29 09:00:00' where id='a1'")
        con.commit()
        assert bf.apply_job(con, job) == 0  # 一行都没被改
        con.close()
        assert fetch(dbfile, "select tokens_measured_at from agents where id='a1'")[0] == (
            "2026-07-29 09:00:00"
        )

    def test_apply_without_a_journal_is_refused(self, dbfile, monkeypatch):
        """journal 是唯一的恢复凭证，没有它就不许写。"""
        assert run(monkeypatch, dbfile, "--apply") == 2

    def test_an_existing_journal_is_never_overwritten(self, dbfile, tmp_path, monkeypatch):
        """二次 apply 会把"回采前基线"覆盖成"回采后状态"，凭证就没了（照 UTC 脚本的先例）。"""
        journal = tmp_path / "j.json"
        journal.write_text("{}")
        assert run(monkeypatch, dbfile, "--apply", "--journal", str(journal)) == 2
        assert journal.read_text() == "{}"

    def test_all_rows_of_one_run_share_one_batch_timestamp(self, dbfile, tmp_path):
        """同一批次共享一个时刻 —— 既是事实，也是硬约束② 分窗的签名。"""
        for i in range(3):
            tp = write_transcript(tmp_path / f"a{i}.jsonl", [("r1", USAGE_ONE)])
            add_agent(dbfile, f"a{i}", transcript_path=str(tp))
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        assert {r.values["tokens_measured_at"] for r in job.written()} == {BATCH}


# ===========================================================================
# 原因码 —— 写不了的行必须分类（no-data ≠ zero）
# ===========================================================================
class TestReasonCodes:
    def test_missing_path_and_gone_file_are_different_reasons(self, dbfile):
        """§3.4：一个"没测到"总数说明不了能不能救，两类的处置完全不同。"""
        add_agent(dbfile, "a1", transcript_path=None)
        add_agent(dbfile, "a2", transcript_path="/definitely/not/here.jsonl")
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        assert job.counts() == {"no_transcript_path": 1, "transcript_gone": 1}

    def test_an_empty_transcript_is_unreadable_not_zero_tokens(self, dbfile, tmp_path):
        """no-data ≠ zero：解析不出快照绝不能落成"用了 0 token"。"""
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        add_agent(dbfile, "a1", transcript_path=str(empty))
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        assert [r.reason for r in job.rows] == ["unreadable_transcript"]
        assert job.written() == []

    def test_gone_rows_are_listed_row_by_row_not_just_counted(self, dbfile, capsys):
        """这类只增不减，报告本身就是"窗口已在这些行上关闭"的存证（§3.4）。"""
        add_agent(dbfile, "a1", name="lost-worker", transcript_path="/gone/x.jsonl")
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        bf.print_transcript_gone([job])
        out = capsys.readouterr().out
        assert "lost-worker" in out and "/gone/x.jsonl" in out

    def test_the_all_clear_case_says_so_explicitly(self, dbfile, capsys):
        """0 行不能留白 —— 空白会被读成 bug，而这里的 0 是一个好消息。"""
        add_agent(dbfile, "a1", transcript_path=None)
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        bf.print_transcript_gone([job])
        assert "0 行" in capsys.readouterr().out

    def test_every_reason_used_by_the_jobs_has_a_human_description(self):
        """原因码是给人读的；漏一条描述，报告里就会出现一个没人认识的代号。"""
        assert set(bf.REASON_ORDER) == set(bf.REASONS)
        assert all(bf.REASONS.values())


# ===========================================================================
# model 观测回填 —— 只观测，不推断
# ===========================================================================
class TestModelObservationBackfill:
    def test_alias_is_replaced_by_the_model_observed_in_the_transcript(self, dbfile, tmp_path):
        """§6.2-4：transcript 的 message.model 永远是完整型号，从不是别名。"""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)], model="claude-opus-4-8")
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="opus", os_agent_id="a1")
        con = bf.open_db(dbfile, writable=False)
        job = bf.job_workflow_model(bf.load_workflow_agents(con), bf.Parser(verbose=False))
        con.close()
        assert job.written()[0].values == {"model": "claude-opus-4-8"}
        assert job.written()[0].guard == {"model": "opus"}  # 守卫钉死"仍是我读到的那个别名"

    def test_a_row_without_a_transcript_is_left_alone_not_guessed(self, dbfile):
        """"无 transcript 的行不猜、不动" —— 别名台账只在读侧兜底，绝不落库。"""
        add_wa(dbfile, "wa1", model="opus", os_agent_id=None)
        con = bf.open_db(dbfile, writable=False)
        job = bf.job_workflow_model(bf.load_workflow_agents(con), bf.Parser(verbose=False))
        con.close()
        assert [r.reason for r in job.rows] == ["no_transcript_path"]
        assert job.written() == []

    def test_the_alias_ledger_is_never_consulted_for_writing(self):
        """MODEL_ALIAS_LEDGER 是读侧兜底，回采脚本连引用都不该有。

        判据走 AST 而不是文本查找：文档字符串里**必须**能谈论这条边界（"别名解析结果绝不
        写进任何行"正是要写下来的纪律），能谈论但不能调用。按文本查会把讲纪律本身判成
        违纪，那种机检只会逼人把纪律从注释里删掉。
        """
        import ast

        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        referenced = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {
            alias.name for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
        }
        assert "MODEL_ALIAS_LEDGER" not in referenced
        assert "resolve_model_alias" not in referenced

    def test_a_concrete_model_is_never_overwritten_by_another_observation(self, dbfile, tmp_path):
        """观测值不覆盖观测值 —— 本脚本只补真，不改判。"""
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)], model="claude-opus-5")
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="claude-opus-4-8", os_agent_id="a1")
        con = bf.open_db(dbfile, writable=False)
        job = bf.job_workflow_model(bf.load_workflow_agents(con), bf.Parser(verbose=False))
        con.close()
        assert [r.reason for r in job.rows] == ["already_concrete"]

    def test_synthetic_only_transcript_yields_no_observed_model(self, dbfile, tmp_path):
        """compact 合成行的 model 是 <synthetic>，不是任何真实型号（§1.3）。"""
        tp = tmp_path / "syn.jsonl"
        tp.write_text(
            json.dumps({
                "type": "assistant", "requestId": "r1",
                "message": {"model": "<synthetic>", "usage": {
                    "input_tokens": 1, "output_tokens": 2,
                    "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}},
            }) + "\n",
            encoding="utf-8",
        )
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="opus", os_agent_id="a1")
        con = bf.open_db(dbfile, writable=False)
        job = bf.job_workflow_model(bf.load_workflow_agents(con), bf.Parser(verbose=False))
        con.close()
        assert [r.reason for r in job.rows] == ["no_observed_model"]

    def test_agents_model_is_filled_only_when_the_column_is_empty(self, dbfile, tmp_path):
        add_agent(dbfile, "a1", model="claude-fable-5",
                  transcript_path=str(write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])))
        job = bf.job_subagent_usage(load(dbfile), bf.Parser(verbose=False), BATCH)
        row = job.written()[0]
        assert "model" not in row.values          # 已有观测值，不动
        assert "不一致" in row.warn                # 但人看得见

    def test_no_model_flag_turns_the_observation_backfill_off(self, dbfile, tmp_path, monkeypatch):
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp))
        add_wa(dbfile, "wa1", model="opus", os_agent_id="a1")
        run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json"), "--no-model")
        assert fetch(dbfile, "select model from workflow_agents where id='wa1'")[0] == "opus"
        assert fetch(dbfile, "select model from agents where id='a1'")[0] is None
        # 但 token 该采的还是采了
        assert fetch(dbfile, "select tokens_source from agents where id='a1'")[0] == "transcript"


# ===========================================================================
# Job B —— 已测量行的 tokens_source 补标（含零容差重算对账）
# ===========================================================================
class TestSourceLabelRecompute:
    def _measured(self, dbfile, tmp_path, stored: dict[str, int]):
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp), measured_at=BATCH, tokens=stored)
        return bf.job_source_label(load(dbfile), bf.Parser(verbose=False))

    def test_label_is_written_only_when_recompute_matches_field_by_field(self, dbfile, tmp_path):
        """闸2① 的形态：解析器是纯函数，容差为零而非 10%。相等才敢标 provenance。"""
        job = self._measured(dbfile, tmp_path, USAGE_ONE)
        assert job.written()[0].values == {"tokens_source": "transcript"}

    def test_a_grown_transcript_is_its_own_class_not_an_anomaly(self, dbfile, tmp_path):
        """agent 还活着（比如跑这个脚本的这一个），测量后 transcript 又长了 —— 正常。"""
        smaller = {k: v - 1 for k, v in USAGE_ONE.items()}
        job = self._measured(dbfile, tmp_path, smaller)
        assert [r.reason for r in job.rows] == ["transcript_grew"]
        assert job.written() == []

    def test_a_genuine_divergence_is_reported_and_never_written(self, dbfile, tmp_path):
        """对不上且非增长 = 真异常，人必须看，而且绝不因此改动任何值。"""
        bigger = {k: v + 1000 for k, v in USAGE_ONE.items()}
        job = self._measured(dbfile, tmp_path, bigger)
        assert [r.reason for r in job.rows] == ["recompute_mismatch"]
        assert job.written() == []

    def test_an_already_labelled_row_is_left_alone(self, dbfile, tmp_path):
        tp = write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "a1", transcript_path=str(tp), measured_at=BATCH, source="transcript")
        job = bf.job_source_label(load(dbfile), bf.Parser(verbose=False))
        assert [r.reason for r in job.rows] == ["already_set"]

    def test_unmeasured_rows_are_not_this_jobs_business(self, dbfile, tmp_path):
        add_agent(dbfile, "a1", transcript_path=str(
            write_transcript(tmp_path / "a1.jsonl", [("r1", USAGE_ONE)])))
        assert bf.job_source_label(load(dbfile), bf.Parser(verbose=False)).rows == []


# ===========================================================================
# Leader 主会话 —— 默认关闭 + 按文件去重
# ===========================================================================
class TestLeaderIsOptIn:
    def test_leader_rows_are_untouched_by_default(self, dbfile, tmp_path, monkeypatch, capsys):
        tp = write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "L1", name="Leader", role="leader", transcript_path=str(tp))
        run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json"))
        assert fetch(dbfile, "select tokens_measured_at from agents where id='L1'")[0] is None
        assert "Job D 默认不执行" in capsys.readouterr().out

    def test_include_leader_actually_writes(self, dbfile, tmp_path, monkeypatch):
        tp = write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "L1", name="Leader", role="leader", transcript_path=str(tp))
        run(monkeypatch, dbfile, "--apply", "--journal", str(tmp_path / "j.json"),
            "--include-leader")
        assert fetch(dbfile, "select tokens_source from agents where id='L1'")[0] == "transcript"

    def test_one_transcript_is_counted_once_no_matter_how_many_rows_point_at_it(
        self, dbfile, tmp_path
    ):
        """实测 47 个 Leader 行只指向 13 份文件，最多一份被 11 行共享。照单全收 =
        把同一份 8.5 亿 token 的用量重复计入 11 次。"""
        tp = str(write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)]))
        add_agent(dbfile, "L1", name="Leader", role="leader", transcript_path=tp,
                  created_at="2026-07-20 08:00:00")
        add_agent(dbfile, "L2", name="Leader", role="leader", transcript_path=tp,
                  created_at="2026-07-20 09:00:00")
        add_agent(dbfile, "L3", name="Leader", role="leader", transcript_path=tp,
                  created_at="2026-07-20 10:00:00")
        job = bf.job_leader_usage(load(dbfile), bf.Parser(verbose=False), BATCH, enabled=True)
        assert len(job.written()) == 1
        assert job.written()[0].row_id == "L1"  # 最早那行代表，与 _find_leader 同源
        assert job.counts()["duplicate_main_transcript"] == 2

    def test_a_disabled_job_is_previewed_but_never_applied(self, dbfile, tmp_path):
        """预览必须照常算 —— 不然人无从判断该不该开这个开关。"""
        tp = write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)])
        add_agent(dbfile, "L1", name="Leader", role="leader", transcript_path=str(tp))
        job = bf.job_leader_usage(load(dbfile), bf.Parser(verbose=False), BATCH, enabled=False)
        assert len(job.written()) == 1  # 判定照做
        con = sqlite3.connect(dbfile)
        assert bf.apply_job(con, job) == 0  # 但写入层直接跳过
        con.close()


# ===========================================================================
# 解析器复用 —— 同一份文件不重复读
# ===========================================================================
class TestParserCache:
    def test_a_shared_transcript_is_parsed_once(self, tmp_path):
        """13 份主会话文件被 47 行共享，逐行解析等于把 2 GB 读上好几遍。"""
        tp = str(write_transcript(tmp_path / "main.jsonl", [("r1", USAGE_ONE)]))
        parser = bf.Parser(verbose=False)
        first = parser.usage(tp)
        second = parser.usage(tp)
        assert first == second
        assert parser.parsed == 1

    def test_a_missing_file_is_cached_as_none_not_retried(self, tmp_path):
        parser = bf.Parser(verbose=False)
        assert parser.usage(str(tmp_path / "nope.jsonl")) is None
        assert parser.usage(str(tmp_path / "nope.jsonl")) is None
        assert parser.parsed == 1
