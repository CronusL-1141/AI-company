"""Claimed, written, confirmed, released and lost (docs/user-notice-design.md §5.6)."""

from __future__ import annotations

from datetime import timedelta

from aiteam.clock import utc_now
from aiteam.services.notices import ledger

from .conftest import StubDetector, finding, request


def _stub():
    return StubDetector("v", ["api_version_stale"], [finding("api_version_stale", "1:2", old="v1", ver="v2")])


async def test_unreported_claim_is_released_after_sixty_seconds(repo):
    stub, now = _stub(), utc_now()
    first = await ledger.pending(repo, request(), registry=[stub], now=now)
    assert first.delivery_ids
    soon = await ledger.pending(repo, request(event="UserPromptSubmit"), registry=[stub],
                                now=now + timedelta(seconds=30))
    assert soon.user_text == "", "a fresh claim still counts as delivered"
    late = await ledger.pending(repo, request(event="UserPromptSubmit"), registry=[stub],
                                now=now + timedelta(seconds=61))
    assert "v1" in late.user_text and late.delivery_ids == first.delivery_ids
    row = (await repo.list_notice_deliveries(session_id="s1"))[0]
    assert row.event == "UserPromptSubmit" and row.emitted_at is None


async def test_reported_delivery_is_not_shown_again(repo):
    stub, now = _stub(), utc_now()
    first = await ledger.pending(repo, request(), registry=[stub], now=now)
    reported = await ledger.pending(repo, request(event="UserPromptSubmit", emitted=first.delivery_ids),
                                    registry=[stub], now=now + timedelta(seconds=90))
    assert reported.user_text == ""
    row = (await repo.list_notice_deliveries(session_id="s1"))[0]
    assert row.emitted_at == now + timedelta(seconds=90)
    assert row.confirmed_at == row.emitted_at, "startup is a reliable channel"


async def test_unreliable_delivery_is_written_but_not_confirmed(repo):
    stub, now = _stub(), utc_now()
    first = await ledger.pending(repo, request(source="resume"), registry=[stub], now=now)
    await ledger.pending(repo, request(event="PostToolUse", emitted=first.delivery_ids),
                         registry=[stub], now=now + timedelta(seconds=5))
    row = (await repo.list_notice_deliveries(session_id="s1"))[0]
    assert row.channel_reliable is False and row.emitted_at is not None and row.confirmed_at is None


async def test_claims_never_reported_are_marked_lost_after_ten_minutes(repo):
    stub, now = _stub(), utc_now()
    await ledger.pending(repo, request(), registry=[stub], now=now)
    await ledger.pending(repo, request(event="PostToolUse", session="other"), registry=[stub],
                         now=now + timedelta(minutes=9))
    assert (await repo.list_notice_deliveries(session_id="s1"))[0].lost_at is None
    await ledger.pending(repo, request(event="PostToolUse", session="other"), registry=[stub],
                         now=now + timedelta(minutes=11))
    assert (await repo.list_notice_deliveries(session_id="s1"))[0].lost_at is not None


async def test_emitted_report_is_idempotent_and_keeps_the_first_time(repo):
    stub, now = _stub(), utc_now()
    first = await ledger.pending(repo, request(), registry=[stub], now=now)
    for offset in (10, 20):
        await ledger.pending(repo, request(event="PostToolUse", emitted=first.delivery_ids),
                             registry=[stub], now=now + timedelta(seconds=offset))
    row = (await repo.list_notice_deliveries(session_id="s1"))[0]
    assert row.emitted_at == now + timedelta(seconds=10)


async def test_emitted_records_in_the_local_file_count_too(repo):
    stub, now = _stub(), utc_now()
    first = await ledger.pending(repo, request(), registry=[stub], now=now)
    record = {"uuid": "e1", "kind": "emitted", "delivery_ids": first.delivery_ids}
    await ledger.pending(repo, request(event="PostToolUse", local_records=[record]), registry=[stub],
                         now=now + timedelta(seconds=3))
    assert (await repo.list_notice_deliveries(session_id="s1"))[0].emitted_at is not None


async def test_local_line_counts_as_confirmed_only_on_a_reliable_exit(repo):
    """A local line written at a resume start may never have been displayed (§5.6)."""
    now = utc_now()
    records = [
        {"uuid": f"l-{source}", "kind": "local_notice", "catalog_id": "api_down", "key": "api_down",
         "session_id": f"s-{source}", "event": f"SessionStart:{source}", "language": "zh", "displayed": True}
        for source in ("startup", "resume", "clear", "compact")
    ] + [{"uuid": "l-ups", "kind": "local_notice", "catalog_id": "api_down", "key": "api_down",
          "session_id": "s-ups", "event": "UserPromptSubmit", "language": "zh", "displayed": True}]
    await ledger.pending(repo, request(event="PostToolUse", local_records=records), registry=[], now=now)
    rows = {row.session_id: row for row in await repo.list_notice_deliveries(keys=["api_down"])}
    assert {sid: (row.channel_reliable, row.confirmed_at is not None) for sid, row in rows.items()} == {
        "s-startup": (True, True), "s-ups": (True, True),
        "s-resume": (False, False), "s-clear": (False, False), "s-compact": (False, False),
    }
    assert all(row.emitted_at is not None for row in rows.values())
