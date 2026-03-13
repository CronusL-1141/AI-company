"""AI Team OS — 数据持久化仓库.

StorageRepository 是所有数据库操作的统一入口，
上层模块只通过此接口访问数据。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select

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
                raise ValueError(msg)

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
    ) -> AgentActivity:
        """记录Agent的一次工具调用活动."""
        activity = AgentActivity(
            agent_id=agent_id,
            session_id=session_id,
            tool_name=tool_name,
            input_summary=input_summary[:500],
            output_summary=output_summary[:500],
        )
        orm = AgentActivityModel.from_pydantic(activity)
        async with get_session(self._db_url) as session:
            session.add(orm)
        return activity

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
