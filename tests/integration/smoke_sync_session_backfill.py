"""Smoke run, phase 2 acceptance 3: the center's own hook writes while an edge catches up.

Not collected by pytest (a measurement, minutes long, its numbers go into the report).
Two real nodes from one baseline (sync_cluster.Cluster). Each round, alternating in
the same processes:

1. the tunnel is cut and the edge writes ``--rows`` session rows (a day offline);
2. baseline: 48 concurrent hook writers hit the center, p95 of their requests;
3. the tunnel opens, the edge's backlog starts going up; the same load again, while
   the backfill is still running (checked before and after);
4. the backlog drains before the next round.

The verdict takes each round's p95 during backfill / p95 baseline: their median must
stay within ``--median-threshold`` (1.15), every round within ``--threshold`` (1.25).
``--duty 1`` turns the center's yielding of its write lock off (aiteam.sync.intake),
for the comparison run.

Usage: PYTHONPATH=src python3 tests/integration/smoke_sync_session_backfill.py [--rounds 5] [--rows 60000]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))

from sync_cluster import EDGE_ID, Cluster, wait_for  # noqa: E402

WRITERS = 48


def _bulk(cluster: Cluster, rows: int, tag: str) -> None:
    con = sqlite3.connect(cluster.edge.db, timeout=60)
    try:
        con.executemany(
            "INSERT INTO events (id, type, source, data, timestamp, origin_node) VALUES "
            "(?, 'cc.tool_use', ?, ?, '2026-11-15 08:00:00', ?)",
            [(f"{tag}-{n:07d}", f"session:{tag}", json.dumps({"tool": "Bash", "input": "x" * 200, "n": n}), EDGE_ID)
             for n in range(rows)])
        con.commit()
    finally:
        con.close()


async def _load(url: str, requests_per_writer: int, tag: str) -> list[float]:
    """48 writers, each a session posting tool-call pairs; the latency of every request."""
    latencies: list[float] = []

    async def writer(client: httpx.AsyncClient, n: int) -> None:
        session = f"{tag}-{n:02d}-load"
        for k in range(requests_per_writer):
            event = "PreToolUse" if k % 2 == 0 else "PostToolUse"
            payload = {"hook_event_name": event, "session_id": session, "cwd": "/work/shared", "transcript_path": "",
                       "tool_name": "Bash", "tool_input": {"command": "true"}, "tool_use_id": f"{session}-{k // 2}"}
            started = time.perf_counter()
            response = await client.post("/api/hooks/event", json=payload)
            latencies.append(time.perf_counter() - started)
            response.raise_for_status()

    limits = httpx.Limits(max_connections=WRITERS, max_keepalive_connections=WRITERS)
    async with httpx.AsyncClient(base_url=url, timeout=60, trust_env=False, limits=limits) as client:
        await asyncio.gather(*(writer(client, n) for n in range(WRITERS)))
    return latencies


def _p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=100)[94] * 1000


def _taken(cluster: Cluster, tag: str) -> int:
    return cluster.center.sql("SELECT count(*) FROM events WHERE id LIKE ?", f"{tag}-%")[0][0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--rows", type=int, default=60000)
    parser.add_argument("--requests", type=int, default=16, help="requests per writer per measurement")
    parser.add_argument("--duty", default="", help="override the center's write-lock duty (1: no yielding)")
    parser.add_argument("--threshold", type=float, default=1.25, help="every round's ratio at most")
    parser.add_argument("--median-threshold", type=float, default=1.15, help="the median ratio at most")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="sync-backfill-smoke-"))
    cluster = Cluster(root)
    results = []
    try:
        cluster.cut_over(center_env={"AITEAM_SYNC_INTAKE_DUTY": args.duty} if args.duty else {})
        for round_no in range(1, args.rounds + 1):
            tag = f"round{round_no}"
            cluster.tunnel.cut()
            cluster.wait_detached()
            _bulk(cluster, args.rows, tag)
            baseline = asyncio.run(_load(cluster.center.url, args.requests, f"{tag}-base"))
            cluster.tunnel.open()
            wait_for(lambda: _taken(cluster, tag) > 0, timeout=60, what="the backfill to start")
            before = _taken(cluster, tag)
            during = asyncio.run(_load(cluster.center.url, args.requests, f"{tag}-fill"))
            after = _taken(cluster, tag)
            valid = 0 < before and after < args.rows
            results.append({"round": round_no, "baseline_p95_ms": round(_p95(baseline), 1),
                            "backfill_p95_ms": round(_p95(during), 1),
                            "ratio": round(_p95(during) / _p95(baseline), 3), "rows_taken_during": after - before,
                            "backfill_running_throughout": valid})
            print(json.dumps(results[-1]), flush=True)
            cluster.wait_uploaded(timeout=600)
        ratios = [r["ratio"] for r in results]
        verdict = {"duty": args.duty or "default", "rows_per_round": args.rows, "writers": WRITERS,
                   "median_ratio": round(statistics.median(ratios), 3), "max_ratio": max(ratios),
                   "median_threshold": args.median_threshold, "threshold": args.threshold,
                   "all_rounds_valid": all(r["backfill_running_throughout"] for r in results)}
        verdict["pass"] = (verdict["all_rounds_valid"] and verdict["median_ratio"] <= args.median_threshold
                           and verdict["max_ratio"] <= args.threshold)
        print(json.dumps(verdict), flush=True)
        return 0 if verdict["pass"] else 1
    finally:
        cluster.close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
