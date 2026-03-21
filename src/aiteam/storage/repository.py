"""AI Team OS — 数据持久化仓库.

StorageRepository 是所有数据库操作的统一入口，
上层模块只通过此接口访问数据。
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
    EventModel,
    MeetingMessageModel,
    MeetingModel,
    MemoryModel,
    PhaseModel,
    ProjectModel,
    TaskModel,
    TeamModel,
)
from aiteam.types import (
    Agent,
    AgentActivity,
    AgentStatus,
    Event,
    EventType,
    Meeting,
    MeetingMessage,
    MeetingStatus,
    Memory,
    MemoryScope,
    OrchestrationMode,
    Phase,
    PhaseStatus,
    Project,
    Task,
    TaskStatus,
    Team,
)


class StorageRepository:
    """数据持久化仓库 — 统一数据访问接口."""

    def __init__(self, db_url: str | None = None) -> None:
        self._db_url = db_url

    async def init_db(self) -> None:
        """初始化数据库（创建表/运行迁移）."""
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
        """创建项目."""
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
        """根据 ID 获取项目."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ProjectModel).where(ProjectModel.id == project_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_projects(self) -> list[Project]:
        """列出所有项目."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(ProjectModel).order_by(ProjectModel.created_at.desc())
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_project(self, project_id: str, **kwargs: object) -> Project | None:
        """更新项目信息."""
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
        """删除项目."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(ProjectModel).where(ProjectModel.id == project_id)
            )
            return result.rowcount > 0  # type: ignore[union-attr]

    async def get_project_by_root_path(self, root_path: str) -> Project | None:
        """根据 root_path 获取项目."""
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
        """创建阶段."""
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
        """根据 ID 获取阶段."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(PhaseModel).where(PhaseModel.id == phase_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_phases(self, project_id: str) -> list[Phase]:
        """列出项目下所有阶段，按 order 排序."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(PhaseModel)
                .where(PhaseModel.project_id == project_id)
                .order_by(PhaseModel.order, PhaseModel.created_at)
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_phase(self, phase_id: str, **kwargs: object) -> Phase | None:
        """更新阶段信息."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(PhaseModel).where(PhaseModel.id == phase_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None

            # 处理 status 字段: 转为字符串值
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
        """删除阶段."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(PhaseModel).where(PhaseModel.id == phase_id)
            )
            return result.rowcount > 0  # type: ignore[union-attr]

    async def get_active_phase(self, project_id: str) -> Phase | None:
        """获取项目当前 active 阶段."""
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
        """将项目下所有 active 阶段设为 completed."""
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
        self, name: str, mode: str, config: dict | None = None, **kwargs: Any,
    ) -> Team:
        """创建团队."""
        team = Team(
            name=name,
            mode=OrchestrationMode(mode),
            config=config or {},
            project_id=kwargs.get("project_id"),
            leader_agent_id=kwargs.get("leader_agent_id"),
        )
        orm = TeamModel.from_pydantic(team)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return team

    async def get_team(self, team_id: str) -> Team | None:
        """根据 ID 获取团队."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel).where(TeamModel.id == team_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def get_team_by_name(self, name: str) -> Team | None:
        """根据名称获取团队."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel).where(TeamModel.name == name)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_teams(self) -> list[Team]:
        """列出所有团队."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel).order_by(TeamModel.created_at.desc())
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def list_teams_by_project(self, project_id: str) -> list[Team]:
        """列出项目下所有团队."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel)
                .where(TeamModel.project_id == project_id)
                .order_by(TeamModel.created_at.desc())
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def find_active_team_by_leader(self, leader_agent_id: str) -> Team | None:
        """查找Leader当前领导的active团队."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel)
                .where(TeamModel.leader_agent_id == leader_agent_id)
                .where(TeamModel.status == "active")
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def find_leader_by_project(self, project_id: str) -> Agent | None:
        """查找项目的Leader agent（role=leader + project_id匹配）."""
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
        """更新团队信息."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel).where(TeamModel.id == team_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"团队 {team_id} 不存在"
                raise NotFoundError(msg)

            # 处理 mode 字段: 转为字符串值
            if "mode" in kwargs:
                mode_val = kwargs["mode"]
                if isinstance(mode_val, OrchestrationMode):
                    kwargs["mode"] = mode_val.value
                elif isinstance(mode_val, str):
                    # 验证值是否合法
                    OrchestrationMode(mode_val)

            kwargs["updated_at"] = datetime.now()

            for key, value in kwargs.items():
                if hasattr(row, key):
                    setattr(row, key, value)

            return row.to_pydantic()

    async def delete_team(self, team_id: str) -> bool:
        """删除团队."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(TeamModel).where(TeamModel.id == team_id)
            )
            return result.rowcount > 0  # type: ignore[union-attr]

    # ================================================================
    # Agents
    # ================================================================

    async def create_agent(
        self, team_id: str, name: str, role: str, **kwargs: object
    ) -> Agent:
        """创建 Agent."""
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
        """根据 ID 获取 Agent."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(AgentModel).where(AgentModel.id == agent_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_agents(self, team_id: str) -> list[Agent]:
        """列出团队中所有 Agent."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(AgentModel)
                .where(AgentModel.team_id == team_id)
                .order_by(AgentModel.created_at)
            )
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_agent(self, agent_id: str, **kwargs: object) -> Agent:
        """更新 Agent 信息."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(AgentModel).where(AgentModel.id == agent_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"Agent {agent_id} 不存在"
                raise NotFoundError(msg)

            # 处理 status 字段: 转为字符串值
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
        """删除 Agent."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(AgentModel).where(AgentModel.id == agent_id)
            )
            return result.rowcount > 0  # type: ignore[union-attr]

    # ================================================================
    # Tasks
    # ================================================================

    async def create_task(
        self, team_id: str | None, title: str, description: str = "", **kwargs: object
    ) -> Task:
        """创建任务.

        team_id 可为 None（项目级任务，不绑定团队）。
        """
        # 构建可选参数
        optional: dict[str, object] = {}
        for key in ("assigned_to", "parent_id", "project_id", "depends_on",
                     "depth", "order", "template_id", "priority", "horizon",
                     "tags", "config"):
            if key in kwargs:
                optional[key] = kwargs[key]
        # 设置默认值
        optional.setdefault("depends_on", [])
        optional.setdefault("depth", 0)
        optional.setdefault("order", 0)

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
        """列出某任务的所有子任务，按order排序."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(TaskModel)
                .where(TaskModel.parent_id == parent_id)
                .order_by(TaskModel.order, TaskModel.created_at)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def get_task(self, task_id: str) -> Task | None:
        """根据 ID 获取任务."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TaskModel).where(TaskModel.id == task_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_tasks(
        self, team_id: str, status: TaskStatus | None = None
    ) -> list[Task]:
        """列出团队任务，可按状态过滤."""
        async with get_session(self._db_url) as session:
            stmt = select(TaskModel).where(TaskModel.team_id == team_id)
            if status is not None:
                stmt = stmt.where(TaskModel.status == status.value)
            stmt = stmt.order_by(TaskModel.created_at.desc())
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def list_tasks_by_project(
        self, project_id: str, status: TaskStatus | None = None
    ) -> list[Task]:
        """列出项目下所有任务（包括team_id=None的项目级任务和团队任务）."""
        async with get_session(self._db_url) as session:
            stmt = select(TaskModel).where(TaskModel.project_id == project_id)
            if status is not None:
                stmt = stmt.where(TaskModel.status == status.value)
            stmt = stmt.order_by(TaskModel.created_at.desc())
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_task(self, task_id: str, **kwargs: object) -> Task:
        """更新任务信息."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TaskModel).where(TaskModel.id == task_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"任务 {task_id} 不存在"
                raise NotFoundError(msg)

            # 处理 status 字段: 转为字符串值
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
        """查找所有depends_on包含task_id的任务（即依赖此任务的下游任务）."""
        async with get_session(self._db_url) as session:
            # SQL LIKE预筛选缩小范围，再Python精确检查JSON数组
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
        """任务完成时的级联解锁.

        检查所有依赖此task_id的下游BLOCKED任务，
        如果其所有依赖都已完成则解锁为PENDING。

        Returns:
            被解锁的任务列表.
        """
        downstream = await self.get_downstream_tasks(task_id)
        unblocked: list[Task] = []

        for task in downstream:
            if task.status != TaskStatus.BLOCKED:
                continue

            # 检查该任务的所有依赖是否都已完成
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
        """检测添加依赖后是否产生环.

        从new_dep_id出发沿depends_on链向上追溯，
        如果能回到task_id则说明会形成环。

        Returns:
            True 表示存在环，False 表示安全.
        """
        # 如果自己依赖自己，直接是环
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
                    return True  # 找到环
                if dep_id not in visited:
                    stack.append(dep_id)

        return False

    # ================================================================
    # Events
    # ================================================================

    async def create_event(
        self, event_type: str, source: str, data: dict
    ) -> Event:
        """创建系统事件."""
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
        """列出事件，可按类型和来源过滤.

        Args:
            event_type: 精确匹配事件类型（如 "agent.created"）
            source: 精确匹配事件来源
            limit: 返回数量上限
            type_prefix: 前缀匹配事件类型（如 "decision." 匹配所有决策事件）
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
        """创建记忆."""
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
        """根据 ID 获取记忆."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(MemoryModel).where(MemoryModel.id == memory_id)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                # 更新访问时间
                row.accessed_at = datetime.now()
                return row.to_pydantic()
            return None

    async def list_memories(self, scope: str, scope_id: str) -> list[Memory]:
        """列出指定作用域的所有记忆."""
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
        """搜索记忆（M1阶段使用简单的 LIKE 关键词匹配）."""
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
        """删除记忆."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                delete(MemoryModel).where(MemoryModel.id == memory_id)
            )
            return result.rowcount > 0  # type: ignore[union-attr]

    async def list_team_knowledge(
        self,
        team_id: str,
        memory_type: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """列出团队知识库（scope=team的记忆），支持按类型过滤.

        Args:
            team_id: 团队 ID
            memory_type: 可选类型过滤，匹配 metadata.type 字段
                         如 failure_alchemy / lesson_learned / loop_review
            limit: 返回数量上限
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
            memories = [
                m for m in memories
                if m.metadata.get("type") == memory_type
            ]
        return memories

    async def list_agent_experience(
        self,
        agent_id: str,
        limit: int = 50,
    ) -> list[Memory]:
        """列出 Agent 的经验记忆（scope=agent）.

        Args:
            agent_id: Agent ID
            limit: 返回数量上限
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
        self, team_id: str, topic: str, participants: list[str] | None = None,
    ) -> Meeting:
        """创建会议."""
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
        """根据 ID 获取会议."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(MeetingModel).where(MeetingModel.id == meeting_id)
            )
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def list_meetings(
        self, team_id: str, status: MeetingStatus | None = None,
    ) -> list[Meeting]:
        """列出团队会议，可按状态过滤."""
        async with get_session(self._db_url) as session:
            stmt = select(MeetingModel).where(MeetingModel.team_id == team_id)
            if status is not None:
                stmt = stmt.where(MeetingModel.status == status.value)
            stmt = stmt.order_by(MeetingModel.created_at.desc())
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def update_meeting(self, meeting_id: str, **kwargs: object) -> Meeting:
        """更新会议信息."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(MeetingModel).where(MeetingModel.id == meeting_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"会议 {meeting_id} 不存在"
                raise NotFoundError(msg)

            # 处理 status 字段: 转为字符串值
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
        """创建会议消息."""
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
        self, meeting_id: str, limit: int = 100,
    ) -> list[MeetingMessage]:
        """列出会议消息."""
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
        """获取超过指定小时无新消息的active会议.

        判定逻辑：
        - 有消息的会议：最后一条消息时间距今超过 hours 小时
        - 无消息的会议：创建时间距今超过 hours 小时
        """
        cutoff = datetime.now() - timedelta(hours=hours)
        async with get_session(self._db_url) as session:
            # 子查询：每个会议的最后消息时间
            last_msg_subq = (
                select(
                    MeetingMessageModel.meeting_id,
                    func.max(MeetingMessageModel.timestamp).label("last_msg_time"),
                )
                .group_by(MeetingMessageModel.meeting_id)
                .subquery()
            )

            # 主查询：active会议 LEFT JOIN 最后消息时间
            stmt = (
                select(MeetingModel)
                .outerjoin(
                    last_msg_subq,
                    MeetingModel.id == last_msg_subq.c.meeting_id,
                )
                .where(
                    MeetingModel.status == MeetingStatus.ACTIVE.value,
                    # 有消息则看最后消息时间，无消息则看创建时间
                    func.coalesce(
                        last_msg_subq.c.last_msg_time,
                        MeetingModel.created_at,
                    ) < cutoff,
                )
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            return [r.to_pydantic() for r in rows]

    async def conclude_meeting(self, meeting_id: str) -> Meeting | None:
        """结束会议，将状态设为concluded."""
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
    # Hooks — CC会话相关查询
    # ================================================================

    async def find_agent_by_session(
        self, session_id: str, agent_name: str,
    ) -> Agent | None:
        """根据CC会话ID和Agent名称查找已注册的Agent."""
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
        """查找CC会话中所有关联的Agent."""
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
        """根据CC内部agent_id查找Agent."""
        async with get_session(self._db_url) as session:
            stmt = (
                select(AgentModel)
                .where(AgentModel.cc_tool_use_id == cc_agent_id)
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            return row.to_pydantic() if row else None

    async def find_agents_by_role(self, role: str) -> list[Agent]:
        """按角色查找所有Agent（跨团队）."""
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
        self, source: str, session_id: str | None = None,
    ) -> int:
        """按来源统计Agent数量，可选按session过滤."""
        async with get_session(self._db_url) as session:
            stmt = select(func.count()).select_from(AgentModel).where(
                AgentModel.source == source,
            )
            if session_id is not None:
                stmt = stmt.where(AgentModel.session_id == session_id)
            result = await session.execute(stmt)
            return result.scalar_one()

    # ================================================================
    # Agent Activities — 工具调用活动日志
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
        """记录Agent的一次工具调用活动."""
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
        """查找匹配的running状态activity（Pre→Post关联）."""
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
        """更新activity字段（用于PostToolUse回填duration_ms/status/output）."""
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
        self, agent_id: str, limit: int = 50,
    ) -> list[AgentActivity]:
        """获取Agent的活动日志."""
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
        self, session_id: str, limit: int = 100,
    ) -> list[AgentActivity]:
        """获取某session下所有活动."""
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
        """获取团队下所有agent的活动日志，按timestamp降序."""
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
    # Analytics — 聚合统计查询
    # ================================================================

    async def count_activities_by_tool(
        self,
        agent_id: str | None = None,
        team_id: str | None = None,
    ) -> list[dict]:
        """按工具名称统计活动次数."""
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
                # 通过agent表关联筛选team
                stmt = stmt.join(
                    AgentModel,
                    AgentActivityModel.agent_id == AgentModel.id,
                ).where(AgentModel.team_id == team_id)

            result = await session.execute(stmt)
            return [
                {"tool_name": row.tool_name, "count": row.count}
                for row in result.all()
            ]

    async def get_activity_timeline(
        self,
        team_id: str | None = None,
        hours: int = 24,
    ) -> list[dict]:
        """按小时统计活动数量（最近 N 小时）."""
        cutoff = datetime.now() - timedelta(hours=hours)
        async with get_session(self._db_url) as session:
            # 使用 strftime 提取小时粒度（SQLite 兼容）
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
            return [
                {"hour": row.hour, "count": row.count}
                for row in result.all()
            ]

    async def get_agent_productivity(
        self,
        team_id: str | None = None,
    ) -> list[dict]:
        """每个Agent的产能指标：活动次数、工具多样性、最后活跃时间."""
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
        """任务完成率和平均完成时间统计."""
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
        """Agent利用率：基于活动时间戳计算活跃时段占比."""
        async with get_session(self._db_url) as session:
            # 查询每个Agent的活动时间跨度和活动次数（JOIN获取名称，避免N+1）
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
                # 估算利用率：活动密度（每小时活动数）
                span_hours = 0.0
                if row.first_active and row.last_active:
                    span = (row.last_active - row.first_active).total_seconds() / 3600
                    span_hours = max(span, 1.0)  # 最少算1小时

                utilization = round(row.activity_count / span_hours, 2) if span_hours > 0 else 0

                output.append({
                    "agent_id": row.agent_id,
                    "agent_name": row.agent_name or "unknown",
                    "activity_count": row.activity_count,
                    "tools_used": row.tools_used,
                    "span_hours": round(span_hours, 2),
                    "activities_per_hour": utilization,
                    "first_active": row.first_active.isoformat() if row.first_active else None,
                    "last_active": row.last_active.isoformat() if row.last_active else None,
                })
            return output
