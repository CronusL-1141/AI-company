"""Storage side of the incremental plan and price summaries.

``services/plan_summary.py`` defines the state and its one-snapshot step. This module
keeps that state in ``account_plan_summaries`` / ``account_plan_cycle_requests``:

* the insert paths advance it in the same transaction as the new row;
* readers accept it only while the table fingerprint (row count and latest row ID
  for the account) still matches, and the saved anchors are the ones it was folded
  against;
* anything else (first use after upgrade, a row written by a path that does not
  maintain the summary, an out-of-order insert) is rebuilt from the immutable rows
  by a one-shot child interpreter, never on the API event loop (task 551aee38:
  folding ~18K JSON rows in-process froze the API for seconds per round).

Design: docs/account-usage-monitor-design.md section 2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from aiteam.clock import utc_now
from aiteam.services.plan_summary import (
    CycleState,
    PlanWindow,
    PriceWindow,
    Summary,
    WindowKey,
    advance_cycle,
    advance_run_start,
    anchor_matches,
    order_key,
    window_key,
)
from aiteam.storage.connection import get_session
from aiteam.storage.models import (
    AccountPlanCycleRequestModel,
    AccountPlanPriceAnchorModel,
    AccountPlanPriceSnapshotModel,
    AccountPlanSnapshotModel,
    AccountPlanSummaryModel,
)
from aiteam.types import PlanUsageSnapshot, PricingPlanAnchor, PricingPlanSnapshot

logger = logging.getLogger(__name__)

_TABLES = {"plan": AccountPlanSnapshotModel, "price": AccountPlanPriceSnapshotModel}
_REQUEST_CHUNK = 500
REBUILD_CHILD_TIMEOUT_SECONDS = 120.0  # as the memory reconcile pairing child
_REBUILD_ATTEMPTS = 3


class SummaryUnavailableError(RuntimeError):
    """The summary could not be brought up to date; the caller must not guess.

    Infrastructure, not data: deliberately not a ValueError, so a capture that meets
    it fails the round (nothing saved, the next round retries) instead of being
    recorded as an unavailable price, which would mark the whole quota cycle.
    """


class RebuildChildError(RuntimeError):
    """The rebuild child failed to start, timed out or returned garbage."""


# ---------------------------------------------------------------- session helpers


async def fingerprint(session: AsyncSession, kind: str, account_key: str) -> tuple[int, str | None]:
    model = _TABLES[kind]
    count = await session.scalar(
        select(func.count()).select_from(model).where(model.account_key == account_key),
    )
    last = await session.scalar(
        select(model.id).where(model.account_key == account_key)
        .order_by(model.observed_at.desc(), model.id.desc()).limit(1),
    )
    return int(count or 0), last


async def fresh_summary(session: AsyncSession, kind: str, account_key: str) -> Summary | None:
    """The stored summary while it still describes the table; None when it must be rebuilt."""
    count, last = await fingerprint(session, kind, account_key)
    row = await session.get(AccountPlanSummaryModel, (account_key, kind))
    if row is None:
        return Summary.empty(kind) if count == 0 else None
    summary = Summary.from_json(row.payload)
    if summary is None or summary.kind != kind or (summary.count, summary.last_id) != (count, last):
        return None
    return summary


async def save_summary(session: AsyncSession, account_key: str, summary: Summary) -> None:
    row = await session.get(AccountPlanSummaryModel, (account_key, summary.kind))
    if row is None:
        session.add(AccountPlanSummaryModel(
            account_key=account_key, kind=summary.kind, payload=summary.to_json(), updated_at=utc_now(),
        ))
    else:
        row.payload = summary.to_json()
        row.updated_at = utc_now()
    await session.flush()


async def invalidate_summary(session: AsyncSession, account_key: str, kind: str) -> None:
    """Drop derived state; the next reader rebuilds it from the immutable rows."""
    await session.execute(delete(AccountPlanSummaryModel).where(
        AccountPlanSummaryModel.account_key == account_key, AccountPlanSummaryModel.kind == kind,
    ))
    if kind == "price":
        await session.execute(delete(AccountPlanCycleRequestModel).where(
            AccountPlanCycleRequestModel.account_key == account_key,
        ))
    await session.flush()


async def seen_hits(
    session: AsyncSession, account_key: str, key: WindowKey, kind: str, cycle_id: str,
    request_ids: Iterable[str],
) -> set[str]:
    """The subset of ``request_ids`` already counted in the given cycle."""
    ids = sorted(set(request_ids))
    hits: set[str] = set()
    model = AccountPlanCycleRequestModel
    for offset in range(0, len(ids), _REQUEST_CHUNK):
        rows = await session.scalars(select(model.request_id).where(
            model.account_key == account_key, model.limit_id == key[0],
            model.window_duration_ms == key[1], model.kind == kind, model.cycle_id == cycle_id,
            model.request_id.in_(ids[offset:offset + _REQUEST_CHUNK]),
        ))
        hits.update(rows)
    return hits


async def clear_cycle_requests(session: AsyncSession, account_key: str, key: WindowKey, kind: str) -> None:
    model = AccountPlanCycleRequestModel
    await session.execute(delete(model).where(
        model.account_key == account_key, model.limit_id == key[0],
        model.window_duration_ms == key[1], model.kind == kind,
    ))


def _request_ids(snapshot: PricingPlanSnapshot) -> list[str]:
    if snapshot.pricing is None:
        return []
    return [item.request_id for quote in snapshot.pricing.quotes for item in quote.items]


async def cycle_step(
    session: AsyncSession, account_key: str, key: WindowKey, kind: str,
    state: CycleState | None, previous_used_percent: int | None, snapshot: PricingPlanSnapshot,
):
    """``advance_cycle`` with the de-duplication set read from the cycle's request rows."""
    hits: set[str] = set()
    if state is not None:
        hits = await seen_hits(session, account_key, key, kind, state.baseline_id, _request_ids(snapshot))
    return advance_cycle(state, previous_used_percent, snapshot, hits)


