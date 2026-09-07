"""Tests for _sqlite_migrate() idempotency and correctness."""

import sqlite3
from pathlib import Path

from aiteam.storage.connection import COLUMNS_TO_ENSURE, _sqlite_migrate


def _create_legacy_db(path: str) -> None:
    """Create a SQLite DB with old schema (missing new columns)."""
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE meetings (
            id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            status TEXT NOT NULL,
            participants JSON NOT NULL,
            created_at DATETIME NOT NULL
        )"""
    )
    con.execute(
        """CREATE TABLE meeting_messages (
            id TEXT PRIMARY KEY,
            meeting_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            content TEXT NOT NULL,
            round_number INTEGER NOT NULL,
            timestamp DATETIME NOT NULL
        )"""
    )
    con.commit()
    con.close()


def _column_names(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


class TestSqliteMigration:
    def test_adds_missing_columns(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.db")
        _create_legacy_db(db)
        _sqlite_migrate(db)
        con = sqlite3.connect(db)
        assert "meta_json" in _column_names(con, "meetings")
        assert "metadata_json" in _column_names(con, "meeting_messages")
        con.close()

    def test_idempotent_on_repeated_calls(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.db")
        _create_legacy_db(db)
        # Running twice must not raise
        _sqlite_migrate(db)
        _sqlite_migrate(db)
        con = sqlite3.connect(db)
        assert "meta_json" in _column_names(con, "meetings")
        assert "metadata_json" in _column_names(con, "meeting_messages")
        con.close()

    def test_no_error_when_columns_already_exist(self, tmp_path: Path) -> None:
        """Migration must succeed on a fully up-to-date schema (e.g. fresh install)."""
        db = str(tmp_path / "test.db")
        con = sqlite3.connect(db)
        con.execute(
            """CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                meta_json JSON DEFAULT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE meeting_messages (
                id TEXT PRIMARY KEY,
                metadata_json JSON DEFAULT NULL
            )"""
        )
        con.commit()
        con.close()
        _sqlite_migrate(db)  # must not raise

    def test_columns_to_ensure_covers_known_targets(self) -> None:
        pairs = {(t, c) for t, c, _ in COLUMNS_TO_ENSURE}
        assert ("meetings", "meta_json") in pairs
        assert ("meeting_messages", "metadata_json") in pairs

    def test_new_columns_default_null(self, tmp_path: Path) -> None:
        db = str(tmp_path / "test.db")
        _create_legacy_db(db)
        con_pre = sqlite3.connect(db)
        con_pre.execute(
            "INSERT INTO meetings VALUES (?,?,?,?,?,?)",
            ("m1", "t1", "topic", "active", "[]", "2026-01-01"),
        )
        con_pre.execute(
            "INSERT INTO meeting_messages VALUES (?,?,?,?,?,?,?)",
            ("mm1", "m1", "a1", "agent", "hi", 1, "2026-01-01"),
        )
        con_pre.commit()
        con_pre.close()

        _sqlite_migrate(db)

        con = sqlite3.connect(db)
        row_m = con.execute("SELECT meta_json FROM meetings WHERE id='m1'").fetchone()
        row_mm = con.execute(
            "SELECT metadata_json FROM meeting_messages WHERE id='mm1'"
        ).fetchone()
        assert row_m[0] is None
        assert row_mm[0] is None
        con.close()


class TestHarnessColumnsMigration:
    """Codex 兼容 P0-1：agents 四列 + agent_activities 一列的迁移往返（r5 §4.4）。

    断言全部落在**真 sqlite 文件**上，不用内存对象拼出来的模型。理由是本仓的实锤
    教训：内存对象拼出的响应"有值"不算数，跨请求查库才抓得到漏字段——迁移这条路
    上同型的漏法是"ORM 声明了列而 COLUMNS_TO_ENSURE 没跟上"，既有库于是永远没有
    这一列，而所有走内存库的测试全绿。
    """

    HARNESS_COLUMNS = (
        "harness",
        "harness_version",
        "dispatch_call_id",
        "reasoning_output_tokens",
    )

    @staticmethod
    def _legacy_agents_db(path: str) -> None:
        """一个没有任何新列的老 agents / agent_activities 表。"""
        con = sqlite3.connect(path)
        con.execute(
            """CREATE TABLE agents (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at DATETIME NOT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE agent_activities (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                timestamp DATETIME NOT NULL
            )"""
        )
        con.execute(
            "INSERT INTO agents VALUES (?,?,?,?,?)",
            ("a1", "t1", "worker", "worker", "2026-01-01"),
        )
        con.execute(
            "INSERT INTO agent_activities VALUES (?,?,?,?,?)",
            ("act1", "a1", "s1", "Bash", "2026-01-01"),
        )
        con.commit()
        con.close()

    def test_declared_in_columns_to_ensure(self) -> None:
        """铁律：ORM 加列必须同步 COLUMNS_TO_ENSURE，否则既有库永远没有这一列。"""
        pairs = {(t, c) for t, c, _ in COLUMNS_TO_ENSURE}
        for column in self.HARNESS_COLUMNS:
            assert ("agents", column) in pairs
        assert ("agent_activities", "turn_id") in pairs

    def test_legacy_db_gets_all_five_columns_as_null(self, tmp_path: Path) -> None:
        """老库跑一次迁移：五列全部补出来，且既有行上全是 NULL。

        "全 NULL" 是判据的一半 —— 观测字段默认留空，harness 为 NULL 表示"这一行没人
        标注过"，把它回填成 claude-code 等于给历史行编造一个当时并不存在的判断。
        """
        db = str(tmp_path / "legacy.db")
        self._legacy_agents_db(db)

        _sqlite_migrate(db)

        con = sqlite3.connect(db)
        agent_cols = _column_names(con, "agents")
        for column in self.HARNESS_COLUMNS:
            assert column in agent_cols, f"agents.{column} 未被迁移补出来"
        assert "turn_id" in _column_names(con, "agent_activities")

        row = con.execute(
            "SELECT harness, harness_version, dispatch_call_id, reasoning_output_tokens "
            "FROM agents WHERE id='a1'"
        ).fetchone()
        assert row == (None, None, None, None), f"既有行的新列不是 NULL: {row}"
        turn = con.execute(
            "SELECT turn_id FROM agent_activities WHERE id='act1'"
        ).fetchone()
        assert turn[0] is None
        con.close()

    def test_second_migration_is_idempotent(self, tmp_path: Path) -> None:
        """再迁一次不得报错，也不得改动已有取值。"""
        db = str(tmp_path / "legacy.db")
        self._legacy_agents_db(db)
        _sqlite_migrate(db)

        # 迁移之后写入真实取值，再迁一次 —— 幂等不只是"不崩"，还必须不覆写。
        con = sqlite3.connect(db)
        con.execute(
            "UPDATE agents SET harness='codex', harness_version='0.153.0-alpha.5', "
            "dispatch_call_id='call_x', reasoning_output_tokens=42 WHERE id='a1'"
        )
        con.execute("UPDATE agent_activities SET turn_id='turn-1' WHERE id='act1'")
        con.commit()
        con.close()

        _sqlite_migrate(db)

        con = sqlite3.connect(db)
        row = con.execute(
            "SELECT harness, harness_version, dispatch_call_id, reasoning_output_tokens "
            "FROM agents WHERE id='a1'"
        ).fetchone()
        assert row == ("codex", "0.153.0-alpha.5", "call_x", 42)
        assert con.execute(
            "SELECT turn_id FROM agent_activities WHERE id='act1'"
        ).fetchone()[0] == "turn-1"
        # 列数不得因为重复迁移而增长（ADD COLUMN 跑两次会直接报错，这里兜住静默重复）
        assert len([c for c in _column_names(con, "agents") if c == "harness"]) == 1
        con.close()

    def test_no_unique_index_on_dispatch_call_id(self, tmp_path: Path) -> None:
        """``dispatch_call_id`` 刻意无唯一约束 —— 别"补齐对称性"把它加回来。

        派工边的三级来源链可能全失落（落 ``DISPATCH_EDGE_UNRESOLVED``），也可能因
        模型自发派工而重名。加了 UNIQUE，这两种情况一批量出现就会把入库整个打死；
        r5 §4.3 为此明令不加，并点名不得新增 ``uq_agents_agent_key``。
        """
        db = str(tmp_path / "legacy.db")
        self._legacy_agents_db(db)
        _sqlite_migrate(db)

        con = sqlite3.connect(db)
        index_sql = [
            (name, sql or "")
            for name, sql in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index'"
            )
        ]
        con.close()

        assert not any(name == "uq_agents_agent_key" for name, _ in index_sql), (
            "uq_agents_agent_key 不该存在 —— 见 r5 §4.3"
        )
        offenders = [name for name, sql in index_sql if "dispatch_call_id" in sql]
        assert not offenders, f"dispatch_call_id 被加了索引/约束: {offenders}"
