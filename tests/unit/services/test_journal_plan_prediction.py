"""Plan dollars from the usage journal, end to end (task a0110751).

Native logs are recorded by the production recorder, the monitor capture
consumes the journal, samples are saved, and the estimate is read back through
the HTTP route from a fresh repository. Only the remote quota read is a fixture.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from aiteam.api.routes import account_usage as routes
from aiteam.services import codex_local_usage
from aiteam.services import local_plan_capture as capture
from aiteam.services.codex_usage_recorder import CodexUsageRecorder
from aiteam.services.pricing import load_catalog
from aiteam.storage import account_usage as usage_storage
from aiteam.storage.account_usage import AccountUsageRepository
from aiteam.storage.codex_usage_journal import CodexUsageJournalRepository
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import PlanUsageSnapshot, PricingAccount, PricingPlanAnchorReset, PricingQuotaSnapshot

from .._codex_journal import record_all

START = datetime(2026, 9, 15, tzinfo=UTC)
KEY = "a" * 64
WEEK_MS = 7 * 24 * 3600 * 1000


def row(kind: str, at: datetime, payload: dict) -> str:
    return json.dumps({"timestamp": at.isoformat(), "type": kind, "payload": payload}) + "\n"


def response(response_id: str, at: datetime, *, tokens: int = 1000, model: str = "gpt-6-astra",
             thread: str = "main", padding: str = "") -> str:
    payload = {"response_id": response_id, "thread_id": thread, "model": model, "service_tier": "default",
               "usage": {"input_tokens": tokens, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                         "output_tokens": 0}}
    if padding:
        payload["text"] = padding  # bulk the recorder skips, like real tool output
    return row("token_usage_record", at, payload)


def session(session_id: str = "main", *, parent: str | None = None, extra: dict | None = None) -> str:
    payload = {"id": session_id, "model_provider": "openai", **(extra or {})}
    if parent:
        payload["parent_thread_id"] = parent
    return row("session_meta", START - timedelta(hours=1), payload)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    (root / "sessions").mkdir(parents=True)
    log = root / "sessions" / "main.jsonl"
    log.write_text(session())
    url = f"sqlite+aiosqlite:///{tmp_path / 'os.sqlite'}"
    state = SimpleNamespace(root=root, log=log, url=url, now=START, percent=20, catalog=load_catalog())
    await AccountUsageRepository(url).init_db()
    state.journal = CodexUsageJournalRepository(url)
    await state.journal.init_db()

    async def quota():
        data = dict(snapshot_id=str(uuid4()), account_key=KEY, limit_id="codex", window_duration_ms=WEEK_MS,
                    observed_at=state.now, resets_at=START + timedelta(days=7), used_percent=state.percent)
        plan = PlanUsageSnapshot(**data, activity_tokens=None, activity_scope=None, activity_observed_at=None)
        return (PricingAccount(account_key=KEY, label="fixture", created_at=START),
                [PricingQuotaSnapshot(**data, source="codex_app_server")], [plan])

    monkeypatch.setattr(capture, "capture_plan_quota", quota)
    monkeypatch.setattr(capture, "_local_source", lambda: (root, "generation"))
    monkeypatch.setattr(capture, "load_catalog", lambda: state.catalog)
    monkeypatch.setattr(usage_storage, "utc_now", lambda: state.now)
    monkeypatch.setattr(routes, "utc_now", lambda: state.now)
    yield state
    await engine_pool.get_engine(url).dispose()


async def monitor_round(state, *, seconds: int, percent: int, record: bool = True):
    """One saved capture after a service restart: every repository is new."""
    await engine_pool.get_engine(state.url).dispose()
    if record:
        await record_all(state.journal, state.root)
    state.now, state.percent = START + timedelta(seconds=seconds), percent
    repository = AccountUsageRepository(state.url)
    account, quotas, plans, prices = await capture.capture_local_plan_account(repository=repository)
    await repository.save_capture(account, quotas, plan_snapshots=plans, pricing_plan_snapshots=prices)
    return plans[0], prices[0]


async def page_estimate(state) -> dict:
    """The estimate the account page receives, from a fresh repository behind the route."""
    await engine_pool.get_engine(state.url).dispose()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_account_repository] = lambda: AccountUsageRepository(state.url)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        reply = await client.get(f"/api/account-usage/{KEY}?include_pricing=false")
    assert reply.status_code == 200, reply.text[:300]
    (estimate,) = reply.json()["data"]["pricing_plan_estimates"]
    return estimate


def append(path, *lines: str) -> None:
    with path.open("a") as stream:
        stream.write("".join(lines))


async def test_history_far_beyond_a_read_budget_still_prices_new_usage(env, monkeypatch):
    # A long session: its history is many times the recorder's per-tick budget,
    # and many times the budget the former whole-file rescan was given here.
    append(env.log, *(response(f"history-{index}", START - timedelta(minutes=30), padding="x" * 400)
                      for index in range(2500)))
    recorder = CodexUsageRecorder(env.journal, codex_home=env.root, max_scan_bytes=64 * 1024)
    for _ in range(200):
        await recorder.run_once()
        cursors = await env.journal.list_cursors()
        if cursors and all(cursor.offset == env.log.stat().st_size for cursor in cursors):
            break
    assert env.log.stat().st_size > 1_000_000
    assert await env.journal.count_observations() == 2500

    await monitor_round(env, seconds=0, percent=20, record=False)
    append(env.log, response("new-one", START + timedelta(seconds=5)),
           response("new-two", START + timedelta(seconds=6), tokens=2000))
    await recorder.run_once()
    monkeypatch.setattr(codex_local_usage, "_MAX_BYTES", 64 * 1024, raising=False)
    real_open = os.open

    def no_log_reads(path, *args, **kwargs):
        if str(path).endswith(".jsonl"):
            raise PermissionError("the capture must not reread session logs")
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(os, "open", no_log_reads)
        tokens, prices = await monitor_round(env, seconds=10, percent=21, record=False)
    assert tokens.activity_tokens == 3000
    assert sorted(entry.request.request_id for entry in prices.pricing.entries) == ["new-one", "new-two"]
    estimate = await page_estimate(env)
    assert estimate["status"] == "estimated" and estimate["delta_used_percent"] == 1
    assert Decimal(estimate["delta_usd"]) == Decimal("0.03")  # 3,000 standard input tokens
    assert Decimal(estimate["estimated_total_usd"]) == Decimal("3")


async def test_late_facts_copies_restarts_and_a_manual_reset_count_each_request_once(env, tmp_path):
    await monitor_round(env, seconds=0, percent=20)
    append(env.log, response("a", START + timedelta(seconds=5)))
    await monitor_round(env, seconds=10, percent=21)
    anchor = await AccountUsageRepository(env.url).reset_plan_anchor(
        KEY, PricingPlanAnchorReset(limit_id="codex", window_duration_ms=WEEK_MS),
    )
    assert anchor.observed_at == START + timedelta(seconds=10)
    # "b" happened before the reset but is recorded only now; "c" after it.
    append(env.log, response("b", START + timedelta(seconds=8)), response("c", START + timedelta(seconds=15)))
    tokens, prices = await monitor_round(env, seconds=20, percent=22)
    assert tokens.activity_tokens == 3000
    assert sorted(entry.request.request_id for entry in prices.pricing.entries) == ["b", "c"]

    # The log is rewritten before its responses (a new generation records them
    # again under new fact IDs), a fork copies "a" as inherited history, and a
    # resumed log of the same session copies "c". Only "d" is new.
    env.log.write_text(session(extra={"cwd": "/rewritten"}) + response("a", START + timedelta(seconds=5))
                       + response("b", START + timedelta(seconds=8)) + response("c", START + timedelta(seconds=15))
                       + response("d", START + timedelta(seconds=25)))
    (env.root / "sessions" / "fork.jsonl").write_text(
        session("fork", parent="main") + response("a", START + timedelta(seconds=5)),
    )
    (env.root / "archived_sessions").mkdir()
    (env.root / "archived_sessions" / "resumed.jsonl").write_text(
        session(extra={"cwd": "/resumed"}) + response("c", START + timedelta(seconds=15)),
    )
    tokens, prices = await monitor_round(env, seconds=30, percent=23)
    assert tokens.activity_tokens == 4000
    assert [entry.request.request_id for entry in prices.pricing.entries] == ["d"]
    assert prices.pricing.complete

    estimate = await page_estimate(env)
    # The manual cycle starts at the reset: "c" and "d" only, against 21% -> 23%.
    assert estimate["start_snapshot_id"] == anchor.snapshot_id
    assert estimate["priced_request_count"] == 2 and estimate["delta_used_percent"] == 2
    assert Decimal(estimate["delta_usd"]) == Decimal("0.02")
    assert Decimal(estimate["estimated_total_usd"]) == Decimal("1")
    # The automatic cycle saw every request exactly once.
    window = (await AccountUsageRepository(env.url).summaries(KEY)).price.windows[("codex", WEEK_MS)]
    assert window.auto.priced == 4 and window.auto.total == Decimal("0.04")


async def test_unpriced_models_are_named_instead_of_reading_as_spent_nothing(env):
    await monitor_round(env, seconds=0, percent=20)
    append(env.log, response("new-model", START + timedelta(seconds=5), model="gpt-9-preview"))
    await monitor_round(env, seconds=10, percent=21)
    estimate = await page_estimate(env)
    assert Decimal(estimate["estimated_total_usd"]) == 0
    assert estimate["unpriced_models"] == ["gpt-9-preview"] and estimate["unpriced_request_count"] == 1

    append(env.log, response("known", START + timedelta(seconds=15)))
    await monitor_round(env, seconds=20, percent=22)
    estimate = await page_estimate(env)
    assert Decimal(estimate["delta_usd"]) == Decimal("0.01")  # the known part still accumulates
    assert Decimal(estimate["estimated_total_usd"]) == Decimal("0.5")
    assert estimate["unpriced_models"] == ["gpt-9-preview"] and estimate["unpriced_request_count"] == 1
    assert estimate["reason_code"] == "pricing_incomplete"

    await AccountUsageRepository(env.url).reset_plan_anchor(
        KEY, PricingPlanAnchorReset(limit_id="codex", window_duration_ms=WEEK_MS),
    )
    append(env.log, response("after-reset", START + timedelta(seconds=25)))
    await monitor_round(env, seconds=30, percent=23)
    assert (await page_estimate(env))["unpriced_models"] == []  # a new boundary starts a clean list


@pytest.mark.parametrize("model", ["gpt-5.3-codex-spark"], ids=["spark"])
async def test_the_separately_metered_bucket_is_neither_priced_nor_named(env, model):
    await monitor_round(env, seconds=0, percent=20)
    append(env.log, response("spark", START + timedelta(seconds=5), model=model))
    await monitor_round(env, seconds=10, percent=21)
    estimate = await page_estimate(env)
    assert estimate["unpriced_models"] == [] and Decimal(estimate["estimated_total_usd"]) == 0
