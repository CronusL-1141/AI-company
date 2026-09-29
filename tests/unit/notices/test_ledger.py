"""Ledger dedup, clearing and atomic claims (docs/user-notice-design.md §5.4, §5.6)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from aiteam.clock import utc_now
from aiteam.services.notices import ledger
from aiteam.types import NoticeDelivery, NoticeStatus

from .conftest import StubDetector, finding, request


async def _fetch(repo, registry, **kwargs):
    now = kwargs.pop("now", None)
    return await ledger.pending(repo, request(**kwargs), registry=registry, now=now)


async def _report(repo, response, registry, session="s1", now=None):
    """The hook's next fetch reports what it wrote (facts.emitted)."""
    return await _fetch(repo, registry, event="UserPromptSubmit", session=session,
                        emitted=response.delivery_ids, now=now)


async def test_per_session_shows_once_per_session_and_again_in_a_new_one(repo):
    stub = StubDetector("v", ["api_version_stale"], [finding("api_version_stale", "1:2", old="v1", ver="v2")])
    first = await _fetch(repo, [stub])
    assert "v1" in first.user_text and len(first.delivery_ids) == 1
    await _report(repo, first, [stub])
    again = await _fetch(repo, [stub], source="compact")
    assert again.user_text == ""
    other = await _fetch(repo, [stub], session="s2")
    assert "v1" in other.user_text


async def test_once_is_across_sessions_of_the_host_and_never_crosses_hosts(repo):
    stub = StubDetector("c", ["channel_mention"], [finding(
        "channel_mention", "leader-cc:p:t1", sender="bob", channel="global", n=1, details="d")],
        timing=("prompt",))
    first = await _fetch(repo, [stub], event="UserPromptSubmit")
    assert "bob" in first.user_text
    await _report(repo, first, [stub])
    assert (await _fetch(repo, [stub], event="UserPromptSubmit", session="s2")).user_text == ""
    # The stub named no host: the mention belongs to the host whose request found it.
    assert (await repo.get_notice("channel_mention:leader-cc:p:t1")).host == "cc"
    codex = await _fetch(repo, [], event="UserPromptSubmit", session="x1", host="codex")
    assert "bob" not in codex.user_text and "bob" not in codex.model_text


async def test_cooldown_counts_only_confirmed_deliveries(repo):
    stub = StubDetector("r", ["release_available"], [finding(
        "release_available", "cc:1.15.0", ver="v1.15.0", old="v1.14.0", url="u")], timing=("session_start",))
    now = utc_now()
    # A resume delivery (unreliable channel) that was written but never confirmed.
    resumed = await _fetch(repo, [stub], source="resume", now=now)
    assert resumed.delivery_ids
    await _report(repo, resumed, [stub], now=now + timedelta(seconds=5))
    fresh = await _fetch(repo, [stub], session="s2", now=now + timedelta(minutes=2))
    assert "v1.15.0" in fresh.user_text, "an unconfirmed delivery must not start the cooldown"
    await _report(repo, fresh, [stub], session="s2", now=now + timedelta(minutes=3))
    blocked = await _fetch(repo, [stub], session="s3", now=now + timedelta(hours=23))
    assert blocked.user_text == ""
    later = await _fetch(repo, [stub], session="s4", now=now + timedelta(hours=25))
    assert "v1.15.0" in later.user_text


async def test_superseded_key_is_cleared_when_a_new_one_appears(repo):
    stub = StubDetector("d", ["decisions_pending"], [finding("decisions_pending", "aaaa1111", n=1, title="t")])
    await _fetch(repo, [stub])
    stub.findings = [finding("decisions_pending", "bbbb2222", n=2, title="t2")]
    await _fetch(repo, [stub], session="s2")
    old = await repo.get_notice("decisions_pending:aaaa1111")
    new = await repo.get_notice("decisions_pending:bbbb2222")
    assert old.status == NoticeStatus.CLEARED and new.status == NoticeStatus.ACTIVE
    stub.findings = []
    await _fetch(repo, [stub], session="s3")
    assert (await repo.get_notice("decisions_pending:bbbb2222")).status == NoticeStatus.CLEARED


async def test_cleared_key_revives_with_a_new_first_seen(repo):
    stub = StubDetector("v", ["api_version_stale"], [finding("api_version_stale", "1:2", old="v1", ver="v2")])
    now = utc_now()
    await _fetch(repo, [stub], now=now)
    stub.findings = []
    await _fetch(repo, [stub], session="s2", now=now + timedelta(minutes=1))
    stub.findings = [finding("api_version_stale", "1:2", old="v1", ver="v2")]
    await _fetch(repo, [stub], session="s3", now=now + timedelta(minutes=2))
    row = await repo.get_notice("api_version_stale:1:2")
    assert row.status == NoticeStatus.ACTIVE and row.first_seen_at == now + timedelta(minutes=2)


