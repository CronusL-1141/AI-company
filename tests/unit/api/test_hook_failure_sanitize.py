"""PostToolUseFailure 清洗后才落库；真实 HTTP 请求与独立 SQLite 回读。"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
import uuid

import pytest


def _post(port: int, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status in (200, 201)
        return json.load(response)


@pytest.fixture
def failure_session(hook_server, tmp_path):
    port, database, _ = hook_server
    session_id = str(uuid.uuid4())
    base = {"session_id": session_id, "cwd": str(tmp_path)}
    _post(port, "/api/projects", {"name": "failure-sanitize", "root_path": str(tmp_path)})
    for event in ("SessionStart", "Stop"):
        _post(port, "/api/hooks/event", {
            **base, "hook_event_name": event, "source": "startup", "stop_hook_active": False,
        })
    return port, database, base


def _hook(session, event: str, call_id: str | None, **extra) -> dict:
    port, _, base = session
    payload = {
        **base, "hook_event_name": event, "tool_name": "Bash",
        "tool_input": {"command": "exit 3", "description": "failure-sanitize"},
        **extra,
    }
    if call_id is not None:
        payload["tool_use_id"] = call_id
    return _post(port, "/api/hooks/event", payload)


def _persisted(session) -> tuple[list[tuple], list[dict]]:
    """Every assertion opens a new read-only connection, outside the app's ORM."""
    _, database, base = session
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        activities = db.execute(
            "SELECT id, status, error, duration_ms FROM agent_activities "
            "WHERE session_id = ? AND tool_name = 'Bash' ORDER BY rowid",
            (base["session_id"],),
        ).fetchall()
        events = db.execute(
            "SELECT data FROM events WHERE type = 'cc.tool_failed' AND source = ? ORDER BY rowid",
            (f"session:{base['session_id']}",),
        ).fetchall()
    return activities, [json.loads(data) for (data,) in events]


@pytest.mark.parametrize(("extra", "expected", "interrupted"), [
    pytest.param({"error": "esc\x1bff\x0cnul\x00zero\u200bwidth"},
                 "esc ff nul zero width", False, id="controls-and-zero-width"),
    pytest.param({"error": " \tline one\r\nline  two  "},
                 "line one line two", False, id="collapsed-whitespace"),
    pytest.param({"error": "\x1b[31mfailed\x1b[0m"},
                 "[31mfailed [0m", False, id="ansi-escape-follows-shared-cleaner"),
    pytest.param({"error": "\x1b\x0c\x00\u200b"}, "failed", False, id="only-controls"),
    pytest.param({"error": " \t\r\n "}, "failed", False, id="only-whitespace"),
    pytest.param({"error": ""}, "failed", False, id="empty"),
    pytest.param({}, "failed", False, id="missing"),
    pytest.param({"error": None}, "failed", False, id="null"),
    pytest.param({"error": 42}, "failed", False, id="number"),
    pytest.param({"error": {"message": "bad"}}, "failed", False, id="object"),
    pytest.param({"error": ["bad"]}, "failed", False, id="array"),
    pytest.param({"error": " \x1buser\u200bcancelled\x0c", "is_interrupt": True},
                 "interrupted: user cancelled", True, id="interrupt-with-error"),
    pytest.param({"error": "\x1b\x0c\x00\u200b", "is_interrupt": True},
                 "interrupted", True, id="interrupt-with-only-controls"),
    pytest.param({"error": " \t", "is_interrupt": True},
                 "interrupted", True, id="interrupt-with-only-whitespace"),
    pytest.param({"error": "", "is_interrupt": True},
                 "interrupted", True, id="interrupt-with-empty-error"),
    pytest.param({"error": 42, "is_interrupt": True},
                 "interrupted", True, id="interrupt-with-non-string-error"),
    pytest.param({"error": "bad", "is_interrupt": "true"},
                 "bad", False, id="interrupt-requires-boolean-true"),
    pytest.param({"error": "\x00" * 300 + "x" * 600},
                 "x" * 600, False, id="clean-before-existing-caps"),
])
def test_failure_sanitizes_activity_and_event(failure_session, extra, expected, interrupted):
    call_id = f"toolu_{uuid.uuid4().hex}"
    _hook(failure_session, "PreToolUse", call_id)
    assert _hook(failure_session, "PostToolUseFailure", call_id, **extra)["status"] == "recorded"

    activities, events = _persisted(failure_session)
    assert len(activities) == 1
    assert activities[0][1:3] == ("error", expected[:500])
    assert len(events) == 1
    assert events[0]["error"] == expected[:200]
    assert events[0]["interrupted"] is interrupted


@pytest.mark.parametrize(("pre_id", "post_id"), [
    pytest.param(None, None, id="legacy-paired"),
    pytest.param("toolu_stripped_failure", None, id="legacy-stripped-post-id"),
    pytest.param(False, None, id="legacy-without-start"),
    pytest.param(False, "toolu_failure_without_start", id="identified-without-start"),
])
def test_failure_fallback_paths_keep_cleaned_error(failure_session, pre_id, post_id):
    if pre_id is not False:
        _hook(failure_session, "PreToolUse", pre_id)
    _hook(failure_session, "PostToolUseFailure", post_id, error="bad\x0c\x00\u200bcall", duration_ms=40)

    activities, events = _persisted(failure_session)
    assert len(activities) == 1
    assert activities[0][1:3] == ("error", "bad call")
    assert events[0]["error"] == "bad call"
    if pre_id is False:
        assert activities[0][3] == 40


def test_failure_pairs_by_id_and_repeat_does_not_rewrite_finished_activity(failure_session):
    for call_id in ("toolu_failure_a", "toolu_failure_b"):
        _hook(failure_session, "PreToolUse", call_id)
    _hook(failure_session, "PostToolUseFailure", "toolu_failure_a", error="first\x1berror")
    before, _ = _persisted(failure_session)
    assert [(row[1], row[2]) for row in before] == [("error", "first error"), ("running", None)]

    duplicate = _hook(failure_session, "PostToolUseFailure", "toolu_failure_a", error="new\x00error")
    assert duplicate["duplicate"] is True
    _hook(failure_session, "PostToolUseFailure", "toolu_failure_b", error="second\u200berror")
    after, events = _persisted(failure_session)
    assert after[0] == before[0]
    assert after[1][0] == before[1][0]
    assert after[1][1:3] == ("error", "second error")
    assert [event["error"] for event in events] == ["first error", "second error"]
