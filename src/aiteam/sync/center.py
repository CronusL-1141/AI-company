"""The center node: the one authoritative copy of the shared face.

Five parts, each in its own module:

* ``operations`` carries out the edges' operations, each exactly once, merged by rule;
* ``intake`` takes in the edges' session faces, each row from its own node only;
* ``feed`` serves the handshake, the change log and rows to keep replicas with;
* ``conflicts`` keeps the decisions taken between writes, batches and flips them;
* ``wakes`` hands due wakes over to the node whose session they wake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiteam.storage.repository import StorageRepository
from aiteam.sync.conflicts import ConflictDesk
from aiteam.sync.feed import Feed
from aiteam.sync.intake import Intake
from aiteam.sync.operations import Operations
from aiteam.sync.wakes import Wakes


@dataclass(frozen=True)
class Center:
    node_id: str
    operations: Operations
    intake: Intake
    feed: Feed
    conflicts: ConflictDesk
    wakes: Wakes

    @classmethod
    def create(cls, app: Any, repo: StorageRepository, node_id: str) -> Center:
        intake = Intake(repo.db_url, node_id)
        return cls(node_id=node_id, operations=Operations(app, repo, node_id, intake), intake=intake,
                   feed=Feed(repo.db_url), conflicts=ConflictDesk(app, repo), wakes=Wakes(repo, node_id))