@pytest.mark.parametrize("failure", ["timeout", "error"])
async def test_failed_detector_clears_nothing(repo, failure):
    stub = StubDetector("v", ["api_version_stale"], [finding("api_version_stale", "1:2", old="v1", ver="v2")])
    await _fetch(repo, [stub])
    stub.findings = []
    if failure == "timeout":
        stub.delay, stub.timeout_s = 1.0, 0.05
    else:
        stub.error = RuntimeError("probe failed")
    await _fetch(repo, [stub], session="s2")
    assert (await repo.get_notice("api_version_stale:1:2")).status == NoticeStatus.ACTIVE


async def test_invalid_findings_are_rejected_by_the_catalog_check(repo):
    stub = StubDetector("v", ["api_version_stale"], [
        finding("api_version_stale", "1:2", old="v1", ver="v2", surprise="x"),
    ])
    await _fetch(repo, [stub])
    assert await repo.get_notice("api_version_stale:1:2") is None
    with pytest.raises(ValueError):
        await ledger.register(repo, key="free_text:1", catalog_id="free_text")
    with pytest.raises(ValueError):
        await ledger.register(repo, key="api_version_stale:x", catalog_id="api_version_stale", variant="nope")
    with pytest.raises(ValueError):
        await ledger.register(repo, key="more_pending:1", catalog_id="more_pending")


async def test_scope_limits_clearing_to_its_own_project(repo):
    one = StubDetector("d", ["decisions_pending"], [finding("decisions_pending", "p1", n=1, title="a")],
                       project_id="")
    await _fetch(repo, [one])
    other = StubDetector("d", ["decisions_pending"], [], project_id="project-b")
    await _fetch(repo, [other], session="s2")
    assert (await repo.get_notice("decisions_pending:p1")).status == NoticeStatus.ACTIVE


CONCURRENCY = 48


async def _race(repo, session="s1"):
    delivery = lambda: NoticeDelivery(  # noqa: E731
        key="api_version_stale:1:2", host="cc", session_id=session, event="SessionStart:startup",
    )
    now = utc_now()
    results = await asyncio.gather(*(
        repo.claim_notice_delivery(delivery(), inflight_after=now - timedelta(seconds=60))
        for _ in range(CONCURRENCY)
    ))
    return sum(results)


async def test_forty_eight_concurrent_claims_win_exactly_once(repo):
    assert await _race(repo) == 1
    rows = await repo.list_notice_deliveries(session_id="s1")
    assert len(rows) == 1


async def test_race_has_teeth_without_the_unique_index(repo):
    """Reverse check: drop the claim index and the same race hands out duplicates."""
    from sqlalchemy import text

    from aiteam.storage.connection import get_session

    async with get_session(repo._db_url) as session:
        await session.execute(text("DROP INDEX uq_notice_deliveries_claim"))
    assert await _race(repo) > 1


async def test_forty_eight_concurrent_fetches_show_a_notice_once(repo):
    """Two hook copies (or Codex SessionStart plus UPS) racing through the full pipeline."""
    stub = StubDetector("v", ["api_version_stale"], [finding("api_version_stale", "1:2", old="v1", ver="v2")])
    responses = await asyncio.gather(*(_fetch(repo, [stub]) for _ in range(CONCURRENCY)))
    assert sum("v1" in response.user_text for response in responses) == 1


async def test_once_claim_guard_is_atomic_across_sessions(repo):
    now = utc_now()

    async def claim(session):
        return await repo.claim_notice_delivery(
            NoticeDelivery(key="channel_mention:r:p:t", host="cc", session_id=session, event="UserPromptSubmit"),
            inflight_after=now - timedelta(seconds=60), once=True,
        )

    results = await asyncio.gather(*(claim(f"s{i}") for i in range(CONCURRENCY)))
    assert sum(results) == 1


# --- host-bound keys (a release command or a reader belongs to one host) ---


def _lines(response):
    return [line for line in response.user_text.split("\n") if line]


async def test_release_line_reaches_only_the_host_it_was_found_for(repo, tmp_path, monkeypatch, isolated_home):
    """A Codex session must not get the Claude Code plugin command (and vice versa)."""
    import httpx

    from aiteam.api import release_updates
    from aiteam.services.notices.detectors.release import ReleaseDetector

    from .conftest import write_json

    write_json(isolated_home / ".claude/plugins/installed_plugins.json",
               {"plugins": {"ai-team-os@m": [{"version": "1"}]}})
    monkeypatch.setattr(release_updates, "release_checker", release_updates.ReleaseChecker(
        tmp_path / "r.json", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"tag_name": "v99.1.0", "draft": False, "prerelease": False}))))
    registry = [ReleaseDetector()]

    cc = await _fetch(repo, registry)
    assert len(_lines(cc)) == 1 and "claude plugin update" in cc.user_text

    codex = await _fetch(repo, registry, session="x1", host="codex")
    assert len(_lines(codex)) == 1, codex.user_text
    assert "codex_adapter.py upgrade" in codex.user_text and "claude plugin" not in codex.user_text
    assert "claude plugin" not in codex.model_text

    # The audience is stored on the row (read back from the database).
    rows, _ = await repo.list_notices(statuses=["active"], catalog_ids=["release_available"])
    assert {row.key: row.host for row in rows} == {
        "release_available:cc:99.1.0": "cc", "release_available:codex:99.1.0": "codex"}

    other_cc = await _fetch(repo, registry, session="s2", now=utc_now() + timedelta(minutes=5))
    assert "codex_adapter" not in other_cc.user_text


