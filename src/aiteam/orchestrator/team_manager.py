"""AI Team OS — TeamManager.

Unified entry point for all team operations; used by both CLI and API.
Handles team CRUD, Agent management, task execution, and status queries.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiteam.api.exceptions import NotFoundError
from aiteam.orchestrator.graph_compiler import compile_graph
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    Agent,
    AgentStatus,
    OrchestrationMode,
    Task,
    TaskResult,
    TaskStatus,
    Team,
    TeamStatusSummary,
)

if TYPE_CHECKING:
    from aiteam.api.event_bus import EventBus

logger = logging.getLogger(__name__)


class TeamManager:
    """Team manager — unified entry point for all team operations."""

    def __init__(
        self,
        repository: StorageRepository,
        memory: Any | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        """Initialize TeamManager.

        Args:
            repository: Data persistence repository.
            memory: Optional MemoryStore instance (in development, can be None).
            event_bus: Optional event bus (for persistence + WS event broadcasting).
        """
        self._repo = repository
        self._memory = memory
        self._event_bus = event_bus

    # ================================================================
    # Internal helpers
    # ================================================================

    async def _emit(self, event_type: str, source: str, data: dict) -> None:
        """Emit event (if event_bus is available)."""
        if self._event_bus is not None:
            try:
                await self._event_bus.emit(event_type, source, data)
            except Exception:
                logger.warning("事件发射失败: %s", event_type)

    async def _set_agents_status(
        self, agents: list[Agent], status: AgentStatus, team_id: str,
    ) -> None:
        """Batch set Agent status and emit events."""
        for agent in agents:
            await self._repo.update_agent(agent.id, status=status)
            await self._emit(
                "agent.status_changed",
                f"agent:{agent.id}",
                {
                    "agent_id": agent.id,
                    "team_id": team_id,
                    "status": status.value,
                },
            )

    # ================================================================
    # Team management
    # ================================================================

    async def create_team(
        self,
        name: str,
        mode: str = "coordinate",
        config: dict | None = None,
    ) -> Team:
        """Create a team.

        Args:
            name: Team name.
            mode: Orchestration mode, defaults to coordinate.
            config: Optional team configuration.

        Returns:
            Created Team object.
        """
        # Validate mode
        OrchestrationMode(mode)
        team = await self._repo.create_team(name=name, mode=mode, config=config)
        await self._emit(
            "team.created",
            f"team:{team.id}",
            {"team_id": team.id, "name": name, "mode": mode},
        )
        return team

    async def get_team(self, name_or_id: str) -> Team:
        """Get team by name or ID.

        Args:
            name_or_id: Team name or ID.

        Returns:
            Team object.

        Raises:
            ValueError: When team does not exist.
        """
        # Search by name first
        team = await self._repo.get_team_by_name(name_or_id)
        if team is not None:
            return team
        # Then search by ID
        team = await self._repo.get_team(name_or_id)
        if team is not None:
            return team
        msg = f"团队 '{name_or_id}' 不存在"
        raise NotFoundError(msg)

    async def list_teams(self) -> list[Team]:
        """List all teams."""
        return await self._repo.list_teams()

    async def delete_team(self, name_or_id: str) -> bool:
        """Delete a team.

        Args:
            name_or_id: Team name or ID.

        Returns:
            Whether deletion was successful.
        """
        team = await self.get_team(name_or_id)
        result = await self._repo.delete_team(team.id)
        if result:
            await self._emit(
                "team.deleted",
                f"team:{team.id}",
                {"team_id": team.id},
            )
        return result

    async def set_mode(self, name_or_id: str, mode: str) -> Team:
        """Set team orchestration mode.

        Args:
            name_or_id: Team name or ID.
            mode: New orchestration mode.

        Returns:
            Updated Team object.
        """
        team = await self.get_team(name_or_id)
        OrchestrationMode(mode)
        updated_team = await self._repo.update_team(team.id, mode=mode)
        await self._emit(
            "team.mode_changed",
            f"team:{team.id}",
            {"team_id": team.id, "mode": mode},
        )
        return updated_team

    # ================================================================
    # Agent management
    # ================================================================

    async def add_agent(
        self,
        team_name: str,
        name: str,
        role: str,
        system_prompt: str = "",
        model: str = "claude-opus-4-6",
    ) -> Agent:
        """Add an Agent to a team.

        Args:
            team_name: Team name.
            name: Agent name.
            role: Agent role.
            system_prompt: System prompt.
            model: Model ID to use.

        Returns:
            Created Agent object.
        """
        team = await self.get_team(team_name)
        agent = await self._repo.create_agent(
            team_id=team.id,
            name=name,
            role=role,
            system_prompt=system_prompt,
            model=model,
        )
        await self._emit(
            "agent.created",
            f"agent:{agent.id}",
            {
                "agent_id": agent.id,
                "team_id": team.id,
                "name": name,
                "role": role,
            },
        )
        return agent

    async def remove_agent(self, team_name: str, agent_name: str) -> bool:
        """Remove an Agent from a team.

        Args:
            team_name: Team name.
            agent_name: Agent name.

        Returns:
            Whether removal was successful.
        """
        team = await self.get_team(team_name)
        agents = await self._repo.list_agents(team.id)
        for agent in agents:
            if agent.name == agent_name:
                return await self._repo.delete_agent(agent.id)
        msg = f"Agent '{agent_name}' 在团队 '{team_name}' 中不存在"
        raise NotFoundError(msg)

    async def list_agents(self, team_name: str) -> list[Agent]:
        """List all Agents in a team.

        Args:
            team_name: Team name.

        Returns:
            List of Agents.
        """
        team = await self.get_team(team_name)
        return await self._repo.list_agents(team.id)

    # ================================================================
    # Task execution
    # ================================================================

    async def run_task(
        self,
        team_name: str,
        task_description: str,
        **kwargs: Any,
    ) -> TaskResult:
        """Execute a task (core method).

        Flow:
        1. Create Task record (pending)
        2. Get team's agents
        3. Compile the corresponding mode's StateGraph
        4. Execute graph (ainvoke) with task description
        5. Update Task record (completed/failed)
        6. Return TaskResult

        Args:
            team_name: Team name.
            task_description: Task description.
            **kwargs: Additional parameters.

        Returns:
            TaskResult with execution results.
        """
        team = await self.get_team(team_name)
        agents = await self._repo.list_agents(team.id)

        # 1. Create Task record
        title = kwargs.get("title", task_description[:50])
        task = await self._repo.create_task(
            team_id=team.id,
            title=title,
            description=task_description,
        )
        await self._emit(
            "task.created",
            f"task:{task.id}",
            {"task_id": task.id, "team_id": team.id, "title": title},
        )

        # 2. Update task status to running
        await self._repo.update_task(
            task.id,
            status=TaskStatus.RUNNING,
            started_at=datetime.now(),
        )
        await self._emit(
            "task.started",
            f"task:{task.id}",
            {"task_id": task.id, "team_id": team.id},
        )

        # Set all Agents to BUSY
        await self._set_agents_status(agents, AgentStatus.BUSY, team.id)

        start_time = time.time()

        try:
            # 3. Determine LLM model
            llm_model = kwargs.get("model", "claude-opus-4-6")
            if agents:
                # Use the first Agent's model as default
                llm_model = agents[0].model or llm_model

            # 4. Compile StateGraph
            compiled_graph = compile_graph(
                team=team,
                agents=agents,
                memory_store=self._memory,
                llm_model=llm_model,
            )

            # 5. Execute graph
            initial_state = {
                "team_id": team.id,
                "current_task": task_description,
                "messages": [],
                "agent_outputs": {},
                "leader_plan": None,
                "final_result": None,
            }

            result_state = await compiled_graph.ainvoke(
                initial_state,
                config={
                    "configurable": {
                        "agents": agents,
                        "llm_model": llm_model,
                    }
                },
            )

            duration = time.time() - start_time
            final_result = result_state.get("final_result", "")
            agent_outputs = result_state.get("agent_outputs", {})

            # 6. Update Task to completed
            await self._repo.update_task(
                task.id,
                status=TaskStatus.COMPLETED,
                result=final_result,
                completed_at=datetime.now(),
            )

            # Restore all Agents to IDLE
            await self._set_agents_status(agents, AgentStatus.WAITING, team.id)

            await self._emit(
                "task.completed",
                f"task:{task.id}",
                {
                    "task_id": task.id,
                    "team_id": team.id,
                    "duration_seconds": duration,
                },
            )

            return TaskResult(
                task_id=task.id,
                status=TaskStatus.COMPLETED,
                result=final_result or "",
                agent_outputs=agent_outputs,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = time.time() - start_time
            error_msg = f"任务执行失败: {e}"

            # Update Task to failed
            await self._repo.update_task(
                task.id,
                status=TaskStatus.FAILED,
                result=error_msg,
                completed_at=datetime.now(),
            )

            # Restore all Agents to IDLE
            await self._set_agents_status(agents, AgentStatus.WAITING, team.id)

            await self._emit(
                "task.failed",
                f"task:{task.id}",
                {
                    "task_id": task.id,
                    "team_id": team.id,
                    "error": error_msg,
                    "duration_seconds": duration,
                },
            )

            return TaskResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                result=error_msg,
                agent_outputs={},
                duration_seconds=duration,
            )

    # ================================================================
    # Status queries
    # ================================================================

    async def get_task_status(self, task_id: str) -> Task:
        """Query task status.

        Args:
            task_id: Task ID.

        Returns:
            Task object.

        Raises:
            ValueError: When task does not exist.
        """
        task = await self._repo.get_task(task_id)
        if task is None:
            msg = f"任务 '{task_id}' 不存在"
            raise NotFoundError(msg)
        return task

    async def list_tasks(self, team_name: str) -> list[Task]:
        """List all tasks for a team.

        Args:
            team_name: Team name.

        Returns:
            List of Tasks.
        """
        team = await self.get_team(team_name)
        return await self._repo.list_tasks(team.id)

    async def get_status(self, team_name: str | None = None) -> TeamStatusSummary:
        """Get team status summary.

        Args:
            team_name: Team name. If None, returns the first team's status.

        Returns:
            TeamStatusSummary with team status overview.

        Raises:
            ValueError: When team does not exist.
        """
        if team_name is None:
            teams = await self._repo.list_teams()
            if not teams:
                msg = "没有可用的团队"
                raise NotFoundError(msg)
            team = teams[0]
        else:
            team = await self.get_team(team_name)

        agents = await self._repo.list_agents(team.id)
        all_tasks = await self._repo.list_tasks(team.id)
        active_tasks = [
            t for t in all_tasks
            if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
        ]
        completed_count = sum(
            1 for t in all_tasks if t.status == TaskStatus.COMPLETED
        )

        return TeamStatusSummary(
            team=team,
            agents=agents,
            active_tasks=active_tasks,
            completed_tasks=completed_count,
            total_tasks=len(all_tasks),
        )
