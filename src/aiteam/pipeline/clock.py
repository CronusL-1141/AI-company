"""时间抽象层 — 全 OS Pipeline 模块强制走 Clock 接口，禁止直接 datetime.now()。"""
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class WallClock:
    """Production 实现：返回真实墙钟时间（UTC）。"""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FakeClock:
    """Test 实现：可控时钟，支持 advance。"""

    def __init__(self, start: datetime | None = None):
        self._t = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._t

    def advance(self, seconds: float = 0, minutes: float = 0) -> None:
        from datetime import timedelta
        self._t = self._t + timedelta(seconds=seconds, minutes=minutes)
