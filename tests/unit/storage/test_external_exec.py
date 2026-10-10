"""Independent SQLite connections verify minimal facts and receipt idempotency."""

import asyncio
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest

from aiteam.storage.engine_pool import engine_pool
from aiteam.storage.external_exec import ExternalExecConflictError, ExternalExecRepository
from aiteam.types import CodexUsageTokens, ExternalExecFinish, ExternalExecReceipt, ExternalExecRegistration


def registration(**changes):
    return ExternalExecRegistration(invocation_id=str(uuid4()), source_namespace="a" * 64, **changes)


@pytest.fixture
async def stores(tmp_path):
    path = tmp_path / "exec.sqlite"
    url = f"sqlite+aiosqlite:///{path}"
    first, second = ExternalExecRepository(url), ExternalExecRepository(url)
    await asyncio.gather(first.init_db(), second.init_db())
    yield first, second, path
    await engine_pool.get_engine(url).dispose()


async def test_registration_retry_and_frozen_identity(stores):
    first, second, path = stores
    item = registration(caller_harness="claude-code", caller_session_id="parent-claude", ephemeral=True)
    await first.register(item)
    assert (await second.register(item)).revision == 1
    with pytest.raises(ExternalExecConflictError, match="identity"):
        await second.register(item.model_copy(update={"caller_session_id": "another-parent"}))
    actual = await second.get(item.invocation_id)
    assert actual.receipts == {} and actual.exit_code is None
    assert actual.callee_harness == "codex"
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"external_exec_invocations"}


async def test_seal_refuses_new_or_amended_receipts_but_repeat_never_double_counts(stores):
    first, second, _ = stores
    item = registration()
    await first.register(item)
    receipt = ExternalExecReceipt(
        receipt_id="turn:1",
        counting_level="turn_aggregate",
        thread_id="thread-one",
        usage=CodexUsageTokens(
            input_tokens=100,
            cached_input_tokens=80,
            output_tokens=20,
            reasoning_output_tokens=10,
        ),
    )
    await second.receipt(item.invocation_id, receipt)
    await first.finish(item.invocation_id, ExternalExecFinish(status="failed", exit_code=1, receipt_ids=["turn:1"]))
    before = await first.get(item.invocation_id)
    await second.receipt(
        item.invocation_id, receipt.model_copy(update={"observed_at": receipt.observed_at + timedelta(seconds=2)})
    )
    await first.finish(item.invocation_id, ExternalExecFinish(status="failed", exit_code=1))
    after = await second.get(item.invocation_id)
    assert after == before
    assert after.status == "failed" and len(after.receipts) == 1
    assert after.receipts["turn:1"].counting_level == "turn_aggregate"
    assert after.receipts["turn:1"].usage.total_tokens is None
    with pytest.raises(ExternalExecConflictError, match="sealed"):
        await second.receipt(
            item.invocation_id, receipt.model_copy(update={"usage": CodexUsageTokens(input_tokens=101)})
        )
    with pytest.raises(ExternalExecConflictError, match="sealed"):
        await second.receipt(item.invocation_id, receipt.model_copy(update={"receipt_id": "late-new"}))


async def test_native_turn_and_thread_identity_cannot_be_duplicated(stores):
    first, second, _ = stores
    item = registration()
    await first.register(item)
    receipt = ExternalExecReceipt(receipt_id="receipt-1", thread_id="thread", turn_id="native-turn")
    await first.receipt(item.invocation_id, receipt)
    with pytest.raises(ExternalExecConflictError, match="duplicate_native_turn"):
        await second.receipt(item.invocation_id, receipt.model_copy(update={"receipt_id": "receipt-2"}))
    with pytest.raises(ExternalExecConflictError, match="thread"):
        await second.bind_thread(item.invocation_id, "other-thread")
    assert (await first.get(item.invocation_id)).receipts["receipt-1"].usage is None


async def test_missing_usage_can_be_filled_without_changing_known_facts(stores):
    first, second, _ = stores
    item = registration()
    await first.register(item)
    await first.receipt(item.invocation_id, ExternalExecReceipt(receipt_id="turn:1"))
    await second.receipt(
        item.invocation_id,
        ExternalExecReceipt(
            receipt_id="turn:1",
            thread_id="late-thread",
            usage=CodexUsageTokens(input_tokens=0),
        ),
    )
    await first.finish(item.invocation_id, ExternalExecFinish(status="completed", exit_code=0, receipt_ids=["turn:1"]))
    actual = await first.get(item.invocation_id)
    assert actual.status == "completed" and actual.receipts["turn:1"].usage.input_tokens == 0
    assert "missing_usage" not in actual.diagnostics and actual.receipts["turn:1"].usage.output_tokens is None
    with pytest.raises(ExternalExecConflictError):
        await second.receipt(
            item.invocation_id,
            ExternalExecReceipt(
                receipt_id="turn:1",
                usage=CodexUsageTokens(input_tokens=1),
            ),
        )


async def test_concurrent_distinct_receipts_are_atomic_and_ack_does_not_cover_late_write(stores):
    first, second, _ = stores
    item = registration()
    await first.register(item)
    await asyncio.gather(
        *[
            (first if n % 2 else second).receipt(
                item.invocation_id,
                ExternalExecReceipt(
                    receipt_id=f"turn:{n}",
                    usage=CodexUsageTokens(input_tokens=n),
                ),
            )
            for n in range(48)
        ]
    )
    snapshot = await first.get(item.invocation_id)
    assert len(snapshot.receipts) == 48
    await second.bind_thread(item.invocation_id, "late-thread")
    assert await first.mark_uploaded(item.invocation_id, snapshot.revision)
    items, total = await second.list(pending_only=True)
    assert total == 1 and items[0].thread_id == "late-thread"
    assert not await first.mark_uploaded(item.invocation_id, 9999)


@pytest.mark.parametrize(
    "payload",
    [
        {"usage": {"input_tokens": True}},
        {"usage": {"input_tokens": 5, "cached_input_tokens": 6}},
        {"model": "invented-model"},
        {"prompt": "SECRET"},
    ],
    ids=["bool-token", "invalid-subset", "unobserved-model", "prompt-not-allowed"],
)
def test_receipt_whitelist(payload):
    with pytest.raises(ValueError):
        ExternalExecReceipt(receipt_id="turn:1", **payload)
