"""Hybrid logical clock: wall-clock milliseconds plus a counter, never going backwards.

Used where two nodes' writes are compared (last writer wins on the shared face,
the order operations are replayed in). The text form is fixed width,
``0001763200123456-0002-air-3f9c``, so comparing strings compares clocks.

The same tick runs in two places: here for Python callers, and as SQL inside the
change-capture triggers (``tick_sql``), where the node's last recorded change is the
clock's state. Both must agree; the test suite runs one against the other.
"""

from __future__ import annotations

import re
import time

from aiteam.types import Hlc

WALL_DIGITS = 16
COUNTER_DIGITS = 4
COUNTER_MAX = 10**COUNTER_DIGITS - 1
# A peer clock further ahead than this is refused rather than followed: one machine
# with a wrong clock would otherwise drag every node's clock into its future.
MAX_DRIFT_MS = 5 * 60 * 1000

_TEXT = re.compile(rf"^(\d{{{WALL_DIGITS}}})-(\d{{{COUNTER_DIGITS}}})-(.*)$", re.DOTALL)


class HlcDriftError(ValueError):
    """A remote clock reading lies too far in the future to be trusted."""


def encode(value: Hlc) -> str:
    return f"{value.wall_ms:0{WALL_DIGITS}d}-{value.counter:0{COUNTER_DIGITS}d}-{value.node}"


def parse(text: str) -> Hlc:
    match = _TEXT.match(text)
    if match is None:
        raise ValueError(f"not an HLC value: {text!r}")
    return Hlc(int(match.group(1)), int(match.group(2)), match.group(3))


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _carry(wall_ms: int, counter: int, node: str) -> Hlc:
    # The counter has a fixed width; on overflow the clock borrows the next millisecond.
    if counter > COUNTER_MAX:
        return Hlc(wall_ms + 1, 0, node)
    return Hlc(wall_ms, counter, node)


def tick(last: Hlc | None, node: str, physical_ms: int) -> Hlc:
    """The clock for a local event, given the last value this node issued."""
    if last is None or physical_ms > last.wall_ms:
        return Hlc(physical_ms, 0, node)
    return _carry(last.wall_ms, last.counter + 1, node)


def observe(last: Hlc | None, remote: Hlc, node: str, physical_ms: int,
            max_drift_ms: int = MAX_DRIFT_MS) -> Hlc:
    """The clock after receiving ``remote``: later than both it and anything issued here."""
    if remote.wall_ms - physical_ms > max_drift_ms:
        raise HlcDriftError(
            f"remote clock {encode(remote)} is {remote.wall_ms - physical_ms} ms ahead of this machine"
        )
    local_wall = last.wall_ms if last is not None else -1
    wall = max(local_wall, remote.wall_ms, physical_ms)
    if wall == local_wall == remote.wall_ms:
        counter = max(last.counter, remote.counter) + 1  # type: ignore[union-attr]
    elif wall == local_wall:
        counter = last.counter + 1  # type: ignore[union-attr]
    elif wall == remote.wall_ms:
        counter = remote.counter + 1
    else:
        counter = 0
    return _carry(wall, counter, node)


def tick_sql(last_sql: str, node_sql: str, physical_ms_sql: str) -> str:
    """A SELECT yielding one column ``hlc``: the SQL twin of :func:`tick`.

    The arguments are SQL expressions for the last issued value (text or NULL), the
    node id and the physical clock in milliseconds. Written without a WITH clause:
    trigger bodies cannot use one.
    """
    wall, counter = f"substr(last, 1, {WALL_DIGITS})", f"substr(last, {WALL_DIGITS + 2}, {COUNTER_DIGITS})"
    return (
        f"SELECT printf('%0{WALL_DIGITS}d-%0{COUNTER_DIGITS}d-%s', "
        f"CASE WHEN c > {COUNTER_MAX} THEN w + 1 ELSE w END, "
        f"CASE WHEN c > {COUNTER_MAX} THEN 0 ELSE c END, n) AS hlc "
        "FROM (SELECT max(pt, lw) AS w, CASE WHEN lw >= pt THEN lc + 1 ELSE 0 END AS c, n "
        f"FROM (SELECT {physical_ms_sql} AS pt, "
        f"coalesce(CAST({wall} AS INTEGER), -1) AS lw, "
        f"coalesce(CAST({counter} AS INTEGER), 0) AS lc, "
        f"coalesce({node_sql}, '') AS n "
        f"FROM (SELECT {last_sql} AS last)))"
    )
