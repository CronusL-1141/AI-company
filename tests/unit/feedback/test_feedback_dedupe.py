"""The same text is never published twice, the rate limit holds, and an unknown send is settled by reading,
through gh and through OS's App token alike (``any_stack``).

Duplicates are checked across a new app instance on the same database (the claim
lives in the database, not in memory) and under 48 concurrent submissions.
"""

from __future__ import annotations

import asyncio

import pytest

from aiteam.feedback import forms
from aiteam.feedback.github import Timeouts

from .conftest import BUG, IDEA, authorize, close_stack, open_stack, without_gh

CONCURRENCY = 48


async def _sent(stack, **body) -> dict:
    draft = await stack.draft(**body)
    response = await stack.submit(draft["draft_id"], draft["content_hash"])
    assert response.json()["status"] == "sent", response.text
    return draft


async def test_the_same_text_again_answers_the_post_even_after_a_restart(any_stack, tmp_path, monkeypatch) -> None:
    stack = any_stack
    first = await _sent(stack, **BUG)
    url = stack.ledger(first["draft_id"])["remote_url"]
    again = await stack.draft(**{**BUG, "title": BUG["title"].upper() + "  "})  # case and spacing do not count
    assert (again["status"], again["remote_url"]) == ("duplicate", url)
    restarted = await open_stack(tmp_path, monkeypatch, stack.gh, database=stack.database)
    try:
        assert (await restarted.draft(**BUG))["remote_url"] == url
    finally:
        await close_stack(restarted)
    assert len(stack.gh.repo()["issues"]) == 1


async def test_one_draft_submitted_48_times_at_once_is_published_once(any_stack) -> None:
    stack = any_stack
    draft = await stack.draft(**BUG)
    answers = await asyncio.gather(*(stack.submit(draft["draft_id"], draft["content_hash"])
                                     for _ in range(CONCURRENCY)))
    published = [a.json() for a in answers if a.status_code == 200 and a.json()["status"] == "sent"]
    assert sum(1 for a in published if not a.get("already")) == 1  # the rest answer the post, or 409 while sending
    assert len(stack.gh.repo()["issues"]) == 1 and len(stack.gh.calls("create_issue")) == 1


async def test_48_drafts_of_one_text_submitted_at_once_are_published_once(any_stack) -> None:
    stack = any_stack
    drafts = await asyncio.gather(*(stack.draft(**IDEA) for _ in range(CONCURRENCY)))
    assert len({d["draft_id"] for d in drafts}) == CONCURRENCY and len({d["content_hash"] for d in drafts}) == 1
    answers = await asyncio.gather(*(stack.submit(d["draft_id"], d["content_hash"]) for d in drafts))
    statuses = [a.json().get("status") for a in answers]
    assert statuses.count("sent") == 1 and statuses.count("duplicate") == CONCURRENCY - 1, statuses
    assert len(stack.gh.repo()["discussions"]) == 1 and len(stack.gh.calls("CreateDiscussion")) == 1
    assert stack.rows("SELECT count(*) FROM feedback_submissions WHERE claim_hash != ''") == [(1,)]


async def test_a_copy_filed_from_another_machine_is_found_by_its_marker(any_stack) -> None:
    stack = any_stack
    draft = await stack.draft(**BUG)
    stack.gh.add_issue("Filed elsewhere", body=f"text\n{forms.marker('bug_report', 'other000', draft['content_hash'])}")
    answer = (await stack.submit(draft["draft_id"], draft["content_hash"])).json()
    assert answer["status"] == "duplicate" and answer["remote_url"].endswith("/issues/1")
    assert not stack.gh.calls("create_issue")
    assert (stack.ledger(draft["draft_id"])["status"], stack.ledger(draft["draft_id"])["claim_hash"]) == ("failed", "")


async def test_the_sixth_post_in_24_hours_is_refused(any_stack) -> None:
    stack = any_stack
    for n in range(5):
        await _sent(stack, **{**BUG, "title": f"Broken button number {n}"})
    sixth = await stack.draft(**{**BUG, "title": "Broken button number 6"})
    assert sixth["quota"] == {"used": 5, "limit": 5}
    response = await stack.submit(sixth["draft_id"], sixth["content_hash"])
    assert response.status_code == 429
    assert stack.ledger(sixth["draft_id"])["status"] == "draft" and len(stack.gh.repo()["issues"]) == 5
    stack.clock.advance(hours=24, seconds=1)
    later = await stack.draft(**{**BUG, "title": "Broken button number 7"})
    assert (await stack.submit(later["draft_id"], later["content_hash"])).json()["status"] == "sent"