async def _record_counted(
    session: AsyncSession, account_key: str, key: WindowKey, kind: str, step, snapshot_id: str,
) -> None:
    if step.reset:
        await clear_cycle_requests(session, account_key, key, kind)
    session.add_all([
        AccountPlanCycleRequestModel(
            account_key=account_key, limit_id=key[0], window_duration_ms=key[1], kind=kind,
            request_id=request_id, cycle_id=step.state.baseline_id, snapshot_id=snapshot_id,
        )
        for request_id, _ in step.counted
    ])


async def _stamp(session: AsyncSession, summary: Summary, account_key: str) -> None:
    """Advance the fingerprint by exactly the row just inserted."""
    summary.count += 1
    summary.last_id = (await fingerprint(session, summary.kind, account_key))[1]
    await save_summary(session, account_key, summary)


# ---------------------------------------------------------------- insert maintenance


@dataclass
class PriceInsertPlan:
    """What an insert does to the price summary, decided before the row is written."""

    summary: Summary | None
    window: PriceWindow | None
    in_order: bool
    step: Any  # CycleStep | None


async def plan_price_insert(session: AsyncSession, snapshot: PricingPlanSnapshot) -> PriceInsertPlan:
    """Read the summary and compute the automatic cycle step for ``snapshot``.

    ``step`` is None when the summary is stale or the row does not come after the
    window's latest observation; then the cycle state at this row is unknown and a
    stored prediction cannot be verified here.
    """
    summary = await fresh_summary(session, "price", snapshot.account_key)
    if summary is None:
        return PriceInsertPlan(None, None, False, None)
    key = window_key(snapshot)
    window = summary.windows.get(key)
    in_order = window is None or order_key(snapshot) > (window.last_observed_at, window.last_id)
    if not in_order:
        return PriceInsertPlan(summary, window, False, None)
    step = await cycle_step(
        session, snapshot.account_key, key, "auto",
        window.auto if window is not None else None,
        window.last_used_percent if window is not None else None, snapshot,
    )
    return PriceInsertPlan(summary, window, True, step)


async def apply_price_insert(session: AsyncSession, plan: PriceInsertPlan, snapshot: PricingPlanSnapshot) -> None:
    """Advance the summary after the row was added in the same transaction."""
    account_key = snapshot.account_key
    if plan.summary is None:
        return  # stale already: the next reader rebuilds it
    if not plan.in_order:
        await invalidate_summary(session, account_key, "price")
        return
    key = window_key(snapshot)
    window, step = plan.window, plan.step
    manual = window.manual if window is not None else None
    if manual is not None:
        if step.reset:
            # The automatic cycle that contained the anchor ended: the anchor no
            # longer applies (plan_pricing.fold_window drops it the same way).
            manual = None
            await clear_cycle_requests(session, account_key, key, "manual")
        else:
            manual_step = await cycle_step(
                session, account_key, key, "manual", manual, window.last_used_percent, snapshot,
            )
            await _record_counted(session, account_key, key, "manual", manual_step, snapshot.snapshot_id)
            manual = manual_step.state
    await _record_counted(session, account_key, key, "auto", step, snapshot.snapshot_id)
    plan.summary.windows[key] = PriceWindow(
        limit_id=key[0], window_duration_ms=key[1],
        count=(window.count if window is not None else 0) + 1,
        last_id=snapshot.snapshot_id, last_observed_at=snapshot.observed_at,
        last_used_percent=snapshot.used_percent, auto=step.state, manual=manual,
        anchor=window.anchor if window is not None else None,
    )
    await _stamp(session, plan.summary, account_key)


