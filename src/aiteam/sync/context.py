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
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from aiteam.ids import operation_ids

_process_origin = ""
_process_role = "standalone"
_refused_role = ""
_origin: ContextVar[str | None] = ContextVar("aiteam_sync_origin", default=None)


@dataclass(frozen=True)
class Operation:
    op_id: str
    origin: str
    staged: bool
    wall: datetime | None


_operation: ContextVar[Operation | None] = ContextVar("aiteam_sync_operation", default=None)


def set_process_node(node_id: str, role: str, *, refused_role: str = "") -> None:
    """Record this process's node id and effective role (startup and shutdown only).

    ``refused_role``: the role node.json declared and this machine refused; the
    process then runs standalone and a notice says so (sync_identity_refused).
    """
    global _process_origin, _process_role, _refused_role  # noqa: PLW0603 - one process, one node
    _process_origin, _process_role, _refused_role = node_id, role, refused_role


def refused_role() -> str:
    return _refused_role


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
