"""Clock 抽象层测试。"""
from datetime import UTC, datetime

from aiteam.pipeline.clock import FakeClock, WallClock


class TestFakeClock:
    def test_default_start(self):
        clock = FakeClock()
        assert clock.now() == datetime(2026, 1, 1, tzinfo=UTC)

    def test_custom_start(self):
        start = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)
        clock = FakeClock(start=start)
        assert clock.now() == start

    def test_advance_seconds(self):
        clock = FakeClock()
        clock.advance(seconds=90)
        expected = datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC)
        assert clock.now() == expected

    def test_advance_minutes(self):
        clock = FakeClock()
        clock.advance(minutes=5)
        expected = datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC)
        assert clock.now() == expected

    def test_advance_combined(self):
        clock = FakeClock()
        clock.advance(minutes=1, seconds=30)
        expected = datetime(2026, 1, 1, 0, 1, 30, tzinfo=UTC)
        assert clock.now() == expected

    def test_advance_multiple_times(self):
        clock = FakeClock()
        clock.advance(minutes=1)
        clock.advance(minutes=2)
        expected = datetime(2026, 1, 1, 0, 3, 0, tzinfo=UTC)
        assert clock.now() == expected

    def test_now_is_stable_without_advance(self):
        clock = FakeClock()
        t1 = clock.now()
        t2 = clock.now()
        assert t1 == t2


class TestWallClock:
    def test_returns_datetime(self):
        clock = WallClock()
        result = clock.now()
        assert isinstance(result, datetime)

    def test_returns_utc(self):
        clock = WallClock()
        result = clock.now()
        assert result.tzinfo == UTC

    def test_monotonic(self):
        clock = WallClock()
        t1 = clock.now()
        t2 = clock.now()
        assert t2 >= t1