async def test_a_draft_expires_after_30_minutes(stack) -> None:
    draft = await stack.draft(**BUG)
    stack.clock.advance(minutes=30, seconds=1)
    assert (await stack.submit(draft["draft_id"], draft["content_hash"])).status_code == 410
    assert stack.ledger(draft["draft_id"])["status"] == "expired"
    other = await stack.draft(**IDEA)
    stack.clock.advance(minutes=31)
    await stack.client.get("/api/feedback")
    assert stack.ledger(other["draft_id"])["status"] == "expired"
    assert not stack.gh.calls("create_issue") and not stack.gh.calls("CreateDiscussion")


async def test_changing_one_character_is_a_new_draft_and_the_old_confirmation_does_not_fit(stack) -> None:
    first = await stack.draft(**BUG)
    second = await stack.draft(**{**BUG, "body": BUG["body"] + "!"})
    assert first["draft_id"] != second["draft_id"] and first["content_hash"] != second["content_hash"]
    assert (await stack.submit(second["draft_id"], first["content_hash"])).status_code == 409
    assert not stack.gh.calls("create_issue")


async def test_a_handed_off_text_is_not_blocked_but_the_link_is_mentioned(stack) -> None:
    first = await stack.draft(**BUG)
    await stack.client.post(f"/api/feedback/drafts/{first['draft_id']}/handoff", json={"reason": "web"})
    again = await stack.draft(**BUG)
    assert again["status"] == "draft" and again["previous_handoff"]["draft_id"] == first["draft_id"]


async def _timing_out(tmp_path, monkeypatch, fake_gh, via: str):
    stack = await open_stack(tmp_path, monkeypatch, fake_gh, timeouts=Timeouts(login=20, lookup=20, search=20,
                                                                                send=1))
    if via == "app":  # over HTTP a hung call times out at once: nothing waits
        without_gh(monkeypatch)
        await authorize(stack.auth)
    return stack


@pytest.mark.parametrize("via", ["gh", "app"])
async def test_a_send_that_times_out_is_unknown_never_retried_and_settled_when_read(tmp_path, monkeypatch,
                                                                                   fake_gh, via) -> None:
    stack = await _timing_out(tmp_path, monkeypatch, fake_gh, via)
    try:
        fake_gh.set(hang={"create_issue": "after"})  # created on GitHub, the answer never comes
        draft = await stack.draft(**BUG)
        answer = (await stack.submit(draft["draft_id"], draft["content_hash"])).json()
        assert answer["status"] == "unknown"
        assert stack.ledger(draft["draft_id"])["status"] == "unknown"
        fake_gh.set(hang={})
        (row,) = (await stack.client.get("/api/feedback")).json()["data"]  # reading settles it
        (issue,) = fake_gh.repo()["issues"]
        assert (row["status"], row["remote_url"]) == ("sent", issue["html_url"])
        (event,) = stack.events("feedback.submitted")
        assert (event["reconciled"], event["remote_url"]) == (True, issue["html_url"])
        assert (await stack.draft(**BUG))["remote_url"] == issue["html_url"]
        assert len(fake_gh.calls("create_issue")) == 1
    finally:
        await close_stack(stack)


@pytest.mark.parametrize("via", ["gh", "app"])
async def test_an_unknown_send_that_never_landed_fails_after_10_minutes(tmp_path, monkeypatch, fake_gh, via) -> None:
    stack = await _timing_out(tmp_path, monkeypatch, fake_gh, via)
    try:
        fake_gh.set(hang={"create_issue": "before"})  # never reached GitHub
        draft = await stack.draft(**BUG)
        assert (await stack.submit(draft["draft_id"], draft["content_hash"])).json()["status"] == "unknown"
        fake_gh.set(hang={})
        assert (await stack.draft(**BUG))["status"] == "pending"  # not on GitHub yet: neither drafted nor resent
        await stack.client.get("/api/feedback")
        assert stack.ledger(draft["draft_id"])["status"] == "unknown"
        stack.clock.advance(minutes=10, seconds=1)
        await stack.client.get("/api/feedback")
        row = stack.ledger(draft["draft_id"])
        assert (row["status"], row["claim_hash"]) == ("failed", "")
        assert (await stack.draft(**BUG))["status"] == "draft"  # the text may be filed again
        assert fake_gh.repo()["issues"] == [] and len(fake_gh.calls("create_issue")) == 1
    finally:
        await close_stack(stack)
