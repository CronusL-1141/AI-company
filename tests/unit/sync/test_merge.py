"""The center's merge rules for a write made against an older state.

Pure function: an operation, the row as the center has it now, the field clocks.
"""

from __future__ import annotations

import json

import pytest

from aiteam.sync.merge import RULES, merge
from aiteam.types import SyncOperation

EDGE, CENTER = "air-3f9c", "mini-c0de"
T0, T1, T2 = "0001763200000000-0000-x", "0001763200000100-0000-x", "0001763200000200-0000-x"
UPDATE = RULES[("PUT", "/api/tasks/{task_id}")]
COMPLETE = RULES[("PUT", "/api/tasks/{task_id}/complete")]
RESOLVE = RULES[("PUT", "/api/leader-briefings/{briefing_id}/resolve")]


def _row(status: str = "pending", revision: int = 3, **fields) -> dict:
    return {"id": "t1", "title": "old", "assigned_to": None, "priority": "medium", "result": None,
            "status": status, "config": json.dumps({"task_management": {"revision": revision}}), **fields}


def _op(base: dict, hlc: str = T1, **body) -> tuple[SyncOperation, dict]:
    op = SyncOperation(op_id="0193a3e4-0000-7000-8000-000000000001", origin_node=EDGE, hlc=hlc,
                       method="PUT", path="/api/tasks/t1", body=json.dumps(body), base=base)
    return op, body


def test_an_edit_of_the_revision_still_current_runs_as_sent() -> None:
    op, body = _op({"revision": 3, "status": "pending"}, title="new", status="running")
    plan = merge(UPDATE, op, body, _row(), {"title": (T2, CENTER)})
    assert plan.body == body and not plan.merged and not plan.skip and plan.conflicts == []


def test_the_later_writer_wins_each_field() -> None:
    op, body = _op({"revision": 2, "status": "pending"}, title="edge", assigned_to="air", priority="high")
    clocks = {"title": (T2, CENTER), "assigned_to": (T0, CENTER)}  # priority: never written there
    plan = merge(UPDATE, op, body, _row(assigned_to="mini"), clocks)
    assert plan.body == {"assigned_to": "air", "priority": "high"} and plan.merged
    lost = {(c.field, c.loser, c.loser_node, c.rule) for c in plan.conflicts}
    assert lost == {("title", "edge", EDGE, "last_writer"), ("assigned_to", "mini", CENTER, "last_writer")}


@pytest.mark.parametrize(("theirs", "ours", "wins"), [
    ("completed", "running", False), ("running", "completed", True), ("failed", "blocked", False),
    ("blocked", "running", True),
], ids=["completed-beats-running", "completed-wins", "failed-beats-blocked", "running-beats-blocked"])
def test_status_unseen_by_either_side_moves_forward(theirs: str, ours: str, wins: bool) -> None:
    op, body = _op({"revision": 2, "status": "pending"}, status=ours)
    plan = merge(UPDATE, op, body, _row(status=theirs), {"status": (T2 if wins else T0, CENTER)})
    assert ("status" in plan.body) is wins
    # Neither saw the other: whichever value lost is on file, with the node that wrote it.
    lost = [(c.winner, c.loser, c.loser_node, c.rule) for c in plan.conflicts if c.field == "status"]
    assert lost == ([(ours, theirs, CENTER, "status_rank")] if wins else [(theirs, ours, EDGE, "status_rank")])


def test_an_edit_made_against_the_current_status_is_applied_even_backwards() -> None:
    """Seeing the task completed and reopening it is a decision, not a race."""
    op, body = _op({"revision": 2, "status": "completed"}, status="pending")
    plan = merge(UPDATE, op, body, _row(status="completed"), {"status": (T2, CENTER)})
    assert plan.body == {"status": "pending"} and plan.conflicts == []


def test_two_completions_keep_the_later_result_and_file_the_other_as_a_memo() -> None:
    op, body = _op({"revision": 2, "status": "running"}, result="edge result")
    plan = merge(COMPLETE, op, body, _row(status="completed", result="center result"), {"result": (T2, CENTER)})
    assert plan.body == {} and not plan.skip  # completing again, without overwriting the result
    assert plan.memos == [f"来自 {EDGE} 的完成结果（未采用）：edge result"]
    op, body = _op({"revision": 2, "status": "running"}, hlc=T2, result="edge result")
    plan = merge(COMPLETE, op, body, _row(status="completed", result="center result"), {"result": (T1, CENTER)})
    assert plan.body == {"result": "edge result"}
    assert plan.memos == [f"中心原有的完成结果（被 {EDGE} 的完成取代）：center result"]


def test_a_route_that_sets_a_losing_status_by_its_nature_does_not_run() -> None:
    op, body = _op({"status": "pending"}, resolution="edge decision")
    briefing = {"id": "b1", "status": "dismissed", "resolution": None}
    plan = merge(RESOLVE, op, body, briefing, {"status": (T2, CENTER)})
    assert plan.skip and [(c.field, c.loser) for c in plan.conflicts] == [("status", "resolved")]


def test_a_write_whose_every_field_lost_does_not_run() -> None:
    op, body = _op({"revision": 2, "status": "pending"}, title="edge")
    plan = merge(UPDATE, op, body, _row(), {"title": (T2, CENTER)})
    assert plan.skip and plan.merged


def test_a_row_gone_meanwhile_is_left_to_the_route_and_a_revision_check_to_the_writer() -> None:
    op, body = _op({"revision": 2, "status": "pending"}, title="edge")
    assert merge(UPDATE, op, body, None, {}).body == body
    op, body = _op({"revision": 2, "status": "pending"}, title="edge", expected_revision=2)
    assert merge(UPDATE, op, body, _row(), {"title": (T2, CENTER)}).body == body


