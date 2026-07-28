"""存量平移脚本的行为契约 —— 见 docs/utc-unification-design.md §6。

这个脚本会一次性改写 32 万个单元且由缔造者亲手执行，所以它的承诺必须机检：
**无损**（亚秒位不丢）、**可逆**（回滚逐字节还原）、**拦得住**（顺序颠倒时中止）、
**认得出混口径**（新代码已写进去的 UTC 行逐行排除，两次平移都不许碰它们）、
**只做一次**（幂等标记 + journal 防覆盖）。

全部用例只跑 tmp_path 里的临时库，一个字节都不碰生产库。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_timestamps_utc.py"

_spec = importlib.util.spec_from_file_location("migrate_timestamps_utc", SCRIPT)
assert _spec and _spec.loader
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


LOCAL_SHIFT_HOURS = mig.shift_for(datetime(2026, 7, 28, 17, 0)).total_seconds() / 3600.0
IS_UTC_HOST = LOCAL_SHIFT_HOURS == 0


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    """一个只含 events 表的最小库，行的时间戳按本地墙钟写。"""
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("create table events (id integer primary key, timestamp text)")
    conn.commit()
    return conn


def _seed(conn: sqlite3.Connection, values: list[str]) -> None:
    conn.executemany("insert into events (timestamp) values (?)", [(v,) for v in values])
    conn.commit()


def _reports(conn: sqlite3.Connection, now_local: datetime):
    original = mig.LOCAL_COLUMNS
    mig.LOCAL_COLUMNS = {"events": ("timestamp",)}
    try:
        return mig.collect(conn, now_local)
    finally:
        mig.LOCAL_COLUMNS = original


class TestLosslessShift:
    def test_subsecond_digits_survive(self, db: sqlite3.Connection) -> None:
        """整秒截断会抹掉 32 万行的微秒位，而事件账本按 timestamp 排同秒内的先后。"""
        _seed(db, ["2026-07-28 17:01:25.068573"])
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', -8)}")
        db.commit()
        got = db.execute("select timestamp from events").fetchone()[0]
        assert got == "2026-07-28 09:01:25.068573"

    def test_values_without_subseconds_are_fine_too(self, db: sqlite3.Connection) -> None:
        _seed(db, ["2026-07-28 17:01:25"])
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', -8)}")
        db.commit()
        assert db.execute("select timestamp from events").fetchone()[0] == "2026-07-28 09:01:25"

    def test_shift_crosses_the_date_boundary(self, db: sqlite3.Connection) -> None:
        _seed(db, ["2026-07-28 03:30:00.500000"])
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', -8)}")
        db.commit()
        assert db.execute("select timestamp from events").fetchone()[0] == "2026-07-27 19:30:00.500000"

    def test_half_hour_zones_are_supported(self, db: sqlite3.Connection) -> None:
        """+05:30 这类时区不是理论边角——印度是它。"""
        _seed(db, ["2026-07-28 17:01:25.068573"])
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', -5.5)}")
        db.commit()
        assert db.execute("select timestamp from events").fetchone()[0] == "2026-07-28 11:31:25.068573"

    def test_round_trip_is_byte_exact(self, db: sqlite3.Connection) -> None:
        values = [
            "2026-07-28 17:01:25.068573",
            "2026-03-13 11:59:54.000001",
            "2026-06-23 20:50:09",
        ]
        _seed(db, values)
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', -8)}")
        db.execute(f"update events set timestamp = {mig.shift_expr('timestamp', 8)}")
        db.commit()
        assert [r[0] for r in db.execute("select timestamp from events order by id")] == values


class TestWidthGuard:
    def test_date_only_values_abort_instead_of_being_nulled(
        self, db: sqlite3.Connection
    ) -> None:
        """前缀切分遇到只有日期的值会拼出非法串，SQLite 静默给 NULL —— 那是数据丢失。"""
        _seed(db, ["2026-07-28"])
        reports = _reports(db, datetime(2026, 7, 28, 17, 0))
        with pytest.raises(SystemExit, match="无法安全切分"):
            mig.assert_uniform_width(db, reports)


@pytest.mark.skipif(IS_UTC_HOST, reason="宿主时区即 UTC，无本地/UTC 之分可判")
class TestOrderGuard:
    """护栏拦的是"先部署新代码、后跑平移"的顺序颠倒。"""

    @staticmethod
    def _now() -> datetime:
        return datetime.now()

    def test_local_written_rows_are_positively_proven(self, db: sqlite3.Connection) -> None:
        """UTC 时钟写不出未来的值 —— 所以 max 晚于 UTC 此刻就是正面确证，不是启发式。"""
        now_local = self._now()
        _seed(db, [now_local.strftime("%Y-%m-%d %H:%M:%S.%f")])
        reports = _reports(db, now_local)
        assert reports[0].guard.startswith("确证本地")
        passed, notes = mig.verdict(reports, now_local)
        assert passed, notes

    def test_utc_written_rows_block_the_migration(self, db: sqlite3.Connection) -> None:
        """库已经是 UTC 了还去平移，等于把每一行再推早一个时区。"""
        now_local = self._now()
        now_utc_naive = now_local.astimezone(UTC).replace(tzinfo=None)
        _seed(db, [now_utc_naive.strftime("%Y-%m-%d %H:%M:%S.%f")])
        reports = _reports(db, now_local)
        passed, notes = mig.verdict(reports, now_local)
        assert not passed, notes
        assert any("新代码很可能已经在写库" in n for n in notes)

    def test_all_cold_data_is_refused_rather_than_guessed(
        self, db: sqlite3.Connection
    ) -> None:
        """全是冷数据时无从判定 —— 宁可停下要人确认，也不替人猜。"""
        now_local = self._now()
        _seed(db, [(now_local - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S.%f")])
        reports = _reports(db, now_local)
        passed, notes = mig.verdict(reports, now_local)
        assert not passed
        assert any("请人工确认" in n for n in notes)


class TestManifest:
    def test_ecosystem_tables_are_never_in_the_shift_list(self) -> None:
        """ecosystem 域本就写 UTC；平移它等于把这些行推早一个时区。"""
        offenders = [t for t in mig.LOCAL_COLUMNS if t.startswith("ecosystem")]
        assert not offenders, offenders
        assert "pipeline_stage_history" not in mig.LOCAL_COLUMNS

    def test_manifest_matches_the_design_document(self) -> None:
        """清单与设计文档 §3.1 是同一份账；数量对不上说明有一侧被单方面改过。"""
        columns = sum(len(v) for v in mig.LOCAL_COLUMNS.values())
        assert len(mig.LOCAL_COLUMNS) == 20, f"表数变了：{len(mig.LOCAL_COLUMNS)}"
        assert columns == 46, f"列数变了：{columns}"

    def test_every_table_declares_a_birth_column(self) -> None:
        """出生列是逐行口径判定的地基；漏一张表就等于那张表的行没人判。"""
        assert set(mig.ROW_BIRTH_COLUMN) == set(mig.LOCAL_COLUMNS)
        for table, birth in mig.ROW_BIRTH_COLUMN.items():
            assert birth in mig.LOCAL_COLUMNS[table], f"{table}.{birth} 不在平移清单里"

    def test_birth_column_is_classified_before_the_columns_that_anchor_on_it(self) -> None:
        """原地更新列拿出生列当锚，所以出生列必须排在同表首位，先被判定。"""
        for table, columns in mig.LOCAL_COLUMNS.items():
            assert columns[0] == mig.ROW_BIRTH_COLUMN[table], f"{table} 的出生列不在首位"

    def test_after_birth_whitelist_excludes_columns_that_predate_creation(self) -> None:
        """白名单一旦混进"可能早于创建"的列，预言机就是在对着假不变量下判断。

        实测反例：workflow_* 的 started_at/queued_at/completed_at 来自 CC 状态文件的
        epoch，最早到 2026-06-23，比该行 created_at(2026-07-06) 早好几周；
        scheduled_tasks.next_run_at 干脆记的是未来。
        """
        forbidden = {
            ("workflow_runs", "started_at"),
            ("workflow_runs", "completed_at"),
            ("workflow_agents", "started_at"),
            ("workflow_agents", "queued_at"),
            ("scheduled_tasks", "next_run_at"),
        }
        assert not (mig.AFTER_BIRTH_COLUMNS & forbidden)
        for table, column in mig.AFTER_BIRTH_COLUMNS:
            assert column in mig.LOCAL_COLUMNS[table]
            assert column != mig.ROW_BIRTH_COLUMN[table]


# ---------------------------------------------------------------------------
# 混口径 —— 2026-07-28 的生产现状：UTC 新代码已合入 master 且是 editable 安装，
# 于是每一个新起的进程都往还没平移的库里写 UTC 行。以下用例全部是对着这个现实
# 做的故障注入。
# ---------------------------------------------------------------------------
OFFSET = timedelta(hours=-LOCAL_SHIFT_HOURS)  # 本地 − UTC；UTC+8 上为 +8h


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S.%f")


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["migrate_timestamps_utc.py", *argv])
    return mig.main()


def _dump(conn: sqlite3.Connection, table: str, columns: str) -> list[tuple]:
    return list(conn.execute(f"select rowid, {columns} from {table} order by rowid"))


def _read_journal(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture()
def mixed(tmp_path: Path) -> tuple[Path, sqlite3.Connection, datetime]:
    """旧代码还在写、新代码刚混进来几行的库 —— 即 --apply 真正要面对的那个库。

    events（追加型）：一串本地行 → 3 行新代码写的 UTC 行 → 又几行本地行。
    teams（含原地更新列）：一行更新值可判为 UTC、一行两种读法都自洽（该进人审）、
    一行全干净。
    """
    now = datetime.now().replace(microsecond=246810)
    path = tmp_path / "mixed.db"
    conn = sqlite3.connect(path)
    conn.execute("create table events (id integer primary key, timestamp text)")
    conn.execute(
        "create table teams (id integer primary key, created_at text, "
        "updated_at text, completed_at text)"
    )
    early = [now - timedelta(minutes=20) + timedelta(seconds=30 * i) for i in range(35)]
    utc_rows = [now - timedelta(minutes=2), now - timedelta(seconds=110), now - timedelta(seconds=100)]
    late = [now - timedelta(seconds=60), now - timedelta(seconds=30), now]
    conn.executemany(
        "insert into events (timestamp) values (?)",
        [(_stamp(m),) for m in early]
        + [(_stamp(m - OFFSET),) for m in utc_rows]  # 新代码写的：同一瞬刻的 UTC 表达
        + [(_stamp(m),) for m in late],
    )
    conn.executemany(
        "insert into teams (created_at, updated_at, completed_at) values (?, ?, ?)",
        [
            # ① 更新值比创建早了整整一个时区 —— 只可能是 UTC 口径
            (_stamp(now - timedelta(minutes=5)), _stamp(now - timedelta(seconds=90) - OFFSET), None),
            # ② 三天前建的行，更新值落在风险带内：两种读法都不违反"更新不早于创建"
            (_stamp(now - timedelta(days=3)), _stamp(now - timedelta(seconds=100) - OFFSET), None),
            # ③ 全干净
            (
                _stamp(now - timedelta(minutes=4)),
                _stamp(now - timedelta(minutes=2)),
                _stamp(now - timedelta(minutes=1)),
            ),
        ],
    )
    conn.commit()
    return path, conn, now


def _contamination(conn: sqlite3.Connection, now_local: datetime) -> dict:
    reports = mig.collect(conn, now_local)
    found, _, _ = mig.detect_contamination(conn, reports, LOCAL_SHIFT_HOURS, now_local)
    return found


@pytest.mark.skipif(IS_UTC_HOST, reason="宿主时区即 UTC，无本地/UTC 之分可判")
class TestMixedCaliberDetection:
    """混口径是**逐行**问题，不是"放行/拦停"的二元问题。"""

    def test_minority_utc_rows_are_pinned_down_row_by_row(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        """少数派 UTC 行既不影响列 max 也不影响全库 max —— 只有 rowid 写入序抓得到。"""
        _, conn, now = mixed
        found = _contamination(conn, now)
        assert found["events.timestamp"].excluded == [36, 37, 38]
        assert found["events.timestamp"].undecidable == []

    def test_clean_rows_are_never_excluded(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        """排除是有代价的：漏排会推早 8 小时，错排会留下一行永远晚 8 小时。"""
        _, conn, now = mixed
        found = _contamination(conn, now)
        assert found["teams.created_at"].excluded == []
        assert found["teams.completed_at"].excluded == []

    def test_in_place_columns_are_judged_against_their_birth_column(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        """rowid 对原地更新列没意义，靠的是"更新不可能早于创建"这条不变量。"""
        _, conn, now = mixed
        found = _contamination(conn, now)
        assert found["teams.updated_at"].excluded == [1]

    def test_undecidable_rows_are_listed_rather_than_silently_shifted(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        """判不了就点名交人审 —— 静默按多数派处理正是这类事故的成因。"""
        _, conn, now = mixed
        found = _contamination(conn, now)
        pending = mig.all_undecidable(found)
        assert [(p["column"], p["rowid"]) for p in pending] == [("teams.updated_at", 2)]

    def test_legitimate_out_of_order_backfill_is_not_mistaken_for_a_timezone(
        self, db: sqlite3.Connection
    ) -> None:
        """生产库 task_memos.created_at 有 7.1~8.7 小时的合法乱序（记忆 v2 批量回填）。

        容差给到小时级就会把它们当成时区指纹整批排除掉 —— 所以容差按分钟给。
        """
        now = datetime.now()
        _seed(
            db,
            [
                _stamp(now - timedelta(minutes=5)),
                _stamp(now - timedelta(hours=8, minutes=31)),  # 回填行，倒退 8.5h
                _stamp(now - timedelta(minutes=1)),
            ],
        )
        reports = _reports(db, now)
        found, since, evidence = mig.detect_contamination(db, reports, LOCAL_SHIFT_HOURS, now)
        assert evidence == []
        assert since is None
        assert found["events.timestamp"].excluded == []


@pytest.mark.skipif(IS_UTC_HOST, reason="宿主时区即 UTC，无本地/UTC 之分可判")
class TestGuardSeesMinorities:
    def test_already_shifted_db_with_one_stray_local_row_is_not_waved_through(
        self, db: sqlite3.Connection
    ) -> None:
        """旧护栏在这里放绿：一行旧代码补写的本地值就把全库 max 顶到本地此刻。

        真正的判据必须是**多数**近期写入的口径，少数派掀不翻中位数。
        """
        now_local = datetime.now()
        now_utc_naive = now_local.astimezone(UTC).replace(tzinfo=None)
        _seed(
            db,
            [_stamp(now_utc_naive - timedelta(seconds=5 * i)) for i in range(40, 0, -1)]
            + [_stamp(now_local)],  # 没死透的旧代码进程刚补的一行
        )
        reports = _reports(db, now_local)
        assert any(r.guard.startswith("确证本地") for r in reports)  # 旧判据仍然"确证"
        passed, notes = mig.verdict(reports, now_local)
        assert not passed, notes
        assert any("新代码很可能已经在写库" in n for n in notes)

    def test_guard_reports_how_many_rows_will_be_excluded(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        """混口径不再是放行/拦停的二元 —— 护栏的职责是确证污染行已被识别并排除。"""
        _, conn, now = mixed
        found = _contamination(conn, now)
        _, notes = mig.verdict(mig.collect(conn, now), now, found, ack_undecidable=True)
        assert any("events.timestamp: 3 行" in n for n in notes)

    def test_undecidable_rows_block_until_acknowledged(
        self, mixed: tuple[Path, sqlite3.Connection, datetime]
    ) -> None:
        _, conn, now = mixed
        found = _contamination(conn, now)
        reports = mig.collect(conn, now)
        passed, notes = mig.verdict(reports, now, found, ack_undecidable=False)
        assert not passed
        assert any("--ack-undecidable" in n for n in notes)
        passed, _ = mig.verdict(reports, now, found, ack_undecidable=True)
        assert passed


@pytest.mark.skipif(IS_UTC_HOST, reason="宿主时区即 UTC，无本地/UTC 之分可判")
class TestApplyIsIdempotentAndReversible:
    def test_apply_excludes_utc_rows_and_rollback_leaves_them_alone_too(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """排除行两次都不能动：平移时 −8h 会推早，回滚时 +8h 会推晚。"""
        path, conn, _ = mixed
        before_events = _dump(conn, "events", "timestamp")
        before_teams = _dump(conn, "teams", "created_at, updated_at, completed_at")
        journal = path.parent / "j.json"

        assert _run(monkeypatch, "--db", str(path), "--apply", "--journal", str(journal),
                    "--ack-undecidable") == 0
        after = dict(_dump(conn, "events", "timestamp"))
        for rid, value in before_events:
            expected = value if rid in (36, 37, 38) else _stamp(
                datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f") + timedelta(hours=LOCAL_SHIFT_HOURS)
            )
            assert after[rid] == expected, f"rowid {rid}"
        assert mig.read_marker(conn) == mig.MIGRATION_MARKER

        recorded = _read_journal(journal)["detection"]["columns"]["events.timestamp"]
        assert recorded["excluded_rowids"] == [36, 37, 38]

        assert _run(monkeypatch, "--db", str(path), "--rollback", "--journal", str(journal)) == 0
        assert _dump(conn, "events", "timestamp") == before_events
        assert _dump(conn, "teams", "created_at, updated_at, completed_at") == before_teams
        assert mig.read_marker(conn) == 0

    def test_second_apply_is_refused_by_the_marker(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """二次 --apply 会再减 8 小时，且旧护栏对"多数已 UTC + 少数本地"仍可能放绿。"""
        path, conn, _ = mixed
        assert _run(monkeypatch, "--db", str(path), "--apply", "--journal",
                    str(path.parent / "j1.json"), "--ack-undecidable") == 0
        snapshot = _dump(conn, "events", "timestamp")
        assert _run(monkeypatch, "--db", str(path), "--apply", "--journal",
                    str(path.parent / "j2.json"), "--ack-undecidable", "--force") == 3
        assert _dump(conn, "events", "timestamp") == snapshot

    def test_rollback_without_the_marker_is_refused(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没平移过的库回滚一次就是整体推晚 8 小时。"""
        path, conn, _ = mixed
        journal = path.parent / "fake.json"
        journal.write_text('{"db": "x", "shift_hours": -8, "columns": []}')
        snapshot = _dump(conn, "events", "timestamp")
        assert _run(monkeypatch, "--db", str(path), "--rollback", "--journal", str(journal)) == 3
        assert _dump(conn, "events", "timestamp") == snapshot

    def test_existing_journal_is_never_overwritten(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """journal 是唯一的恢复凭证（含逐行排除清单），覆盖它等于销毁它。"""
        path, conn, _ = mixed
        journal = path.parent / "j.json"
        journal.write_text('{"这是上一次平移的凭证": true}')
        snapshot = _dump(conn, "events", "timestamp")
        assert _run(monkeypatch, "--db", str(path), "--apply", "--journal", str(journal),
                    "--ack-undecidable", "--force") == 2
        assert journal.read_text() == '{"这是上一次平移的凭证": true}'
        assert _dump(conn, "events", "timestamp") == snapshot

    def test_apply_demands_a_journal(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path, conn, _ = mixed
        snapshot = _dump(conn, "events", "timestamp")
        assert _run(monkeypatch, "--db", str(path), "--apply", "--ack-undecidable") == 2
        assert _dump(conn, "events", "timestamp") == snapshot

    def test_undecidable_rows_stop_apply_even_with_force(
        self, mixed: tuple[Path, sqlite3.Connection, datetime], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--force 是给"冷数据无法自证"用的，不是给"有行判不了口径"用的。"""
        path, conn, _ = mixed
        snapshot = _dump(conn, "events", "timestamp")
        assert _run(monkeypatch, "--db", str(path), "--apply", "--journal",
                    str(path.parent / "j.json"), "--force") == 1
        assert _dump(conn, "events", "timestamp") == snapshot
        assert mig.read_marker(conn) == 0
