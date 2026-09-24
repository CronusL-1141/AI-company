"""Confirm or refire deliveries on unreliable channels (docs/user-notice-design.md §5.6)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from aiteam.clock import utc_now
from aiteam.services.notices import ledger, transcript

from .conftest import StubDetector, finding, request


def _transcript(home: Path, records: list[dict], *, name="t.jsonl") -> Path:
    path = home / ".claude" / "projects" / "-proj" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def _shown(content: str, stamp, event="SessionStart") -> dict:
    return {"type": "attachment", "timestamp": stamp.isoformat().replace("+00:00", "Z"),
            "attachment": {"type": "hook_system_message", "content": content,
                           "hookName": f"{event}:resume", "hookEvent": event}}


def _stubs():
    return [
        StubDetector("v", ["api_version_stale"], [finding("api_version_stale", "1:2", old="v1", ver="v2")]),
        StubDetector("r", ["release_available"], [finding(
            "release_available", "cc:1.15.0", ver="v1.15.0", old="v1.14.0", url="u")], timing=("session_start",)),
    ]


async def _resume_then_prompt(repo, path, now):
    stubs = _stubs()
    start = await ledger.pending(repo, request(source="resume"), registry=stubs, now=now)
    assert len(start.delivery_ids) == 2
    prompt = await ledger.pending(
        repo, request(event="UserPromptSubmit", emitted=start.delivery_ids).model_copy(
            update={"transcript_path": str(path)}),
        registry=stubs, now=now + timedelta(seconds=30),
    )
    return start, prompt


async def test_displayed_line_is_confirmed_and_not_refired(repo, isolated_home):
    now = utc_now()
    stubs = _stubs()
    start = await ledger.pending(repo, request(source="resume"), registry=stubs, now=now)
    path = _transcript(isolated_home, [_shown(start.user_text, now + timedelta(seconds=1))])
    prompt = await ledger.pending(
        repo, request(event="UserPromptSubmit", emitted=start.delivery_ids).model_copy(
            update={"transcript_path": str(path)}),
        registry=stubs, now=now + timedelta(seconds=30),
    )
    assert prompt.user_text == ""
    rows = await repo.list_notice_deliveries(session_id="s1")
    assert all(row.confirmed_at is not None and row.refired_at is None for row in rows)


async def test_missing_line_is_refired_once(repo, isolated_home):
    now = utc_now()
    path = _transcript(isolated_home, [_shown("[AI Team OS] something else", now)])
    start, prompt = await _resume_then_prompt(repo, path, now)
    assert "v1" in prompt.user_text and "v1.15.0" in prompt.user_text
    assert sorted(prompt.delivery_ids) == sorted(start.delivery_ids)
    assert prompt.model_text.startswith("AI Team OS just showed the user")
    again = await ledger.pending(
        repo, request(event="UserPromptSubmit", emitted=prompt.delivery_ids).model_copy(
            update={"transcript_path": str(path)}),
        registry=_stubs(), now=now + timedelta(seconds=60),
    )
    assert again.user_text == ""
    rows = await repo.list_notice_deliveries(session_id="s1")
    assert all(row.refired_at is not None for row in rows)
    # The refire went out on a prompt (a reliable exit) and was reported: confirmed.
    assert all(row.confirmed_at is not None for row in rows)


async def test_refired_release_line_starts_the_cool_down(repo, isolated_home):
    """Only confirmed deliveries start the 24h cool-down; a refired one must count."""
    now = utc_now()
    path = _transcript(isolated_home, [_shown("[AI Team OS] something else", now)])
    _, prompt = await _resume_then_prompt(repo, path, now)
    await ledger.pending(
        repo, request(event="UserPromptSubmit", emitted=prompt.delivery_ids).model_copy(
            update={"transcript_path": str(path)}),
        registry=_stubs(), now=now + timedelta(seconds=60),
    )
    other = await ledger.pending(repo, request(session="s2"), registry=_stubs(), now=now + timedelta(hours=1))
    assert "v1.15.0" not in other.user_text


async def test_old_display_before_the_claim_does_not_count(repo, isolated_home):
    now = utc_now()
    stubs = _stubs()
    start = await ledger.pending(repo, request(source="resume"), registry=stubs, now=now)
    path = _transcript(isolated_home, [_shown(start.user_text, now - timedelta(hours=2))])
    prompt = await ledger.pending(
        repo, request(event="UserPromptSubmit", emitted=start.delivery_ids).model_copy(
            update={"transcript_path": str(path)}),
        registry=stubs, now=now + timedelta(seconds=30),
    )
    assert "v1" in prompt.user_text


async def test_unreadable_transcript_refires_only_action_level(repo, isolated_home):
    now = utc_now()
    start, prompt = await _resume_then_prompt(repo, isolated_home / "missing.jsonl", now)
    assert "v1" in prompt.user_text  # api_version_stale is action level
    assert "v1.15.0" not in prompt.user_text  # release_available is status (info)


async def test_paths_outside_the_projects_folder_are_refused(isolated_home, tmp_path):
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(json.dumps(_shown("x", utc_now())) + "\n")
    assert await transcript.displayed_messages(str(outside), since=utc_now() - timedelta(hours=1)) is None
    escape = isolated_home / ".claude" / "projects" / ".." / "secret.jsonl"
    escape.parent.mkdir(parents=True, exist_ok=True)
    (isolated_home / ".claude" / "secret.jsonl").write_text("{}\n")
    assert await transcript.displayed_messages(str(escape), since=utc_now()) is None
    inside = _transcript(isolated_home, [_shown("[AI Team OS] hi", utc_now())])
    assert await transcript.displayed_messages(str(inside), since=utc_now() - timedelta(minutes=1)) == [
        "[AI Team OS] hi"]


async def test_claude_config_dir_moves_the_projects_folder(isolated_home, tmp_path, monkeypatch):
    config = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    path = config / "projects" / "p" / "t.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_shown("[AI Team OS] hi", utc_now())) + "\n")
    assert await transcript.displayed_messages(str(path), since=utc_now() - timedelta(minutes=1))
    default = _transcript(isolated_home, [_shown("[AI Team OS] hi", utc_now())])
    assert await transcript.displayed_messages(str(default), since=utc_now()) is None


async def test_ansi_in_the_recorded_message_is_ignored(isolated_home):
    stamp = utc_now()
    path = _transcript(isolated_home, [_shown("[AI Team OS] \x1b[33mbody\x1b[39m", stamp)])
    messages = await transcript.displayed_messages(str(path), since=stamp)
    assert transcript.was_displayed("[AI Team OS] body", messages)


async def test_only_the_tail_is_read(isolated_home):
    stamp = utc_now()
    filler = [{"type": "user", "pad": "x" * 1000} for _ in range(700)]
    path = _transcript(isolated_home, [_shown("[AI Team OS] early", stamp)] + filler
                       + [_shown("[AI Team OS] late", stamp)])
    messages = await transcript.displayed_messages(str(path), since=stamp - timedelta(minutes=1))
    assert messages == ["[AI Team OS] late"]


def _item_lines(response):
    return [line for line in response.user_text.split("\n") if line and " more item" not in line]


async def _prompt_with(repo, path, emitted, registry, now):
    return await ledger.pending(
        repo, request(event="UserPromptSubmit", emitted=list(emitted)).model_copy(
            update={"transcript_path": str(path)}),
        registry=registry, now=now,
    )


async def test_a_lost_action_line_is_refired_even_when_the_prompt_budget_is_spent(repo, isolated_home):
    """Losing a line is worse than one line over budget; status lines still wait for budget."""
    now = utc_now()
    prompt_items = StubDetector("p", ["decisions_pending", "api_version_stale", "channel_mention"], [
        finding("decisions_pending", "d1", n=1, title="t"),
        finding("api_version_stale", "1:2", old="v1", ver="v2"),
        finding("channel_mention", "r:p:t", sender="s", channel="c", n=1, details="d"),
    ], timing=("prompt",))
    start_items = StubDetector("s", ["installed_copy_stale", "release_available"], [
        finding("installed_copy_stale", "cc:aaaa", n=3),
        finding("release_available", "cc:1.15.0", ver="v1.15.0", old="v1.14.0", url="u"),
    ], timing=("session_start",))
    path = _transcript(isolated_home, [_shown("[AI Team OS] something else", now + timedelta(seconds=40))])

    assert (await ledger.pending(repo, request(), registry=[prompt_items], now=now)).user_text == ""
    emitted: list[str] = []
    for index in (1, 2):  # the prompt budget (3) goes to the three prompt items
        response = await _prompt_with(repo, path, emitted, [prompt_items], now + timedelta(seconds=index))
        emitted = response.delivery_ids
    compact = await ledger.pending(repo, request(source="compact", emitted=emitted), registry=[start_items],
                                   now=now + timedelta(seconds=30))
    assert len(compact.delivery_ids) == 2

    prompt = await _prompt_with(repo, path, compact.delivery_ids, [], now + timedelta(seconds=60))
    assert len(_item_lines(prompt)) == 1 and "3 installed hook/skill copies" in prompt.user_text
    assert "v1.15.0" not in prompt.user_text
    rows = {row.key: row for row in await repo.list_notice_deliveries(session_id="s1")}
    assert rows["installed_copy_stale:cc:aaaa"].refired_at is not None
    assert rows["release_available:cc:1.15.0"].refired_at is None


async def test_a_refire_uses_the_budget_before_new_lines(repo, isolated_home):
    """With one line of budget left, the lost line goes first even if a new one outranks it."""
    now = utc_now()
    path = _transcript(isolated_home, [_shown("[AI Team OS] something else", now + timedelta(seconds=40))])
    versions = StubDetector("v", ["api_version_stale"], [
        finding("api_version_stale", "1:2", old="v1", ver="v2"),
        finding("api_version_stale", "3:4", old="v3", ver="v4"),
    ], timing=("prompt",))
    first = await _prompt_with(repo, path, [], [versions], now)
    assert len(first.delivery_ids) == 2
    decision = StubDetector("d", ["decisions_pending"], [finding("decisions_pending", "d1", n=1, title="t")],
                            timing=("session_start",))
    compact = await ledger.pending(repo, request(source="compact", emitted=first.delivery_ids),
                                   registry=[decision], now=now + timedelta(seconds=30))
    assert len(compact.delivery_ids) == 1
    versions.findings = [finding("api_version_stale", "5:6", old="v5", ver="v6")]  # outranks a decision
    prompt = await _prompt_with(repo, path, compact.delivery_ids, [versions], now + timedelta(seconds=60))
    lines = _item_lines(prompt)
    assert len(lines) == 1 and "waiting for you" in lines[0] and "v5" not in prompt.user_text