async def apply_plan_insert(session: AsyncSession, snapshot: PlanUsageSnapshot, summary: Summary | None) -> None:
    """Advance the plan summary read before the insert (``fresh_summary``)."""
    account_key = snapshot.account_key
    if summary is None:
        return
    key = window_key(snapshot)
    window = summary.windows.get(key)
    if window is not None and not order_key(snapshot) > (window.last_observed_at, window.last_id):
        await invalidate_summary(session, account_key, "plan")
        return
    previous = None
    if window is not None:
        row = await session.get(AccountPlanSnapshotModel, window.last_id)
        previous = None if row is None else PlanUsageSnapshot.model_validate(row.payload)
        if previous is None:
            await invalidate_summary(session, account_key, "plan")
            return
    summary.windows[key] = PlanWindow(
        limit_id=key[0], window_duration_ms=key[1],
        count=(window.count if window is not None else 0) + 1,
        last_id=snapshot.snapshot_id, last_observed_at=snapshot.observed_at,
        run_start_id=advance_run_start(window.run_start_id if window is not None else None, previous, snapshot),
    )
    await _stamp(session, summary, account_key)


# ---------------------------------------------------------------- anchors


async def saved_anchors(session: AsyncSession, account_key: str) -> dict[WindowKey, PricingPlanAnchor]:
    exists = await session.scalar(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_plan_price_anchors'",
    ))
    if not exists:
        return {}
    rows = await session.scalars(select(AccountPlanPriceAnchorModel).where(
        AccountPlanPriceAnchorModel.account_key == account_key,
    ))
    anchors = [PricingPlanAnchor.model_validate(row.payload) for row in rows]
    return {(anchor.limit_id, anchor.window_duration_ms): anchor for anchor in anchors}


def anchors_agree(price: Summary, anchors: dict[WindowKey, PricingPlanAnchor]) -> bool:
    for key, window in price.windows.items():
        anchor = anchors.get(key)
        expected = None if anchor is None else (anchor.snapshot_id, anchor.revision)
        if window.anchor != expected:
            return False
    return True


# ---------------------------------------------------------------- rebuild (child side)


