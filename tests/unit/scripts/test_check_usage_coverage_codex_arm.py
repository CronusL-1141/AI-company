"""I13 第三臂（Codex 桶）—— 各分支的红绿与无库半跑。

前两臂问"这个呈现面写得对不对"，第三臂问"这条归因链今天还活着吗"。两者的失败长得
一样（页面上一个 0），成因完全不同，所以判据也不同：第三臂读实库。

读实库的机检有一个特有的失败模式——**在没有库的机器上稳定假红**。CI 跑在裸 ubuntu
上，那里既没有 OS 库也没有 Codex 目录；一条在那里必红的断言等于把红线机检整条废掉
（人会去关它，而不是去修它）。所以这里逐条钉的不只是"该红时红"，还有"没有源时不红"。

同样逐条钉住的是**两桶禁相加**：生产滚动桶与 golden 冻结桶的分母算的是两个不同的
总体，加起来那个比值不对应任何真实分母。正向的"请不要相加"是注释，注释拦不住人；
这里钉的是反向断言——合并后的那个数字不许出现在输出里。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from aiteam.storage.connection import _sqlite_migrate
from aiteam.storage.models import AgentActivityModel, AgentModel, Base

ROOT = Path(__file__).resolve().parents[3]


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_usage_coverage_probe", ROOT / "scripts" / "check_usage_coverage.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

# 两个真的 v7（版本位 7、变体位 8~b）。取自 P-1 夹具的 thread_spawn 线程 id ——
# 用真值而不是手搓，是为了让"这个正则认不认得真实数据"也被这几条用例覆盖。
V7_SESSION = "019f8b21-a6ae-7533-b326-2d260efbf40b"
V7_TOOL_USE = "019f8b4d-1b95-7763-9625-e9b0691b5a3e"
V4_SESSION = "3f2a91c4-5b6d-4e8f-9a1b-2c3d4e5f6a7b"  # CC 形状：版本位 4

# 模型名一律用中性占位，不写任何真实型号代号：跟踪文件是要发出去的，型号代号进了
# 树面就等于把它发布出去（发版第 5 步扫的正是这个）。夹具那边同一条纪律已有落点
# （``scripts/redact_codex_fixture.py`` 把学到的型号映射成 ``codex-model-<x>``）。
# 第三臂对 model 列只判空与非空，具体写什么不承载任何断言。
PROVIDER_MODEL = "provider-main-model"  # 另一 harness 侧的行
CC_MODEL = "cc-main-model"  # CC 侧的行


def _now():
    from aiteam.clock import to_naive_utc, utc_now

    now = utc_now()
    return now, str(to_naive_utc(now))


def _build_production_schema(path: Path):
    """按生产的列定义建表 —— 建表语句不在这里手写。

    手写 ``create table`` 的替身天然比生产宽松：生产把 ``agents.transcript_path``
    改个名或挪走，第三臂对实库的 SQL 当场崩，而这些用例照绿——"stub 不得比生产宽松"
    的标准形状。本机跑 I13 时读的是真库，能兜住；CI 上没有库，兜不住，于是这层假绿
    只在没人看得见的地方成立。

    所以替身与生产共读同一份 ``Base.metadata``，再跑一遍生产的列迁移：列改名会让
    这里的插入或第三臂的 SELECT 直接报错，而不是安静地继续绿。
    """
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine


def _as_datetime(value):
    """用例里的时间戳写成字符串更好读，落库要的是 datetime。"""
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _make_os_db(path: Path, agents: list[dict], activities: list[tuple[str, str]]) -> None:
    """一个按生产 schema 建起来的 OS 库，只填第三臂真的会读的那几列。

    行也走生产的 ORM 模型插：NOT NULL 与列默认值一并由生产那份定义说了算，替身
    不许自己放宽。
    """
    engine = _build_production_schema(path)
    try:
        with Session(engine) as session:
            for row in agents:
                session.add(
                    AgentModel(
                        id=row["id"],
                        team_id=row.get("team_id", "team-1"),
                        name=row.get("name", row["id"]),
                        role=row.get("role", "worker"),
                        session_id=row.get("session_id"),
                        cc_tool_use_id=row.get("cc_tool_use_id"),
                        model=row.get("model") or "",
                        transcript_path=row.get("transcript_path"),
                        harness=row.get("harness"),
                        created_at=_as_datetime(row["created_at"]),
                    )
                )
            owner = agents[0]["id"] if agents else "a0"
            for n, (tool, ts) in enumerate(activities):
                session.add(
                    AgentActivityModel(
                        id=f"act{n}",
                        agent_id=owner,
                        session_id="s0",
                        tool_name=tool,
                        timestamp=_as_datetime(ts),
                    )
                )
            session.commit()
    finally:
        engine.dispose()
    _sqlite_migrate(str(path))


# state_5 是宿主的库，本仓没有它的 ORM，只能手建。手建的代价是列集会与检查器发散，
# 所以列名不在这里抄第二遍：类型在这里声明，列集与顺序取检查器那份常量，两边对不上
# 就当场断言失败，而不是安静地建出一张检查器读不了的表。
_THREAD_COLUMN_TYPES = {
    "id": "text primary key",
    "source": "text",
    "thread_source": "text",
    "created_at": "integer",
}


def _make_state_db(path: Path, threads: list[tuple[str, str, str, int]]) -> None:
    """Codex 的 state 库，列集与第三臂共读 ``checker.CODEX_THREAD_COLUMNS``。"""
    columns = checker.CODEX_THREAD_COLUMNS
    assert set(columns) == set(_THREAD_COLUMN_TYPES), (
        f"第三臂读的列集变成了 {columns}，替身这边的类型声明没跟上"
    )
    ddl = ", ".join(f"{name} {_THREAD_COLUMN_TYPES[name]}" for name in columns)
    con = sqlite3.connect(path)
    con.execute(f"create table threads ({ddl})")
    con.executemany(
        f"insert into threads ({', '.join(columns)})"
        f" values ({', '.join('?' * len(columns))})",
        threads,
    )
    con.commit()
    con.close()


def _spawn_source() -> str:
    return json.dumps({"subagent": {"thread_spawn": {"parent": "p"}}})


def _guardian_source() -> str:
    return json.dumps({"subagent": {"other": "guardian"}})


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """把第三臂的两个数据源都指向临时目录 —— 一个字节都不碰真实的库。"""
    home = tmp_path / "codex-home"
    home.mkdir()
    db = tmp_path / "aiteam.db"
    monkeypatch.setenv("AITEAM_DB_PATH", str(db))
    monkeypatch.setenv("CODEX_HOME", str(home))
    return {"home": home, "db": db, "state": home / checker.CODEX_STATE_DB}


# ---------------------------------------------------------------------------
# 纯函数：三面工具名 / v7 判定 / 线程分桶 / 禁相加
# ---------------------------------------------------------------------------
def test_uuid_v7_accepts_codex_ids_and_rejects_cc_ones():
    assert checker.is_uuid_v7(V7_SESSION)
    assert checker.is_uuid_v7(V7_TOOL_USE)
    assert checker.is_uuid_v7(V7_SESSION.upper()), "大小写不该改变身份判定"
    assert not checker.is_uuid_v7(V4_SESSION), "v4 是 CC 形状，两个 harness 必须分得开"
    assert not checker.is_uuid_v7(V7_SESSION[:-1]), "长度不足 36"
    assert not checker.is_uuid_v7(None)
    assert not checker.is_uuid_v7("")


def test_dispatch_tool_matches_all_three_faces():
    """§6.3 三面互不相等，判据必须三面都认。"""
    assert checker.is_dispatch_tool("collaboration.spawn_agent")  # 模型面
    assert checker.is_dispatch_tool("collaborationspawn_agent")  # hook 载荷面
    assert checker.is_dispatch_tool("spawn_agent")  # rollout 记录面


def test_dispatch_tool_does_not_swallow_neighbours():
    """近邻不得误命中 —— 误报的机检等于没有机检。"""
    for name in (
        "Agent",  # CC 的派工工具，另一个 harness 的事
        "Task",
        "collaborationwait_agent",  # 同族但不是派工
        "mcp__ai-team-os__agent_list",
        "ListAgents",
    ):
        assert not checker.is_dispatch_tool(name), name


def test_split_dispatch_threads_keeps_guardian_out_of_the_denominator():
    rows = [
        ("t-spawn", _spawn_source(), "subagent", 100),
        ("t-guard", _guardian_source(), "subagent", 101),
        ("t-plain", json.dumps({"vscode": {}}), "user", 102),
        ("t-broken", "not json", "subagent", 103),
    ]
    spawn, other = checker.split_dispatch_threads(rows)
    assert set(spawn) == {"t-spawn"}
    assert set(other) == {"t-guard"}
    assert spawn["t-spawn"] == 100, "created_at 要带出来，R7 前置要按窗口筛"
    assert not set(spawn) & set(other)


def test_forbid_bucket_merge_catches_the_summed_ratio():
    prod = checker.Bucket("生产滚动桶", 1, 3, "统计时点 X")
    frozen = checker.Bucket("冻结桶", 0, 2, "冻结总体")
    clean = [prod.line(), frozen.line()]
    assert checker.forbid_bucket_merge(clean, [prod, frozen]) == []

    merged = clean + ["两桶合起来 1/5"]
    problems = checker.forbid_bucket_merge(merged, [prod, frozen])
    assert problems and "1/5" in problems[0]


def test_forbid_bucket_merge_catches_a_total_line():
    prod = checker.Bucket("生产滚动桶", 1, 3, "统计时点 X")
    frozen = checker.Bucket("冻结桶", 0, 2, "冻结总体")
    problems = checker.forbid_bucket_merge(["合计 1/5"], [prod, frozen])
    assert any("合计" in p for p in problems)


def test_golden_frozen_bucket_comes_from_the_fixture():
    """冻结桶只能从夹具读 —— 抄一份到脚本里就是第二真相源。"""
    frozen, note = checker.golden_frozen_bucket()
    assert frozen is not None, "仓内应有 tests/fixtures/codex/golden.json 的 G11"
    assert note == ""
    assert (frozen.numerator, frozen.denominator) == (0, 2)
    assert "禁相加" in frozen.scope


def test_absent_golden_is_silent_but_a_broken_one_speaks_up(tmp_path, monkeypatch):
    """夹具不在 = 精简 checkout，静默；夹具在而 G11 键不全 = 有人动了它，要说出来。"""
    monkeypatch.setattr(checker, "CODEX_GOLDEN", tmp_path / "absent" / "golden.json")
    assert checker.golden_frozen_bucket() == (None, "")

    broken = tmp_path / "golden.json"
    broken.write_text(json.dumps({"items": [{"name": "G11_x", "values": {}}]}), encoding="utf-8")
    monkeypatch.setattr(checker, "CODEX_GOLDEN", broken)
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    bucket, note = checker.golden_frozen_bucket()
    assert bucket is None
    assert "读不出 G11 冻结桶" in note


# ---------------------------------------------------------------------------
# I-CDX-R3：黑名单扫描
# ---------------------------------------------------------------------------
def test_r3_flags_the_forbidden_column_in_code(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "collector.py").write_text(
        'rows = [t for t in threads if t["has_user_event"]]\n', encoding="utf-8"
    )
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "R3_SCAN_ROOTS", ("pkg",))
    problems = checker.scan_forbidden_judgement_column()
    assert len(problems) == 1
    assert "pkg/collector.py:1" in problems[0]


def test_r3_ignores_comments_and_fixtures(tmp_path, monkeypatch):
    """写下"禁用 X"这句话本身不该把机检点着，夹具里的行转储也不是判据。"""
    pkg = tmp_path / "pkg"
    (pkg / "state").mkdir(parents=True)
    (pkg / "note.py").write_text("# 禁用 has_user_event 作判据\nx = 1\n", encoding="utf-8")
    (pkg / "state" / "threads.sample.json").write_text(
        '{"has_user_event": 0}\n', encoding="utf-8"
    )
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "R3_SCAN_ROOTS", ("pkg",))
    assert checker.scan_forbidden_judgement_column() == []


def test_r3_is_clean_in_this_repo():
    """回归位：仓内今天零命中，将来有人拿那个恒 0 的列当判据就会在这里红。"""
    assert checker.scan_forbidden_judgement_column() == []


# ---------------------------------------------------------------------------
# 第三臂整体：各分支红绿
# ---------------------------------------------------------------------------
def test_half_run_without_a_live_database(bench):
    """CI 无库：只跑静态半边，不红，且冻结桶照样报得出来。"""
    problems, warnings, notes = checker.check_codex_bucket()
    assert problems == []
    assert warnings == []
    assert any("实库缺失" in n for n in notes)
    assert any("冻结桶" in n for n in notes)


def test_healthy_dataset_is_green_and_shows_both_buckets(bench):
    now, stamp = _now()
    _make_os_db(
        bench["db"],
        [{
            "id": "a1", "session_id": V7_SESSION, "cc_tool_use_id": V7_TOOL_USE,
            "model": PROVIDER_MODEL, "transcript_path": "/tmp/rollout.jsonl",
            "harness": "codex", "created_at": stamp,
        }],
        [("collaborationspawn_agent", stamp)],
    )
    _make_state_db(
        bench["state"],
        [(V7_TOOL_USE, _spawn_source(), "subagent", int(now.timestamp()))],
    )
    problems, warnings, notes = checker.check_codex_bucket()
    assert problems == []
    assert warnings == [], "字段齐、matcher 活着，不该有任何告警"

    rolling = [n for n in notes if "生产滚动桶" in n]
    frozen = [n for n in notes if "冻结桶" in n]
    assert len(rolling) == 1 and len(frozen) == 1, "两桶必须分列，各占一行"
    assert "1/1" in rolling[0], "分子=命中的双 v7 行，分母=thread_spawn 线程数"
    assert "0/2" in frozen[0]
    assert any("统计时点" in n for n in rolling), "生产桶必带统计时点"
    assert any("命中 1 行" in n for n in notes), "matcher 活性要报出命中数"


def test_broken_attribution_chain_warns(bench):
    """本机有 Codex 在用，agents 里却一条双 v7 都没有 = 归因链断。

    本期 warn 不 fail：另一台开发机可能装了 Codex 却从未经 OS 派过子 agent，那里
    state 目录在而双 v7 行恒为 0。fail 会让它在那台机器上必红，而红的成因不是缺陷。
    """
    now, stamp = _now()
    _make_os_db(
        bench["db"],
        [{"id": "cc1", "session_id": V4_SESSION, "cc_tool_use_id": "toolu_abc",
          "model": CC_MODEL, "transcript_path": "/tmp/t.jsonl",
          "harness": None, "created_at": stamp}],
        [],
    )
    _make_state_db(
        bench["state"], [(V7_TOOL_USE, _spawn_source(), "subagent", int(now.timestamp()))]
    )
    problems, warnings, _notes = checker.check_codex_bucket()
    assert problems == [], "本期 warn 级，不得拦提交"
    assert any("归因链断" in w and "P0-3a 升 fail" in w for w in warnings)


def test_no_codex_on_this_machine_stays_silent(bench):
    """同一份"没有双 v7 行"的库，本机没装 Codex 就连告警都不该有 —— 那是个假问题。"""
    _now_, stamp = _now()
    _make_os_db(
        bench["db"],
        [{"id": "cc1", "session_id": V4_SESSION, "cc_tool_use_id": "toolu_abc",
          "model": CC_MODEL, "transcript_path": "/tmp/t.jsonl",
          "harness": None, "created_at": stamp}],
        [],
    )
    problems, warnings, notes = checker.check_codex_bucket()
    assert problems == []
    assert not any("归因链断" in w for w in warnings)
    assert any("本机无" in n for n in notes)


def test_r6_warns_when_hook_side_fields_were_dropped(bench):
    """0903 实测基线：双 v7 行两列均空。本期 warn 不 fail —— 它是 P0-3a 的红→绿判据。"""
    now, stamp = _now()
    _make_os_db(
        bench["db"],
        [{"id": "a1", "session_id": V7_SESSION, "cc_tool_use_id": V7_TOOL_USE,
          "model": "", "transcript_path": None, "harness": None, "created_at": stamp}],
        [("collaborationspawn_agent", stamp)],
    )
    _make_state_db(
        bench["state"], [(V7_TOOL_USE, _spawn_source(), "subagent", int(now.timestamp()))]
    )
    problems, warnings, _notes = checker.check_codex_bucket()
    assert problems == [], "R6 本期是 warn 级，不得拦提交"
    assert any("I-CDX-R6" in w and "model 空 1 条" in w for w in warnings)
    assert any("transcript_path 空 1 条" in w for w in warnings)


def test_harness_null_rows_are_counted_not_excluded(bench):
    """NULL = 未标注，不等于 claude-code。硬过滤 harness='codex' 会把它们一起筛掉。"""
    now, stamp = _now()
    _make_os_db(
        bench["db"],
        [{"id": "a1", "session_id": V7_SESSION, "cc_tool_use_id": V7_TOOL_USE,
          "model": PROVIDER_MODEL, "transcript_path": "/tmp/r.jsonl",
          "harness": None, "created_at": stamp}],
        [("collaborationspawn_agent", stamp)],
    )
    _make_state_db(
        bench["state"], [(V7_TOOL_USE, _spawn_source(), "subagent", int(now.timestamp()))]
    )
    problems, _warnings, notes = checker.check_codex_bucket()
    assert problems == [], "未标注的行不该被当成链断"
    assert any("生产滚动桶 1/1" in n for n in notes)


def test_guardian_threads_stay_out_of_the_denominator(bench):
    now, stamp = _now()
    epoch = int(now.timestamp())
    _make_os_db(
        bench["db"],
        [{"id": "a1", "session_id": V7_SESSION, "cc_tool_use_id": V7_TOOL_USE,
          "model": PROVIDER_MODEL, "transcript_path": "/tmp/r.jsonl",
          "harness": "codex", "created_at": stamp}],
        [("collaborationspawn_agent", stamp)],
    )
    _make_state_db(
        bench["state"],
        [(V7_TOOL_USE, _spawn_source(), "subagent", epoch)]
        + [(f"g{n}", _guardian_source(), "subagent", epoch) for n in range(12)],
    )
    _problems, _warnings, notes = checker.check_codex_bucket()
    rolling = next(n for n in notes if "生产滚动桶" in n)
    assert "1/1" in rolling, "12 个 guardian 线程不得把分母抬成 13"
    assert "免检桶 12 个线程不进分母" in rolling


def test_the_two_buckets_are_never_summed(bench):
    """生产 1/1 + 冻结 0/2 的合并比值 1/3 不许出现在任何一行里。"""
    now, stamp = _now()
    _make_os_db(
        bench["db"],
        [{"id": "a1", "session_id": V7_SESSION, "cc_tool_use_id": V7_TOOL_USE,
          "model": PROVIDER_MODEL, "transcript_path": "/tmp/r.jsonl",
          "harness": "codex", "created_at": stamp}],
        [("collaborationspawn_agent", stamp)],
    )
    _make_state_db(
        bench["state"], [(V7_TOOL_USE, _spawn_source(), "subagent", int(now.timestamp()))]
    )
    problems, _warnings, notes = checker.check_codex_bucket()
    assert problems == []
    bucket_lines = [n for n in notes if "可达率" in n]
    assert len(bucket_lines) == 2
    assert "1/3" not in "\n".join(bucket_lines)


def test_r7_warns_when_the_matcher_looks_dead(bench):
    """窗口内有派工样本（新的双 v7 行）却零命中 = matcher 可能已失配。"""
    now, stamp = _now()
    _make_os_db(
        bench["db"],
        [{"id": "a1", "session_id": V7_SESSION, "cc_tool_use_id": V7_TOOL_USE,
          "model": PROVIDER_MODEL, "transcript_path": "/tmp/r.jsonl",
          "harness": "codex", "created_at": stamp}],
        [("Bash", stamp)],  # 有活动，但没有一条是派工
    )
    _make_state_db(
        bench["state"], [(V7_TOOL_USE, _spawn_source(), "subagent", int(now.timestamp()))]
    )
    problems, warnings, _notes = checker.check_codex_bucket()
    assert problems == [], "R7 本期是 warn 级"
    assert any("I-CDX-R7" in w and "codex_dispatch_matcher_dead" in w for w in warnings)


def test_r7_stays_quiet_without_a_dispatch_sample(bench):
    """窗口内没人派工与 matcher 全线失配在数据上同形 —— 没有前置就是天天误报。"""
    _now_, stamp = _now()
    old_ts = "2020-01-01 00:00:00.000000"
    _make_os_db(
        bench["db"],
        [{"id": "a1", "session_id": V7_SESSION, "cc_tool_use_id": V7_TOOL_USE,
          "model": PROVIDER_MODEL, "transcript_path": "/tmp/r.jsonl",
          "harness": "codex", "created_at": old_ts}],
        [("Bash", stamp)],
    )
    _make_state_db(bench["state"], [(V7_TOOL_USE, _spawn_source(), "subagent", 1577836800)])
    problems, warnings, notes = checker.check_codex_bucket()
    assert problems == []
    assert warnings == []
    assert any("无派工样本" in n for n in notes)


def test_arm_never_writes_to_either_source(bench):
    """只读铁律：跑完之后两个源的字节数与 mtime 都不许变。"""
    now, stamp = _now()
    _make_os_db(
        bench["db"],
        [{"id": "a1", "session_id": V7_SESSION, "cc_tool_use_id": V7_TOOL_USE,
          "model": PROVIDER_MODEL, "transcript_path": "/tmp/r.jsonl",
          "harness": "codex", "created_at": stamp}],
        [("collaborationspawn_agent", stamp)],
    )
    _make_state_db(
        bench["state"], [(V7_TOOL_USE, _spawn_source(), "subagent", int(now.timestamp()))]
    )
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in (bench["db"], bench["state"])}
    checker.check_codex_bucket()
    after = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in (bench["db"], bench["state"])}
    assert before == after
    assert not (bench["state"].with_name(bench["state"].name + "-shm")).exists(), (
        "只读打开一个 WAL 库可能建 -shm —— 那就是往用户的 Codex 目录里写东西"
    )