CONCLUDE = RULES[("PUT", "/api/meetings/{meeting_id}/conclude")]
MEETING = RULES[("PUT", "/api/meetings/{meeting_id}")]


def test_a_meeting_concluded_on_both_sides_stays_as_the_first_left_it() -> None:
    op, body = _op({"status": "active"}, summary="edge summary", validate_attendance=False)
    meeting = {"id": "m1", "status": "concluded", "participants": "[]", "concluded_at": "2026-11-15 10:00:00"}
    plan = merge(CONCLUDE, op, body, meeting, {"status": (T2, CENTER)})
    assert plan.skip and plan.conflicts == []


def test_a_briefing_resolved_on_both_sides_takes_the_later_resolution() -> None:
    briefing = {"id": "b1", "status": "resolved", "resolution": "center's"}
    op, body = _op({"status": "pending"}, hlc=T2, resolution="edge's")
    plan = merge(RESOLVE, op, body, briefing, {"resolution": (T1, CENTER)})
    assert not plan.skip and plan.body == body  # the route gets the whole request it needs
    op, body = _op({"status": "pending"}, hlc=T0, resolution="edge's")
    plan = merge(RESOLVE, op, body, briefing, {"resolution": (T1, CENTER)})
    assert plan.skip and [(c.field, c.loser) for c in plan.conflicts] == [("resolution", "edge's")]


def test_meeting_participants_are_the_union_of_both_sides() -> None:
    op, body = _op({"status": "active"}, hlc=T0, participants=["a", "edge-only"])
    meeting = {"id": "m1", "status": "active", "participants": json.dumps(["a", "center-only"])}
    plan = merge(MEETING, op, body, meeting, {"participants": (T2, CENTER)})
    assert plan.body == {"participants": ["a", "center-only", "edge-only"]} and plan.conflicts == []


def test_two_replacements_of_one_memory_are_both_kept() -> None:
    from aiteam.sync.merge import merge_supersede

    op, body = _op({}, content="edge's wording", supersedes="old")
    assert merge_supersede("memories", op, body, {"id": "old", "invalid_at": None}).body == body
    plan = merge_supersede("memories", op, body, {"id": "old", "invalid_at": "2026-11-15", "invalidated_by": "m2"})
    assert plan.body == {**body, "supersedes": None} and plan.merged
    assert [(c.rule, c.winner, c.loser_node) for c in plan.conflicts] == [("both_kept", "m2", EDGE)]


def test_an_equal_revision_counted_over_unconfirmed_edits_is_no_proof_nobody_else_wrote() -> None:
    """The edge staged running (its count 2) and now writes pending; the center completed
    the task meanwhile (its count 2 as well). Equal by coincidence: completed must stand."""
    center_row = _row(status="completed", revision=2, assigned_to="mini")
    op, body = _op({"revision": 1, "status": "pending", "tentative": True}, hlc=T2, status="pending",
                   assigned_to=None)
    plan = merge(UPDATE, op, body, center_row, {"status": (T1, CENTER), "assigned_to": (T1, CENTER)})
    assert "status" not in plan.body and plan.body == {"assigned_to": None}
    assert [(c.field, c.winner, c.loser, c.rule) for c in plan.conflicts if c.field == "status"] == [
        ("status", "completed", "pending", "status_rank")]
    # Even a count equal to the center's proves nothing over unconfirmed edits: merged by field.
    op, body = _op({"revision": 2, "status": "running", "tentative": True}, hlc=T2, status="pending")
    plan = merge(UPDATE, op, body, center_row, {"status": (T1, CENTER)})
    assert plan.skip and [(c.winner, c.loser) for c in plan.conflicts] == [("completed", "pending")]
    # Without unconfirmed edits an equal count is the shortcut, as before.
    op, body = _op({"revision": 2, "status": "completed"}, status="pending")
    assert merge(UPDATE, op, body, center_row, {"status": (T2, CENTER)}).body == body


def test_fields_kept_in_config_follow_the_later_writer_too() -> None:
    config = {"task_management": {"revision": 4, "status_note": "center's newer note",
                                  "waiting_on": {"kind": "user", "reason": "center asks"}}}
    center_row = _row(revision=4) | {"config": json.dumps(config)}
    op, body = _op({"revision": 2, "status": "pending"}, hlc=T1, status_note="edge's older note",
                   waiting_on={"kind": "user", "reason": "edge asks"})
    plan = merge(UPDATE, op, body, center_row, {"status_note": (T2, CENTER), "waiting_on": (T2, CENTER)})
    assert plan.skip and {c.field for c in plan.conflicts} == {"status_note", "waiting_on"}
    assert all(c.loser_node == EDGE and c.winner_hlc == T2 for c in plan.conflicts)
    op, body = _op({"revision": 2, "status": "pending"}, hlc=T2, status_note="edge's newer note")
    plan = merge(UPDATE, op, body, center_row, {"status_note": (T1, CENTER)})
    assert plan.body == body
    assert [(c.loser, c.loser_node, c.winner_hlc) for c in plan.conflicts] == [("center's newer note", CENTER, T2)]


def test_every_decision_carries_the_winning_writes_clock() -> None:
    op, body = _op({"revision": 2, "status": "pending"}, hlc=T1, title="edge")
    plan = merge(UPDATE, op, body, _row(), {"title": (T2, CENTER)})
    assert [(c.winner_hlc, c.loser_node) for c in plan.conflicts] == [(T2, EDGE)]
