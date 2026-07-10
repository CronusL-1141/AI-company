"""v1.5.0-C — Unit tests for the meeting → ecosystem writeback hook.

Covers the pure-function helpers exposed in
``aiteam.hooks.meeting_ecosystem_writeback``:

- ``topic_matches_ecosystem``: detect ecosystem keywords in meeting topics.
- ``extract_meeting_id``: pull meeting_id from PostToolUse payloads.
- ``build_reminder``: render the human-readable reminder string.
- ``main``: end-to-end stdin → stdout (network calls mocked).

The hook itself only emits stdout (never blocks), so tests verify
detection logic and rendered output rather than tool side effects.
"""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest

from aiteam.hooks import meeting_ecosystem_writeback as hook

# ============================================================
# Pure-function helpers
# ============================================================


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("讨论生态库选型", ["生态", "生态库"]),
        ("Ecosystem candidate review", ["ecosystem"]),
        ("深扫结果合议", ["深扫"]),
        ("常规站会", []),
        ("", []),
        ("Sprint planning for Q3", []),
        ("DEEP REVIEW: deep_review row update", ["deep review", "deep_review"]),
    ],
)
def test_topic_matches_ecosystem(topic: str, expected: list[str]) -> None:
    matched = hook.topic_matches_ecosystem(topic)
    assert sorted(matched) == sorted(expected)


def test_extract_meeting_id_from_tool_input() -> None:
    payload = {
        "tool_name": "mcp__ai-team-os__meeting_conclude",
        "tool_input": {"meeting_id": "m-abc"},
    }
    assert hook.extract_meeting_id(payload) == "m-abc"


def test_extract_meeting_id_from_response() -> None:
    payload = {
        "tool_name": "mcp__ai-team-os__meeting_conclude",
        "tool_input": {},
        "tool_response": {"data": {"id": "m-from-response"}},
    }
    assert hook.extract_meeting_id(payload) == "m-from-response"


def test_extract_meeting_id_skips_other_tools() -> None:
    payload = {
        "tool_name": "mcp__ai-team-os__report_save",
        "tool_input": {"meeting_id": "should-be-ignored"},
    }
    assert hook.extract_meeting_id(payload) == ""


def test_extract_meeting_id_handles_short_alias() -> None:
    """Hook should also accept the short tool-name alias."""
    payload = {
        "tool_name": "meeting_conclude",
        "tool_input": {"meeting_id": "m-short"},
    }
    assert hook.extract_meeting_id(payload) == "m-short"


# ============================================================
# Reminder rendering
# ============================================================


def test_build_reminder_with_linked_reviews() -> None:
    text = hook.build_reminder(
        meeting_id="m-1",
        topic="生态库选型辩论",
        matched_keywords=["生态", "生态库"],
        linked_reviews=[
            {"id": "rev-A"},
            {"id": "rev-B"},
        ],
    )
    assert "m-1" in text
    assert "生态库选型辩论" in text
    assert "rev-A" in text and "rev-B" in text
    assert "ecosystem_apply_debate_result" in text
    assert "ecosystem_mark_as_reference" in text or "ecosystem_start_integration" in text


def test_build_reminder_without_linked_reviews_offers_fallback() -> None:
    text = hook.build_reminder(
        meeting_id="m-2",
        topic="Ecosystem free-form discussion",
        matched_keywords=["ecosystem"],
        linked_reviews=[],
    )
    assert "m-2" in text
    assert "ecosystem_trigger_debate" in text  # falls back to suggestion


def test_build_reminder_truncates_long_topic() -> None:
    long_topic = "x" * 500
    text = hook.build_reminder(
        meeting_id="m-3",
        topic=long_topic,
        matched_keywords=["ecosystem"],
        linked_reviews=[],
    )
    # Topic line shouldn't exceed 250 chars (200 + label).
    topic_line = next(line for line in text.splitlines() if line.startswith("  topic:"))
    assert len(topic_line) <= 260


# ============================================================
# main() — end to end (with network mocked)
# ============================================================


def _capture_stdout() -> tuple[io.StringIO, Any]:
    """Replace sys.stdout with a StringIO capture; caller restores."""
    captured = io.StringIO()
    original = sys.stdout
    sys.stdout = captured
    return captured, original


def _capture_stdin(payload: dict[str, Any]) -> Any:
    """Replace sys.stdin with a JSON payload; returns original to restore."""
    original = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    return original


def test_main_skips_when_not_meeting_conclude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-target tool name → main() exits 0 with no stdout."""
    payload = {
        "tool_name": "mcp__ai-team-os__report_save",
        "tool_input": {},
    }
    captured, orig_stdout = _capture_stdout()
    orig_stdin = _capture_stdin(payload)
    try:
        rc = hook.main()
    finally:
        sys.stdout = orig_stdout
        sys.stdin = orig_stdin
    assert rc == 0
    assert captured.getvalue() == ""


def test_main_emits_reminder_when_topic_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Topic containing ecosystem keyword + meeting_id → reminder printed."""
    monkeypatch.setattr(
        hook,
        "fetch_meeting_topic",
        lambda mid: "生态库选型辩论",
    )
    monkeypatch.setattr(hook, "fetch_linked_reviews", lambda mid: [])

    payload = {
        "tool_name": "mcp__ai-team-os__meeting_conclude",
        "tool_input": {"meeting_id": "m-99"},
    }
    captured, orig_stdout = _capture_stdout()
    orig_stdin = _capture_stdin(payload)
    try:
        rc = hook.main()
    finally:
        sys.stdout = orig_stdout
        sys.stdin = orig_stdin
    assert rc == 0
    out = captured.getvalue()
    assert "m-99" in out
    assert "生态库选型辩论" in out


def test_main_silent_when_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """No keyword match + no linked review → no output."""
    monkeypatch.setattr(hook, "fetch_meeting_topic", lambda mid: "Sprint planning")
    monkeypatch.setattr(hook, "fetch_linked_reviews", lambda mid: [])

    payload = {
        "tool_name": "mcp__ai-team-os__meeting_conclude",
        "tool_input": {"meeting_id": "m-quiet"},
    }
    captured, orig_stdout = _capture_stdout()
    orig_stdin = _capture_stdin(payload)
    try:
        rc = hook.main()
    finally:
        sys.stdout = orig_stdout
        sys.stdin = orig_stdin
    assert rc == 0
    assert captured.getvalue() == ""


def test_main_emits_when_linked_review_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No keyword match but a linked review still triggers reminder."""
    monkeypatch.setattr(hook, "fetch_meeting_topic", lambda mid: "Architecture sync")
    monkeypatch.setattr(
        hook,
        "fetch_linked_reviews",
        lambda mid: [{"id": "rev-linked"}],
    )

    payload = {
        "tool_name": "mcp__ai-team-os__meeting_conclude",
        "tool_input": {"meeting_id": "m-linked"},
    }
    captured, orig_stdout = _capture_stdout()
    orig_stdin = _capture_stdin(payload)
    try:
        rc = hook.main()
    finally:
        sys.stdout = orig_stdout
        sys.stdin = orig_stdin
    assert rc == 0
    out = captured.getvalue()
    assert "rev-linked" in out
