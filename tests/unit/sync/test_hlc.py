"""Hybrid logical clock: text order, local ticks, merges, and the SQL twin of the tick."""

from __future__ import annotations

import random
import sqlite3

import pytest

from aiteam.sync import hlc
from aiteam.types import Hlc


def test_text_form_round_trips_and_sorts_like_the_clock() -> None:
    rng = random.Random(7)
    values = [Hlc(rng.randrange(10**13), rng.randrange(10**4), rng.choice(["air-3f9c", "mini-01", ""]))
              for _ in range(500)]
    assert [hlc.parse(hlc.encode(v)) for v in values] == values
    assert sorted(values) == sorted(values, key=hlc.encode)
    assert hlc.encode(Hlc(1763200123456, 2, "air-3f9c")) == "0001763200123456-0002-air-3f9c"
    with pytest.raises(ValueError):
        hlc.parse("1763200123456-2-air")


def test_tick_follows_the_wall_clock_and_never_goes_back() -> None:
    first = hlc.tick(None, "air", 1000)
    assert first == Hlc(1000, 0, "air")
    assert hlc.tick(first, "air", 1005) == Hlc(1005, 0, "air")
    # Same millisecond, or a clock that stepped back: the counter carries the order.
    assert hlc.tick(first, "air", 1000) == Hlc(1000, 1, "air")
    assert hlc.tick(Hlc(1000, 3, "air"), "air", 400) == Hlc(1000, 4, "air")
    # The counter has four digits; the clock borrows the next millisecond.
    assert hlc.tick(Hlc(1000, 9999, "air"), "air", 900) == Hlc(1001, 0, "air")


def test_observe_stays_after_both_clocks() -> None:
    local, remote = Hlc(2000, 5, "air"), Hlc(2000, 9, "mini")
    merged = hlc.observe(local, remote, "air", 1500)
    assert merged == Hlc(2000, 10, "air") and merged > local and hlc.encode(merged) > hlc.encode(remote)
    assert hlc.observe(local, Hlc(2500, 1, "mini"), "air", 1500) == Hlc(2500, 2, "air")
    assert hlc.observe(local, Hlc(1000, 1, "mini"), "air", 1500) == Hlc(2000, 6, "air")
    assert hlc.observe(None, Hlc(1000, 1, "mini"), "air", 3000) == Hlc(3000, 0, "air")


def test_observe_refuses_a_clock_from_the_future() -> None:
    with pytest.raises(hlc.HlcDriftError):
        hlc.observe(None, Hlc(10_000_000, 0, "mini"), "air", 1000)


def _sql_tick(last: Hlc | None, node: str, physical_ms: int) -> Hlc:
    con = sqlite3.connect(":memory:")
    try:
        row = con.execute(hlc.tick_sql(":last", ":node", ":pt"), {
            "last": hlc.encode(last) if last is not None else None, "node": node, "pt": physical_ms,
        }).fetchone()
    finally:
        con.close()
    return hlc.parse(row[0])


@pytest.mark.parametrize("seed", range(4))
def test_the_trigger_sql_ticks_exactly_like_python(seed: int) -> None:
    rng = random.Random(seed)
    cases = [(None, "air", 1000), (Hlc(1000, 9999, "air"), "air", 1000), (Hlc(5, 0, "x"), None, 4)]
    for _ in range(150):
        wall = rng.randrange(1, 10**13)
        last = None if rng.random() < 0.1 else Hlc(wall, rng.choice([0, 1, 9998, 9999, rng.randrange(10**4)]), "n")
        cases.append((last, rng.choice(["air-3f9c", "", "mini"]), wall + rng.randrange(-3, 4)))
    for last, node, physical in cases:
        assert _sql_tick(last, node, physical) == hlc.tick(last, node or "", physical), (last, node, physical)
