"""Regression: reconcile candidates must not freeze or slow the API while it pairs.

2026-09-28 incident. GET /api/memory/reconcile/candidates ran the pairwise BM25
pass (build_candidate_groups) straight on the event loop. On this project's 3000
valid memos (largest cluster 461) that is 30-40s of pure-Python CPU on an idle
machine and 219-553s under load; for all of it /api/health, every hook POST and
every MCP tool call queued behind it.

The harness is the real stack: the full app under uvicorn on a temp SQLite file
(hook_server), a production-sized corpus (3000 memos, largest cluster ~460), and a
client polling /api/health while candidates runs, exactly as the incident was
observed. Memo bodies are shorter than production's so the fixed code stays around
a second; the pre-fix code still blocks the loop for several seconds on them.

Properties checked:
* /api/health neither stalls (worst probe) nor slows across the board (median probe)
  while a production-sized corpus is paired;
* the pairing runs outside the API interpreter;
* identical concurrent requests share one pairing child, different ones take turns;
* at most two candidates requests hold an ordinary middleware slot, the rest get an
  immediate 503 with Retry-After, and ordinary requests keep flowing;
* a failed child is a 503 carrying its stderr, and frees a lease that request issued.
"""

from __future__ import annotations

import http.client
import json
import random
import sqlite3
import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest

from aiteam.memory import reconcile

PROJECT = "offloop-project"
MEMOS = 3000
LARGEST_CLUSTER_BASE = 420  # plus one near-duplicate per 10 memos: ~460 in the end
PROBE_TIMEOUT = 60.0  # a probe stuck longer is recorded as a stall of this length
MEDIAN_BOUND_RATIO = 0.02  # see the median assertion
CPU_SPLIT_FACTOR = 2.0  # see test_the_api_process_does_not_pay_for_the_pairing

# Zipf-weighted CJK pool: a handful of characters appear in nearly every memo (like
# real prose), so document frequencies inside the big cluster are production-like.
_CHARS = [chr(0x4E00 + i * 7) for i in range(600)]
_WEIGHTS = [1 / (rank + 1) for rank in range(len(_CHARS))]
_BASE_TIME = datetime(2026, 9, 28)
_WORDS = ["api", "hook", "memo", "reconcile", "sqlite", "worker", "lease", "uvicorn"]
_NEAR_DUPLICATES = [
    ("task-1", "部署 API 到生产环境使用 docker compose 命令"),
    ("task-1", "生产环境部署 API 用 docker compose 命令启动"),
]


def _body(rng: random.Random, length: int) -> str:
    parts: list[str] = []
    while sum(map(len, parts)) < length:
        parts.append("".join(rng.choices(_CHARS, _WEIGHTS, k=rng.randint(4, 12))))
        parts.append(rng.choice(_WORDS))
    return " ".join(parts)


def _corpus() -> list[tuple[str, str]]:
    """(task_id, content) pairs; memos without scope_path cluster by task."""
    rng = random.Random(20260928)
    sizes = [LARGEST_CLUSTER_BASE] + [rng.randint(2, 30) for _ in range(400)]
    rows: list[tuple[str, str]] = []
    for task_no, size in enumerate(sizes):
        for k in range(size):
            if len(rows) >= MEMOS:
                return rows
            body = _body(rng, 150 if task_no == 0 else 75)
            rows.append((f"task-{task_no}", body))
            if k % 10 == 0 and len(rows) < MEMOS:  # near-duplicate: gives real candidate groups
                rows.append((f"task-{task_no}", body + " 补充"))
    return rows


