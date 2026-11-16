"""Which tables sync, and on which face.

Every ORM table belongs to exactly one group; a new table with no group is a
machine-check failure (I32), because a table nobody classified silently never syncs.

* session: written only by the node the session runs on and mirrored up to the
  center, which keeps the union by origin.
* shared: the center holds the one authoritative copy; edges write through it.
* local: this node's own state (leases, receipts, local cursors, derived caches,
  notices about this machine's installation). Never leaves the node.
* dead: no code writes them any more; kept readable, never synced.
* sync: the bookkeeping of sync itself, local to each node.

Session and shared tables carry an ``origin_node`` column and change-capture
triggers; the rest carry neither.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy import Table

SESSION_TABLES = frozenset({
    "events",
    "agent_activities",
    "agents",
    "teams",
    "workflow_runs",
    "workflow_agents",
    "wake_sessions",
    "codex_usage_observations",
    "external_exec_invocations",
    "codex_exec_launches",
    "account_usage_accounts",
    "account_usage_snapshots",
    "account_usage_batches",
    "account_plan_snapshots",
    "account_plan_price_snapshots",
    # What this node's sessions filed on GitHub and how the user confirmed it: written
    # where the gh login is, mirrored to the center (a second copy of a record that
    # cannot be rebuilt), never routed through it (filing must not wait for the center).
    "feedback_submissions",
})

SHARED_TABLES = frozenset({
    # Append-only: the union never conflicts.
    "task_memos",
    "reports",
    "channel_messages",
    "meeting_messages",
    "knowledge_links",
    "task_create_batches",
    # Workflow asset backup: contents by hash, versions append-only; items and
    # findings have ids derived from their keys, so two nodes' rows coincide.
    "asset_versions",
    "asset_blobs",
    "asset_items",
    # Replayed by operation, merged by rule.
    "tasks",
    "memories",
    "meetings",
    "leader_briefings",
    "projects",
    "phases",
    "scheduled_tasks",
    "channel_read_cursors",
    "account_plan_price_anchors",
    # Workflow asset backup: profiles and roots by derived id, later writer wins
    # field by field (a root is written by its own node only); a scan finding's
    # disposition likewise.
    "asset_profiles",
    "asset_roots",
    "asset_scan_findings",
    # Ecosystem archive: written only while the center is reachable.
    "ecosystem_repo_profiles",
    "ecosystem_deep_reviews",
    "ecosystem_tags",
    "ecosystem_repo_tags",
    "ecosystem_relations",
    "ecosystem_scan_runs",
    "ecosystem_repo_status_snapshots",
    "ecosystem_project_settings",
    "ecosystem_data_sources",
    "ecosystem_scan_profiles",
    "ecosystem_index_diffs",
    "ecosystem_status_changes",
    "ecosystem_repo_events",
    "ecosystem_shallow_batches",
})

# Not synced, each with the reason: a table that does not travel has to say why.
LOCAL_TABLES: dict[str, str] = {
    "governance_lease": "this database's governance lease, held by a process of this node",
    "hook_event_receipts": "dedupe receipts of this machine's hook deliveries, pruned by age",
    "notices": "this machine's installation state; the same key means different things elsewhere",
    "notice_deliveries": "deliveries of this machine's notices, claimed per session",
    "account_usage_monitors": "the sampling lease and fence of this machine's monitor",
    "account_usage_request_dedupe": "dedupe of this machine's requests",
    "account_plan_summaries": "derived cache, rebuilt from the snapshots on each node",
    "account_plan_cycle_requests": "derived cache, rebuilt from the snapshots on each node",
    "codex_usage_source_cursors": "read positions in this machine's files",
    "asset_root_state": "the comparison list of this machine's config folders (size, mtime, last stored hash)",
    "asset_restore_plans": "restores of this machine's config folders, written and run by this node",
}

DEAD_TABLES: dict[str, str] = {
    "cross_messages": "no code writes it any more; kept readable",
    "pipeline_stage_history": "no code writes it any more; kept readable",
}

# The bookkeeping of sync itself, never synced: the change log and its switches,
# an edge's outbox, staged rows and link to its center, the center's receipts,
# field clocks and conflict list, where each edge's session upload stands, the
# marker of an operation carried out for another node, and the ids of deleted
# natural-key rows.
SYNC_BOOKKEEPING_TABLES = frozenset({
    "sync_changes", "sync_state", "sync_suppress", "sync_guard", "sync_tombstones",
    "sync_outbox", "sync_tentative", "sync_peers",
    "sync_op_receipts", "sync_field_clock", "sync_conflicts", "sync_intake",
})

SYNCED_TABLES = SESSION_TABLES | SHARED_TABLES

# Each face's tables as an SQL list, for ``table_name IN (...)`` over the change log.
SHARED_TABLES_SQL = ", ".join(f"'{name}'" for name in sorted(SHARED_TABLES))
SESSION_TABLES_SQL = ", ".join(f"'{name}'" for name in sorted(SESSION_TABLES))

ORIGIN_COLUMN = "origin_node"

# The key a change is recorded under, where it is not the primary key. The
# integer id of knowledge_links is numbered per database; the link is its
# five-part natural key.
SYNC_KEYS: dict[str, tuple[str, ...]] = {
    "knowledge_links": ("from_kind", "from_id", "to_kind", "to_id", "link_type"),
}

# Session columns numbered per database (a sequence the node's own code allocates
# and reads positions in): the center numbers an edge's row anew and keeps the
# edge's number in the second column, which only means something with origin_node.
SESSION_RENUMBERED: dict[str, tuple[str, str]] = {
    "codex_usage_observations": ("seq", "origin_seq"),
}

# Columns that record this node's own use and never travel: an update that sets
# only these is not a change. memories.accessed_at is written on every read.
LOCAL_COLUMNS: dict[str, frozenset[str]] = {
    "memories": frozenset({"accessed_at"}),
}

# Shared tables whose rows two nodes may both edit, merged field by field: each
# field written on the center gets a clock, so a write replayed later is ordered
# against it. Append-only tables never need one.
FIELD_CLOCK_TABLES = frozenset({
    "tasks", "projects", "phases", "scheduled_tasks", "leader_briefings", "meetings",
})

# Fields a mergeable table keeps inside a JSON column rather than in a column of its
# own, by the path that leads to them in ``config``: clocked and merged like columns.
CONFIG_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "tasks": {
        "waiting_on": ("task_management", "waiting_on"),
        "status_note": ("task_management", "status_note"),
    },
}

# Tables whose ids derive from a natural key (aiteam.ids): their deletions are
# remembered, so re-creating the object takes the next generation of the id.
TOMBSTONE_TABLES = frozenset({"projects", "teams"})

_GROUPS = {
    "session": SESSION_TABLES,
    "shared": SHARED_TABLES,
    "local": LOCAL_TABLES.keys(),
    "dead": DEAD_TABLES.keys(),
    "sync": SYNC_BOOKKEEPING_TABLES,
}


def schema_problems(tables: Mapping[str, Table]) -> list[str]:
    """What is wrong with the ORM tables from the point of view of sync.

    Every table belongs to exactly one group, an unsynced one says why, a synced
    one has a key to record its changes under and the ``origin_node`` column, and
    no other table has that column. Takes ``Base.metadata.tables``.
    """
    problems = []
    for name in sorted(tables):
        groups = [group for group, members in _GROUPS.items() if name in members]
        if not groups:
            problems.append(f"{name}: 未归入 session/shared/local/dead/sync 任何一组（新表必须先定同步面）")
        elif len(groups) > 1:
            problems.append(f"{name}: 同时归入 {'/'.join(groups)}")
    classified = set().union(*_GROUPS.values())
    problems += [f"{name}: 已分组，但 ORM 里没有这张表" for name in sorted(classified - set(tables))]
    problems += [f"{name}: 不同步却没写理由" for name, why in {**LOCAL_TABLES, **DEAD_TABLES}.items()
                 if not why.strip()]
    for name, table in sorted(tables.items()):
        columns = set(table.columns.keys())
        if name in SYNCED_TABLES:
            if ORIGIN_COLUMN not in columns:
                problems.append(f"{name}: 同步表的 ORM 模型缺 {ORIGIN_COLUMN} 列（漏挂 OriginNodeMixin）")
            key = sync_key(name, [c.name for c in table.primary_key.columns])
            if not key:
                problems.append(f"{name}: 同步表既无主键也无 SYNC_KEYS，变更无从按行记录（补键，或归入不同步并写理由）")
            elif not set(key) <= columns:
                problems.append(f"{name}: SYNC_KEYS 里的列 {sorted(set(key) - columns)} 不在表上")
            renumbered = set(SESSION_RENUMBERED.get(name, ()))
            if not renumbered <= columns:
                problems.append(f"{name}: SESSION_RENUMBERED 的列 {sorted(renumbered - columns)} 不在表上"
                                "（按库编号的列上行到中心时另行编号，来源的编号要有处可放）")
        elif ORIGIN_COLUMN in columns and name not in SYNC_BOOKKEEPING_TABLES:
            problems.append(f"{name}: 不同步的表不该有 {ORIGIN_COLUMN} 列")
    return problems


def sync_key(table: str, primary_key: Iterable[str]) -> tuple[str, ...]:
    """The columns a change to ``table`` is recorded under: SYNC_KEYS, else the primary key."""
    return SYNC_KEYS.get(table) or tuple(primary_key)
