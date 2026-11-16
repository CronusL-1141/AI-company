"""What the code running right now is doing, from the point of view of sync.

A leaf module (standard library only): the ORM models read the origin of new rows
from here, and the storage layer reads whether events are to be written.

* ``origin``: the node new synced rows are stamped with (``origin_node``). The
  process default is this node's id (set at startup; empty on a standalone node,
  which therefore writes exactly the rows it always wrote). The center applying an
  edge's operation runs it under that edge's id.
* ``operation``: one shared-face write being carried out under an operation id.
  Its ids come from ``aiteam.ids.operation_ids``; ``staged`` marks the edge's local
  stand-in for a write the center will carry out (no events: the center emits
  them when it applies the operation); ``wall`` is the operation's clock time,
  the time the center's events carry instead of the moment of the replay.
* ``role``: the effective role of this process, for code far from the node
  identity that must behave differently on an edge (shared-face governance and
  writes-on-read run on the center and on standalone nodes only).
* ``own_origins``: the rows this node derives data from and governs (its partition,
  aiteam.sync.node.governance_scope): None on a standalone node (every row), the
  node itself on the center, the node and the baseline's empty origin on an edge.
  Usage projections, quota summaries and the session part of a project's deletion
  read only these, so the center never recomputes what another node derived.
* ``effects``: what an operation carried out for another node (the center replaying
  an edge's write) did to the session face. One node writes the session rows of
  one origin: the center hands those writes back with its verdict and the edge
  writes them (aiteam.sync.effects). ``baseline_owner`` is the edge that owns the
  rows with an empty origin.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiteam.ids import operation_ids

_process_origin = ""
_process_role = "standalone"
_identity_trouble: tuple[str, str] | None = None
_own_origins: tuple[str, ...] | None = None
_baseline_owner = ""
_origin: ContextVar[str | None] = ContextVar("aiteam_sync_origin", default=None)


@dataclass(frozen=True)
class Operation:
    op_id: str
    origin: str
    staged: bool
    wall: datetime | None
    # Session-face writes handed back to ``origin`` (aiteam.sync.effects builds them).
    effects: list[Any] = field(default_factory=list)


_operation: ContextVar[Operation | None] = ContextVar("aiteam_sync_operation", default=None)


def set_process_node(node_id: str, role: str, *, own_origins: tuple[str, ...] | None = None,
                     identity_trouble: tuple[str, str] | None = None) -> None:
    """Record this process's node id, effective role and partition (startup and shutdown only).

    ``identity_trouble``: (how the node runs, the role it was to have) when its identity
    did not hold at startup (aiteam.sync.node.decide); a notice says so
    (sync_identity_refused).
    """
    global _process_origin, _process_role, _identity_trouble, _own_origins  # noqa: PLW0603 - one process, one node
    _process_origin, _process_role, _identity_trouble, _own_origins = node_id, role, identity_trouble, own_origins


def node_id() -> str:
    """This process's node id (empty on a standalone node)."""
    return _process_origin


def own_origins() -> tuple[str, ...] | None:
    """The origins of the rows this node derives data from; None: every row."""
    return _own_origins


def set_baseline_owner(node_id: str) -> None:
    """The edge whose rows the empty origin stands for (the center learns it from its intake)."""
    global _baseline_owner  # noqa: PLW0603 - one process, one center
    _baseline_owner = node_id


def baseline_owner() -> str:
    return _baseline_owner


def identity_trouble() -> tuple[str, str] | None:
    """(how the node runs, the role it was to have) when its identity did not hold at startup."""
    return _identity_trouble


def current_origin() -> str:
    """The origin new synced rows get: the operation's node, else this process's."""
    override = _origin.get()
    return _process_origin if override is None else override


def is_edge() -> bool:
    return _process_role == "edge"


def current_operation() -> Operation | None:
    return _operation.get()


def for_another_node() -> bool:
    """True while carrying out another node's operation (the center replaying an edge's).

    Checks of the writer's own machine (does this folder exist?) were made where the
    write happened; this machine cannot see that one's disks.
    """
    operation = _operation.get()
    return operation is not None and not operation.staged and operation.origin != _process_origin


def staging() -> bool:
    """True while an edge stands in locally for a write the center will carry out."""
    operation = _operation.get()
    return operation is not None and operation.staged


def foreign_effects() -> tuple[str, list[Any]] | None:
    """(the operation's node, its effects list) while carrying out another node's
    operation; None otherwise, when session writes happen here as always."""
    operation = _operation.get()
    if operation is None or not for_another_node():
        return None
    return operation.origin, operation.effects


def owner_of(origin_node: str) -> str:
    """The node that writes a session row of ``origin_node``: the empty origin is the baseline's."""
    return origin_node or _baseline_owner


def operation_wall() -> datetime | None:
    """The wall-clock time of the operation being applied, if any."""
    operation = _operation.get()
    return operation.wall if operation is not None else None


@contextmanager
def operation(op_id: str, *, origin: str, staged: bool, wall: datetime | None = None) -> Iterator[Operation]:
    """Carry out one shared-face write: reproducible ids, its origin, its time."""
    current = Operation(op_id=op_id, origin=origin, staged=staged, wall=wall)
    token, origin_token = _operation.set(current), _origin.set(origin)
    try:
        with operation_ids(op_id):
            yield current
    finally:
        _origin.reset(origin_token)
        _operation.reset(token)
