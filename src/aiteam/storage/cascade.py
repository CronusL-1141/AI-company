"""The session-face part of deleting a project: its teams and their events.

One definition for every node that deletes it. A standalone node deletes them all.
On a node that syncs, each node deletes the rows of its own sessions and nobody
else's (``origins``, aiteam.sync.context.own_origins): the center when it carries
the deletion out, an edge when the deletion reaches its replica (aiteam.sync.worker);
the edge's deletions then go up to the center like any write of its sessions.
Agent rows are never deleted: they are the audit trail and carry token attribution
that cannot be rebuilt.

Synchronous, over the caller's connection and transaction.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.engine import Connection

from aiteam.storage.models import EventModel, TeamModel
from aiteam.storage.partition import Origins, within


def project_team_ids(conn: Connection, project_id: str, origins: Origins = None) -> list[str]:
    """The project's teams, those of ``origins`` only when given."""
    query = select(TeamModel.id).where(TeamModel.project_id == project_id, within(TeamModel.origin_node, origins))
    return list(conn.execute(query).scalars())


def delete_project_session_rows(conn: Connection, project_id: str, origins: Origins) -> int:
    """Delete the project's teams of ``origins`` (None: all) and their events; returns the team count."""
    team_ids = project_team_ids(conn, project_id, origins)
    if not team_ids:
        return 0
    conn.execute(delete(TeamModel).where(TeamModel.id.in_(team_ids)))
    # Events have no team column: they point at a team through entity_id.
    conn.execute(delete(EventModel).where(EventModel.entity_id.in_(team_ids), within(EventModel.origin_node, origins)))
    return len(team_ids)
