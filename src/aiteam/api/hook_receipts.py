"""Redelivery idempotency for hook events.

A hook client that times out cannot tell "never handled" from "handled, receipt
lost", and once the ingest middleware reads the body before queueing, the second
case is the common one. A retrying client (the planned CC replay queue) must
be able to send the same event again without it being recorded twice.

Events are keyed by (session_id, hook_event_name, tool_use_id). Events without a
tool_use_id carry no stable identity and are handled every time, as before.

Codex payloads are left out. Their handling already depends on seeing a packet
again: a re-sent event with a newer source observation may revive an agent that a
stale one could not, and completion replays are correlated by tool_call_id in the
translator. Deduplicating them here would change that contract, so it stays with
the Codex adapter.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

from fastapi.encoders import jsonable_encoder

from aiteam.clock import utc_now
from aiteam.storage.repository import StorageRepository
from aiteam.types import HarnessId

logger = logging.getLogger(__name__)

# A claim still unanswered after this long belongs to a delivery that died between
# claim and completion. Longer than any handler run observed under lock contention
# (30s busy_timeout per statement), short enough for a replay queue to recover.
CLAIM_STALE_AFTER = timedelta(seconds=120)
# Receipts only need to outlive the redelivery horizon. Expired ones are pruned by
# the reaper's hourly housekeeping (``state_reaper``), never on a hook request.
RECEIPT_RETENTION = timedelta(days=7)


def receipt_key(data: dict) -> tuple[str, str, str] | None:
    """The dedupe key of a hook payload, or None when it has no stable identity."""
    if data.get("harness") == HarnessId.CODEX:
        return None
    parts = (data.get("session_id"), data.get("hook_event_name"), data.get("tool_use_id"))
    if all(isinstance(part, str) and part for part in parts):
        return parts  # type: ignore[return-value]
    return None


async def handle_once(
    repo: StorageRepository, data: dict, handler: Callable[[dict], Awaitable[dict]],
) -> dict:
    """Run ``handler`` at most once per receipt key and replay its response after that.

    A duplicate gets the first delivery's response with ``duplicate: true``; while the
    first delivery is still being handled it gets ``status: duplicate``. Receipt
    storage failures never block the event: dedupe is skipped and the event handled.
    """
    key = receipt_key(data)
    if key is None:
        return await handler(data)
    try:
        claim = await repo.claim_hook_receipt(*key, stale_before=utc_now() - CLAIM_STALE_AFTER)
    except Exception:  # noqa: BLE001 - losing dedupe beats losing the event
        logger.warning("hook receipt claim failed, handling without dedupe", exc_info=True)
        return await handler(data)
    if not claim.claimed:
        if claim.response is None:
            return {"status": "duplicate", "reason": "in_flight", "duplicate": True}
        return {**claim.response, "duplicate": True}

    try:
        result = await handler(data)
    except Exception:
        try:
            await repo.release_hook_receipt(*key, token=claim.token)
        except Exception:  # noqa: BLE001 - the claim then goes stale and is taken over
            logger.warning("hook receipt release failed", exc_info=True)
        raise
    try:
        await repo.complete_hook_receipt(*key, token=claim.token, response=jsonable_encoder(result))
    except Exception:  # noqa: BLE001 - the event is recorded; only replay of the answer is lost
        logger.warning("hook receipt completion failed", exc_info=True)
    return result
