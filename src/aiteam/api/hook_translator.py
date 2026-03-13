"""AI Team OS — Hook事件翻译器.

将Claude Code的Hook事件转化为OS系统操作，
实现CC会话与OS之间的自动同步桥接。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiteam.api.event_bus import EventBus
from aiteam.storage.repository import StorageRepository

logger = logging.getLogger(__name__)


class HookTranslator:
    """将Claude Code hook事件转化为OS系统操作."""

    # 文件编辑工具名称集合，用于冲突检测
    _FILE_EDIT_TOOLS = frozenset({"Edit", "Write"})

    def __init__(self, repo: StorageRepository, event_bus: EventBus) -> None:
        self.repo = repo
        self.event_bus = event_bus

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
        """
        cc_agent_id = payload.get("agent_id", "")
        agent_name = payload.get("agent_type", "unnamed-agent")
        session_id = payload.get("session_id", "")

        # 查找是否已通过API注册（按名称匹配）
        existing = await self.repo.find_agent_by_session(session_id, agent_name)
        if existing:
            # 已注册 -> 更新状态、CC agent ID和最后活跃时间
            await self.repo.update_agent(
                existing.id, status="busy", cc_tool_use_id=cc_agent_id,
                last_active_at=datetime.now(),
            )
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

        # 未注册 -> 自动创建（兜底）
        team = await self._find_or_create_session_team(session_id, payload)
        if team:
            agent = await self.repo.create_agent(
                team_id=team.id,
                name=agent_name,
                role="general",
                backstory=f"Auto-captured from CC session {session_id[:8]}",
                source="hook",
                session_id=session_id,
                cc_tool_use_id=cc_agent_id,
            )
            await self.repo.update_agent(
                agent.id, status="busy", last_active_at=datetime.now(),
            )
            await self.event_bus.emit(
                "agent.auto_registered",
                f"agent:{agent.id}",
                {
                    "agent_id": agent.id,
                    "name": agent_name,
                    "session_id": session_id,
                    "message": "Hooks自动捕获",
                },
            )
            return {"status": "auto_created", "agent_id": agent.id}

        return {"status": "skipped", "reason": "no team context"}

    async def _on_subagent_stop(self, payload: dict) -> dict:
        """处理子Agent停止事件.

        CC SubagentStop payload包含 agent_id 用于精确匹配。
        """
        cc_agent_id = payload.get("agent_id", "")
        session_id = payload.get("session_id", "")

        updated: list[str] = []
        if cc_agent_id:
            # 精确匹配CC agent ID
            agent = await self.repo.find_agent_by_cc_id(cc_agent_id)
            if agent and agent.status == "busy":
                await self.repo.update_agent(
                    agent.id, status="idle", current_task=None,
                    last_active_at=datetime.now(),
                )
                await self.event_bus.emit(
                    "agent.status_changed",
                    f"agent:{agent.id}",
                    {
                        "agent_id": agent.id,
                        "name": agent.name,
                        "status": "idle",
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
                        agent.id, status="idle", last_active_at=datetime.now(),
                    )
                    await self.event_bus.emit(
                        "agent.status_changed",
                        f"agent:{agent.id}",
                        {
                            "agent_id": agent.id,
                            "status": "idle",
                            "trigger": "hook",
                        },
                    )
                    updated.append(agent.id)
        return {"status": "updated", "agents_idle": updated}

    async def _find_leader(self, session_id: str) -> object | None:
        """查找当前session的leader agent.

        严格按session_id匹配，避免错误关联到其他session的agent。
        """
        if not session_id:
            return None

        # 按session_id精确匹配
        agents = await self.repo.find_agents_by_session(session_id)
        if not agents:
            return None

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

    async def _self_heal_agent(self, agent, trigger: str = "self_heal") -> None:
        """自愈：IDLE agent收到工具事件 → 修正为BUSY."""
        if agent.status != "idle":
            return
        await self.repo.update_agent(agent.id, status="busy")
        await self.event_bus.emit(
            "agent.status_changed",
            f"agent:{agent.id}",
            {
                "agent_id": agent.id,
                "name": agent.name,
                "old_status": "idle",
                "status": "busy",
                "trigger": trigger,
            },
        )
        logger.info("自愈: %s IDLE→BUSY (trigger=%s)", agent.name, trigger)

    async def _check_file_edit_conflict(
        self, tool_name: str, tool_input: dict | str,
        target_agent_id: str, target_agent_name: str, session_id: str,
    ) -> None:
        """检测文件编辑冲突 — 查询同session其他BUSY agent的近5分钟activity."""
        if tool_name not in self._FILE_EDIT_TOOLS:
            return

        file_path = ""
        if isinstance(tool_input, dict):
            file_path = tool_input.get("file_path", "") or tool_input.get("path", "")
        if not file_path:
            return

        # 查找同session的其他BUSY agent
        session_agents = await self.repo.find_agents_by_session(session_id)
        other_busy = [
            a for a in session_agents
            if a.id != target_agent_id and a.status == "busy"
        ]
        if not other_busy:
            return

        cutoff = datetime.now() - timedelta(minutes=5)
        for other in other_busy:
            # 查询该agent最近的activity记录
            activities = await self.repo.list_activities(other.id, limit=20)
            for act in activities:
                # 只看5分钟内的Edit/Write活动
                if act.timestamp and act.timestamp < cutoff:
                    break
                if act.tool_name not in self._FILE_EDIT_TOOLS:
                    continue
                # 检查是否编辑了同一文件
                if file_path in (act.input_summary or ""):
                    await self.event_bus.emit(
                        "file.edit_conflict",
                        f"file:{file_path}",
                        {
                            "file_path": file_path,
                            "previous_agent_name": other.name,
                            "previous_agent_id": other.id,
                            "current_agent_name": target_agent_name,
                            "current_agent_id": target_agent_id,
                            "session_id": session_id,
                        },
                    )
                    logger.warning(
                        "文件编辑冲突: %s — %s (先) vs %s (后)",
                        file_path, other.name, target_agent_name,
                    )
                    return  # 找到一个冲突即可，不需要重复记录

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
        tool_input = payload.get("tool_input", {})

        # 提取输入摘要
        input_summary = ""
        if isinstance(tool_input, dict):
            input_summary = (
                tool_input.get("description", "")
                or tool_input.get("command", "")
                or tool_input.get("file_path", "")
                or tool_input.get("pattern", "")
                or str(tool_input)[:200]
            )
        elif isinstance(tool_input, str):
            input_summary = tool_input[:200]

        # 通过CC agent_id精确关联到子代理
        target_agent = None
        if cc_agent_id:
            target_agent = await self.repo.find_agent_by_cc_id(cc_agent_id)
        else:
            # 没有agent_id → 主session的工具调用（Tech Lead）
            target_agent = await self._find_leader(session_id)

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
            # 更新agent的current_task为当前工具的语义描述
            task_desc = input_summary[:100] if input_summary else f"正在使用 {tool_name}"
            try:
                await self.repo.update_agent(
                    target_agent.id, current_task=task_desc,
                )
            except Exception:
                pass  # current_task列可能尚未生效

            # 文件编辑冲突检测（仅记录事件，不阻止操作）
            await self._check_file_edit_conflict(
                tool_name, tool_input,
                target_agent.id, target_agent.name, session_id,
            )

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

        # 提取输入摘要
        input_summary = ""
        if isinstance(tool_input, dict):
            input_summary = (
                tool_input.get("description", "")
                or tool_input.get("command", "")
                or tool_input.get("file_path", "")
                or tool_input.get("pattern", "")
                or str(tool_input)[:200]
            )
        elif isinstance(tool_input, str):
            input_summary = tool_input[:200]

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

        # 通过CC agent_id精确关联
        target_agent = None
        if cc_agent_id:
            target_agent = await self.repo.find_agent_by_cc_id(cc_agent_id)
        else:
            # 主session的工具调用 → 关联Tech Lead
            target_agent = await self._find_leader(session_id)

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

        自动为session注册leader agent，确保后续工具事件能正确关联。
        """
        session_id = payload.get("session_id", "")
        cwd = payload.get("cwd", "")

        # 查找是否已有此session的agent
        existing = await self.repo.find_agents_by_session(session_id)
        leader = None

        if not existing:
            # 新session → 在最近的团队中注册一个leader agent
            team = await self._find_or_create_session_team(session_id, payload)
            if team:
                leader = await self.repo.create_agent(
                    team_id=team.id,
                    name=f"session-{session_id[:8]}",
                    role="leader",
                    backstory=f"Auto-registered leader for session {session_id[:8]}",
                    source="hook",
                    session_id=session_id,
                )
                await self.repo.update_agent(
                    leader.id, status="busy", last_active_at=datetime.now(),
                )
                logger.info("SessionStart: 自动注册leader %s → team %s", leader.name, team.name)
        else:
            # 已有agent → 更新session关联和状态
            leader = existing[0]
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
        # 对账：将本session所有agent设为IDLE并清除session_id
        agents = await self.repo.find_agents_by_session(session_id)
        for agent in agents:
            updates: dict = {"session_id": None}
            if agent.status == "busy":
                updates["status"] = "idle"
                updates["current_task"] = None
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
                    agent.id, status="idle", current_task=None,
                )
                await self.event_bus.emit(
                    "agent.status_changed",
                    f"agent:{agent.id}",
                    {
                        "agent_id": agent.id,
                        "name": agent.name,
                        "status": "idle",
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
                            agent.id, status="idle", current_task=None,
                        )
                        await self.event_bus.emit(
                            "agent.status_changed",
                            f"agent:{agent.id}",
                            {
                                "agent_id": agent.id,
                                "name": agent.name,
                                "status": "idle",
                                "trigger": "stop_global",
                            },
                        )
                        updated.append(agent.id)

        logger.info("Stop event: %d hook agents set to idle", len(updated))
        return {"status": "cleaned", "agents_idle": updated}

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

        # 策略2: 返回最近创建的团队
        teams = await self.repo.list_teams()
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