def _seed(database, rows: list[tuple[str, str]], project: str = PROJECT) -> None:
    con = sqlite3.connect(database)
    try:
        con.executemany(
            "INSERT INTO task_memos (id, task_id, project_id, author, memo_type, content,"
            " scope_path, meta, created_at) VALUES (?, ?, ?, 'leader', 'progress', ?, '',"
            " '{}', ?)",
            [
                (str(uuid.uuid4()), task_id, project, content,
                 (_BASE_TIME + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S.%f"))
                for i, (task_id, content) in enumerate(rows)
            ],
        )
        con.commit()
    finally:
        con.close()


def _get(
    port: int, path: str, headers: dict | None = None, timeout: float = 600
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _candidates(port: int, query: str = "peek=true", headers: dict | None = None):
    return _get(
        port,
        f"/api/memory/reconcile/candidates?{query}",
        headers={"X-Project-Id": PROJECT, **(headers or {})},
    )


def _concurrently(calls) -> list:
    """Run the zero-argument callables at once (released together), return their results."""
    barrier = threading.Barrier(len(calls))
    results: list = [None] * len(calls)

    def run(index: int) -> None:
        barrier.wait()
        results[index] = calls[index]()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    return results


def _spy_on_children(monkeypatch, hold: float) -> list[tuple[float, float, float]]:
    """Record every pairing child as (threshold, start, end); keep each open `hold` s."""
    calls: list[tuple[float, float, float]] = []
    real = reconcile._cluster_edges_in_child

    def spy(contents, threshold):
        started = time.monotonic()
        time.sleep(hold)  # keep the pairing in flight while the other requests arrive
        try:
            return real(contents, threshold)
        finally:
            calls.append((threshold, started, time.monotonic()))

    monkeypatch.setattr(reconcile, "_cluster_edges_in_child", spy)
    return calls


def test_health_stays_responsive_while_candidates_computes(hook_server):
    port, database, _ = hook_server
    _seed(database, _corpus())
    _get(port, "/api/health")  # warm-up outside the measured window

    probes: list[tuple[float, float]] = []  # (start, end) of each /api/health
    failed_probes = 0
    stop = threading.Event()

    def poll() -> None:
        nonlocal failed_probes
        while not stop.is_set():
            started = time.monotonic()
            try:
                _get(port, "/api/health", timeout=PROBE_TIMEOUT)
            except (OSError, http.client.HTTPException):
                failed_probes += 1  # still a probe: its stall is recorded below
            probes.append((started, time.monotonic()))
            time.sleep(0.01)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    time.sleep(0.1)
    call_start = time.monotonic()
    status, body = _candidates(port)
    call_end = time.monotonic()
    # Let a probe that was stuck behind the call finish and record its latency.
    time.sleep(0.1)
    stop.set()
    poller.join(PROBE_TIMEOUT + 5)

    assert status == 200, body[:300]
    stats = json.loads(body)["data"]["stats"]
    assert stats["total_valid_memos"] == MEMOS
    assert stats["candidate_group_count"] > 0
    assert stats["pairing"] == "child"

    elapsed = call_end - call_start
    overlapping = [end - start for start, end in probes if end > call_start and start < call_end]
    assert overlapping, "no health probe overlapped the candidates call"
    worst = max(overlapping)
    median = statistics.median(overlapping)
    summary = (
        f"during a {elapsed:.2f}s candidates call: worst probe {worst * 1000:.0f}ms, "
        f"median {median * 1000:.1f}ms, {len(overlapping)} probes, {failed_probes} failed"
    )
    # Worst probe, relative bound: pairing on the loop (in-process, or waiting on the
    # child synchronously) stalls a probe for nearly the whole call, ratio 0.87-0.99;
    # off-loop the worst probe took 0.03-0.07 of the call, idle or with 20 busy
    # processes on 10 cores. Both sides scale with CPU contention, so the ratio holds
    # on a loaded runner; the floor covers a machine where the whole call is sub-second.
    assert worst < max(0.3 * elapsed, 0.15), f"/api/health stalled {summary}"
    # Median probe: pairing in a worker thread of this process never stalls a probe
    # for long, it slows every one of them (GIL contention). See the report for the
    # measured medians; the bound sits between the two with a wide margin each way.
    assert median < MEDIAN_BOUND_RATIO * elapsed, f"/api/health slowed {summary}"


def test_the_api_process_does_not_pay_for_the_pairing(hook_server):
    """Whoever runs the pairing pays its CPU seconds, whatever the load.

    The latency bounds above cannot tell a pairing thread inside the API process
    apart under heavy load: with 40 busy processes on 10 cores its median probe
    ratio was 0.002-0.003, under the median bound (idle: 0.21-0.25). CPU accounting
    does not blur that way and does not depend on any function name.
    """
    resource = pytest.importorskip("resource")
    port, database, _ = hook_server
    _seed(database, _corpus())
    _get(port, "/api/health")  # warm-up outside the measured window

    def children_cpu() -> float:
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)  # reaped children only
        return usage.ru_utime + usage.ru_stime

    # In-process uvicorn: this process is the API process.
    parent_before, children_before = time.process_time(), children_cpu()
    status, body = _candidates(port)
    parent = time.process_time() - parent_before
    children = children_cpu() - children_before

    assert status == 200, body[:300]
    assert json.loads(body)["data"]["stats"]["candidate_group_count"] > 0
    # See the report for the measured split; the pairing dominates either side.
    assert children > CPU_SPLIT_FACTOR * parent, (
        f"pairing CPU landed in the API process: API {parent:.2f}s, children {children:.2f}s"
    )


def test_candidates_pairs_outside_the_api_interpreter(hook_server, monkeypatch):
    """Named guard for the same property, with the clearest failure message.

    Measured on the 3000-memo corpus with the pairing in a thread: hook POSTs went from
    10ms to 0.65s (p50) for the whole computation. The copy of the pairing in this
    process is poisoned, so the call only works if the pairing runs in a child.
    """
    port, database, _ = hook_server
    _seed(database, _NEAR_DUPLICATES)

    def poisoned(*_args, **_kwargs):
        raise AssertionError("pairing ran in the API interpreter")

    # raising=False: the pre-fix module has no cluster_edges; there the latency test
    # above is the one that fails.
    monkeypatch.setattr(reconcile, "cluster_edges", poisoned, raising=False)
    status, body = _candidates(port)
    assert status == 200, body[:300]
    stats = json.loads(body)["data"]["stats"]
    assert stats["candidate_group_count"] == 1
    assert stats["pairing"] == "child"


def test_identical_concurrent_requests_share_one_child(hook_server, monkeypatch):
    """Concurrent peeks on unchanged memos pair once and both get the same groups.

    Two requests, the admission limit: in practice a caller's retry arriving while
    its first attempt is still pairing.
    """
    port, database, _ = hook_server
    _seed(database, _NEAR_DUPLICATES)
    calls = _spy_on_children(monkeypatch, hold=0.5)

    results = _concurrently([lambda: _candidates(port)] * 2)

    assert [status for status, _ in results] == [200] * 2, results
    groups = [json.loads(body)["data"]["candidate_groups"] for _, body in results]
    assert groups[0] == groups[1] and len(groups[0]) == 1
    assert len(calls) == 1, f"{len(calls)} pairing children for 2 identical requests"


def test_different_requests_pair_one_at_a_time(hook_server, monkeypatch):
    """Two, the admission limit: a third would be turned away, not queued."""
    port, database, _ = hook_server
    _seed(database, _NEAR_DUPLICATES)
    calls = _spy_on_children(monkeypatch, hold=0.3)

    results = _concurrently(
        [lambda t=t: _candidates(port, f"peek=true&threshold={t}") for t in (0.3, 0.6)]
    )

    assert [status for status, _ in results] == [200] * 2, results
    assert sorted(threshold for threshold, _, _ in calls) == [0.3, 0.6]
    spans = sorted((start, end) for _, start, end in calls)
    for (_, previous_end), (next_start, _) in zip(spans, spans[1:], strict=False):
        assert next_start >= previous_end, f"pairing children overlapped: {spans}"


def test_candidates_beyond_the_limit_are_turned_away_and_ordinary_requests_flow(
    hook_server, monkeypatch
):
    """At most two candidates requests hold an ordinary middleware slot at once.

    Six arrive together while the pairing is held open: two are admitted (one pairs,
    one waits on it), the rest get an immediate 503 with Retry-After. The middleware
    has four ordinary slots, so without the limit four candidates would sit in them
    for the whole pairing and GET /api/projects would queue behind them.
    """
    port, database, _ = hook_server
    _seed(database, _NEAR_DUPLICATES)
    hold = 2.0
    _spy_on_children(monkeypatch, hold=hold)

    def timed_candidates():
        started = time.monotonic()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            conn.request(
                "GET", "/api/memory/reconcile/candidates?peek=true",
                headers={"X-Project-Id": PROJECT},
            )
            response = conn.getresponse()
            body = response.read()
            return response.status, response.getheader("Retry-After"), body, time.monotonic() - started
        finally:
            conn.close()

    def projects_after_admission():
        time.sleep(0.3 * hold)  # the candidates are in (or turned away) by now
        started = time.monotonic()
        status, _ = _get(port, "/api/projects", timeout=60)
        return status, time.monotonic() - started

    *candidates, (projects_status, projects_latency) = _concurrently(
        [timed_candidates] * 6 + [projects_after_admission]
    )

    admitted = [c for c in candidates if c[0] == 200]
    turned_away = [c for c in candidates if c[0] == 503]
    assert len(admitted) == 2 and len(turned_away) == 4, [(c[0], c[3]) for c in candidates]
    for _status, retry_after, body, latency in turned_away:
        assert retry_after is not None and int(retry_after) > 0
        assert "整理候选正在计算中，稍后重试" in json.loads(body)["detail"]
        assert latency < hold / 2, f"a turned-away request took {latency:.2f}s"
    assert projects_status == 200
    assert projects_latency < hold / 2, (
        f"GET /api/projects waited {projects_latency:.2f}s behind candidates requests"
    )


def test_failed_child_is_a_503_and_frees_the_lease_it_issued(hook_server, monkeypatch):
    """No session header: the lease is known only by the lease_id this response carries.

    Kept after a 503, it would lock the caller out of its own project for the TTL.
    """
    port, database, _ = hook_server
    con = sqlite3.connect(database)
    try:
        con.execute(
            "INSERT INTO projects (id, name, root_path, description, config, created_at,"
            " updated_at) VALUES (?, 'offloop', '/tmp/offloop-project', '', '{}', ?, ?)",
            (PROJECT, "2026-09-28 00:00:00.000000", "2026-09-28 00:00:00.000000"),
        )
        con.commit()
    finally:
        con.close()
    _seed(database, _NEAR_DUPLICATES)
    boot = reconcile._CHILD_BOOT
    monkeypatch.setattr(
        reconcile, "_CHILD_BOOT", "import sys; sys.stderr.write('child broke'); sys.exit(3)"
    )

    status, body = _candidates(port, query="")
    assert status == 503, body[:300]
    detail = json.loads(body)["detail"]
    assert "exited 3" in detail and "child broke" in detail

    monkeypatch.setattr(reconcile, "_CHILD_BOOT", boot)
    status, body = _candidates(port, query="")
    assert status == 200, body[:300]
    data = json.loads(body)["data"]
    assert data["reconcile_lease"]["status"] == "acquired", data["reconcile_lease"]