async def test_a_mention_for_the_cc_reader_stays_out_of_codex_sessions(repo):
    from aiteam.services.notices.detectors.channels import ChannelMentionDetector

    await repo.create_channel_message(channel="global", sender="bob", content="ping",
                                      mentions=["leader-cc"], project_id="p1")
    registry = [ChannelMentionDetector()]

    def prompt(host, reader, session):
        return request(event="UserPromptSubmit", session=session, host=host).model_copy(
            update={"reader": reader, "project_id": "p1"})

    cc = await ledger.pending(repo, prompt("cc", "leader-cc", "s1"), registry=registry)
    assert "bob mentioned you" in cc.user_text
    codex = await ledger.pending(repo, prompt("codex", "leader-codex", "x1"), registry=registry)
    assert codex.user_text == "" and 'reader="leader-cc"' not in codex.model_text


async def test_a_host_outside_the_entry_audience_is_rejected(repo):
    from aiteam.services.notices.detectors import Finding

    stub = StubDetector("s", ["installed_copy_stale"], [Finding(
        catalog_id="installed_copy_stale", key="installed_copy_stale:cc:1", params={"n": 1}, host="codex")])
    await _fetch(repo, [stub])
    assert await repo.get_notice("installed_copy_stale:cc:1") is None
    with pytest.raises(ValueError):
        await ledger.register(repo, key="installed_copy_stale:cc:1", catalog_id="installed_copy_stale",
                              params={"n": 1}, host="codex")
    with pytest.raises(ValueError):  # a per-host entry needs its host
        await ledger.register(repo, key="release_available:codex:9", catalog_id="release_available",
                              variant="codex", params={"ver": "v9"})
    # A row written before it had an audience gets one on its next refresh.
    await repo.upsert_notice(key="release_available:codex:9", catalog_id="release_available", now=utc_now(),
                             variant="codex", params={"ver": "v9"})
    row = await ledger.register(repo, key="release_available:codex:9", catalog_id="release_available",
                                variant="codex", params={"ver": "v9"}, host="codex")
    assert (await repo.get_notice(row.key)).host == "codex"


async def test_a_per_host_finding_without_a_host_belongs_to_the_requesting_host(repo):
    """Same shape as the L2 probe: detectors that name no host still cannot leak across hosts."""
    cc = StubDetector("r", ["release_available"], [ledger_finding(
        "release_available", "cc:9.0.0", "cc_plugin", ver="v9.0.0", old="v1.0.0", url="u")],
        timing=("session_start",), prefixes=("release_available:cc:",))
    codex = StubDetector("r", ["release_available"], [ledger_finding(
        "release_available", "codex:9.0.0", "codex", ver="v9.0.0", old="v1.0.0", url="u")],
        timing=("session_start",), prefixes=("release_available:codex:",))
    first = await _fetch(repo, [cc])
    assert "claude plugin update" in first.user_text
    other = await _fetch(repo, [codex], session="x1", host="codex")
    assert _lines(other) == [line for line in _lines(other) if "codex_adapter.py upgrade" in line]
    assert len(_lines(other)) == 1


def test_per_host_entries_are_the_host_specific_ones():
    from aiteam.services.notices.catalog import CATALOG

    assert {entry.id for entry in CATALOG.values() if entry.per_host} == {
        "release_available", "channel_mention", "host_version_mismatch",
    }


def ledger_finding(catalog_id, suffix, variant="", **params):
    from aiteam.services.notices.detectors import Finding

    return Finding(catalog_id=catalog_id, key=f"{catalog_id}:{suffix}", params=params, variant=variant)


async def test_a_fresh_refresh_binds_per_host_hits_to_the_asking_host(repo):
    stub = StubDetector("r", ["release_available"], [ledger_finding(
        "release_available", "codex:9.0.0", "codex", ver="v9.0.0", old="v1.0.0", url="u")],
        timing=("demand",), prefixes=("release_available:codex:",))
    await ledger.refresh(repo, host="codex", registry=[stub])
    assert (await repo.get_notice("release_available:codex:9.0.0")).host == "codex"