def compute_account_summaries(connection: sqlite3.Connection, account_key: str) -> dict[str, Any]:
    """Fold every window of one account from a consistent read of the database.

    Pure over the connection; runs in the rebuild child. Uses the same validation
    and step functions as the insert path, so its output equals what incremental
    maintenance produces for the same rows.
    """
    connection.execute("BEGIN")  # one read transaction: all three reads see one snapshot
    try:
        fingerprints = {}
        rows_by_kind = {}
        for kind, table in (("plan", "account_plan_snapshots"), ("price", "account_plan_price_snapshots")):
            rows = connection.execute(
                f"SELECT id, payload FROM {table} WHERE account_key = ? ORDER BY observed_at, id",  # noqa: S608
                (account_key,),
            ).fetchall()
            rows_by_kind[kind] = rows
            fingerprints[kind] = [len(rows), rows[-1][0] if rows else None]
        try:
            anchor_rows = connection.execute(
                "SELECT payload FROM account_plan_price_anchors WHERE account_key = ?", (account_key,),
            ).fetchall()
        except sqlite3.OperationalError:
            anchor_rows = []
    finally:
        connection.rollback()

    anchors = {}
    for (payload,) in anchor_rows:
        anchor = PricingPlanAnchor.model_validate(json.loads(payload))
        anchors[(anchor.limit_id, anchor.window_duration_ms)] = anchor

    plan_groups: dict[WindowKey, list[PlanUsageSnapshot]] = {}
    for _, payload in rows_by_kind["plan"]:
        snapshot = PlanUsageSnapshot.model_validate(json.loads(payload))
        plan_groups.setdefault(window_key(snapshot), []).append(snapshot)
    plan = Summary(kind="plan", count=fingerprints["plan"][0], last_id=fingerprints["plan"][1], windows={})
    for key, group in plan_groups.items():
        group.sort(key=order_key)
        run_start, previous = None, None
        for snapshot in group:
            run_start = advance_run_start(run_start, previous, snapshot)
            previous = snapshot
        plan.windows[key] = PlanWindow(
            limit_id=key[0], window_duration_ms=key[1], count=len(group),
            last_id=group[-1].snapshot_id, last_observed_at=group[-1].observed_at, run_start_id=run_start,
        )

    price_groups: dict[WindowKey, list[PricingPlanSnapshot]] = {}
    for _, payload in rows_by_kind["price"]:
        snapshot = PricingPlanSnapshot.model_validate(json.loads(payload))
        price_groups.setdefault(window_key(snapshot), []).append(snapshot)
    price = Summary(kind="price", count=fingerprints["price"][0], last_id=fingerprints["price"][1], windows={})
    requests: list[list[Any]] = []
    for key, group in price_groups.items():
        group.sort(key=order_key)
        anchor = anchors.get(key)
        auto = manual = None
        seen: dict[str, str] = {}  # request_id -> snapshot that first counted it
        manual_seen: dict[str, str] = {}
        previous = None
        for snapshot in group:
            previous_used = previous.used_percent if previous is not None else None
            step = advance_cycle(auto, previous_used, snapshot, seen)
            if step.reset:
                seen = {}
                if manual is not None:
                    manual, manual_seen = None, {}
            seen.update((request_id, snapshot.snapshot_id) for request_id, _ in step.counted)
            auto = step.state
            if manual is not None:
                manual_step = advance_cycle(manual, previous_used, snapshot, manual_seen)
                manual_seen.update((request_id, snapshot.snapshot_id) for request_id, _ in manual_step.counted)
                manual = manual_step.state
            if anchor is not None and anchor_matches(snapshot, anchor):
                manual, manual_seen = advance_cycle(None, None, snapshot).state, {}
            previous = snapshot
        price.windows[key] = PriceWindow(
            limit_id=key[0], window_duration_ms=key[1], count=len(group),
            last_id=group[-1].snapshot_id, last_observed_at=group[-1].observed_at,
            last_used_percent=group[-1].used_percent, auto=auto, manual=manual,
            anchor=None if anchor is None else (anchor.snapshot_id, anchor.revision),
        )
        requests.extend([key[0], key[1], "auto", request_id, auto.baseline_id, snapshot_id]
                        for request_id, snapshot_id in seen.items())
        if manual is not None:
            requests.extend([key[0], key[1], "manual", request_id, manual.baseline_id, snapshot_id]
                            for request_id, snapshot_id in manual_seen.items())
    return {
        "fingerprints": fingerprints,
        "anchors": {f"{key[0]}\x1f{key[1]}": [anchor.snapshot_id, anchor.revision] for key, anchor in anchors.items()},
        "plan": plan.to_json(), "price": price.to_json(), "requests": requests,
    }


def _open_read_only(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _child_main() -> None:
    request = json.loads(sys.stdin.buffer.read())
    connection = _open_read_only(request["db_path"])
    try:
        result = compute_account_summaries(connection, request["account_key"])
    finally:
        connection.close()
    json.dump(result, sys.stdout, separators=(",", ":"))


# The child imports this module from the parent's own source tree (argv[1]), so both
# sides always run the same code whatever the child's sys.path would find first.
_CHILD_BOOT = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from aiteam.storage.plan_summary_store import _child_main; _child_main()"
)


