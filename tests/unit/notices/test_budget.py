"""Per-output and per-session line budgets (docs/user-notice-design.md §5.6)."""

from __future__ import annotations

from datetime import timedelta

from aiteam.clock import utc_now
from aiteam.services.notices import ledger

from .conftest import StubDetector, finding, request


def _three_actions():
    # Session-start-only entries, all action level.
    return StubDetector("three", ["installed_copy_stale", "codex_copy_stale", "api_version_stale"], [
        finding("installed_copy_stale", "cc:aaaa", n=3),
        finding("codex_copy_stale", "bbbb", n=2),
        finding("api_version_stale", "1:2", old="v1", ver="v2"),
    ], timing=("session_start",))


def _lines(response):
    return [line for line in response.user_text.split("\n") if line]


async def test_three_candidates_give_two_lines_plus_summary_then_the_rest_at_the_first_prompt(repo):
    stub = _three_actions()
    start = await ledger.pending(repo, request(), registry=[stub])
    lines = _lines(start)
    assert len(lines) == 3 and len(start.delivery_ids) == 2
    assert "1 more items are pending" in lines[-1]
    assert start.model_text.count("notice_list()") == 1
    prompt = await ledger.pending(repo, request(event="UserPromptSubmit", emitted=start.delivery_ids),
                                  registry=[stub])
    assert len(_lines(prompt)) == 1 and len(prompt.delivery_ids) == 1
    again = await ledger.pending(repo, request(event="UserPromptSubmit", emitted=prompt.delivery_ids),
                                 registry=[stub])
    assert again.user_text == ""


async def test_session_start_budget_is_shared_by_all_sources(repo):
    stub = _three_actions()
    first = await ledger.pending(repo, request(), registry=[stub])
    assert len(first.delivery_ids) == 2
    compact = await ledger.pending(repo, request(source="compact", emitted=first.delivery_ids), registry=[stub])
    assert compact.user_text == "" and compact.delivery_ids == []


def _many(prefix_count=8):
    return StubDetector("many", ["decisions_pending", "api_version_stale", "installed_copy_stale",
                                 "codex_copy_stale", "host_version_mismatch"], [
        finding("decisions_pending", "d1", n=1, title="a"),
        finding("api_version_stale", "1:2", old="v1", ver="v2"),
        finding("installed_copy_stale", "cc:1", n=1),
        finding("codex_copy_stale", "c1", n=1),
        finding("host_version_mismatch", "a:b", cc="v1", cx="v2"),
    ], timing=("session_start", "prompt"))


async def test_prompt_budget_and_session_total(repo):
    stub = _many()
    now = utc_now()
    shown = 0
    emitted: list[str] = []
    # Session start: 2; prompts: at most 3 in total; the session never exceeds 5.
    start = await ledger.pending(repo, request(), registry=[stub], now=now)
    shown += len(start.delivery_ids)
    emitted = start.delivery_ids
    for index in range(6):
        response = await ledger.pending(
            repo, request(event="UserPromptSubmit", emitted=emitted), registry=[stub],
            now=now + timedelta(seconds=index + 1),
        )
        assert len(response.delivery_ids) <= ledger.PER_OUTPUT
        shown += len(response.delivery_ids)
        emitted = response.delivery_ids
    assert shown == 5


async def test_immediate_and_local_lines_do_not_use_the_budget(repo):
    stub = _three_actions()
    records = [
        {"uuid": f"u{i}", "kind": "local_notice", "catalog_id": "blocked_foreign_branch",
         "key": f"blocked_foreign_branch:s1:b{i}", "params": {"branch": f"b{i}"}, "session_id": "s1",
         "event": "PreToolUse"}
        for i in range(4)
    ]
    response = await ledger.pending(repo, request(local_records=records), registry=[stub])
    assert len(response.delivery_ids) == 2
    assert "Blocked" not in response.user_text  # imported, never re-shown by the API


async def test_other_events_get_zero_lines(repo):
    stub = _three_actions()
    response = await ledger.pending(repo, request(event="PostToolUse"), registry=[stub])
    assert response.user_text == "" and response.model_text == ""
    assert stub.calls == 0


