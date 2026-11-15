"""How the center merges an operation into rows another node may have changed meanwhile.

Only updates of mergeable rows need a rule; creations and
append-only writes take ids derived from the operation and never collide. A rule
names the row a route edits; the center compares the operation, field by field,
with the row as it is now:

* A task whose revision still equals the one the edge last got from the center
  (``base``) was edited by nobody else: the operation runs as sent. A row holding the
  edge's own unconfirmed edits is always merged field by field.
* ``status`` of tasks, briefings and meetings only moves forward when the two
  writers did not see each other: an edit made against the current status is
  applied (reopening a completed task stays possible), otherwise the status further
  along wins (``STATUS_RANKS``), equal ranks by clock.
* Every other field, including those a task keeps in its config (waiting_on,
  status_note): the later write by clock wins (the field clocks the center keeps).
* Two completions with different results keep the winner's; the loser's result
  becomes a memo on the task. A meeting or briefing concluded, resolved or dismissed
  on both sides stays as the first one left it, unless the writer's own fields are
  newer.
* A meeting's participants are the union of both sides'.
* Two nodes each replacing the same direction memory keep both replacements (the
  old one stays invalidated once); memory reconciliation proposes the merge later.

Every losing value is returned as a conflict for the center's list; nothing is
dropped silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aiteam.sync.tables import CONFIG_FIELDS
from aiteam.types import SyncFieldConflict, SyncOperation

STATUS_RANKS: dict[str, dict[str, int]] = {
    "tasks": {"pending": 0, "blocked": 1, "running": 2, "failed": 3, "completed": 4},
    "leader_briefings": {"pending": 0, "resolved": 1, "dismissed": 1},
    "meetings": {"active": 0, "concluded": 1},
}

# Body keys that steer a write instead of setting a field.
_CONTROL_KEYS = frozenset({"expected_revision"})


@dataclass(frozen=True)
class MergeRule:
    """The row one route edits, and what the route sets by its nature."""

    table: str
    key_param: str
    implied: dict[str, Any] = field(default_factory=dict)
    revision: bool = False

    @property
    def implies_status(self) -> bool:
        return "status" in self.implied


RULES: dict[tuple[str, str], MergeRule] = {
    ("PUT", "/api/tasks/{task_id}"): MergeRule("tasks", "task_id", revision=True),
    ("PUT", "/api/tasks/{task_id}/complete"): MergeRule(
        "tasks", "task_id", implied={"status": "completed"}, revision=True),
    ("PUT", "/api/projects/{project_id}"): MergeRule("projects", "project_id"),
    ("PUT", "/api/projects/{project_id}/phases/{phase_id}/status"): MergeRule("phases", "phase_id"),
    ("PUT", "/api/scheduler/{task_id}"): MergeRule("scheduled_tasks", "task_id"),
    ("PUT", "/api/meetings/{meeting_id}"): MergeRule("meetings", "meeting_id"),
    ("PUT", "/api/meetings/{meeting_id}/conclude"): MergeRule(
        "meetings", "meeting_id", implied={"status": "concluded"}),
    ("PUT", "/api/leader-briefings/{briefing_id}/resolve"): MergeRule(
        "leader_briefings", "briefing_id", implied={"status": "resolved"}),
    ("PUT", "/api/leader-briefings/{briefing_id}/dismiss"): MergeRule(
        "leader_briefings", "briefing_id", implied={"status": "dismissed"}),
}

# Writes that name the row they replace in their body (``supersedes``), not their path.
SUPERSEDES: dict[tuple[str, str], str] = {("POST", "/api/memories"): "memories"}

# The plain field update of each mergeable table: how a flipped decision is written.
UPDATE_ROUTES: dict[str, tuple[str, str]] = {
    "tasks": ("PUT", "/api/tasks/{key}"),
    "projects": ("PUT", "/api/projects/{key}"),
    "scheduled_tasks": ("PUT", "/api/scheduler/{key}"),
    "meetings": ("PUT", "/api/meetings/{key}"),
}


def rule_for(method: str, template: str) -> MergeRule | None:
    return RULES.get((method, template))


def revision_of(row: dict[str, Any]) -> int | None:
    config = decode(row.get("config"))
    if not isinstance(config, dict):
        return None
    management = config.get("task_management")
    return int(management.get("revision", 0)) if isinstance(management, dict) else 0


def base_of(rule: MergeRule, row: dict[str, Any] | None) -> dict[str, Any]:
    """What the writer saw of the row: recorded in the operation (``SyncOperation.base``)."""
    if row is None:
        return {}
    base: dict[str, Any] = {}
    if rule.table in STATUS_RANKS:
        base["status"] = row.get("status")
    if rule.revision:
        base["revision"] = revision_of(row)
    return base


def decode(value: Any) -> Any:
    """A stored column value as the application sees it (JSON columns are text)."""
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


@dataclass
class MergePlan:
    """What the center does with one operation.

    ``merged`` means ``body`` differs from what was sent and is what runs; ``skip``
    means nothing is left to run (every field lost to a newer value).
    """

    body: Any
    conflicts: list[SyncFieldConflict] = field(default_factory=list)
    memos: list[str] = field(default_factory=list)
    merged: bool = False
    skip: bool = False


def _rank(table: str, status: Any) -> int:
    return STATUS_RANKS[table].get(str(status), -1)


_LOST = object()  # a field whose value lost to the center's


def merge(
    rule: MergeRule, op: SyncOperation, body: dict[str, Any], row: dict[str, Any] | None,
    clocks: dict[str, tuple[str, str]],
) -> MergePlan:
    """Merge ``body`` (what the operation sets) into ``row`` (the center's row now).

    ``clocks`` maps a field to (hlc, node) of its last write on the center.
    """
    if row is None:
        return MergePlan(body)  # gone meanwhile: the route answers 404, a conflict
    if _seen_unchanged(rule, op, row) or any(key in body for key in _CONTROL_KEYS):
        # Nobody else edited it, or the writer asked to be checked against its revision.
        return MergePlan(body)
    plan = MergePlan(body)
    effective = {**rule.implied, **body}
    kept = {}
    for key, value in effective.items():
        merged = _merge_field(rule, op, row, key, value, clocks, plan.conflicts)
        if merged is not _LOST:
            kept[key] = merged
    _merge_results(rule, op, row, effective, kept, plan)
    _finish(rule, body, row, kept, plan)
    return plan


def _seen_unchanged(rule: MergeRule, op: SyncOperation, row: dict[str, Any]) -> bool:
    """The revision the writer saw is the center's current one: nobody else edited the row.

    Not when the writer's row held its own unconfirmed edits (``base.tentative``): its
    base is then the center's last version, but an equal count may still be a
    coincidence of two different histories, so every field is merged.
    """
    seen = op.base.get("revision")
    return rule.revision and not op.base.get("tentative") and seen is not None and seen == revision_of(row)


def _current(rule: MergeRule, row: dict[str, Any], key: str) -> tuple[bool, Any]:
    """Whether ``key`` is a field of the row, and its value: a column, or a value kept in config."""
    if key in row:
        return True, decode(row[key])
    path = CONFIG_FIELDS.get(rule.table, {}).get(key)
    if path is None:
        return False, None  # a flag of the request (validate_attendance, summary...)
    value: Any = decode(row.get("config"))
    for step in path:
        value = value.get(step) if isinstance(value, dict) else None
    return True, value


def _merge_field(rule: MergeRule, op: SyncOperation, row: dict[str, Any], key: str, value: Any,
                 clocks: dict[str, tuple[str, str]], conflicts: list[SyncFieldConflict]) -> Any:
    """The value of one field after the merge, or _LOST; losing values go to ``conflicts``."""
    known, current = _current(rule, row, key)
    if not known:
        return value
    if rule.table == "meetings" and key == "participants" and isinstance(value, list) \
            and isinstance(current, list):
        return current + [name for name in value if name not in current]  # a union loses nothing
    if value == current:
        return value
    clock = clocks.get(key)
    op_wins, reason = _decide(rule, op, key, value, current, clock)
    pk = str(row.get("id") or "")  # every mergeable table is keyed by id
    if op_wins:
        # The other side's value lost too, unless the writer saw it and changed it.
        if rule.revision and clock and clock[1] != op.origin_node and reason != "status_seen":
            conflicts.append(SyncFieldConflict(
                table_name=rule.table, pk=pk, field=key, winner=value, loser=current, loser_node=clock[1],
                winner_hlc=op.hlc, rule=reason))
        return value
    conflicts.append(SyncFieldConflict(
        table_name=rule.table, pk=pk, field=key, winner=current, loser=value, loser_node=op.origin_node,
        winner_hlc=clock[0] if clock else "", rule=reason))
    return _LOST


def _decide(rule: MergeRule, op: SyncOperation, key: str, value: Any, current: Any,
            clock: tuple[str, str] | None) -> tuple[bool, str]:
    if key == "status" and rule.table in STATUS_RANKS:
        if op.base.get("status") == current:
            return True, "status_seen"
        theirs, ours = _rank(rule.table, current), _rank(rule.table, value)
        if ours != theirs:
            return ours > theirs, "status_rank"
    return clock is None or op.hlc > clock[0], "last_writer"


def _finish(rule: MergeRule, body: dict[str, Any], row: dict[str, Any], kept: dict[str, Any],
            plan: MergePlan) -> None:
    """Decide what runs: the fields that won, or nothing."""
    sent = {key: value for key, value in kept.items() if key in body}
    # A route that sets the status by its nature cannot run without it; any other
    # runs with what is left, unless every field it sent lost.
    plan.skip = ("status" not in kept) if rule.implies_status else (bool(body) and not sent)
    if rule.implies_status and not rule.revision and not plan.skip and row.get("status") == rule.implied["status"]:
        # Both sides already got there: the first stands, unless the writer's own fields
        # are newer (they then go in with the whole request the route needs).
        newer = any(key in row and key in kept and kept[key] != decode(row[key]) for key in body)
        plan.skip, sent = not newer, (body if newer else sent)
    plan.merged = plan.skip or sent != body
    plan.body = sent


def merge_supersede(table: str, op: SyncOperation, body: dict[str, Any], old: dict[str, Any] | None) -> MergePlan:
    """A replacement of an entry the other side already replaced: keep it as a new entry.

    The old entry stays invalidated by whoever came first; both replacements stay,
    for memory reconciliation to propose merging.
    """
    if old is None or old.get("invalid_at") is None:
        return MergePlan(body)
    kept = {**body, "supersedes": None}
    conflict = SyncFieldConflict(table_name=table, pk=str(old.get("id") or ""), field="supersedes",
                                 winner=old.get("invalidated_by"), loser=body.get("content"),
                                 loser_node=op.origin_node, rule="both_kept")
    return MergePlan(kept, conflicts=[conflict], merged=True)


def _merge_results(rule: MergeRule, op: SyncOperation, row: dict[str, Any], effective: dict[str, Any],
                   kept: dict[str, Any], plan: MergePlan) -> None:
    """Both sides completed a task with different results: the loser's becomes a memo."""
    if rule.table != "tasks" or effective.get("status") != "completed" or row.get("status") != "completed":
        return
    ours, theirs = effective.get("result"), row.get("result")
    if not ours or ours == theirs:
        return
    if "status" in kept and "result" in kept and theirs:
        plan.memos.append(f"中心原有的完成结果（被 {op.origin_node} 的完成取代）：{theirs}")
    elif "status" not in kept or "result" not in kept:
        kept.pop("result", None)
        plan.memos.append(f"来自 {op.origin_node} 的完成结果（未采用）：{ours}")
