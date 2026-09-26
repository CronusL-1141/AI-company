"""Hook-sourced POSTs keep their data when the body carries a lone surrogate.

Ordinary routes refuse such a body with a clean 400 (the API middleware). A hook
request is different: the host never resends it, so a refusal silently loses a
denial diagnosis, a notice round, a compact checkpoint, a consent record or a
project resolution. These routes replace the surrogate with U+FFFD instead. The
bodies below are the shapes the hooks send, JSON-encoded the way they encode them
(ASCII escapes, so the lone surrogate travels as the escape).
"""

from __future__ import annotations

import json

import pytest

from aiteam.api import middleware
from aiteam.api.guardrails import LONE_SURROGATE_DETAIL
from tests.unit.test_meeting_security import _make_client, _teardown

LONE = "x" + chr(0xD800) + "y"

# Every POST a hook script sends besides /api/hooks/event (covered end to end with
# the real send_event.py in test_lone_surrogate_e2e): plugin/hooks and
# plugin/harness/codex/hooks, found by their POST call sites.
HOOK_POSTS = {
    "/api/hooks/diagnose_denial": {"tool_name": "Bash", "tool_input": {"command": LONE}, "reason": LONE},
    "/api/hooks/compact-checkpoint": {"session_id": "s1", "trigger": "auto", "cwd": "/w/" + LONE},
    "/api/notices/pending": {"host": "cc", "event": "UserPromptSubmit", "session_id": "s1",
                             "cwd": "/w/" + LONE, "reader": "leader-cc"},
    "/api/notices/consent": {"uuid": "abcdef0123456789", "kind": "consent", "at": "2026-09-24T01:00:00Z",
                             "change": "sync_installed_copies", "user_quote": LONE, "host": "cc",
                             "targets": [{"path": "/x", "action": "write"}]},
    "/api/context/resolve": {"cwd": "/w/" + LONE, "auto_create": False},
    "/api/ecosystem/deep_reviews/dr-1/link_report": {"report_id": "r1", "summary_md": LONE},
}


def test_the_middleware_exempts_exactly_the_hook_posts():
    exempt = {path for path in [*HOOK_POSTS, "/api/hooks/event"] if middleware._hook_sourced("POST", path)}
    assert exempt == {*HOOK_POSTS, "/api/hooks/event"}
    assert len(middleware.HOOK_SOURCED_ROUTES) == len(HOOK_POSTS) + 1
    # Not a blanket exemption: the same prefix with another method or path is refused.
    assert not middleware._hook_sourced("PUT", "/api/context/resolve")
    assert not middleware._hook_sourced("POST", "/api/hooks/eventx")


@pytest.mark.parametrize("path", list(HOOK_POSTS), ids=list(HOOK_POSTS))
def test_a_hook_post_with_a_lone_surrogate_is_kept(path):
    client, _, _ = _make_client()
    try:
        resp = client.post(path, content=json.dumps(HOOK_POSTS[path]),
                           headers={"content-type": "application/json"})
        body = resp.text
        assert LONE_SURROGATE_DETAIL not in body, f"{path} refused a hook request: {body[:160]}"
        assert resp.status_code not in (400, 422) and resp.status_code < 500, (
            f"{path} -> {resp.status_code}: {body[:160]}")
        body.encode("utf-8")  # nothing unencodable came back
    finally:
        _teardown()


def test_an_ordinary_route_still_refuses():
    client, _, _ = _make_client()
    try:
        resp = client.post("/api/leader-briefings", content=json.dumps({"title": LONE}),
                           headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == LONE_SURROGATE_DETAIL
    finally:
        _teardown()