def run_rebuild_child(db_path: str, account_key: str) -> dict[str, Any]:
    """Blocking: run the fold in a one-shot interpreter. Call it from a worker thread."""
    if not sys.executable:
        raise RebuildChildError("no interpreter to start the summary rebuild")
    package_root = str(Path(__file__).resolve().parents[2])
    request = json.dumps({"db_path": db_path, "account_key": account_key})
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_BOOT, package_root],
            input=request.encode(), capture_output=True, check=False,
            timeout=REBUILD_CHILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RebuildChildError(f"summary rebuild killed after {exc.timeout}s") from exc
    except OSError as exc:
        raise RebuildChildError(f"summary rebuild could not start: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode(errors="replace")[-500:].strip()
        raise RebuildChildError(f"summary rebuild exited {proc.returncode}: {tail}")
    try:
        result = json.loads(proc.stdout)
        if not isinstance(result, dict) or not {"fingerprints", "plan", "price", "requests"} <= result.keys():
            raise ValueError("missing keys")
        return result
    except ValueError as exc:
        raise RebuildChildError(f"summary rebuild returned a malformed result: {exc}") from exc


# ---------------------------------------------------------------- rebuild (parent side)


def database_path(db_url: str) -> str | None:
    """The file behind a SQLite URL, or None for an in-memory database."""
    database = make_url(db_url).database
    if not database or database == ":memory:" or database.startswith("file::memory:"):
        return None
    return database


async def _write_rebuild(db_url: str, account_key: str, result: dict[str, Any]) -> bool:
    """Store the child's fold if the rows it read are still exactly the current rows."""
    async with get_session(db_url) as session:
        await session.execute(text("BEGIN IMMEDIATE"))
        for kind in ("plan", "price"):
            if list(await fingerprint(session, kind, account_key)) != result["fingerprints"][kind]:
                return False
        anchors = await saved_anchors(session, account_key)
        current = {f"{key[0]}\x1f{key[1]}": [anchor.snapshot_id, anchor.revision] for key, anchor in anchors.items()}
        if current != result["anchors"]:
            return False
        for kind in ("plan", "price"):
            summary = Summary.from_json(result[kind])
            if summary is None:
                raise RebuildChildError("summary rebuild returned an unknown version")
            await save_summary(session, account_key, summary)
        await session.execute(delete(AccountPlanCycleRequestModel).where(
            AccountPlanCycleRequestModel.account_key == account_key,
        ))
        rows = [
            {"account_key": account_key, "limit_id": limit_id, "window_duration_ms": int(window), "kind": kind,
             "request_id": request_id, "cycle_id": cycle_id, "snapshot_id": snapshot_id}
            for limit_id, window, kind, request_id, cycle_id, snapshot_id in result["requests"]
        ]
        # Core executemany: a cycle can hold thousands of IDs, and ORM unit-of-work
        # bookkeeping for each would run on the event loop.
        for offset in range(0, len(rows), _REQUEST_CHUNK):
            await session.execute(insert(AccountPlanCycleRequestModel), rows[offset:offset + _REQUEST_CHUNK])
        await session.flush()
        return True


async def _rebuild(db_url: str, account_key: str) -> bool:
    db_path = database_path(db_url)
    if db_path is None:
        raise SummaryUnavailableError("an in-memory database cannot be summarised off the event loop")
    result = await asyncio.to_thread(run_rebuild_child, db_path, account_key)
    written = await _write_rebuild(db_url, account_key, result)
    logger.info(
        "Account plan summaries rebuilt for %s…: plan %d rows, price %d rows, %s",
        account_key[:8], result["fingerprints"]["plan"][0], result["fingerprints"]["price"][0],
        "stored" if written else "discarded (rows changed meanwhile)",
    )
    return written


_rebuilds: dict[tuple[int, str, str], asyncio.Task[bool]] = {}


async def rebuild_account_summaries(db_url: str, account_key: str) -> bool:
    """Rebuild once per (loop, database, account) at a time; callers share the result.

    The rebuild task is shielded: a caller that times out or is cancelled does not
    abort it, so the next reader finds the stored result instead of starting over.
    """
    loop = asyncio.get_running_loop()
    key = (id(loop), db_url, account_key)
    task = _rebuilds.get(key)
    if task is None or task.done():
        task = loop.create_task(_rebuild(db_url, account_key), name=f"plan-summary-rebuild:{account_key[:8]}")
        _rebuilds[key] = task

        def _forget(done: asyncio.Task[bool], key: tuple[int, str, str] = key) -> None:
            if _rebuilds.get(key) is done:
                del _rebuilds[key]
            if not done.cancelled():
                done.exception()  # retrieved: every waiter may already be gone

        task.add_done_callback(_forget)
    return await asyncio.shield(task)


@dataclass
class AccountSummaries:
    plan: Summary
    price: Summary
    anchors: dict[WindowKey, PricingPlanAnchor]


async def read_fresh(db_url: str, account_key: str) -> AccountSummaries | None:
    async with get_session(db_url) as session:
        plan = await fresh_summary(session, "plan", account_key)
        price = await fresh_summary(session, "price", account_key)
        anchors = await saved_anchors(session, account_key)
    if plan is None or price is None or not anchors_agree(price, anchors):
        return None
    return AccountSummaries(plan=plan, price=price, anchors=anchors)


async def ensure_summaries(db_url: str, account_key: str) -> AccountSummaries:
    """Current summaries for the account, rebuilding them off the loop when stale."""
    for _ in range(_REBUILD_ATTEMPTS):
        current = await read_fresh(db_url, account_key)
        if current is not None:
            return current
        try:
            await rebuild_account_summaries(db_url, account_key)
        except RebuildChildError as exc:
            raise SummaryUnavailableError(str(exc)) from exc
    current = await read_fresh(db_url, account_key)
    if current is None:
        raise SummaryUnavailableError("account plan summaries kept changing during rebuild")
    return current
