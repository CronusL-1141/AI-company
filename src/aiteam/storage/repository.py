"""AI Team OS — Data persistence repository.

StorageRepository is the unified entry point for all database operations.
Upper-layer modules access data only through this interface.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String as SAString
from sqlalchemy import case, delete, func, select

from aiteam.api.exceptions import NotFoundError
from aiteam.storage.connection import get_session
from aiteam.storage.connection import init_db as _init_db
from aiteam.storage.models import (
    AgentActivityModel,
    AgentModel,
    CrossMessageModel,
    EventModel,
    LeaderBriefingModel,
    MeetingMessageModel,
    MeetingModel,
    MemoryModel,
    PhaseModel,
    ProjectModel,
    ScheduledTaskModel,
    TaskModel,
    TeamModel,
    WakeSessionModel,
)
from aiteam.types import (
    Agent,
    AgentActivity,
    AgentStatus,
    CrossMessage,
    CrossMessageType,
    Event,
    EventType,
    LeaderBriefing,
    Meeting,
    MeetingMessage,
    MeetingStatus,
    Memory,
    MemoryScope,
    OrchestrationMode,
    Phase,
    PhaseStatus,
    Project,
    ScheduledTask,
    Task,
    TaskStatus,
    Team,
    WakeSession,
)


class StorageRepository:
    """Data persistence repository — unified data access interface."""

    def __init__(self, db_url: str | None = None, project_scope: str = "") -> None:
        self._db_url = db_url
        self._project_scope = project_scope  # Current project ID; empty = no filtering

    def _apply_project_filter(self, query: Any, model_class: Any) -> Any:
        """Auto-apply project_id filter if project_scope is set and model has project_id."""
        if self._project_scope and hasattr(model_class, "project_id"):
            query = query.where(model_class.project_id == self._project_scope)
        return query

    async def init_db(self) -> None:
        """Initialize database (create tables / run migrations)."""
        await _init_db(self._db_url)

    # ================================================================
    # Projects
    # ================================================================

    async def create_project(
        self,
        name: str,
        root_path: str = "",
        description: str = "",
        config: dict | None = None,
    ) -> Project:
        """Create a project."""
        project = Project(
            name=name,
            root_path=root_path,
            description=description,
            config=config or {},
        )
        orm = ProjectModel.from_pydantic(project)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return project

    async def get_project(self, project_id: str) -> Project | None:
        """Get a project by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ProjectModel).where(ProjectModel.id == project_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_projects(self) -> list[Project]:
        """List all projects."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ProjectModel).order_by(ProjectModel.created_at.desc())
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_project(self, project_id: str, **kwargs: object) -> Project | None:
        """Update project information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ProjectModel).where(ProjectModel.id == project_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None

            kwargs["updated_at"] = datetime.now()

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def delete_project(self, project_id: str) -> bool:
        """Delete a project."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(ProjectModel).where(ProjectModel.id == project_id)
            )
            return result.rowcount > 0  # type: ignore[union-attr]

    async def get_project_by_root_path(self, root_path: str) -> Project | None:
        """Get a project by root_path."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ProjectModel).where(ProjectModel.root_path == root_path)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    # ================================================================
    # Phases
    # ================================================================

    async def create_phase(
        self,
        project_id: str,
        name: str,
        description: str = "",
        order: int = 0,
        config: dict | None = None,
    ) -> Phase:
        """Create a phase."""
        phase = Phase(
            project_id=project_id,
            name=name,
            description=description,
            order=order,
            config=config or {},
        )
        orm = PhaseModel.from_pydantic(phase)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return phase

    async def get_phase(self, phase_id: str) -> Phase | None:
        """Get a phase by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(PhaseModel).where(PhaseModel.id == phase_id))
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_phases(self, project_id: str) -> list[Phase]:
        """List all phases under a project, sorted by order."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(PhaseModel)
                .where(PhaseModel.project_id == project_id)
                .order_by(PhaseModel.order, PhaseModel.created_at)
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_phase(self, phase_id: str, **kwargs: object) -> Phase | None:
        """Update phase information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(PhaseModel).where(PhaseModel.id == phase_id))
            row = result.scalar_one_or_none()
            if row is None:
                return None

            # Handle status field: convert to string value
            if "status" in kwargs:
                status_val = kwargs["status"]
                if isinstance(status_val, PhaseStatus):
                    kwargs["status"] = status_val.value
                elif isinstance(status_val, str):
                    PhaseStatus(status_val)

            kwargs["updated_at"] = datetime.now()

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def delete_phase(self, phase_id: str) -> bool:
        """Delete a phase."""
        async with get_session(self._db_url) as session:
            result = await session.execute(delete(PhaseModel).where(PhaseModel.id == phase_id))
            return result.rowcount > 0  # type: ignore[union-attr]

    async def get_active_phase(self, project_id: str) -> Phase | None:
        """Get the currently active phase of a project."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(PhaseModel).where(
                    PhaseModel.project_id == project_id,
                    PhaseModel.status == PhaseStatus.ACTIVE.value,
                )
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def deactivate_phases(self, project_id: str) -> int:
        """Set all active phases under a project to completed."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(PhaseModel).where(
                    PhaseModel.project_id == project_id,
                    PhaseModel.status == PhaseStatus.ACTIVE.value,
                )
            )
            rows = result.scalars().all()
            for row in rows:
                row.status = PhaseStatus.COMPLETED.value
                row.updated_at = datetime.now()
            return len(rows)

    # ================================================================
    # Teams
    # ================================================================

    async def create_team(
        self,
        name: str,
        mode: str,
        config: dict | None = None,
        **kwargs: Any,
    ) -> Team:
        """Create a team."""
        # Auto-fill project_id from scope if not explicitly provided
        effective_project_id = kwargs.get("project_id") or (self._project_scope or None)
        team = Team(
            name=name,
            mode=OrchestrationMode(mode),
            config=config or {},
            project_id=effective_project_id,
            leader_agent_id=kwargs.get("leader_agent_id"),
        )
        orm = TeamModel.from_pydantic(team)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return team

    async def get_team(self, team_id: str) -> Team | None:
        """Get a team by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(TeamModel).where(TeamModel.id == team_id))
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def get_team_by_name(self, name: str) -> Team | None:
        """Get a team by name."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(TeamModel).where(TeamModel.name == name))
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_teams(self) -> list[Team]:
        """List all teams."""
        async with get_session(self._db_url) as session:
            stmt = select(TeamModel).order_by(TeamModel.created_at.desc())
            stmt = self._apply_project_filter(stmt, TeamModel)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def list_teams_by_project(self, project_id: str) -> list[Team]:
        """List all teams under a project."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel)
                .where(TeamModel.project_id == project_id)
                .order_by(TeamModel.created_at.desc())
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def find_active_team_by_leader(self, leader_agent_id: str) -> Team | None:
        """Find the active team currently led by a Leader."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel)
                .where(TeamModel.leader_agent_id == leader_agent_id)
                .where(TeamModel.status == "active")
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def find_leader_by_project(self, project_id: str) -> Agent | None:
        """Find the Leader agent for a project (role=leader + project_id match)."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(AgentModel)
                .where(AgentModel.project_id == project_id)
                .where(AgentModel.role == "leader")
                .order_by(AgentModel.created_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def update_team(self, team_id: str, **kwargs: object) -> Team:
        """Update team information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(TeamModel).where(TeamModel.id == team_id))
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"团队 {team_id} 不存在"
                raise NotFoundError(msg)

            # Handle mode field: convert to string value
            if "mode" in kwargs:
                mode_val = kwargs["mode"]
                if isinstance(mode_val, OrchestrationMode):
                    kwargs["mode"] = mode_val.value
                elif isinstance(mode_val, str):
                    # Validate the value is valid
                    OrchestrationMode(mode_val)

            kwargs["updated_at"] = datetime.now()

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def delete_team(self, team_id: str) -> bool:
        """Delete a team."""
        async with get_session(self._db_url) as session:
            result = await session.execute(delete(TeamModel).where(TeamModel.id == team_id))
            return result.rowcount > 0  # type: ignore[union-attr]

    # ================================================================
    # Agents
    # ================================================================

    async def create_agent(self, team_id: str, name: str, role: str, **kwargs: object) -> Agent:
        """Create an Agent."""
        agent = Agent(
            team_id=team_id,
            name=name,
            role=role,
            system_prompt=str(kwargs.get("system_prompt", "")),
            model=str(kwargs.get("model", "claude-opus-4-6")),
            config=kwargs.get("config", {}),  # type: ignore[arg-type]
            source=str(kwargs.get("source", "api")),
            session_id=kwargs.get("session_id"),  # type: ignore[arg-type]
            cc_tool_use_id=kwargs.get("cc_tool_use_id"),  # type: ignore[arg-type]
        )
        orm = AgentModel.from_pydantic(agent)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return agent

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Get an Agent by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(AgentModel).where(AgentModel.id == agent_id))
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_agents(self, team_id: str) -> list[Agent]:
        """List all Agents in a team."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentModel)
                .where(AgentModel.team_id == team_id)
                .order_by(AgentModel.created_at)
            )
            stmt = self._apply_project_filter(stmt, AgentModel)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_agent(self, agent_id: str, **kwargs: object) -> Agent:
        """Update Agent information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(AgentModel).where(AgentModel.id == agent_id))
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"Agent {agent_id} 不存在"
                raise NotFoundError(msg)

            # Handle status field: convert to string value
            if "status" in kwargs:
                status_val = kwargs["status"]
                if isinstance(status_val, AgentStatus):
                    kwargs["status"] = status_val.value
                elif isinstance(status_val, str):
                    AgentStatus(status_val)

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete an Agent."""
        async with get_session(self._db_url) as session:
            result = await session.execute(delete(AgentModel).where(AgentModel.id == agent_id))
            return result.rowcount > 0  # type: ignore[union-attr]

    # ================================================================
    # Tasks
    # ================================================================

    async def create_task(
        self, team_id: str | None, title: str, description: str = "", **kwargs: object
    ) -> Task:
        """Create a task.

        team_id can be None (project-level task, not bound to a team).
        """
        # Build optional parameters
        optional: dict[str, object] = {}
        for key in (
            "assigned_to",
            "parent_id",
            "project_id",
            "depends_on",
            "depth",
            "order",
            "template_id",
            "priority",
            "horizon",
            "tags",
            "config",
        ):
            if key in kwargs:
                optional[key] = kwargs[key]
        # Set default values
        optional.setdefault("depends_on", [])
        optional.setdefault("depth", 0)
        optional.setdefault("order", 0)

        # Auto-fill project_id from scope if not explicitly provided
        if self._project_scope and not optional.get("project_id"):
            optional["project_id"] = self._project_scope

        task = Task(
            team_id=team_id,
            title=title,
            description=description,
            **optional,  # type: ignore[arg-type]
        )
        orm = TaskModel.from_pydantic(task)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return task

    async def list_subtasks(self, parent_id: str) -> list[Task]:
        """List all subtasks of a task, sorted by order."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(TaskModel)
                .where(TaskModel.parent_id == parent_id)
                .order_by(TaskModel.order, TaskModel.created_at)
            )
            stmt = self._apply_project_filter(stmt, TaskModel)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_tasks(self, team_id: str, status: TaskStatus | None = None) -> list[Task]:
        """List team tasks, optionally filtered by status."""
        async with get_session(self._db_url) as session:
            stmt = select(TaskModel).where(TaskModel.team_id == team_id)
            stmt = self._apply_project_filter(stmt, TaskModel)
            if status is not None:
                stmt = stmt.where(TaskModel.status == status.value)
            stmt = stmt.order_by(TaskModel.created_at.desc())
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def list_tasks_by_project(
        self, project_id: str, status: TaskStatus | None = None
    ) -> list[Task]:
        """List all tasks under a project (including project-level tasks with team_id=None and team tasks)."""
        async with get_session(self._db_url) as session:
            stmt = select(TaskModel).where(TaskModel.project_id == project_id)
            if status is not None:
                stmt = stmt.where(TaskModel.status == status.value)
            stmt = stmt.order_by(TaskModel.created_at.desc())
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_task(self, task_id: str, **kwargs: object) -> Task:
        """Update task information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(TaskModel).where(TaskModel.id == task_id))
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"任务 {task_id} 不存在"
                raise NotFoundError(msg)

            # Handle status field: convert to string value
            if "status" in kwargs:
                status_val = kwargs["status"]
                if isinstance(status_val, TaskStatus):
                    kwargs["status"] = status_val.value
                elif isinstance(status_val, str):
                    TaskStatus(status_val)

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def get_downstream_tasks(self, task_id: str) -> list[Task]:
        """Find all tasks whose depends_on contains task_id (i.e., downstream tasks depending on this task)."""
        async with get_session(self._db_url) as session:
            # SQL LIKE pre-filter to narrow down results, then Python-level precise check on JSON array
            stmt = select(TaskModel).where(
                TaskModel.depends_on.cast(SAString).contains(task_id),
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            downstream = []
            for row in rows:
                deps = row.depends_on if isinstance(row.depends_on, list) else []
                if task_id in deps:
                    downstream.append(row.to_pydantic())
            return downstream

    async def resolve_task_dependencies(self, task_id: str) -> list[Task]:
        """Cascade unlock when a task completes.

        Check all downstream BLOCKED tasks that depend on this task_id.
        If all their dependencies are completed, unlock them to PENDING.

        Returns:
            List of unblocked tasks.
        """
        downstream = await self.get_downstream_tasks(task_id)
        unblocked: list[Task] = []

        for task in downstream:
            if task.status != TaskStatus.BLOCKED:
                continue

            # Check if all dependencies of this task are completed
            all_deps_done = True
            for dep_id in task.depends_on:
                dep_task = await self.get_task(dep_id)
                if dep_task is None or dep_task.status != TaskStatus.COMPLETED:
                    all_deps_done = False
                    break

            if all_deps_done:
                updated = await self.update_task(task.id, status=TaskStatus.PENDING.value)
                unblocked.append(updated)

        return unblocked

    async def detect_dependency_cycle(self, task_id: str, new_dep_id: str) -> bool:
        """Detect if adding a dependency would create a cycle.

        Starting from new_dep_id, trace upstream along the depends_on chain.
        If it leads back to task_id, a cycle would be formed.

        Returns:
            True if a cycle exists, False if safe.
        """
        # Self-dependency is a direct cycle
        if task_id == new_dep_id:
            return True

        visited: set[str] = set()
        stack = [new_dep_id]

        while stack:
            current_id = stack.pop()
            if current_id in visited:
                continue
            visited.add(current_id)

            current_task = await self.get_task(current_id)
            if current_task is None:
                continue

            for dep_id in current_task.depends_on:
                if dep_id == task_id:
                    return True  # Cycle found
                if dep_id not in visited:
                    stack.append(dep_id)

        return False

    # ================================================================
    # Events
    # ================================================================

    async def create_event(self, event_type: str, source: str, data: dict) -> Event:
        """Create a system event."""
        event = Event(
            type=EventType(event_type),
            source=source,
            data=data,
        )
        orm = EventModel.from_pydantic(event)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return event

    async def list_events(
        self,
        event_type: str | None = None,
        source: str | None = None,
        limit: int = 50,
        type_prefix: str | None = None,
    ) -> list[Event]:
        """List events, optionally filtered by type and source.

        Args:
            event_type: Exact match on event type (e.g., "agent.created")
            source: Exact match on event source
            limit: Maximum number of results to return
            type_prefix: Prefix match on event type (e.g., "decision." matches all decision events)
        """
        async with get_session(self._db_url) as session:
            stmt = select(EventModel)
            if event_type is not None:
                stmt = stmt.where(EventModel.type == event_type)
            elif type_prefix is not None:
                stmt = stmt.where(EventModel.type.like(f"{type_prefix}%"))
            if source is not None:
                stmt = stmt.where(EventModel.source == source)
            stmt = stmt.order_by(EventModel.timestamp.desc()).limit(limit)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Memories
    # ================================================================

    async def create_memory(
        self,
        scope: str,
        scope_id: str,
        content: str,
        metadata: dict | None = None,
    ) -> Memory:
        """Create a memory."""
        memory = Memory(
            scope=MemoryScope(scope),
            scope_id=scope_id,
            content=content,
            metadata=metadata or {},
        )
        orm = MemoryModel.from_pydantic(memory)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return memory

    async def get_memory(self, memory_id: str) -> Memory | None:
        """Get a memory by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(select(MemoryModel).where(MemoryModel.id == memory_id))
            row = result.scalar_one_or_none()
            if row is not None:
                # Update access time
                row.accessed_at = datetime.now()
                return row.to_pydantic()
            return None

    async def list_memories(self, scope: str, scope_id: str) -> list[Memory]:
        """List all memories within the specified scope."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(MemoryModel)
                .where(
                    MemoryModel.scope == scope,
                    MemoryModel.scope_id == scope_id,
                )
                .order_by(MemoryModel.created_at.desc())
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def search_memories(
        self, scope: str, scope_id: str, query: str, limit: int = 5
    ) -> list[Memory]:
        """Search memories (M1 phase uses simple LIKE keyword matching)."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(MemoryModel)
                .where(
                    MemoryModel.scope == scope,
                    MemoryModel.scope_id == scope_id,
                    MemoryModel.content.ilike(
                        "%{}%".format(query.replace("%", "\\%").replace("_", "\\_")),
                    ),
                )
                .order_by(MemoryModel.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def delete_memory(self, memory_id: str) -> bool:
        """Delete a memory."""
        async with get_session(self._db_url) as session:
            result = await session.execute(delete(MemoryModel).where(MemoryModel.id == memory_id))
            return result.rowcount > 0  # type: ignore[union-attr]

    async def list_team_knowledge(
        self,
        team_id: str,
        memory_type: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """List team knowledge base (memories with scope=team), with optional type filtering.

        Args:
            team_id: Team ID
            memory_type: Optional type filter, matches metadata.type field
                         e.g., failure_alchemy / lesson_learned / loop_review
            limit: Maximum number of results to return
        """
        async with get_session(self._db_url) as session:
            stmt = (
                select(MemoryModel)
                .where(
                    MemoryModel.scope == MemoryScope.TEAM.value,
                    MemoryModel.scope_id == team_id,
                )
                .order_by(MemoryModel.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            memories = [r.to_pydantic() for r in rows]

        if memory_type:
            memories = [m for m in memories if m.metadata.get("type") == memory_type]
        return memories

    async def list_agent_experience(
        self,
        agent_id: str,
        limit: int = 50,
    ) -> list[Memory]:
        """List an Agent's experience memories (scope=agent).

        Args:
            agent_id: Agent ID
            limit: Maximum number of results to return
        """
        async with get_session(self._db_url) as session:
            stmt = (
                select(MemoryModel)
                .where(
                    MemoryModel.scope == MemoryScope.AGENT.value,
                    MemoryModel.scope_id == agent_id,
                )
                .order_by(MemoryModel.created_at.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Meetings
    # ================================================================

    async def create_meeting(
        self,
        team_id: str,
        topic: str,
        participants: list[str] | None = None,
    ) -> Meeting:
        """Create a meeting."""
        meeting = Meeting(
            team_id=team_id,
            topic=topic,
            participants=participants or [],
        )
        orm = MeetingModel.from_pydantic(meeting)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return meeting

    async def get_meeting(self, meeting_id: str) -> Meeting | None:
        """Get a meeting by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(MeetingModel).where(MeetingModel.id == meeting_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_meetings(
        self,
        team_id: str,
        status: MeetingStatus | None = None,
    ) -> list[Meeting]:
        """List team meetings, optionally filtered by status."""
        async with get_session(self._db_url) as session:
            stmt = select(MeetingModel).where(MeetingModel.team_id == team_id)
            stmt = self._apply_project_filter(stmt, MeetingModel)
            if status is not None:
                stmt = stmt.where(MeetingModel.status == status.value)
            stmt = stmt.order_by(MeetingModel.created_at.desc())
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_meeting(self, meeting_id: str, **kwargs: object) -> Meeting:
        """Update meeting information."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(MeetingModel).where(MeetingModel.id == meeting_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"会议 {meeting_id} 不存在"
                raise NotFoundError(msg)

            # Handle status field: convert to string value
            if "status" in kwargs:
                status_val = kwargs["status"]
                if isinstance(status_val, MeetingStatus):
                    kwargs["status"] = status_val.value
                elif isinstance(status_val, str):
                    MeetingStatus(status_val)

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def create_meeting_message(
        self,
        meeting_id: str,
        agent_id: str,
        agent_name: str,
        content: str,
        round_number: int = 1,
    ) -> MeetingMessage:
        """Create a meeting message."""
        message = MeetingMessage(
            meeting_id=meeting_id,
            agent_id=agent_id,
            agent_name=agent_name,
            content=content,
            round_number=round_number,
        )
        orm = MeetingMessageModel.from_pydantic(message)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return message

    async def list_meeting_messages(
        self,
        meeting_id: str,
        limit: int = 100,
    ) -> list[MeetingMessage]:
        """List meeting messages."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(MeetingMessageModel)
                .where(MeetingMessageModel.meeting_id == meeting_id)
                .order_by(MeetingMessageModel.timestamp)
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def get_expired_meetings(self, hours: int = 24) -> list[Meeting]:
        """Get active meetings with no new messages for more than the specified hours.

        Determination logic:
        - Meetings with messages: last message timestamp is more than `hours` ago
        - Meetings without messages: creation time is more than `hours` ago
        """
        cutoff = datetime.now() - timedelta(hours=hours)
        async with get_session(self._db_url) as session:
            # Subquery: last message time for each meeting
            last_msg_subq = (
                select(
                    MeetingMessageModel.meeting_id,
                    func.max(MeetingMessageModel.timestamp).label("last_msg_time"),
                )
                .group_by(MeetingMessageModel.meeting_id)
                .subquery()
            )

            # Main query: active meetings LEFT JOIN last message time
            stmt = (
                select(MeetingModel)
                .outerjoin(
                    last_msg_subq,
                    MeetingModel.id == last_msg_subq.c.meeting_id,
                )
                .where(
                    MeetingModel.status == MeetingStatus.ACTIVE.value,
                    # Use last message time if available, otherwise use creation time
                    func.coalesce(
                        last_msg_subq.c.last_msg_time,
                        MeetingModel.created_at,
                    )
                    < cutoff,
                )
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def conclude_meeting(self, meeting_id: str) -> Meeting | None:
        """Conclude a meeting, setting its status to concluded."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(MeetingModel).where(MeetingModel.id == meeting_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.status = MeetingStatus.CONCLUDED.value
            row.concluded_at = datetime.now()
            return row.to_pydantic()

    # ================================================================
    # Hooks — CC session-related queries
    # ================================================================

    async def find_agent_by_session(
        self,
        session_id: str,
        agent_name: str,
    ) -> Agent | None:
        """Find a registered Agent by CC session ID and Agent name."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentModel)
                .where(
                    AgentModel.session_id == session_id,
                    AgentModel.name == agent_name,
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def find_agents_by_session(self, session_id: str) -> list[Agent]:
        """Find all Agents associated with a CC session."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentModel)
                .where(AgentModel.session_id == session_id)
                .order_by(AgentModel.created_at)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def find_agent_by_cc_id(self, cc_agent_id: str) -> Agent | None:
        """Find an Agent by CC internal agent_id."""
        async with get_session(self._db_url) as session:
            stmt = select(AgentModel).where(AgentModel.cc_tool_use_id == cc_agent_id).limit(1)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def find_agents_by_role(self, role: str) -> list[Agent]:
        """Find all Agents by role (across teams)."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentModel)
                .where(AgentModel.role == role)
                .order_by(AgentModel.created_at.desc())
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def count_agents_by_source(
        self,
        source: str,
        session_id: str | None = None,
    ) -> int:
        """Count Agents by source, optionally filtered by session."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(func.count())
                .select_from(AgentModel)
                .where(
                    AgentModel.source == source,
                )
            )
            if session_id is not None:
                stmt = stmt.where(AgentModel.session_id == session_id)
            result = await session.execute(stmt)
            return result.scalar_one()

    # ================================================================
    # Agent Activities — tool call activity logs
    # ================================================================

    async def create_activity(
        self,
        agent_id: str,
        session_id: str,
        tool_name: str,
        input_summary: str = "",
        output_summary: str = "",
        status: str = "completed",
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> AgentActivity:
        """Record a single tool call activity for an Agent."""
        activity = AgentActivity(
            agent_id=agent_id,
            session_id=session_id,
            tool_name=tool_name,
            input_summary=input_summary[:500],
            output_summary=output_summary[:500],
            status=status,
            duration_ms=duration_ms,
            error=error,
        )
        orm = AgentActivityModel.from_pydantic(activity)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return activity

    async def find_running_activity(
        self,
        agent_id: str,
        session_id: str,
        tool_name: str,
    ) -> AgentActivity | None:
        """Find a matching running-status activity (Pre→Post correlation)."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentActivityModel)
                .where(
                    AgentActivityModel.agent_id == agent_id,
                    AgentActivityModel.session_id == session_id,
                    AgentActivityModel.tool_name == tool_name,
                    AgentActivityModel.status == "running",
                )
                .order_by(AgentActivityModel.timestamp.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def update_activity(
        self,
        activity_id: str,
        **kwargs: Any,
    ) -> None:
        """Update activity fields (used by PostToolUse to backfill duration_ms/status/output)."""
        async with get_session(self._db_url) as session:
            stmt = select(AgentActivityModel).where(AgentActivityModel.id == activity_id)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return
            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            session.add(row)

    async def list_activities(
        self,
        agent_id: str,
        limit: int = 50,
    ) -> list[AgentActivity]:
        """Get an Agent's activity log."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentActivityModel)
                .where(AgentActivityModel.agent_id == agent_id)
                .order_by(AgentActivityModel.timestamp.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def list_activities_by_session(
        self,
        session_id: str,
        limit: int = 100,
    ) -> list[AgentActivity]:
        """Get all activities under a session."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentActivityModel)
                .where(AgentActivityModel.session_id == session_id)
                .order_by(AgentActivityModel.timestamp.desc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def list_activities_by_team(
        self,
        team_id: str,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[AgentActivity]:
        """Get activity logs for all agents under a team, sorted by timestamp descending."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentActivityModel)
                .join(AgentModel, AgentActivityModel.agent_id == AgentModel.id)
                .where(AgentModel.team_id == team_id)
                .order_by(AgentActivityModel.timestamp.desc())
                .limit(limit)
            )
            if agent_id is not None:
                stmt = stmt.where(AgentActivityModel.agent_id == agent_id)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Analytics — aggregate statistics queries
    # ================================================================

    async def count_activities_by_tool(
        self,
        agent_id: str | None = None,
        team_id: str | None = None,
    ) -> list[dict]:
        """Count activities grouped by tool name."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(
                    AgentActivityModel.tool_name,
                    func.count().label("count"),
                )
                .group_by(AgentActivityModel.tool_name)
                .order_by(func.count().desc())
            )
            if agent_id is not None:
                stmt = stmt.where(AgentActivityModel.agent_id == agent_id)
            if team_id is not None:
                # Filter by team via agent table join
                stmt = stmt.join(
                    AgentModel,
                    AgentActivityModel.agent_id == AgentModel.id,
                ).where(AgentModel.team_id == team_id)

            result = await session.execute(stmt)
            return [{"tool_name": row.tool_name, "count": row.count} for row in result.all()]

    async def get_activity_timeline(
        self,
        team_id: str | None = None,
        hours: int = 24,
    ) -> list[dict]:
        """Count activities by hour (last N hours)."""
        cutoff = datetime.now() - timedelta(hours=hours)
        async with get_session(self._db_url) as session:
            # Use strftime to extract hour granularity (SQLite compatible)
            hour_expr = func.strftime("%Y-%m-%d %H:00", AgentActivityModel.timestamp)
            stmt = (
                select(
                    hour_expr.label("hour"),
                    func.count().label("count"),
                )
                .where(AgentActivityModel.timestamp >= cutoff)
                .group_by(hour_expr)
                .order_by(hour_expr)
            )
            if team_id is not None:
                stmt = stmt.join(
                    AgentModel,
                    AgentActivityModel.agent_id == AgentModel.id,
                ).where(AgentModel.team_id == team_id)

            result = await session.execute(stmt)
            return [{"hour": row.hour, "count": row.count} for row in result.all()]

    async def get_agent_productivity(
        self,
        team_id: str | None = None,
    ) -> list[dict]:
        """Productivity metrics per Agent: activity count, tool diversity, last active time."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(
                    AgentActivityModel.agent_id,
                    AgentModel.name.label("agent_name"),
                    func.count().label("activity_count"),
                    func.count(AgentActivityModel.tool_name.distinct()).label("tools_used"),
                    func.max(AgentActivityModel.timestamp).label("last_active"),
                )
                .join(
                    AgentModel,
                    AgentActivityModel.agent_id == AgentModel.id,
                )
                .group_by(AgentActivityModel.agent_id, AgentModel.name)
                .order_by(func.count().desc())
            )
            if team_id is not None:
                stmt = stmt.where(AgentModel.team_id == team_id)

            result = await session.execute(stmt)
            rows = result.all()

            return [
                {
                    "agent_id": row.agent_id,
                    "agent_name": row.agent_name or "unknown",
                    "activity_count": row.activity_count,
                    "tools_used": row.tools_used,
                    "last_active": row.last_active.isoformat() if row.last_active else None,
                }
                for row in rows
            ]

    async def get_task_completion_stats(
        self,
        team_id: str | None = None,
    ) -> dict:
        """Task completion rate and average completion time statistics."""
        async with get_session(self._db_url) as session:
            stmt = select(
                func.count().label("total"),
                func.sum(
                    case(
                        (TaskModel.status == TaskStatus.COMPLETED.value, 1),
                        else_=0,
                    )
                ).label("completed"),
                func.avg(
                    case(
                        (
                            TaskModel.completed_at.isnot(None),
                            func.julianday(TaskModel.completed_at)
                            - func.julianday(TaskModel.created_at),
                        ),
                        else_=None,
                    )
                ).label("avg_completion_days"),
            ).select_from(TaskModel)

            if team_id is not None:
                stmt = stmt.where(TaskModel.team_id == team_id)

            result = await session.execute(stmt)
            row = result.one()

            total = row.total or 0
            completed = row.completed or 0
            avg_days = row.avg_completion_days

            return {
                "total_tasks": total,
                "completed_tasks": completed,
                "completion_rate": round(completed / total, 4) if total > 0 else 0,
                "avg_completion_hours": round(avg_days * 24, 2) if avg_days else None,
            }

    async def get_agent_utilization(
        self,
        team_id: str | None = None,
    ) -> list[dict]:
        """Agent utilization: calculate active period ratio based on activity timestamps."""
        async with get_session(self._db_url) as session:
            # Query each Agent's activity time span and count (JOIN to get name, avoid N+1)
            stmt = (
                select(
                    AgentActivityModel.agent_id,
                    AgentModel.name.label("agent_name"),
                    func.count().label("activity_count"),
                    func.min(AgentActivityModel.timestamp).label("first_active"),
                    func.max(AgentActivityModel.timestamp).label("last_active"),
                    func.count(AgentActivityModel.tool_name.distinct()).label("tools_used"),
                )
                .join(
                    AgentModel,
                    AgentActivityModel.agent_id == AgentModel.id,
                )
                .group_by(AgentActivityModel.agent_id, AgentModel.name)
                .order_by(func.count().desc())
            )
            if team_id is not None:
                stmt = stmt.where(AgentModel.team_id == team_id)

            result = await session.execute(stmt)
            rows = result.all()

            output: list[dict] = []
            for row in rows:
                # Estimate utilization: activity density (activities per hour)
                span_hours = 0.0
                if row.first_active and row.last_active:
                    span = (row.last_active - row.first_active).total_seconds() / 3600
                    span_hours = max(span, 1.0)  # Minimum 1 hour

                utilization = round(row.activity_count / span_hours, 2) if span_hours > 0 else 0

                output.append(
                    {
                        "agent_id": row.agent_id,
                        "agent_name": row.agent_name or "unknown",
                        "activity_count": row.activity_count,
                        "tools_used": row.tools_used,
                        "span_hours": round(span_hours, 2),
                        "activities_per_hour": utilization,
                        "first_active": row.first_active.isoformat() if row.first_active else None,
                        "last_active": row.last_active.isoformat() if row.last_active else None,
                    }
                )
            return output

    async def get_token_costs(
        self,
        group_by: str = "agent",
        days: int = 7,
    ) -> list[dict]:
        """Aggregate token costs from activities, grouped by agent / team / task.

        Args:
            group_by: Grouping dimension — "agent", "team", or "task".
            days: Look-back window in days (default 7).

        Returns:
            List of dicts sorted by total_cost desc, each containing
            group_key, label, total_tokens_input, total_tokens_output,
            total_tokens, total_cost_usd, activity_count.
        """
        cutoff = datetime.now() - timedelta(days=days)
        async with get_session(self._db_url) as session:
            if group_by == "team":
                stmt = (
                    select(
                        AgentModel.team_id.label("group_key"),
                        AgentModel.team_id.label("label"),
                        func.sum(AgentActivityModel.tokens_input).label("total_tokens_input"),
                        func.sum(AgentActivityModel.tokens_output).label("total_tokens_output"),
                        func.sum(
                            AgentActivityModel.tokens_input + AgentActivityModel.tokens_output
                        ).label("total_tokens"),
                        func.sum(AgentActivityModel.cost_usd).label("total_cost_usd"),
                        func.count().label("activity_count"),
                    )
                    .join(AgentModel, AgentActivityModel.agent_id == AgentModel.id)
                    .where(AgentActivityModel.timestamp >= cutoff)
                    .group_by(AgentModel.team_id)
                    .order_by(func.sum(AgentActivityModel.cost_usd).desc())
                )
            elif group_by == "task":
                # Group by session_id as a proxy for task (no direct task FK on activity)
                stmt = (
                    select(
                        AgentActivityModel.session_id.label("group_key"),
                        AgentActivityModel.session_id.label("label"),
                        func.sum(AgentActivityModel.tokens_input).label("total_tokens_input"),
                        func.sum(AgentActivityModel.tokens_output).label("total_tokens_output"),
                        func.sum(
                            AgentActivityModel.tokens_input + AgentActivityModel.tokens_output
                        ).label("total_tokens"),
                        func.sum(AgentActivityModel.cost_usd).label("total_cost_usd"),
                        func.count().label("activity_count"),
                    )
                    .where(AgentActivityModel.timestamp >= cutoff)
                    .group_by(AgentActivityModel.session_id)
                    .order_by(func.sum(AgentActivityModel.cost_usd).desc())
                )
            else:
                # Default: group by agent
                stmt = (
                    select(
                        AgentActivityModel.agent_id.label("group_key"),
                        AgentModel.name.label("label"),
                        func.sum(AgentActivityModel.tokens_input).label("total_tokens_input"),
                        func.sum(AgentActivityModel.tokens_output).label("total_tokens_output"),
                        func.sum(
                            AgentActivityModel.tokens_input + AgentActivityModel.tokens_output
                        ).label("total_tokens"),
                        func.sum(AgentActivityModel.cost_usd).label("total_cost_usd"),
                        func.count().label("activity_count"),
                    )
                    .join(AgentModel, AgentActivityModel.agent_id == AgentModel.id)
                    .where(AgentActivityModel.timestamp >= cutoff)
                    .group_by(AgentActivityModel.agent_id, AgentModel.name)
                    .order_by(func.sum(AgentActivityModel.cost_usd).desc())
                )

            result = await session.execute(stmt)
            rows = result.all()
            return [
                {
                    "group_key": row.group_key,
                    "label": row.label or row.group_key,
                    "total_tokens_input": row.total_tokens_input or 0,
                    "total_tokens_output": row.total_tokens_output or 0,
                    "total_tokens": row.total_tokens or 0,
                    "total_cost_usd": round(row.total_cost_usd or 0.0, 6),
                    "activity_count": row.activity_count,
                }
                for row in rows
            ]

    # ================================================================
    # Cross-project messages — always in the global default DB
    # ================================================================

    async def create_cross_message(
        self,
        from_project_id: str,
        from_project_dir: str,
        to_project_id: str | None,
        sender_name: str,
        content: str,
        message_type: str = "notification",
        metadata: dict | None = None,
    ) -> CrossMessage:
        """Create a cross-project message.

        Args:
            from_project_id: Sender's 12-char project ID (from compute_project_id).
            from_project_dir: Sender's project directory path.
            to_project_id: Recipient's 12-char project ID, or None for broadcast.
            sender_name: Name of the sending agent / component.
            content: Message body.
            message_type: One of notification / request / response / broadcast.
            metadata: Optional extra data dict.

        Returns:
            Created CrossMessage Pydantic model.
        """
        msg = CrossMessage(
            from_project_id=from_project_id,
            from_project_dir=from_project_dir,
            to_project_id=to_project_id,
            sender_name=sender_name,
            content=content,
            message_type=CrossMessageType(message_type),
            metadata=metadata or {},
        )
        orm = CrossMessageModel.from_pydantic(msg)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return msg

    async def list_cross_messages(
        self,
        project_id: str,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[CrossMessage]:
        """List inbox messages for a project.

        Returns messages where to_project_id == project_id (direct)
        OR to_project_id IS NULL (broadcast), sorted newest-first.

        Args:
            project_id: Recipient project's 12-char ID.
            unread_only: If True, exclude messages that have been read.
            limit: Maximum number of results to return.

        Returns:
            List of CrossMessage sorted by created_at descending.
        """
        from sqlalchemy import or_

        async with get_session(self._db_url) as session:
            stmt = (
                select(CrossMessageModel)
                .where(
                    or_(
                        CrossMessageModel.to_project_id == project_id,
                        CrossMessageModel.to_project_id.is_(None),
                    )
                )
                .order_by(CrossMessageModel.created_at.desc())
                .limit(limit)
            )
            if unread_only:
                stmt = stmt.where(CrossMessageModel.read_at.is_(None))
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def mark_cross_message_read(self, message_id: str) -> CrossMessage | None:
        """Mark a cross-project message as read.

        Args:
            message_id: Message UUID.

        Returns:
            Updated CrossMessage, or None if not found.
        """
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(CrossMessageModel).where(CrossMessageModel.id == message_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.read_at = datetime.now()
            return row.to_pydantic()

    async def count_unread_cross_messages(self, project_id: str) -> int:
        """Count unread messages in a project's inbox (direct + broadcast).

        Args:
            project_id: Recipient project's 12-char ID.

        Returns:
            Number of unread messages.
        """
        from sqlalchemy import or_

        async with get_session(self._db_url) as session:
            stmt = (
                select(func.count())
                .select_from(CrossMessageModel)
                .where(
                    or_(
                        CrossMessageModel.to_project_id == project_id,
                        CrossMessageModel.to_project_id.is_(None),
                    ),
                    CrossMessageModel.read_at.is_(None),
                )
            )
            result = await session.execute(stmt)
            return result.scalar_one() or 0

    # ================================================================
    # Scheduled Tasks
    # ================================================================

    async def create_scheduled_task(
        self,
        name: str,
        interval_seconds: int,
        action_type: str,
        next_run_at: datetime,
        team_id: str | None = None,
        description: str = "",
        action_config: dict | None = None,
    ) -> ScheduledTask:
        """Create a scheduled task."""
        task = ScheduledTask(
            team_id=team_id,
            name=name,
            description=description,
            interval_seconds=interval_seconds,
            action_type=action_type,
            action_config=action_config or {},
            next_run_at=next_run_at,
        )
        orm = ScheduledTaskModel.from_pydantic(task)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return task

    async def list_scheduled_tasks(self, team_id: str | None = None) -> list[ScheduledTask]:
        """List scheduled tasks, optionally filtered by team_id."""
        async with get_session(self._db_url) as session:
            stmt = select(ScheduledTaskModel).order_by(ScheduledTaskModel.created_at.desc())
            if team_id is not None:
                stmt = stmt.where(ScheduledTaskModel.team_id == team_id)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def get_scheduled_task(self, task_id: str) -> ScheduledTask | None:
        """Get a scheduled task by ID."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ScheduledTaskModel).where(ScheduledTaskModel.id == task_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def update_scheduled_task(self, task_id: str, **kwargs: Any) -> ScheduledTask | None:
        """Update a scheduled task."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ScheduledTaskModel).where(ScheduledTaskModel.id == task_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            return row.to_pydantic()

    async def delete_scheduled_task(self, task_id: str) -> bool:
        """Delete a scheduled task."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(ScheduledTaskModel).where(ScheduledTaskModel.id == task_id)
            )
            return result.rowcount > 0  # type: ignore[union-attr]

    # ================================================================
    # Wake Sessions
    # ================================================================

    async def create_wake_session(
        self, scheduled_task_id: str, agent_name: str, team_id: str = ""
    ) -> WakeSession:
        """Create a new wake session record."""
        ws = WakeSession(
            scheduled_task_id=scheduled_task_id,
            agent_name=agent_name,
            team_id=team_id,
        )
        orm = WakeSessionModel.from_pydantic(ws)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return ws

    async def update_wake_session(self, session_id: str, **kwargs: Any) -> WakeSession | None:
        """Update wake session fields (finished_at, outcome, exit_code, etc)."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(WakeSessionModel).where(WakeSessionModel.id == session_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            return row.to_pydantic()

    async def get_recent_wake_sessions(
        self, agent_name: str, limit: int = 30
    ) -> list[WakeSession]:
        """Get recent wake sessions for an agent, ordered by started_at desc."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(WakeSessionModel)
                .where(WakeSessionModel.agent_name == agent_name)
                .order_by(WakeSessionModel.started_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def get_consecutive_failures(self, agent_name: str) -> int:
        """Count consecutive real failures for an agent (circuit breaker logic).

        Only 'error' and 'timeout' count as failures. Skips like
        'skipped_triage', 'skipped_concurrent', 'cancelled' are ignored.
        """
        _FAILURE_OUTCOMES = {"error", "timeout"}
        sessions = await self.get_recent_wake_sessions(agent_name, limit=30)
        count = 0
        for s in sessions:
            if s.outcome in _FAILURE_OUTCOMES:
                count += 1
            elif s.outcome == "completed":
                break
            # skip non-failure, non-completed outcomes (triage skip, concurrent skip, etc.)
        return count

    async def has_actionable_tasks(self, agent_name: str) -> tuple[bool, str]:
        """Check if agent has pending/running tasks assigned to them."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TaskModel).where(
                    TaskModel.assigned_to == agent_name,
                    TaskModel.status.in_(["pending", "running"]),
                )
            )
            tasks = result.scalars().all()
            if tasks:
                return True, f"{len(tasks)} actionable tasks"
            return False, "no actionable tasks"

    async def toggle_wake_agents(self, enabled: bool) -> int:
        """Enable/disable all wake_agent scheduled tasks. Returns count affected."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ScheduledTaskModel).where(
                    ScheduledTaskModel.action_type == "wake_agent",
                    ScheduledTaskModel.enabled == (not enabled),
                )
            )
            tasks = result.scalars().all()
            for t in tasks:
                t.enabled = enabled
            await session.commit()
            return len(tasks)

    async def cleanup_old_sessions(self, days: int = 30) -> int:
        """Delete wake sessions older than specified days. Returns count deleted."""
        cutoff = datetime.now() - timedelta(days=days)
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(WakeSessionModel).where(WakeSessionModel.started_at < cutoff)
            )
            return result.rowcount  # type: ignore[union-attr]

    async def get_due_tasks(self, now: datetime) -> list[ScheduledTask]:
        """Get all enabled scheduled tasks whose next_run_at <= now."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ScheduledTaskModel).where(
                    ScheduledTaskModel.enabled == True,  # noqa: E712
                    ScheduledTaskModel.next_run_at <= now,
                )
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    # ================================================================
    # Leader Briefings
    # ================================================================

    async def create_briefing(
        self,
        title: str,
        description: str = "",
        options: str = "",
        recommendation: str = "",
        urgency: str = "medium",
        project_id: str = "",
    ) -> LeaderBriefing:
        """Create a new leader briefing item."""
        briefing = LeaderBriefing(
            title=title,
            description=description,
            options=options,
            recommendation=recommendation,
            urgency=urgency,
            project_id=project_id,
        )
        orm = LeaderBriefingModel.from_pydantic(briefing)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return briefing

    async def list_briefings(
        self, status: str = "pending", project_id: str = ""
    ) -> list[LeaderBriefing]:
        """List briefing items, optionally filtered by status and project_id."""
        async with get_session(self._db_url) as session:
            conditions = []
            if status and status != "all":
                conditions.append(LeaderBriefingModel.status == status)
            if project_id:
                conditions.append(LeaderBriefingModel.project_id == project_id)
            stmt = select(LeaderBriefingModel).order_by(
                LeaderBriefingModel.created_at.desc()
            )
            # Apply universal project isolation
            stmt = self._apply_project_filter(stmt, LeaderBriefingModel)
            if conditions:
                stmt = stmt.where(*conditions)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def resolve_briefing(
        self, briefing_id: str, resolution: str, status: str = "resolved"
    ) -> LeaderBriefing | None:
        """Resolve a briefing item with user's decision."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(LeaderBriefingModel).where(LeaderBriefingModel.id == briefing_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            row.resolution = resolution
            row.status = status
            row.resolved_at = datetime.now()
            return row.to_pydantic()

    async def dismiss_briefing(self, briefing_id: str) -> LeaderBriefing | None:
        """Dismiss a briefing item without a resolution."""
        return await self.resolve_briefing(briefing_id, resolution="", status="dismissed")
