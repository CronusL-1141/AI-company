"""v1.1 schema updates - trust_score, channel_messages, leader_briefings, etc.

Revision ID: a2f8e91c3d47
Revises: d1cf5ca56f2b
Create Date: 2026-04-05 14:00:00.000000

Covers all schema additions introduced in v1.0 and v1.1:
- agents.trust_score (REAL DEFAULT 0.5)
- agents.current_phase_id, agents.project_id (already in DB via app init, add safely)
- events.entity_id, events.entity_type, events.state_snapshot
- channel_messages table (new)
- leader_briefings table (new)
- cross_messages table (new)
- wake_sessions table (new)
- scheduled_tasks table (new)
- agent_activities extra columns: duration_ms, status, error, tokens_input, tokens_output, cost_usd
- tasks.status index
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = "a2f8e91c3d47"
down_revision: Union[str, Sequence[str], None] = "d1cf5ca56f2b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """Check if a column exists in a SQLite table (introspect at migration time)."""
    from alembic.operations import ops  # noqa: F401 – ensure ops context
    bind = op.get_bind()
    result = bind.execute(sa.text(f"PRAGMA table_info({table})"))
    return any(row[1] == column for row in result)


def _table_exists(table: str) -> bool:
    """Check if a table exists in the SQLite database."""
    bind = op.get_bind()
    result = bind.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    )
    return result.fetchone() is not None


def _index_exists(index: str, table: str) -> bool:
    """Check if an index exists in the SQLite database."""
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=:i AND tbl_name=:t"
        ),
        {"i": index, "t": table},
    )
    return result.fetchone() is not None


def upgrade() -> None:
    """Apply v1.1 schema additions."""

    # ------------------------------------------------------------------
    # agents table: trust_score, project_id, current_phase_id
    # ------------------------------------------------------------------
    if not _column_exists("agents", "trust_score"):
        op.add_column(
            "agents",
            sa.Column("trust_score", sa.Float(), nullable=False, server_default="0.5"),
        )

    if not _column_exists("agents", "project_id"):
        op.add_column(
            "agents",
            sa.Column("project_id", sa.String(length=36), nullable=True),
        )

    if not _column_exists("agents", "current_phase_id"):
        op.add_column(
            "agents",
            sa.Column("current_phase_id", sa.String(length=36), nullable=True),
        )

    # ------------------------------------------------------------------
    # events table: entity_id, entity_type, state_snapshot
    # ------------------------------------------------------------------
    if not _column_exists("events", "entity_id"):
        op.add_column(
            "events",
            sa.Column("entity_id", sa.String(length=36), nullable=True),
        )
        op.create_index("ix_events_entity_id", "events", ["entity_id"], unique=False)

    if not _column_exists("events", "entity_type"):
        op.add_column(
            "events",
            sa.Column("entity_type", sa.String(length=50), nullable=True),
        )

    if not _column_exists("events", "state_snapshot"):
        op.add_column(
            "events",
            sa.Column("state_snapshot", sqlite.JSON(), nullable=True),
        )

    # ------------------------------------------------------------------
    # agent_activities: add missing columns from v1.0+
    # ------------------------------------------------------------------
    if not _column_exists("agent_activities", "duration_ms"):
        op.add_column(
            "agent_activities",
            sa.Column("duration_ms", sa.Integer(), nullable=True),
        )

    if not _column_exists("agent_activities", "status"):
        op.add_column(
            "agent_activities",
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="completed",
            ),
        )

    if not _column_exists("agent_activities", "error"):
        op.add_column(
            "agent_activities",
            sa.Column("error", sa.Text(), nullable=True),
        )

    if not _column_exists("agent_activities", "tokens_input"):
        op.add_column(
            "agent_activities",
            sa.Column(
                "tokens_input", sa.Integer(), nullable=False, server_default="0"
            ),
        )

    if not _column_exists("agent_activities", "tokens_output"):
        op.add_column(
            "agent_activities",
            sa.Column(
                "tokens_output", sa.Integer(), nullable=False, server_default="0"
            ),
        )

    if not _column_exists("agent_activities", "cost_usd"):
        op.add_column(
            "agent_activities",
            sa.Column(
                "cost_usd", sa.Float(), nullable=False, server_default="0.0"
            ),
        )

    # ------------------------------------------------------------------
    # tasks: add status index if missing
    # ------------------------------------------------------------------
    if not _index_exists("ix_tasks_status", "tasks"):
        op.create_index("ix_tasks_status", "tasks", ["status"], unique=False)

    # ------------------------------------------------------------------
    # channel_messages table (new in v1.1)
    # ------------------------------------------------------------------
    if not _table_exists("channel_messages"):
        op.create_table(
            "channel_messages",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("channel", sa.String(length=100), nullable=False),
            sa.Column("sender", sa.String(length=100), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("mentions", sqlite.JSON(), nullable=False),
            sa.Column("metadata", sqlite.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_channel_messages_channel",
            "channel_messages",
            ["channel"],
            unique=False,
        )

    # ------------------------------------------------------------------
    # leader_briefings table (new in v1.1)
    # ------------------------------------------------------------------
    if not _table_exists("leader_briefings"):
        op.create_table(
            "leader_briefings",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("title", sa.String(length=500), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("options", sa.Text(), nullable=True),
            sa.Column("recommendation", sa.Text(), nullable=True),
            sa.Column(
                "urgency",
                sa.String(length=20),
                nullable=False,
                server_default="medium",
            ),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("resolution", sa.Text(), nullable=True),
            sa.Column("project_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    # ------------------------------------------------------------------
    # cross_messages table (new in v1.1)
    # ------------------------------------------------------------------
    if not _table_exists("cross_messages"):
        op.create_table(
            "cross_messages",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("from_project_id", sa.String(length=12), nullable=False),
            sa.Column("from_project_dir", sa.String(length=500), nullable=False),
            sa.Column("to_project_id", sa.String(length=12), nullable=True),
            sa.Column("sender_name", sa.String(length=100), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column(
                "message_type",
                sa.String(length=20),
                nullable=False,
                server_default="notification",
            ),
            sa.Column("metadata", sqlite.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("read_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_cross_messages_from_project_id",
            "cross_messages",
            ["from_project_id"],
            unique=False,
        )
        op.create_index(
            "ix_cross_messages_to_project_id",
            "cross_messages",
            ["to_project_id"],
            unique=False,
        )

    # ------------------------------------------------------------------
    # scheduled_tasks table (new in v1.1)
    # ------------------------------------------------------------------
    if not _table_exists("scheduled_tasks"):
        op.create_table(
            "scheduled_tasks",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("team_id", sa.String(length=36), nullable=True),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("interval_seconds", sa.Integer(), nullable=False),
            sa.Column("action_type", sa.String(length=50), nullable=False),
            sa.Column("action_config", sqlite.JSON(), nullable=False),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column("last_run_at", sa.DateTime(), nullable=True),
            sa.Column("next_run_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_scheduled_tasks_team_id",
            "scheduled_tasks",
            ["team_id"],
            unique=False,
        )

    # ------------------------------------------------------------------
    # wake_sessions table (new in v1.1)
    # ------------------------------------------------------------------
    if not _table_exists("wake_sessions"):
        op.create_table(
            "wake_sessions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("scheduled_task_id", sa.String(length=36), nullable=False),
            sa.Column("agent_name", sa.String(length=200), nullable=False),
            sa.Column("team_id", sa.String(length=36), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column(
                "outcome", sa.String(length=50), nullable=False, server_default=""
            ),
            sa.Column("triage_result", sa.Text(), nullable=True),
            sa.Column("stdout_summary", sa.Text(), nullable=True),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column(
                "consecutive_failures",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "duration_seconds",
                sa.Float(),
                nullable=False,
                server_default="0.0",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_wake_sessions_scheduled_task_id",
            "wake_sessions",
            ["scheduled_task_id"],
            unique=False,
        )
        op.create_index(
            "ix_wake_sessions_agent_name",
            "wake_sessions",
            ["agent_name"],
            unique=False,
        )


def downgrade() -> None:
    """Revert v1.1 schema additions."""

    # Drop new tables
    for table in [
        "wake_sessions",
        "scheduled_tasks",
        "cross_messages",
        "leader_briefings",
        "channel_messages",
    ]:
        if _table_exists(table):
            op.drop_table(table)

    # Remove tasks status index
    if _index_exists("ix_tasks_status", "tasks"):
        op.drop_index("ix_tasks_status", table_name="tasks")

    # Remove agent_activities columns
    for col in ["cost_usd", "tokens_output", "tokens_input", "error", "status", "duration_ms"]:
        if _column_exists("agent_activities", col):
            op.drop_column("agent_activities", col)

    # Remove events columns and index
    if _index_exists("ix_events_entity_id", "events"):
        op.drop_index("ix_events_entity_id", table_name="events")
    for col in ["state_snapshot", "entity_type", "entity_id"]:
        if _column_exists("events", col):
            op.drop_column("events", col)

    # Remove agents columns
    for col in ["current_phase_id", "project_id", "trust_score"]:
        if _column_exists("agents", col):
            op.drop_column("agents", col)
