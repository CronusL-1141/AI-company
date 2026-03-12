"""AI Team OS — 数据持久化仓库.

StorageRepository 是所有数据库操作的统一入口，
上层模块只通过此接口访问数据。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select

from aiteam.storage.connection import get_session
from aiteam.storage.connection import init_db as _init_db
from aiteam.storage.models import (
    AgentModel,
    EventModel,
    MemoryModel,
    TaskModel,
    TeamModel,
)
from aiteam.types import (
    Agent,
    AgentStatus,
    Event,
    EventType,
    Memory,
    MemoryScope,
    OrchestrationMode,
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
    # Teams
    # ================================================================

    async def create_team(
        self, name: str, mode: str, config: dict | None = None
    ) -> Team:
        """创建团队."""
        team = Team(
            name=name,
            mode=OrchestrationMode(mode),
            config=config or {},
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

    async def update_team(self, team_id: str, **kwargs: object) -> Team:
        """更新团队信息."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TeamModel).where(TeamModel.id == team_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"团队 {team_id} 不存在"
                raise ValueError(msg)

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
                raise ValueError(msg)

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
        self, team_id: str, title: str, description: str = "", **kwargs: object
    ) -> Task:
        """创建任务."""
        task = Task(
            team_id=team_id,
            title=title,
            description=description,
            assigned_to=kwargs.get("assigned_to"),  # type: ignore[arg-type]
            parent_id=kwargs.get("parent_id"),  # type: ignore[arg-type]
        )
        orm = TaskModel.from_pydantic(task)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return task

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

    async def update_task(self, task_id: str, **kwargs: object) -> Task:
        """更新任务信息."""
        async with get_session(self._db_url) as session:
            result = await session.execute(
                select(TaskModel).where(TaskModel.id == task_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                msg = f"任务 {task_id} 不存在"
                raise ValueError(msg)

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
    ) -> list[Event]:
        """列出事件，可按类型和来源过滤."""
        async with get_session(self._db_url) as session:
            stmt = select(EventModel)
            if event_type is not None:
                stmt = stmt.where(EventModel.type == event_type)
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
                    MemoryModel.content.ilike(f"%{query}%"),
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
