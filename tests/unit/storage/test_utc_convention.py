"""全库统一 UTC 的约定网 —— 见 docs/utc-unification-design.md。

这不是普通功能测试，是**约定的机检**。旧的双墙钟之所以能活几个月，正因为它错得
无声：SQLite 落库剥掉 offset，跨域比较返回一个偏 8 小时的答案而不抛异常。这里的
每条断言都对应设计里的一条约定，任何一条被悄悄回退都会在这里红。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import DateTime, select

from aiteam.clock import ensure_utc, from_timestamp, parse_utc, to_naive_utc, utc_now
from aiteam.storage.connection import close_db, get_session
from aiteam.storage.models import Base, EventModel
from aiteam.storage.repository import StorageRepository
from aiteam.storage.utc_type import UtcDateTime

SRC = Path(__file__).resolve().parents[3] / "src" / "aiteam"


@pytest_asyncio.fixture()
async def repo() -> StorageRepository:
    r = StorageRepository(db_url="sqlite+aiosqlite://")
    await r.init_db()
    yield r  # type: ignore[misc]
    await close_db()


# --------------------------------------------------------------------------
# ① schema：每一个时间列都必须走唯一换算点
# --------------------------------------------------------------------------
class TestEveryColumnGoesThroughTheOneConverter:
    def test_no_datetime_column_bypasses_utc_datetime(self) -> None:
        """漏掉一列，那一列就退回旧世界——静默 naive，且读出来不带 tzinfo。"""
        offenders = [
            f"{table.name}.{col.name}"
            for table in Base.metadata.sorted_tables
            for col in table.columns
            if isinstance(col.type, DateTime) and not isinstance(col.type, UtcDateTime)
        ]
        assert not offenders, f"这些列绕过了 UtcDateTime：{offenders}"

    def test_the_inventory_is_not_silently_shrinking(self) -> None:
        """列数是设计文档 §3 清单的锚。变动本身合法，但必须是有意识的。"""
        count = sum(
            1
            for table in Base.metadata.sorted_tables
            for col in table.columns
            if isinstance(col.type, UtcDateTime)
        )
        assert count >= 82, (
            f"UtcDateTime 列只剩 {count} 个（设计文档记录为 82）——"
            "若确有删表/删列，请同步更新 docs/utc-unification-design.md §3"
        )


# --------------------------------------------------------------------------
# ② 往返：写什么读回来还是同一个时刻，且一定带 UTC 标签
# --------------------------------------------------------------------------
class TestRoundTrip:
    @staticmethod
    async def _write_and_read(repo: StorageRepository, moment: datetime | None):
        kwargs = {"timestamp": moment} if moment is not None else {}
        async with get_session(repo._db_url) as session:
            session.add(
                EventModel(id="e1", type="task.created", source="t", data={}, **kwargs)
            )
        async with get_session(repo._db_url) as session:
            return (await session.execute(select(EventModel))).scalar_one().timestamp

    @pytest.mark.asyncio()
    async def test_aware_non_utc_input_is_converted_not_truncated(
        self, repo: StorageRepository
    ) -> None:
        """带 +08:00 的输入必须被**换算**成 UTC，而不是把 offset 一扔了事。

        一扔了事正是 SQLite 方言过去干的事，也是双墙钟的成因。
        """
        shanghai = timezone(timedelta(hours=8))
        moment = datetime(2026, 7, 28, 17, 0, 0, tzinfo=shanghai)
        stored = await self._write_and_read(repo, moment)
        assert stored == moment, "读回来的必须是同一个时刻"
        assert stored.tzinfo is UTC, "读出侧一律 aware UTC"
        assert stored.hour == 9, "17:00+08:00 存进去就是 09:00 UTC"

    @pytest.mark.asyncio()
    async def test_naive_input_is_taken_as_utc(self, repo: StorageRepository) -> None:
        """naive 输入按"已经是 UTC"处理——这是存储约定，不是猜测。"""
        moment = datetime(2026, 7, 28, 9, 0, 0)
        stored = await self._write_and_read(repo, moment)
        assert stored == moment.replace(tzinfo=UTC)

    @pytest.mark.asyncio()
    async def test_default_stamped_rows_come_back_aware(
        self, repo: StorageRepository
    ) -> None:
        """连列默认值都必须是 aware——default 是最容易漏的一处。"""
        stored = await self._write_and_read(repo, None)
        assert stored.tzinfo is UTC
        # 与真实 UTC 只差秒级；若误写本地墙钟，这里会差一个时区偏移
        assert abs((utc_now() - stored).total_seconds()) < 60


# --------------------------------------------------------------------------
# ③ API 契约：串自己带着口径走
# --------------------------------------------------------------------------
class TestWireFormat:
    @pytest.mark.asyncio()
    async def test_isoformat_carries_the_offset(self, repo: StorageRepository) -> None:
        """没有 offset 的时间串就是让消费方去猜——这次改造要消灭的正是猜。"""
        await repo.create_event("task.created", "t", {})
        events = await repo.list_events(limit=1)
        wire = events[0].timestamp.isoformat()
        assert wire.endswith("+00:00"), f"响应里的时间串没带偏移：{wire}"
        # 而且必须能被原样读回同一时刻
        assert parse_utc(wire) == events[0].timestamp


# --------------------------------------------------------------------------
# ④ clock 模块自身的语义
# --------------------------------------------------------------------------
class TestClockHelpers:
    def test_utc_now_is_aware(self) -> None:
        assert utc_now().tzinfo is UTC

    def test_from_timestamp_labels_utc_instead_of_local(self) -> None:
        """裸 fromtimestamp 会贴本地 offset，等于把值送进另一个时钟。"""
        assert from_timestamp(0) == datetime(1970, 1, 1, tzinfo=UTC)

    def test_parse_utc_reads_bare_strings_as_utc(self) -> None:
        assert parse_utc("2026-07-28T09:00:00") == datetime(2026, 7, 28, 9, tzinfo=UTC)

    def test_parse_utc_normalises_every_spelling_of_one_instant(self) -> None:
        one = datetime(2026, 7, 28, 9, tzinfo=UTC)
        assert parse_utc("2026-07-28T09:00:00Z") == one
        assert parse_utc("2026-07-28T17:00:00+08:00") == one
        assert parse_utc("2026-07-28 09:00:00") == one

    def test_parse_utc_returns_none_rather_than_raising(self) -> None:
        assert parse_utc(None) is None
        assert parse_utc("") is None
        assert parse_utc("garbage") is None

    def test_to_naive_utc_shifts_before_dropping_the_offset(self) -> None:
        shanghai = timezone(timedelta(hours=8))
        assert to_naive_utc(datetime(2026, 7, 28, 17, tzinfo=shanghai)) == datetime(
            2026, 7, 28, 9
        )

    def test_ensure_utc_is_idempotent(self) -> None:
        moment = utc_now()
        assert ensure_utc(ensure_utc(moment)) == moment
        assert ensure_utc(None) is None


# --------------------------------------------------------------------------
# ⑤ 源码守卫：不许再有第二个时钟
# --------------------------------------------------------------------------
class TestNoSecondClockInSource:
    """机检 —— 这条守卫的价值高于上面所有断言。

    上面的测试证明"现在是对的"；这条证明"以后也不会悄悄错回去"。双墙钟当年不是
    谁决定的，是一个模块一个模块随手写下宿主本地墙钟攒出来的——没有守卫，同样的
    事会再发生一次，而且同样不会有人看见。
    """

    # hooks 是脱离 aiteam 包运行的独立进程（且必须与 plugin/hooks 逐字节一致，I1），
    # 不能 import aiteam.clock；它们本就只用 datetime.now(UTC)，无需守卫。
    EXEMPT = {"clock.py", "utc_type.py"}

    def _offenders(self, pattern: str) -> list[str]:
        hits: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            if path.name in self.EXEMPT or "hooks" in path.relative_to(SRC).parts:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(pattern, line):
                    hits.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
        return hits

    def test_no_bare_datetime_now(self) -> None:
        offenders = self._offenders(r"\bdatetime\.now\(")
        assert not offenders, (
            "发现绕过 aiteam.clock 的墙钟调用；一律改用 utc_now()：\n"
            + "\n".join(offenders)
        )

    def test_no_bare_fromtimestamp(self) -> None:
        offenders = self._offenders(r"\bdatetime\.fromtimestamp\(")
        assert not offenders, (
            "裸 fromtimestamp 会给时刻贴上本地偏移；改用 from_timestamp()：\n"
            + "\n".join(offenders)
        )

    def test_no_hand_rolled_offset_stripping(self) -> None:
        """``astimezone().replace(tzinfo=None)`` 是把值搬回本地时钟的老动作。"""
        offenders = self._offenders(r"astimezone\(\)\s*\.replace\(tzinfo=None\)")
        assert not offenders, "手工去偏移已被 to_naive_utc() 取代：\n" + "\n".join(offenders)
