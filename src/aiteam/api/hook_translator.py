"""AI Team OS — Hook事件翻译器.

将Claude Code的Hook事件转化为OS系统操作，
实现CC会话与OS之间的自动同步桥接。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aiteam.api.event_bus import EventBus
from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)


@dataclass
class _FileEditRecord:
    """单次文件编辑记录."""

    agent_id: str
    agent_name: str
    timestamp: datetime


@dataclass
class _FileEditTracker:
    """内存中的文件编辑追踪器 — O(1)冲突查询.

    维护每个文件的最近编辑记录列表，支持：
    1. 快速判断某文件是否被其他agent编辑过（冲突检测）
    2. 统计热点文件（被多个agent编辑的文件）
    3. 自动清理过期记录
    """

    # file_path -> list of recent edit records
    _edits: dict[str, list[_FileEditRecord]] = field(
        default_factory=lambda: defaultdict(list),
    )
    # 记录保留时长
    _window: timedelta = field(default_factory=lambda: timedelta(minutes=10))

    def record(self, file_path: str, agent_id: str, agent_name: str) -> None:
        """记录一次文件编辑."""
        if len(self._edits) > 10000:
            self.cleanup()
        self._edits[file_path].append(
            _FileEditRecord(
                agent_id=agent_id,
                agent_name=agent_name,
                timestamp=datetime.now(),
            ),
        )

    def find_conflicts(
        self, file_path: str, current_agent_id: str,
        window_minutes: int = 5,
    ) -> list[_FileEditRecord]:
        """查找与当前agent冲突的其他agent的编辑记录.

        Returns:
            在时间窗口内编辑过同一文件的其他agent记录列表。
        """
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        records = self._edits.get(file_path, [])
        return [
            r for r in records
            if r.agent_id != current_agent_id and r.timestamp >= cutoff
        ]

    def get_hotspots(self, window_minutes: int = 10, min_agents: int = 2) -> list[dict]:
        """获取热点文件 — 在时间窗口内被多个agent编辑的文件.

        Returns:
            热点文件列表，每项包含 file_path, agents, edit_count。
        """
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        hotspots = []
        for file_path, records in self._edits.items():
            recent = [r for r in records if r.timestamp >= cutoff]
            if not recent:
                continue
            unique_agents = {r.agent_name for r in recent}
            if len(unique_agents) >= min_agents:
                hotspots.append({
                    "file_path": file_path,
                    "agents": sorted(unique_agents),
                    "edit_count": len(recent),
                    "last_edit": max(r.timestamp for r in recent).isoformat(),
                })
        # 按编辑次数降序
        hotspots.sort(key=lambda h: h["edit_count"], reverse=True)
        return hotspots

    def get_agent_files(self, agent_id: str, window_minutes: int = 10) -> list[str]:
        """获取某agent近期正在编辑的文件列表."""
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        files = []
        for file_path, records in self._edits.items():
            if any(
                r.agent_id == agent_id and r.timestamp >= cutoff
                for r in records
            ):
                files.append(file_path)
        return files

    def cleanup(self) -> int:
        """清理过期记录，返回清理数量."""
        cutoff = datetime.now() - self._window
        removed = 0
        empty_keys = []
        for file_path, records in self._edits.items():
            before = len(records)
            self._edits[file_path] = [r for r in records if r.timestamp >= cutoff]
            removed += before - len(self._edits[file_path])
            if not self._edits[file_path]:
                empty_keys.append(file_path)
        for k in empty_keys:
            del self._edits[k]
        return removed


class HookTranslator:
    """将Claude Code hook事件转化为OS系统操作."""

    # 文件编辑工具名称集合，用于冲突检测
    _FILE_EDIT_TOOLS = frozenset({"Edit", "Write"})

    def __init__(self, repo: StorageRepository, event_bus: EventBus) -> None:
        self.repo = repo
        self.event_bus = event_bus
        self._file_tracker = _FileEditTracker()

    async def handle_event(self, payload: dict) -> dict:
        """统一事件处理入口."""
        event_name = payload.get("hook_event_name", "")
        handler = {
            "SubagentStart": self._on_subagent_start,
            "SubagentStop": self._on_subagent_stop,
            "PreToolUse": self._on_pre_tool_use,
            "PostToolUse": self._on_post_tool_use,
            "SessionStart": self._on_session_start,
            "SessionEnd": self._on_session_end,
            "Stop": self._on_stop,
        }.get(event_name)

        if handler:
            return await handler(payload)
        return {"status": "ignored", "reason": f"unhandled event: {event_name}"}

    async def _on_subagent_start(self, payload: dict) -> dict:
        """处理子Agent启动事件.

        CC SubagentStart payload结构:
        - agent_type: Agent名称（来自Agent tool的name参数）
        - agent_id: CC内部agent ID（用于关联后续工具调用）
        - session_id: 父session ID
        - cc_team_name: (可选) CC团队名称，由send_event.py注入

        去重策略（4级查找链）：
        1. cc_tool_use_id精确匹配（最快，覆盖重复SubagentStart）
        2. session_id + name匹配
        3. 团队内同名agent匹配（覆盖MCP预注册的情况）
        4. 以上都没找到 → 按cc_team_name查找/创建OS团队 → 注册
        """
        cc_agent_id = payload.get("agent_id", "")
        agent_name = payload.get("agent_type", "unnamed-agent")
        session_id = payload.get("session_id", "")
        cc_team_name = payload.get("cc_team_name", "")

        existing = None
        leader = None
        team = None

        # 1. cc_tool_use_id精确匹配（最快，覆盖重复SubagentStart事件）
        if cc_agent_id:
            existing = await self.repo.find_agent_by_cc_id(cc_agent_id)

        # 2. 确定目标团队，然后在团队内去重
        if not existing:
            if cc_team_name:
                # 有cc_team_name → 解析目标团队，只在该团队内按name去重
                team = await self._resolve_cc_team(cc_team_name, session_id)
                if team:
                    team_agents = await self.repo.list_agents(team.id)
                    matches = [a for a in team_agents if a.name == agent_name]
                    if matches:
                        existing = matches[0]
            else:
                # 无cc_team_name → 旧逻辑兼容：session_id+name全局查找
                existing = await self.repo.find_agent_by_session(
                    session_id, agent_name,
                )

        # 3. 仍未匹配 → 通过Leader找团队，在团队内按name去重
        if not existing and not team:
            leader = await self._find_leader(session_id)
            if leader:
                team = await self.repo.find_active_team_by_leader(leader.id)
            if team:
                team_agents = await self.repo.list_agents(team.id)
                matches = [a for a in team_agents if a.name == agent_name]
                if matches:
                    existing = matches[0]

        if existing:
            # 已注册 -> 更新状态、绑定session和CC agent ID
            update_fields: dict = {
                "status": "busy",
                "cc_tool_use_id": cc_agent_id,
                "session_id": session_id,
                "last_active_at": datetime.now(),
            }
            # 如果已有role含 " — "，自动分割为role + current_task
            if existing.role and " — " in existing.role:
                parts = existing.role.split(" — ", 1)
                update_fields["role"] = parts[0].strip()
                update_fields["current_task"] = parts[1].strip()
            await self.repo.update_agent(existing.id, **update_fields)
            await self.event_bus.emit(
                "agent.status_changed",
                f"agent:{existing.id}",
                {
                    "agent_id": existing.id,
                    "name": agent_name,
                    "status": "busy",
                    "trigger": "hook",
                },
            )
            return {"status": "updated", "agent_id": existing.id}

        # 4. 未注册 → 按cc_team_name查找/创建OS团队，然后注册agent
        if not team and cc_team_name:
            team = await self._resolve_cc_team(cc_team_name, session_id)

        if not team:
            if not leader:
                leader = await self._find_leader(session_id)
            if leader:
                team = await self.repo.find_active_team_by_leader(leader.id)

        if not team:
            logger.info(
                "SubagentStart: agent '%s' 未注册且无法找到active团队，跳过",
                agent_name,
            )
            return {"status": "skipped", "reason": "no active team"}

        # 创建前最终name去重（防并发：MCP可能在查找链执行期间完成注册）
        team_agents = await self.repo.list_agents(team.id)
        late_match = [a for a in team_agents if a.name == agent_name]
        if late_match:
            existing = late_match[0]
            await self.repo.update_agent(
                existing.id, status="busy", cc_tool_use_id=cc_agent_id,
                session_id=session_id,
                last_active_at=datetime.now(),
            )
            logger.info(
                "SubagentStart: 并发去重命中 agent '%s' (id=%s)",
                agent_name, existing.id,
            )
            return {"status": "updated", "agent_id": existing.id}

        # 从agent_name中提取role和current_task（如含 " — " 分隔符）
        if " — " in agent_name:
            parts = agent_name.split(" — ", 1)
            auto_role = parts[0].strip()
            auto_task = parts[1].strip()
        else:
            auto_role = agent_name
            auto_task = None

        new_agent = await self.repo.create_agent(
            team_id=team.id,
            name=agent_name,
            role=auto_role,
            source="hook",
            session_id=session_id,
            cc_tool_use_id=cc_agent_id,
        )
        # create_agent默认status=waiting，立即设为busy
        update_kwargs: dict = {
            "status": "busy",
            "project_id": team.project_id,
            "last_active_at": datetime.now(),
        }
        if auto_task:
            update_kwargs["current_task"] = auto_task
        await self.repo.update_agent(new_agent.id, **update_kwargs)

        await self.event_bus.emit(
            "agent.status_changed",
            f"agent:{new_agent.id}",
            {
                "agent_id": new_agent.id,
                "name": agent_name,
                "status": "busy",
                "trigger": "hook_auto_register",
            },
        )
        logger.info(
            "SubagentStart: 自动注册 agent '%s' → team '%s' (cc_id=%s)",
            agent_name, team.name, cc_agent_id[:8] if cc_agent_id else "?",
        )
        return {"status": "created", "agent_id": new_agent.id}

    async def _on_subagent_stop(self, payload: dict) -> dict:
        """处理子Agent停止事件.

        CC SubagentStop payload包含 agent_id 用于精确匹配。
        """
        cc_agent_id = payload.get("agent_id", "")
        agent_name = payload.get("agent_type", "")
        session_id = payload.get("session_id", "")

        updated: list[str] = []
        if cc_agent_id:
            # 通过_resolve_agent统一查找（支持late binding回退）
            agent = await self._resolve_agent(cc_agent_id, agent_name, session_id)
            if agent and agent.status == "busy":
                await self.repo.update_agent(
                    agent.id, status="waiting", current_task=None,
                    last_active_at=datetime.now(),
                )
                await self.event_bus.emit(
                    "agent.status_changed",
                    f"agent:{agent.id}",
                    {
                        "agent_id": agent.id,
                        "name": agent.name,
                        "status": "waiting",
                        "trigger": "hook",
                    },
                )
                updated.append(agent.id)
        else:
            # 回退：找本session中BUSY的agents
            agents = await self.repo.find_agents_by_session(session_id)
            for agent in agents:
                if agent.status == "busy":
                    await self.repo.update_agent(
                        agent.id, status="waiting", last_active_at=datetime.now(),
                    )
                    await self.event_bus.emit(
                        "agent.status_changed",
                        f"agent:{agent.id}",
                        {
                            "agent_id": agent.id,
                            "status": "waiting",
                            "trigger": "hook",
                        },
                    )
                    updated.append(agent.id)
        return {"status": "updated", "agents_waiting": updated}

    async def _resolve_cc_team(self, cc_team_name: str, session_id: str) -> object | None:
        """根据CC团队名称查找或创建对应的OS团队.

        1. 按名称精确匹配已有OS团队（active状态优先）
        2. 未找到 → 自动创建同名OS团队
        """
        if not cc_team_name:
            return None

        # 1. 按名称查找已有团队
        existing_team = await self.repo.get_team_by_name(cc_team_name)
        if existing_team:
            logger.info(
                "CC团队映射: '%s' → 已有OS团队 (id=%s, status=%s)",
                cc_team_name, existing_team.id, existing_team.status,
            )
            return existing_team

        # 2. 自动创建同名OS团队
        new_team = await self.repo.create_team(
            name=cc_team_name,
            mode="coordinate",
        )
        logger.info(
            "CC团队映射: 自动创建OS团队 '%s' (id=%s)",
            cc_team_name, new_team.id,
        )

        # 尝试关联到现有项目（通过Leader查找）
        leader = await self._find_leader(session_id)
        if leader and leader.project_id:
            await self.repo.update_team(new_team.id, project_id=leader.project_id)
            logger.info(
                "CC团队映射: 团队 '%s' 关联到项目 %s",
                cc_team_name, leader.project_id,
            )

        await self.event_bus.emit(
            "team.created",
            f"team:{new_team.id}",
            {
                "team_id": new_team.id,
                "team_name": cc_team_name,
                "source": "cc_team_mapping",
                "session_id": session_id,
            },
        )
        return new_team

    async def _find_leader(self, session_id: str) -> object | None:
        """查找当前session的leader agent.

        查找策略：
        1. 按session_id精确匹配（最快）
        2. 按role="leader"跨session回退（覆盖DB迁移/API重启后session_id失效）
        找到Leader后自动绑定当前session_id（self-heal）。
        """
        # 1. 按session_id精确匹配
        if session_id:
            agents = await self.repo.find_agents_by_session(session_id)
            if agents:
                # 优先返回leader角色的agent
                leaders = [a for a in agents if a.role == "leader"]
                if leaders:
                    return leaders[0]

                # 其次返回api-source的agent
                api_matches = [a for a in agents if a.source == "api"]
                if api_matches:
                    return api_matches[0]

                # 最后返回任何匹配的agent（BUSY优先）
                agents.sort(key=lambda a: (0 if a.status == "busy" else 1))
                return agents[0]

        # 2. FALLBACK: 按role="leader"跨session查找
        # 覆盖DB迁移、API重启后session_id不匹配的情况
        all_leaders = await self.repo.find_agents_by_role("leader")
        if not all_leaders:
            return None

        # 优先返回有active team的Leader
        chosen = None
        for leader in all_leaders:
            team = await self.repo.find_active_team_by_leader(leader.id)
            if team:
                chosen = leader
                break

        if not chosen:
            chosen = all_leaders[0]

        # Self-heal：绑定当前session_id，后续查找可走快速路径
        if session_id and chosen.session_id != session_id:
            await self.repo.update_agent(chosen.id, session_id=session_id)
            logger.info(
                "Leader self-heal: '%s' session绑定 %s",
                chosen.name, session_id[:8],
            )

        return chosen

    async def _self_heal_agent(self, agent, trigger: str = "self_heal") -> None:
        """自愈：WAITING agent收到工具事件 → 修正为BUSY."""
        if agent.status != "waiting":
            return
        await self.repo.update_agent(agent.id, status="busy")
        await self.event_bus.emit(
            "agent.status_changed",
            f"agent:{agent.id}",
            {
                "agent_id": agent.id,
                "name": agent.name,
                "old_status": "waiting",
                "status": "busy",
                "trigger": trigger,
            },
        )
        logger.info("自愈: %s WAITING→BUSY (trigger=%s)", agent.name, trigger)

    @staticmethod
    def _extract_file_path(tool_input: dict | str) -> str:
        """从工具输入中提取文件路径."""
        if isinstance(tool_input, dict):
            return tool_input.get("file_path", "") or tool_input.get("path", "")
        return ""

    def _extract_input_summary(self, tool_name: str, tool_input: dict | str) -> str:
        """从工具输入中提取摘要 — 文件编辑工具优先存储file_path."""
        if isinstance(tool_input, dict):
            if tool_name in self._FILE_EDIT_TOOLS:
                return (
                    tool_input.get("file_path", "")
                    or tool_input.get("path", "")
                    or tool_input.get("description", "")
                    or str(tool_input)[:200]
                )
            return (
                tool_input.get("description", "")
                or tool_input.get("command", "")
                or tool_input.get("file_path", "")
                or tool_input.get("pattern", "")
                or str(tool_input)[:200]
            )
        if isinstance(tool_input, str):
            return tool_input[:200]
        return ""

    async def _check_file_edit_conflict(
        self, tool_name: str, tool_input: dict | str,
        target_agent_id: str, target_agent_name: str, session_id: str,
    ) -> None:
        """检测文件编辑冲突 — 使用内存追踪器O(1)查询 + DB回退.

        增强点：
        1. 内存追踪器优先：O(1)查询，不需要扫描DB
        2. 精确file_path匹配：不再依赖input_summary子串匹配
        3. 冲突严重度分级：同一文件被2个agent编辑 vs 3+个agent编辑
        4. 记录到tracker供hotspot统计
        """
        if tool_name not in self._FILE_EDIT_TOOLS:
            return

        file_path = self._extract_file_path(tool_input)
        if not file_path:
            return

        # 定期清理过期记录（每次检测时顺带清理，开销极小）
        self._file_tracker.cleanup()

        # 记录本次编辑
        self._file_tracker.record(file_path, target_agent_id, target_agent_name)

        # 使用内存追踪器查找冲突（O(1) lookup）
        conflicts = self._file_tracker.find_conflicts(
            file_path, target_agent_id, window_minutes=5,
        )

        if not conflicts:
            # 内存追踪器无冲突 → DB回退（覆盖tracker重启后的冷启动）
            conflicts = await self._db_fallback_conflict_check(
                file_path, target_agent_id, session_id,
            )

        if not conflicts:
            return

        # 去重：同一agent只报一次
        seen_agents: dict[str, _FileEditRecord] = {}
        for c in conflicts:
            if c.agent_id not in seen_agents:
                seen_agents[c.agent_id] = c

        # 冲突严重度
        conflict_count = len(seen_agents)
        severity = "high" if conflict_count >= 2 else "medium"

        conflicting_agents = [
            {"name": r.agent_name, "id": r.agent_id, "last_edit": r.timestamp.isoformat()}
            for r in seen_agents.values()
        ]

        await self.event_bus.emit(
            "file.edit_conflict",
            f"file:{file_path}",
            {
                "file_path": file_path,
                "current_agent_name": target_agent_name,
                "current_agent_id": target_agent_id,
                "conflicting_agents": conflicting_agents,
                "severity": severity,
                "session_id": session_id,
            },
        )
        agent_names = ", ".join(r.agent_name for r in seen_agents.values())
        logger.warning(
            "文件编辑冲突[%s]: %s — %s (先) vs %s (后)",
            severity, file_path, agent_names, target_agent_name,
        )

    async def _db_fallback_conflict_check(
        self, file_path: str, current_agent_id: str, session_id: str,
    ) -> list[_FileEditRecord]:
        """DB回退冲突检测 — 当内存追踪器无数据时（冷启动）.

        改进：直接匹配file_path而非子串匹配input_summary。
        """
        session_agents = await self.repo.find_agents_by_session(session_id)
        other_busy = [
            a for a in session_agents
            if a.id != current_agent_id and a.status == "busy"
        ]
        if not other_busy:
            return []

        cutoff = datetime.now() - timedelta(minutes=5)
        conflicts: list[_FileEditRecord] = []
        for other in other_busy:
            activities = await self.repo.list_activities(other.id, limit=20)
            for act in activities:
                if act.timestamp and act.timestamp < cutoff:
                    break
                if act.tool_name not in self._FILE_EDIT_TOOLS:
                    continue
                # 改进：精确匹配file_path（规范化路径分隔符）
                act_summary = (act.input_summary or "").replace("\\", "/")
                normalized_path = file_path.replace("\\", "/")
                if normalized_path == act_summary or normalized_path in act_summary:
                    record = _FileEditRecord(
                        agent_id=other.id,
                        agent_name=other.name,
                        timestamp=act.timestamp,
                    )
                    conflicts.append(record)
                    # 同时补充到内存追踪器
                    self._file_tracker.record(
                        file_path, other.id, other.name,
                    )
                    break  # 每个agent只取最近一条
        return conflicts

    def get_file_hotspots(self, window_minutes: int = 10) -> list[dict]:
        """获取热点文件信息 — 供team_briefing使用.

        Returns:
            被多个agent编辑的文件列表，含agents和edit_count。
        """
        self._file_tracker.cleanup()
        return self._file_tracker.get_hotspots(window_minutes=window_minutes)

    def get_agent_editing_files(self, agent_id: str) -> list[str]:
        """获取某agent近期正在编辑的文件 — 供agent注册时告知."""
        return self._file_tracker.get_agent_files(agent_id)

    async def _resolve_agent(
        self, cc_agent_id: str, agent_name: str, session_id: str,
    ) -> object | None:
        """解析工具调用所属的agent — 支持cc_id精确匹配+name回退.

        CC team agent存在race condition：SubagentStart可能在MCP注册前触发，
        导致cc_tool_use_id未绑定。此方法在cc_id查找失败时，按name在团队内
        回退匹配，并补绑cc_tool_use_id（late binding），修复后续所有查找。
        """
        # 1. 优先：通过cc_tool_use_id精确匹配
        if cc_agent_id:
            agent = await self.repo.find_agent_by_cc_id(cc_agent_id)
            if agent:
                return agent

        # 2. 回退：cc_agent_id存在但未绑定（race condition），按name在团队内查找
        if cc_agent_id and agent_name:
            leader = await self._find_leader(session_id)
            if leader:
                team = await self.repo.find_active_team_by_leader(leader.id)
                if team:
                    team_agents = await self.repo.list_agents(team.id)
                    matches = [
                        a for a in team_agents
                        if a.name == agent_name and a.id != leader.id
                    ]
                    if matches:
                        agent = matches[0]
                        # Late binding：补绑cc_tool_use_id，修复后续所有查找
                        await self.repo.update_agent(
                            agent.id,
                            cc_tool_use_id=cc_agent_id,
                            session_id=session_id,
                        )
                        logger.info(
                            "Late binding: agent '%s' 绑定 cc_id=%s",
                            agent_name, cc_agent_id[:8],
                        )
                        return agent

        # 3. 无agent_id → 主session的工具调用（Leader）
        if not cc_agent_id:
            return await self._find_leader(session_id)

        return None

    async def _on_pre_tool_use(self, payload: dict) -> dict:
        """记录工具使用事件.

        CC PreToolUse payload:
        - agent_id/agent_type: 存在时表示来自子代理
        - tool_name, tool_input: 工具信息
        - tool_input.description: 工具调用的描述
        """
        tool_name = payload.get("tool_name", "unknown")
        session_id = payload.get("session_id", "")
        cc_agent_id = payload.get("agent_id", "")
        agent_name = payload.get("agent_type", "")
        tool_input = payload.get("tool_input", {})

        input_summary = self._extract_input_summary(tool_name, tool_input)

        # 解析工具调用所属的agent（支持cc_id精确匹配+name回退）
        target_agent = await self._resolve_agent(cc_agent_id, agent_name, session_id)

        if target_agent:
            # 自愈：IDLE agent收到工具事件 → 修正为BUSY
            await self._self_heal_agent(target_agent)

            # 更新最后活跃时间
            await self.repo.update_agent(
                target_agent.id, last_active_at=datetime.now(),
            )

            await self.repo.create_activity(
                agent_id=target_agent.id,
                session_id=session_id,
                tool_name=tool_name,
                input_summary=input_summary,
            )
            # current_task由Leader通过API设定，hook不自动覆盖

            # 文件编辑冲突检测（仅记录事件，不阻止操作）
            try:
                await self._check_file_edit_conflict(
                    tool_name, tool_input,
                    target_agent.id, target_agent.name, session_id,
                )
            except Exception as exc:
                logger.warning("冲突检测异常（不影响工具使用）: %s", exc)

        await self.event_bus.emit(
            "cc.tool_use",
            f"session:{session_id}",
            {
                "tool_name": tool_name,
                "tool_input_summary": input_summary[:200],
                "session_id": session_id,
                "agent_name": payload.get("agent_type", ""),
            },
        )
        return {"decision": "allow"}

    async def _on_post_tool_use(self, payload: dict) -> dict:
        """记录工具完成事件，包含输出摘要.

        CC PostToolUse payload额外包含:
        - tool_response: {stdout, stderr} 或其他工具输出
        """
        tool_name = payload.get("tool_name", "unknown")
        session_id = payload.get("session_id", "")
        cc_agent_id = payload.get("agent_id", "")
        tool_input = payload.get("tool_input", {})
        tool_response = payload.get("tool_response", {})

        input_summary = self._extract_input_summary(tool_name, tool_input)

        # 提取输出摘要
        output_summary = ""
        if isinstance(tool_response, dict):
            output_summary = (
                tool_response.get("stdout", "")
                or tool_response.get("stderr", "")
                or str(tool_response)[:500]
            )
            output_summary = output_summary[:500]
        elif isinstance(tool_response, str):
            output_summary = tool_response[:500]

        # 解析工具调用所属的agent（支持cc_id精确匹配+name回退）
        agent_name = payload.get("agent_type", "")
        target_agent = await self._resolve_agent(cc_agent_id, agent_name, session_id)

        if target_agent:
            # 自愈：IDLE agent收到工具完成事件 → 修正为BUSY
            await self._self_heal_agent(target_agent, trigger="self_heal_post")

            # 更新最后活跃时间
            await self.repo.update_agent(
                target_agent.id, last_active_at=datetime.now(),
            )

            await self.repo.create_activity(
                agent_id=target_agent.id,
                session_id=session_id,
                tool_name=tool_name,
                input_summary=input_summary,
                output_summary=output_summary,
            )

        await self.event_bus.emit(
            "cc.tool_complete",
            f"session:{session_id}",
            {
                "tool_name": tool_name,
                "session_id": session_id,
                "agent_name": payload.get("agent_type", ""),
            },
        )
        return {"status": "recorded"}

    async def _on_session_start(self, payload: dict) -> dict:
        """记录CC会话启动.

        Leader = 用户打开的CC session。每个session对应一个Leader。
        流程：
        1. 通过cwd找项目
        2. 查找项目中已有的Leader（role=leader + project_id匹配）
        3. 有 → 复用，更新session_id+status=busy
        4. 没有 → 创建新Leader
        不再每次创建session-xxx幽灵agent。
        """
        session_id = payload.get("session_id", "")
        cwd = payload.get("cwd", "")
        leader = None

        # 1. 通过cwd找项目
        project = None
        projects = await self.repo.list_projects()
        for proj in projects:
            if proj.root_path and cwd.replace("\\", "/").startswith(proj.root_path.replace("\\", "/")):
                project = proj
                break

        # 2. 查找此session是否已有Leader
        existing = await self.repo.find_agents_by_session(session_id)
        leaders_in_session = [a for a in existing if a.role == "leader"]

        if leaders_in_session:
            # 复用已有的session Leader
            leader = leaders_in_session[0]
            await self.repo.update_agent(
                leader.id, status="busy", last_active_at=datetime.now(),
            )
        elif project:
            # 3. 查找项目中已有的Leader（可能session_id为空的旧Leader）
            project_leader = await self.repo.find_leader_by_project(project.id)
            if project_leader:
                # 复用项目Leader，绑定新session
                leader = project_leader
                await self.repo.update_agent(
                    leader.id,
                    session_id=session_id,
                    status="busy",
                    last_active_at=datetime.now(),
                )
                logger.info("SessionStart: 复用项目Leader %s (session=%s)", leader.name, session_id[:8])
            else:
                # 4. 项目无Leader → 创建
                team = await self._find_or_create_session_team(session_id, payload)
                if team:
                    leader = await self.repo.create_agent(
                        team_id=team.id,
                        name="Leader",
                        role="leader",
                        backstory="Project Leader",
                        source="hook",
                        session_id=session_id,
                        project_id=project.id,
                    )
                    await self.repo.update_agent(
                        leader.id, status="busy", last_active_at=datetime.now(),
                    )
                    logger.info("SessionStart: 创建项目Leader → team %s", team.name)
        else:
            # 无项目匹配 → 自动创建项目（用cwd作为root_path）
            if cwd:
                import os
                dir_name = os.path.basename(cwd.rstrip("/\\")) or "Project"
                project = await self.repo.create_project(
                    name=f"Project-{dir_name}",
                    root_path=cwd.replace("\\", "/"),
                )
                logger.info("SessionStart: 自动创建项目 %s (root=%s)", project.name, cwd)
            # 创建团队和Leader
            team = await self._find_or_create_session_team(session_id, payload)
            if team:
                proj_id = project.id if project else None
                if project and not team.project_id:
                    await self.repo.update_team(team.id, project_id=proj_id)
                leader = await self.repo.create_agent(
                    team_id=team.id,
                    name="Leader",
                    role="leader",
                    backstory="Project Leader",
                    source="hook",
                    session_id=session_id,
                    project_id=proj_id,
                )
                await self.repo.update_agent(
                    leader.id, status="busy", last_active_at=datetime.now(),
                )

        await self.event_bus.emit(
            "cc.session_start",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "cwd": cwd,
                "leader": leader.name if leader else None,
            },
        )
        return {"status": "recorded", "leader": leader.name if leader else None}

    async def _on_session_end(self, payload: dict) -> dict:
        """处理CC会话结束 — 对账并清理状态."""
        session_id = payload.get("session_id", "")
        # 对账：将本session所有agent设为OFFLINE并清除session_id
        agents = await self.repo.find_agents_by_session(session_id)
        for agent in agents:
            updates: dict = {"session_id": None, "status": "offline", "current_task": None}
            await self.repo.update_agent(agent.id, **updates)

        # 统计对账
        hook_count = await self.repo.count_agents_by_source(
            source="hook", session_id=session_id,
        )
        api_count = await self.repo.count_agents_by_source(
            source="api", session_id=session_id,
        )

        await self.event_bus.emit(
            "cc.session_end",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "agents_hook": hook_count,
                "agents_api": api_count,
                "sync_warning": hook_count > api_count,
            },
        )
        return {
            "status": "reconciled",
            "hook_agents": hook_count,
            "api_agents": api_count,
        }

    async def _on_stop(self, payload: dict) -> dict:
        """处理CC Stop事件 — 清理BUSY的hook-source agent状态.

        Stop事件在CC进程终止时触发（Ctrl+C、shutdown等）。
        只清理hook捕获的子代理，不影响api注册的agent（如Tech Lead）。
        api-source agent的状态由SessionEnd对账管理。
        """
        session_id = payload.get("session_id", "")
        updated: list[str] = []

        # 方式1: 按session_id查找hook-source的BUSY agents
        agents = await self.repo.find_agents_by_session(session_id)
        for agent in agents:
            if agent.status == "busy" and agent.source == "hook":
                await self.repo.update_agent(
                    agent.id, status="offline", current_task=None,
                )
                await self.event_bus.emit(
                    "agent.status_changed",
                    f"agent:{agent.id}",
                    {
                        "agent_id": agent.id,
                        "name": agent.name,
                        "status": "offline",
                        "trigger": "stop",
                    },
                )
                updated.append(agent.id)

        # 方式2: 全局兜底 — 只清理最近10分钟内活跃的BUSY hook-source agent
        if not updated:
            cutoff = datetime.now() - timedelta(minutes=10)
            teams = await self.repo.list_teams()
            for team in teams:
                all_agents = await self.repo.list_agents(team.id)
                for agent in all_agents:
                    if agent.status == "busy" and agent.source == "hook":
                        # 只清理最近活跃的或没有活跃记录的agent
                        if agent.last_active_at and agent.last_active_at < cutoff:
                            continue  # 超出时间窗口，跳过（可能属于其他session）
                        await self.repo.update_agent(
                            agent.id, status="offline", current_task=None,
                        )
                        await self.event_bus.emit(
                            "agent.status_changed",
                            f"agent:{agent.id}",
                            {
                                "agent_id": agent.id,
                                "name": agent.name,
                                "status": "offline",
                                "trigger": "stop_global",
                            },
                        )
                        updated.append(agent.id)

        logger.info("Stop event: %d hook agents set to offline", len(updated))
        return {"status": "cleaned", "agents_offline": updated}

    async def _find_or_create_session_team(
        self, session_id: str, payload: dict,
    ):
        """查找与session关联的团队.

        策略：
        1. 查找leader所在的团队（session_id匹配）
        2. 返回最近创建的团队（fallback）
        3. 自动创建新团队（无团队时）
        """
        # 策略1: 找到leader所在的团队
        if session_id:
            agents = await self.repo.find_agents_by_session(session_id)
            if agents:
                # leader的团队就是目标团队
                return await self.repo.get_team(agents[0].team_id)

        # 策略2: 通过cwd匹配项目，找到关联的团队
        cwd = payload.get("cwd", "")
        teams = await self.repo.list_teams()
        if teams and cwd:
            # 2a: 尝试通过cwd找到所属项目的团队
            projects = await self.repo.list_projects()
            for proj in projects:
                if proj.root_path and cwd.replace("\\", "/").startswith(proj.root_path.replace("\\", "/")):
                    proj_teams = [t for t in teams if t.project_id == proj.id]
                    if proj_teams:
                        return proj_teams[0]
            # 2b: 没有项目匹配，且只有一个团队时安全返回
            if len(teams) == 1:
                return teams[0]
            # 2c: 多团队无法确定，返回最近创建的（并记录警告）
            logger.warning(
                "多团队无法确定归属(cwd=%s, teams=%d)，fallback到最近创建的团队",
                cwd, len(teams),
            )
            return teams[0]
        if teams:
            return teams[0]

        # 策略3: 创建新团队
        cwd = payload.get("cwd", "")
        team = await self.repo.create_team(
            name=f"session-{session_id[:8]}",
            mode="coordinate",
        )
        logger.info("自动创建团队: %s (session=%s, cwd=%s)", team.name, session_id[:8], cwd)
        return team
