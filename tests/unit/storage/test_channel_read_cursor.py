"""信道未读水位的存储层用例 —— 判定规则和落盘都要真跑一遍。

这个功能的失败形态有个共同特征：**全部机检照绿、所有单测照过、用户那边什么都不
发生**。所以这里的每条用例都对着一个具体的静默失败：

- `@` 前缀不对称：改这个功能之前，list_channel_mentions 拼 f"@{name}" 去匹配，而真实
  调用方写的是裸名，于是它对专线上每一条消息都返回 0。徽章永远是 0 和"没有新消息"
  在界面上长得一模一样。
- 子串误命中：contains 会让 "leader-cc" 吃掉发给 "leader-cc-2" 的信。
- 读路径补写水位：把"第一次查看"变成"已读全部"，正好埋掉这个功能要暴露的存量消息。
- 按 now 推进水位：分页只拿了前 N 条时，未返回的那些被静默跳过，再也不会提示。
- 水位回退：已读消息重新变未读，比不推进更糟。

读回一律绕开 SQLAlchemy 用 sqlite3 直接查列（范本见 test_activity_turn_id_persistence），
库用临时文件而非 :memory:，这样建表/迁移按生产路径真跑一遍。
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from aiteam.clock import utc_now
from aiteam.storage.connection import close_db
from aiteam.storage.repository import StorageRepository

PROJ = "proj-alpha"
OTHER_PROJ = "proj-beta"
CHAN = "team:aiteam-os-bridge"


@pytest.fixture
async def file_repo(tmp_path):
    """建在临时文件上的库：建表路径真跑一遍。"""
    db_file = tmp_path / "aiteam.db"
    repo = StorageRepository(db_url=f"sqlite+aiosqlite:///{db_file}")
    await repo.init_db()
    yield repo, db_file
    await close_db()


def _cursor_rows(db_file) -> list[tuple]:
    """绕开 ORM 直接读水位表 —— 映射层不参与作证。"""
    con = sqlite3.connect(db_file)
    try:
        return con.execute(
            "select reader, channel, project_id, last_read_at "
            "from channel_read_cursors order by reader, channel"
        ).fetchall()
    finally:
        con.close()


async def _send(repo, *, sender: str, mentions: list[str], project_id=PROJ, channel=CHAN, content="正文"):
    return await repo.create_channel_message(
        channel=channel,
        sender=sender,
        content=content,
        mentions=mentions,
        project_id=project_id,
    )


# ── 判定规则 ───────────────────────────────────────────────────


async def test_bare_and_at_prefixed_mentions_both_count(file_repo):
    """裸名与 @名 两种书写都要算命中 —— 只认一种就会漏掉半个世界的调用方。"""
    repo, _ = file_repo
    await _send(repo, sender="leader-codex", mentions=["leader-cc"])
    await _send(repo, sender="leader-codex", mentions=["@leader-cc"])

    rows, truncated = await repo.count_channel_unread("leader-cc", PROJ)
    assert truncated is False
    assert len(rows) == 1
    assert rows[0]["count"] == 2, "裸名和 @名 应各算一条"


async def test_mention_match_is_exact_not_substring(file_repo):
    """leader-cc 不得吃掉发给 leader-cc-2 的信。"""
    repo, _ = file_repo
    await _send(repo, sender="leader-codex", mentions=["leader-cc-2"])

    rows, _ = await repo.count_channel_unread("leader-cc", PROJ)
    assert rows == [], "子串误命中：这条是发给 leader-cc-2 的"


async def test_other_readers_mail_is_not_mine(file_repo):
    repo, _ = file_repo
    await _send(repo, sender="leader-cc", mentions=["leader-codex"])

    rows, _ = await repo.count_channel_unread("leader-cc", PROJ)
    assert rows == []


async def test_unread_is_isolated_by_project(file_repo):
    """指定项目的收件人才看得到 —— 不能读错项目的信。"""
    repo, _ = file_repo
    await _send(repo, sender="leader-codex", mentions=["leader-cc"], project_id=OTHER_PROJ)

    rows, _ = await repo.count_channel_unread("leader-cc", PROJ)
    assert rows == [], "别的项目的信不该出现在本项目的未读里"

    rows_other, _ = await repo.count_channel_unread("leader-cc", OTHER_PROJ)
    assert len(rows_other) == 1


async def test_legacy_rows_without_project_are_invisible(file_repo):
    """project_id 为空的历史行不进任何项目的未读（本列 2026-09-08 才加）。"""
    repo, _ = file_repo
    await _send(repo, sender="leader-codex", mentions=["leader-cc"], project_id=None)

    rows, _ = await repo.count_channel_unread("leader-cc", PROJ)
    assert rows == []


async def test_unread_enumerates_channels_and_carries_excerpt(file_repo):
    """未读接口本身就是频道枚举，且带够勾人去读的摘要。"""
    repo, _ = file_repo
    await _send(repo, sender="leader-codex", mentions=["leader-cc"], channel="team:one")
    await _send(
        repo,
        sender="leader-codex",
        mentions=["leader-cc"],
        channel="team:two",
        content="第一行摘要\n第二行不该出现",
    )

    rows, _ = await repo.count_channel_unread("leader-cc", PROJ)
    assert {r["channel"] for r in rows} == {"team:one", "team:two"}
    two = next(r for r in rows if r["channel"] == "team:two")
    assert two["latest_excerpt"] == "第一行摘要"
    assert two["latest_sender"] == "leader-codex"


# ── 水位语义 ───────────────────────────────────────────────────


async def test_missing_cursor_means_epoch_and_read_path_never_writes(file_repo):
    """缺水位=全部未读，且**查询绝不补写行**。

    读路径补写会把"第一次查看"变成"已读全部"，正好埋掉这个功能要暴露的存量消息。
    """
    repo, db_file = file_repo
    await _send(repo, sender="leader-codex", mentions=["leader-cc"])

    rows, _ = await repo.count_channel_unread("leader-cc", PROJ)
    assert rows[0]["count"] == 1
    assert _cursor_rows(db_file) == [], "查询未读不得在库里留下水位行"

    # 再查一次仍是 1：取摘要不消未读
    rows_again, _ = await repo.count_channel_unread("leader-cc", PROJ)
    assert rows_again[0]["count"] == 1


async def test_cursor_lands_on_disk(file_repo):
    """写进去的水位必须真的落在库里那一列上，读回不走 ORM。"""
    repo, db_file = file_repo
    msg = await _send(repo, sender="leader-codex", mentions=["leader-cc"])

    cursor, advanced = await repo.set_channel_cursor("leader-cc", CHAN, PROJ, msg.created_at)
    assert advanced is True
    assert cursor.reader == "leader-cc"

    disk = _cursor_rows(db_file)
    assert len(disk) == 1
    assert disk[0][0] == "leader-cc"
    assert disk[0][1] == CHAN
    assert disk[0][2] == PROJ


async def test_advance_clears_unread(file_repo):
    """闭环：有未读 → 推进到实际读到的那一条 → 归零。"""
    repo, _ = file_repo
    msg = await _send(repo, sender="leader-codex", mentions=["leader-cc"])

    before, _ = await repo.count_channel_unread("leader-cc", PROJ)
    assert before[0]["count"] == 1

    await repo.set_channel_cursor("leader-cc", CHAN, PROJ, msg.created_at)

    after, _ = await repo.count_channel_unread("leader-cc", PROJ)
    assert after == [], "推进到最后一条之后应当归零"


async def test_advance_only_covers_what_was_actually_read(file_repo):
    """分页语义：只推进到实际读到的那条，后面的仍算未读。

    这是最容易埋的坑——按 now 推进会把没返回给调用方的消息一起标成已读，用户永远
    不会再被提示，而且没有任何机检抓得到。
    """
    repo, _ = file_repo
    first = await _send(repo, sender="leader-codex", mentions=["leader-cc"], content="第一条")
    await _send(repo, sender="leader-codex", mentions=["leader-cc"], content="第二条")

    # 只读到了第一条
    await repo.set_channel_cursor("leader-cc", CHAN, PROJ, first.created_at)

    rows, _ = await repo.count_channel_unread("leader-cc", PROJ)
    assert rows[0]["count"] == 1, "第二条没读到，必须还算未读"
    assert rows[0]["latest_excerpt"] == "第二条"


async def test_cursor_is_monotonic(file_repo):
    """水位不回退：给个更早的时间戳应当 noop，否则已读会重新变未读。"""
    repo, db_file = file_repo
    now = utc_now()

    _, first = await repo.set_channel_cursor("leader-cc", CHAN, PROJ, now)
    assert first is True

    cursor, advanced = await repo.set_channel_cursor(
        "leader-cc", CHAN, PROJ, now - timedelta(hours=1)
    )
    assert advanced is False, "更早的时间戳不应推进水位"
    assert cursor.last_read_at.replace(tzinfo=None) == now.replace(tzinfo=None)

    same, advanced_same = await repo.set_channel_cursor("leader-cc", CHAN, PROJ, now)
    assert advanced_same is False, "相同时间戳也不算前进"
    assert same is not None


async def test_composite_key_is_idempotent(file_repo):
    """(reader, channel, project) 是自然主键：重复推进不得长出第二行。"""
    repo, db_file = file_repo
    base = utc_now()
    await repo.set_channel_cursor("leader-cc", CHAN, PROJ, base)
    await repo.set_channel_cursor("leader-cc", CHAN, PROJ, base + timedelta(minutes=1))
    await repo.set_channel_cursor("leader-cc", CHAN, PROJ, base + timedelta(minutes=2))

    assert len(_cursor_rows(db_file)) == 1


async def test_cursors_are_per_channel_and_per_project(file_repo):
    """同一读者在不同频道/不同项目各有独立水位。"""
    repo, db_file = file_repo
    now = utc_now()
    await repo.set_channel_cursor("leader-cc", "team:one", PROJ, now)
    await repo.set_channel_cursor("leader-cc", "team:two", PROJ, now)
    await repo.set_channel_cursor("leader-cc", "team:one", OTHER_PROJ, now)

    assert len(_cursor_rows(db_file)) == 3


async def test_get_cursor_absent_returns_none_without_writing(file_repo):
    repo, db_file = file_repo
    assert await repo.get_channel_cursor("leader-cc", CHAN, PROJ) is None
    assert _cursor_rows(db_file) == []


# ── 老接口的回归 ────────────────────────────────────────────────


async def test_list_channel_mentions_finds_bare_names(file_repo):
    """修 bug 的回归：这个接口过去对裸名收件人恒返回 0。"""
    repo, _ = file_repo
    await _send(repo, sender="leader-codex", mentions=["leader-cc"])

    got = await repo.list_channel_mentions("leader-cc")
    assert len(got) == 1, "裸名收件人必须查得到（旧实现拼 @ 前缀，恒为 0）"

    # 传 @ 形式的查询名也应命中同一条
    got_at = await repo.list_channel_mentions("@leader-cc")
    assert len(got_at) == 1
