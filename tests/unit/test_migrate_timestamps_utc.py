"""存量平移脚本的行为契约 —— 见 docs/utc-unification-design.md §6。

这个脚本会一次性改写 32 万个单元且由缔造者亲手执行，所以它的三条承诺必须机检：
**无损**（亚秒位不丢）、**可逆**（回滚逐字节还原）、**拦得住**（顺序颠倒时中止）。
"""

from __future__ import annotations

import importlib.util
import sqlite3
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