async def test_one_output_never_carries_more_than_two_item_lines(repo):
    """At a prompt the event budget is 3, so the per-output cap of 2 is what binds."""
    stub = StubDetector("p", ["decisions_pending", "api_version_stale", "channel_mention"], [
        finding("decisions_pending", "d1", n=1, title="a"),
        finding("api_version_stale", "1:2", old="v1", ver="v2"),
        finding("channel_mention", "r:p:t", sender="s", channel="c", n=1, details="d"),
    ], timing=("prompt",))
    response = await ledger.pending(repo, request(event="UserPromptSubmit"), registry=[stub])
    lines = _lines(response)
    assert len(response.delivery_ids) == 2 and len(lines) == 3
    assert "1 more items are pending" in lines[-1]


def _spend_the_session_budget():
    """Four start-only action items and one prompt item: 2 + 2 + 1 = 5 lines."""
    start = StubDetector("s", ["installed_copy_stale", "codex_copy_stale", "host_version_mismatch",
                               "codex_untrusted"], [
        finding("installed_copy_stale", "cc:aaaa", n=3), finding("codex_copy_stale", "bbbb", n=2),
        finding("host_version_mismatch", "a:b", cc="v1", cx="v2"), finding("codex_untrusted", "cccc"),
    ], timing=("session_start",))
    version = StubDetector("v", ["api_version_stale"], [finding("api_version_stale", "1:2", old="v1", ver="v2")],
                           timing=("prompt",))
    return start, version


def _mention(stamp: str, sender: str = "bob"):
    details = (f'- channel="global" sender="{sender}" count=2 latest="ping"\n'
               '  channel_read(channel="global") then channel_read_ack(channel="global", reader="leader-cc", '
               'project_id="p1", last_read_at=<created_at of the last message you read>)')
    from aiteam.services.notices.detectors import Finding

    return StubDetector("c", ["channel_mention"], [Finding(
        catalog_id="channel_mention", key=f"channel_mention:leader-cc:p1:{stamp}", project_id="p1",
        params={"sender": sender, "channel": "global", "n": 2, "details": details})],
        timing=("prompt",), prefixes=("channel_mention:leader-cc:p1:",), project_id="p1")


def _prompt(emitted):
    return request(event="UserPromptSubmit", emitted=list(emitted)).model_copy(
        update={"reader": "leader-cc", "project_id": "p1"})


async def test_a_mention_after_the_budget_is_spent_still_reaches_the_model(repo):
    """The line budget limits what the user sees; the model still gets new mentions."""
    start, version = _spend_the_session_budget()
    shown = await ledger.pending(repo, request(), registry=[start])
    emitted = shown.delivery_ids
    for registry in ([], [version]):
        response = await ledger.pending(repo, _prompt(emitted), registry=registry)
        emitted = response.delivery_ids
    assert len(await repo.list_notice_deliveries(session_id="s1")) == 5

    mention = _mention("2026-09-23T10:00:00")
    first = await ledger.pending(repo, _prompt(emitted), registry=[mention])
    assert first.user_text == "" and first.delivery_ids == []
    assert first.model_text.startswith("AI Team OS did not show the user this notice")
    assert "bob mentioned you in global" in first.model_text
    assert 'channel_read_ack(channel="global", reader="leader-cc"' in first.model_text
    # Still within the user budget: nothing new was claimed (read back from the database).
    assert len(await repo.list_notice_deliveries(session_id="s1")) == 5

    later = [await ledger.pending(repo, _prompt([]), registry=[mention]) for _ in range(6)]
    assert all(response.user_text == "" for response in later)
    assert not any("channel_read(" in response.model_text for response in later), "full note only once"
    assert any("[channel unread]" in response.model_text for response in later)

    newer = await ledger.pending(repo, _prompt([]), registry=[_mention("2026-09-23T11:00:00", "amy")])
    assert "amy mentioned you" in newer.model_text and "channel_read(" in newer.model_text


async def test_a_held_line_the_model_does_not_act_on_stays_quiet(repo):
    """Only tell_model_when_held entries bypass the budget toward the model."""
    start, version = _spend_the_session_budget()
    shown = await ledger.pending(repo, request(), registry=[start])
    emitted = shown.delivery_ids
    for registry in ([], [version]):
        response = await ledger.pending(repo, _prompt(emitted), registry=registry)
        emitted = response.delivery_ids
    decisions = StubDetector("d", ["decisions_pending"], [finding("decisions_pending", "d1", n=1, title="t")],
                             timing=("prompt",))
    quiet = await ledger.pending(repo, _prompt(emitted), registry=[decisions])
    assert quiet.user_text == "" and quiet.model_text == ""
