"""Row ids, and the rules by which two nodes of a multi-host sync arrive at the same ones.

Two rules, both inert on a standalone node, and a third that is not:

* Under an operation context (``operation_ids(op_id)``) every new id is
  ``uuid5(op_id, n)`` for the n-th id the operation asks for, so staging a write
  locally and replaying it on the center create the same objects with the same ids.
  Outside one, ids are random (``uuid4``), as they have always been.
* A row with a natural key gets ``uuid5(namespace, key)`` once natural ids are
  enabled for the process (a node that syncs): two machines creating the same
  project offline end up with one row instead of two. Standalone keeps random ids,
  so re-creating a deleted project or team never re-adopts rows that still point
  at the old id.
* A row defined by nothing but its key, in a table with no delete path (the
  workflow asset backup's profiles, roots, items and scan findings), gets
  ``content_id(kind, *parts)`` in every role: re-observing it is idempotent, and
  two machines that observe the same thing offline arrive at one row.

A leaf module, standard library only: ``aiteam.types`` builds its ``_new_id`` on it,
so it must not import anything of this package.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# Fixed forever: changing either changes every derived id.
_NATURAL_NAMESPACE = uuid.UUID("5d0f8a52-7c1e-5b8e-9b7a-2f6d3c1e9a40")
_CONTENT_NAMESPACE = uuid.UUID("a3c1f0e2-4b7d-5e19-8c6a-0d2f9b4e7a13")
_KEY_SEPARATOR = "\x1f"

# The natural keys rows are derived from, named after the columns that hold them.
PROJECT_ROOT = "projects.root_path"
TEAM_NAME = "teams.name"
WORKFLOW_RUN = "workflow_runs.wf_id"
WORKFLOW_AGENT = "workflow_agents.wf_id+cc_agent_id"
NATURAL_KEYS = frozenset({PROJECT_ROOT, TEAM_NAME, WORKFLOW_RUN, WORKFLOW_AGENT})


class _OperationIds:
    def __init__(self, op_id: str) -> None:
        self._namespace = uuid.UUID(op_id)
        self._issued = 0

    def next(self) -> str:
        self._issued += 1
        return str(uuid.uuid5(self._namespace, str(self._issued)))


_operation: ContextVar[_OperationIds | None] = ContextVar("aiteam_operation_ids", default=None)
_natural_ids_enabled = False


def new_id() -> str:
    """A new row id: reproducible inside an operation context, random outside one."""
    operation = _operation.get()
    return operation.next() if operation is not None else str(uuid.uuid4())


@contextmanager
def operation_ids(op_id: str) -> Iterator[None]:
    """Make the ids created in this block a function of ``op_id`` and their order."""
    token = _operation.set(_OperationIds(op_id))
    try:
        yield
    finally:
        _operation.reset(token)


def set_natural_ids(enabled: bool) -> None:
    """Turn natural-key ids on for this process (a syncing node) or off (standalone)."""
    global _natural_ids_enabled  # noqa: PLW0603 - one process, one node role
    _natural_ids_enabled = enabled


def natural_ids_enabled() -> bool:
    return _natural_ids_enabled


def normalize_root_path(root_path: str) -> str:
    """The literal form a project root is keyed by: NFC, no trailing slash.

    Literal on purpose: resolving symlinks or the real path gives different answers
    depending on whether an external disk is mounted at the moment.
    """
    text = unicodedata.normalize("NFC", root_path)
    stripped = text.rstrip("/")
    return stripped or text[:1]


def derived_id(key: str, *parts: str) -> str | None:
    """The id a row with this natural key gets, or None when ids stay random.

    None when natural ids are off for the process or a key part is empty; the
    caller then keeps the id it already has, so a standalone node draws exactly
    the random ids it always drew.
    """
    if key not in NATURAL_KEYS:
        raise ValueError(f"unknown natural key {key!r}")
    if not _natural_ids_enabled or not all(parts):
        return None
    if key == PROJECT_ROOT:
        parts = (normalize_root_path(parts[0]),)
    return str(uuid.uuid5(_NATURAL_NAMESPACE, _KEY_SEPARATOR.join((key, *parts))))


def content_id(kind: str, *parts: str) -> str:
    """The id of a row defined by its key alone, the same on every node and in every role.

    Only for tables that never delete (see the module docstring): a deleted row
    re-created later takes its old id back. ``kind`` names the table, so equal keys
    of different tables never share an id.
    """
    if not kind:
        raise ValueError("content_id needs a kind")
    return str(uuid.uuid5(_CONTENT_NAMESPACE, _KEY_SEPARATOR.join((kind, *parts))))
