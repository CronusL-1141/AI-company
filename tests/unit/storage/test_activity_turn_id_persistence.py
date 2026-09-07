"""turn_id 的跨持久化边界用例 —— 写进去的那个值，必须真的落在库里的那一列上。

在此之前 turn_id 只有两类断言：ORM 对象在内存里 `from_pydantic().to_pydantic()`
往返（test_usage_metric_invariants.py），以及迁移测试用裸 SQL 自插自查
（test_sqlite_migration.py）。两者都绿，中间那段却没人走过——**没有一条用例经
`repo.create_activity(..., turn_id=...)` 写入后再从库读回**。

这正是「断言要跨持久化边界」那个坑的形状：内存对象拼出来的值"有"不算数。
`create_activity` 里少传一个 `turn_id=turn_id`、`from_pydantic` 里漏掉一行映射、
或者建表路径没把这一列建出来，上面两类断言一条都不会红，而生产写进去的每一行
turn_id 都是 NULL。

所以这里的读回刻意**不走 SQLAlchemy**：用 sqlite3 直接打开那个文件查列。绕开 ORM
才能证明值落在磁盘上的列里，而不是落在映射层的想象里。库也刻意用临时文件而不是
`:memory:`——文件库才会真的跑一遍建表/迁移路径。
"""

from __future__ import annotations

import sqlite3

import pytest

from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository


@pytest.fixture
async def file_repo(tmp_path):
    """一个建在临时文件上的库：建表/迁移按生产路径真跑一遍。"""
    db_file = tmp_path / "aiteam.db"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db_file}")
    await repo.init_db()
    yield repo, db_file
    await close_db()


def _read_turn_id(db_file, activity_id: str):
    """绕开 ORM 直接读列 —— 映射层不参与作证。"""
    con = sqlite3.connect(db_file)
    try:
        row = con.execute(
            "select turn_id from agent_activities where id = ?", (activity_id,)
        ).fetchone()
    finally:
        con.close()
    assert row is not None, "写进去的那一行都没落库，后面比什么都没意义"
    return row[0]


async def test_turn_id_survives_the_round_trip_to_disk(file_repo):
    repo, db_file = file_repo
    activity = await repo.create_activity(
        agent_id="a1",
        session_id="s1",
        tool_name="Bash",
        turn_id="turn-0199",
    )

    assert _read_turn_id(db_file, activity.id) == "turn-0199"


async def test_turn_id_stays_null_when_the_caller_omits_it(file_repo):
    """CC 侧从不传这个参数 —— 缺省必须是 NULL，不能是空串或任何占位。

    空串与 NULL 在 SQL 里不是一回事：`where turn_id is null` 会把空串的行漏掉，
    而"挂不上任何主轮"的判据正是按 NULL 写的。
    """
    repo, db_file = file_repo
    activity = await repo.create_activity(
        agent_id="a1",
        session_id="s1",
        tool_name="Bash",
    )

    assert _read_turn_id(db_file, activity.id) is None


async def test_turn_id_is_readable_back_through_the_repository_api(file_repo):
    """再钉一次读侧：list_activities 返回的对象也得带着这个值。

    上面两条证明值落到了列上，这条证明取数路径没把它丢在 SELECT 之外——两处任一
    断了，页面上看到的都是同一个空。
    """
    repo, _db_file = file_repo
    await repo.create_activity(
        agent_id="a2", session_id="s2", tool_name="Bash", turn_id="turn-0200"
    )

    activities = await repo.list_activities(agent_id="a2")
    assert [a.turn_id for a in activities] == ["turn-0200"]
