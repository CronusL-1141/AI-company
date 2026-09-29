"""The session briefing's task-wall block when the API cannot give a usable digest.

The block is the API's rendered text (test_task_wall_digest_e2e pins the byte-for-
byte case against a real server). Here the other side: an API without the
endpoint (an old API behind a new hook), one too slow for the 2-second budget, and
a server that sends raw, hostile or oversized lines. Each ends in one fallback
line or in cleaned, bounded lines, never in a lost briefing.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import aiteam.hooks.session_bootstrap as sb

from ._notice_fakes import FakeApi, hook_env, output, run_hook

FALLBACK = "任务墙摘要取不到，用 task_list_project 查看"


def _serve(api: FakeApi, home: Path, digest=None) -> None:
    api.routes.update({
        ("GET", "/api/teams"): lambda _: (200, {"data": [{"name": "core", "status": "active"}]}),
        ("GET", "/api/projects"): lambda _: (200, {"data": [{"id": "p1", "root_path": str(home)}]}),
        ("GET", "/api/leader-briefings"): lambda _: (200, {"items": [], "total": 0}),
        ("POST", "/api/context/resolve"): lambda _: (200, {"project_id": "p1", "project": {"id": "p1"}}),
        ("GET", "/api/memories"): lambda _: (200, {"data": []}),
    })
    if digest is not None:
        api.routes[("GET", "/api/projects/p1/task-wall/digest")] = digest


def _briefing(home: Path, api: FakeApi) -> str:
    result = run_hook("session_bootstrap.py", {"session_id": "s1", "source": "startup", "cwd": str(home)},
                      hook_env(home, api.url), cwd=home)
    assert result.returncode == 0, result.stderr
    return output(result.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture()
def home(tmp_path) -> Path:
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    return path


def test_an_api_without_the_endpoint_gets_one_line(home):
    with FakeApi() as api:
        _serve(api, home)
        briefing = _briefing(home, api)
    assert FALLBACK in briefing.split("\n")
    assert "Leader核心规则" in briefing, "the rest of the briefing is intact"


def test_a_digest_slower_than_the_budget_gets_one_line(home):
    def slow(_body):
        time.sleep(2.5)
        return 200, {"text": "=== 任务墙：too late ==="}

    with FakeApi() as api:
        _serve(api, home, slow)
        started = time.monotonic()
        briefing = _briefing(home, api)
    assert FALLBACK in briefing.split("\n") and "too late" not in briefing
    assert time.monotonic() - started < 10


def test_a_body_without_text_gets_one_line(home):
    with FakeApi() as api:
        _serve(api, home, lambda _: (200, {"open_total": 3}))
        assert FALLBACK in _briefing(home, api).split("\n")


def test_raw_lines_are_cleaned_one_by_one_and_keep_their_indent():
    raw = "=== 任务墙\u202e：x ===\n  进行中 1（短1）\x07\n\n    [high/short]  two  spaces\u200b #abc\n" + "L" * 500
    assert sb._render_task_wall_digest(raw) == [
        "=== 任务墙 ：x ===",
        "  进行中 1（短1）",
        "    [high/short] two spaces #abc",
        "L" * 200,
    ]


def test_the_fuse_bounds_a_flooding_server():
    lines = sb._render_task_wall_digest("\n".join(f"  line {i:04d} " + "x" * 90 for i in range(500)))
    assert sum(len(line) + 1 for line in lines) <= sb._DIGEST_FUSE + 1
    assert lines[0].startswith("  line 0000") and len(lines) < 30


@pytest.mark.parametrize("text", [None, "", "   \n\u200b\n", 42], ids=["none", "empty", "blank", "number"])
def test_nothing_usable_is_the_fallback_line(text):
    assert sb._render_task_wall_digest(text) == [FALLBACK]
