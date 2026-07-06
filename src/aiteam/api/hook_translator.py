"""AI Team OS — Hook event translator.

Translates Claude Code hook events into OS system operations,
bridging automatic sync between CC sessions and the OS.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from aiteam.api import workflow_ingest
from aiteam.api.event_bus import EventBus
from aiteam.storage.repository import StorageRepository
from aiteam.types import WorkflowRun

# Agent standardized prompt template path
_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[3] / "plugin" / "config" / "agent-prompt-template.md"
)

# CC ultracode/Workflow runtime tags every internal fan-out agent with this fixed
# agent_type. We track each Workflow *run* as its own OS team (strict 1:1) so the
# Dashboard can see "one workflow = one team + N distinct members", instead of the
# old behaviour that collapsed all of them into a single 'workflow-subagent' row.
WORKFLOW_AGENT_TYPE = "workflow-subagent"
# Workflow run id lives in the subagent transcript path: .../workflows/wf_<id>/agent-<aid>.jsonl
_WF_RUN_ID_RE = re.compile(r"wf_[0-9a-z]+(?:-[0-9a-z]+)*", re.IGNORECASE)

logger = logging.getLogger(__name__)


@dataclass
class _FileEditRecord:
    """Single file edit record."""

    agent_id: str
    agent_name: str
    timestamp: datetime


@dataclass
class _FileEditTracker:
    """In-memory file edit tracker — O(1) conflict queries.

    Maintains a list of recent edit records per file, supporting:
    1. Quick check if a file was edited by another agent (conflict detection)
    2. Hotspot file statistics (files edited by multiple agents)
    3. Automatic cleanup of expired records
    """

    # file_path -> list of recent edit records
    _edits: dict[str, list[_FileEditRecord]] = field(
        default_factory=lambda: defaultdict(list),
    )
    # Record retention duration
    _window: timedelta = field(default_factory=lambda: timedelta(minutes=10))

    def record(self, file_path: str, agent_id: str, agent_name: str) -> None:
        """Record a file edit."""
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
        self,
        file_path: str,
        current_agent_id: str,
        window_minutes: int = 5,
    ) -> list[_FileEditRecord]:
        """Find edit records from other agents that conflict with the current agent.

        Returns:
            List of records from other agents who edited the same file within the time window.
        """
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        records = self._edits.get(file_path, [])
        return [r for r in records if r.agent_id != current_agent_id and r.timestamp >= cutoff]

    def get_hotspots(self, window_minutes: int = 10, min_agents: int = 2) -> list[dict]:
        """Get hotspot files — files edited by multiple agents within the time window.

        Returns:
            List of hotspot files, each containing file_path, agents, edit_count.
        """
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        hotspots = []
        for file_path, records in self._edits.items():
            recent = [r for r in records if r.timestamp >= cutoff]
            if not recent:
                continue
            unique_agents = {r.agent_name for r in recent}
            if len(unique_agents) >= min_agents:
                hotspots.append(
                    {
                        "file_path": file_path,
                        "agents": sorted(unique_agents),
                        "edit_count": len(recent),
                        "last_edit": max(r.timestamp for r in recent).isoformat(),
                    }
                )
        # Sort by edit count descending
        hotspots.sort(key=lambda h: h["edit_count"], reverse=True)
        return hotspots

    def get_agent_files(self, agent_id: str, window_minutes: int = 10) -> list[str]:
        """Get list of files recently being edited by a specific agent."""
        cutoff = datetime.now() - timedelta(minutes=window_minutes)
        files = []
        for file_path, records in self._edits.items():
            if any(r.agent_id == agent_id and r.timestamp >= cutoff for r in records):
                files.append(file_path)
        return files

    def cleanup(self) -> int:
        """Clean up expired records, return count of removed records."""
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
    """Translates Claude Code hook events into OS system operations."""

    # File edit tool names, used for conflict detection
    _FILE_EDIT_TOOLS = frozenset({"Edit", "Write"})

    # Substantive tools that trigger intent events
    _INTENT_TOOLS = frozenset({"Read", "Edit", "Write", "Bash"})

    # Intent event throttle interval (seconds)
    _INTENT_THROTTLE_SECS = 10

    def __init__(self, repo: StorageRepository, event_bus: EventBus) -> None:
        self.repo = repo
        self.event_bus = event_bus
        self._file_tracker = _FileEditTracker()
        self._prompt_template: str | None = None
        # pending_spans: key = "{agent_id}:{session_id}:{tool_name}"
        # value = (activity_id, start_time)
        self._pending_spans: dict[str, tuple[str, datetime]] = {}
        # Intent throttle: key = agent_id, value = last_emit_time
        self._intent_last_emit: dict[str, datetime] = {}
        # Last known cwd from hook payload (for project matching)
        self._last_cwd: str = ""
        # I3a: PreToolUse(Workflow) 解析出的静态计划暂存（session_id -> plan），
        # 供 PostToolUse(Workflow) 回执补齐 name/phases/planned_agent_count。
        self._workflow_plans: dict[str, dict] = {}

    def _load_prompt_template(self) -> str:
        """Lazy-load the Agent standardized prompt template."""
        if self._prompt_template is None:
            try:
                self._prompt_template = _TEMPLATE_PATH.read_text(encoding="utf-8")
            except FileNotFoundError:
                logger.warning("Agent prompt template file not found: %s", _TEMPLATE_PATH)
                self._prompt_template = ""
        return self._prompt_template

    def _render_prompt(self, role: str, project_path: str = "") -> str:
        """Fill template with basic info and return system_prompt."""
        template = self._load_prompt_template()
        if not template:
            return ""
        return template.replace("{role}", role).replace("{project_path}", project_path or "未指定")

    async def handle_event(self, payload: dict) -> dict:
        """Unified event handling entry point."""
        # Set per-request cwd for project matching (NOT persistent — safe for multi-session)
        self._current_event_cwd = payload.get("cwd", "")
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

    def _extract_workflow_run_id(self, payload: dict) -> str | None:
        """Pull the Workflow run id (wf_<id>) from the subagent's transcript path.

        CC stores each workflow subagent transcript at
        .../subagents/workflows/wf_<id>/agent-<aid>.jsonl, so the run id is in the path.
        Returns None when no transcript field is present (e.g. trimmed from an oversized
        payload) — the caller then falls back to session-scoped grouping.
        """
        for key in ("transcript_path", "agent_transcript_path", "cwd"):
            val = payload.get(key)
            if not val:
                continue
            m = _WF_RUN_ID_RE.search(str(val).replace("\\", "/"))
            if m:
                return m.group(0)
        return None

    async def _register_workflow_subagent(
        self, payload: dict, cc_agent_id: str, session_id: str
    ) -> dict:
        """Register a CC Workflow fan-out agent as a member of its run's OS team.

        Strict one-workflow-one-team: team key = workflow-<wf_run_id>. Members are
        deduped by cc_agent_id (NOT by name — all workflow agents share the literal
        name 'workflow-subagent', so name-dedup would collapse a 16-agent run into one
        row). Unlike the normal path this does NOT require a pre-existing active team;
        the workflow team is auto-created so the run is always tracked.
        """
        # 1. Dedup by CC agent id — same internal agent re-reporting just refreshes.
        if cc_agent_id:
            existing = await self.repo.find_agent_by_cc_id(cc_agent_id)
            if existing:
                await self.repo.update_agent(
                    existing.id,
                    status="busy",
                    session_id=session_id,
                    last_active_at=datetime.now(),
                )
                return {"status": "updated", "agent_id": existing.id, "kind": "workflow"}

        # 2. Resolve the workflow run -> team key (strict 1:1; fall back to session).
        wf_id = self._extract_workflow_run_id(payload)
        session_key = f"workflow-session-{(session_id or 'unknown')[:8]}"
        team_key = f"workflow-{wf_id}" if wf_id else session_key

        # 3. Bind to the launching project strictly（跨项目修复C）：cwd 最长前缀 →
        #    同会话 Leader；解析不到留 None 绝不跨会话猜——旧 _find_leader 全局回退
        #    会把未注册项目的 workflow 团队/agent 绑到别的项目上（隔离违规实录）。
        project_id = await self._resolve_project_id_by_cwd(payload.get("cwd") or "")
        if not project_id and session_id:
            try:
                _same = await self.repo.find_agents_by_session(session_id)
            except Exception:  # noqa: BLE001
                _same = []
            _leaders = [a for a in _same if a.role == "leader"]
            project_id = (_leaders[0].project_id if _leaders else None) or None

        # 4. Find-or-create the workflow team (idempotent by name across the run's agents).
        team = await self.repo.get_team_by_name(team_key)
        if team is None and wf_id:
            # 反碎片化：wf_id 迟到是常态（SubagentStart 常在回执前到达），本 run 早到的
            # agent 已挂在 session 兜底队——只要兜底队未被别的 run 认领，就继续用它并补
            # workflow_run_id 链接，避免「一 run 两队」（agents 在 session 队、run 挂
            # 空的 wf 队互不认识，2026-07-06 监控实录）。
            fallback = await self.repo.get_team_by_name(session_key)
            fb_wf = (fallback.config or {}).get("workflow_run_id") if fallback else None
            if fallback is not None and fb_wf in (None, "", wf_id):
                team = fallback
                if fb_wf != wf_id:
                    cfg = dict(fallback.config or {})
                    cfg["workflow_run_id"] = wf_id
                    try:
                        await self.repo.update_team(fallback.id, config=cfg)
                    except Exception:  # noqa: BLE001
                        pass
        if team is None:
            team = await self.repo.create_team(
                name=team_key,
                mode="coordinate",
                config={"kind": "workflow", "auto_created": True, "workflow_run_id": wf_id},
                project_id=project_id,
            )
            await self.event_bus.emit(
                "team.created",
                f"team:{team.id}",
                {"team_id": team.id, "name": team_key, "kind": "workflow"},
            )

        # 5. Register this internal agent as a distinct member (unique name per cc id).
        member_name = f"wf-{(cc_agent_id or session_id or 'anon')[:10]}"
        new_agent = await self.repo.create_agent(
            team_id=team.id,
            name=member_name,
            role=WORKFLOW_AGENT_TYPE,
            source="hook",
            session_id=session_id,
            cc_tool_use_id=cc_agent_id,
            # SubagentStart payload 不携带模型信息——留空而非落库仓库默认值
            #（曾把 opus-4-8 的运行显示成 claude-opus-4-7）；真实模型由观测层
            # 从 wf_<id>.json 终态回填到 workflow_agents.model。
            model="",
        )
        await self.repo.update_agent(
            new_agent.id,
            status="busy",
            project_id=project_id,
            last_active_at=datetime.now(),
        )
        await self.event_bus.emit(
            "decision.agent_created",
            f"agent:{new_agent.id}",
            {"agent_id": new_agent.id, "name": member_name, "team_id": team.id, "kind": "workflow"},
        )
        return {
            "status": "created",
            "agent_id": new_agent.id,
            "team_id": team.id,
            "kind": "workflow",
        }

    async def _promote_workflow_team(self, agent: object, payload: dict) -> None:
        """Re-key a workflow subagent from the session-fallback team to its per-run team.

        Strict one-workflow-one-team: wf_id is ABSENT at SubagentStart (payload only
        carries the Leader's main transcript), but PRESENT later in agent_transcript_path
        (SubagentStop) and the subagent's own tool-call payloads. Once a wf_id is visible,
        migrate the agent to workflow-<wf_id>. Idempotent: no-op if already in that team
        or if no wf_id is resolvable from this payload.
        """
        if agent is None or getattr(agent, "role", None) != WORKFLOW_AGENT_TYPE:
            return
        wf_id = self._extract_workflow_run_id(payload)
        if not wf_id:
            return
        team_key = f"workflow-{wf_id}"
        cur_team = await self.repo.get_team(agent.team_id) if getattr(agent, "team_id", None) else None
        if cur_team is not None and cur_team.name == team_key:
            return  # already correctly grouped
        project_id = getattr(agent, "project_id", None)
        team = await self.repo.get_team_by_name(team_key)
        if team is None:
            team = await self.repo.create_team(
                name=team_key,
                mode="coordinate",
                config={"kind": "workflow", "auto_created": True, "workflow_run_id": wf_id},
                project_id=project_id,
            )
        await self.repo.update_agent(agent.id, team_id=team.id)
        await self.event_bus.emit(
            "agent.updated",
            f"agent:{agent.id}",
            {"agent_id": agent.id, "changes": {"team_id": team.id, "reason": "workflow_run_regroup"}},
        )

    def _parse_workflow_plan(self, script: str) -> dict:
        """Statically extract a Workflow run's planned roster from its script (Step 4).

        Knowable up-front (before any agent runs): meta.phases (declarative skeleton)
        and literal agent() calls. NOT knowable: dynamic fan-out (.map / while / pipeline
        over runtime arrays) — reported as a count of dynamic nodes whose size is runtime.
        """
        phases = re.findall(r"title:\s*['\"]([^'\"]+)['\"]", script)
        literal_agents = len(re.findall(r"\bagent\(", script))
        dynamic_nodes = len(re.findall(r"\.map\(|while\s*\(|\bpipeline\(", script))
        m = re.search(r"name:\s*['\"]([^'\"]+)['\"]", script)
        return {
            "name": m.group(1) if m else "",
            "phases": phases,
            "literal_agent_count": literal_agents,
            "dynamic_nodes": dynamic_nodes,
        }

    async def _on_subagent_start(self, payload: dict) -> dict:
        """Handle sub-agent start event.

        CC SubagentStart payload structure:
        - agent_type: Agent name (from Agent tool's name parameter)
        - agent_id: CC internal agent ID (for correlating subsequent tool calls)
        - session_id: Parent session ID
        - cc_team_name: (optional) CC team name, injected by send_event.py

        Deduplication strategy (4-level lookup chain):
        1. Exact match by cc_tool_use_id (fastest, covers duplicate SubagentStart)
        2. Match by session_id + name
        3. Match by same-name agent within team (covers MCP pre-registration)
        4. None found -> find/create OS team by cc_team_name -> register
        """
        cc_agent_id = payload.get("agent_id", "")
        agent_name = payload.get("agent_type", "unnamed-agent")
        session_id = payload.get("session_id", "")
        cc_team_name = payload.get("cc_team_name", "")

        # Workflow (ultracode) fan-out agents take a dedicated path: each Workflow
        # run becomes its own OS team, every internal agent a distinct member.
        if agent_name == WORKFLOW_AGENT_TYPE:
            return await self._register_workflow_subagent(payload, cc_agent_id, session_id)

        existing = None
        leader = None
        team = None

        # 1. Exact match by cc_tool_use_id (fastest, covers duplicate SubagentStart events)
        if cc_agent_id:
            existing = await self.repo.find_agent_by_cc_id(cc_agent_id)

        # 2. Determine target team, then deduplicate within team
        if not existing:
            if cc_team_name:
                # Has cc_team_name -> resolve target team, deduplicate by name within that team only
                team = await self._resolve_cc_team(cc_team_name, session_id)
                if team:
                    team_agents = await self.repo.list_agents(team.id)
                    matches = [a for a in team_agents if a.name == agent_name]
                    if matches:
                        existing = matches[0]
            else:
                # No cc_team_name -> legacy compat: global lookup by session_id+name
                existing = await self.repo.find_agent_by_session(
                    session_id,
                    agent_name,
                )

        # 3. Still no match -> find team via Leader, deduplicate by name within team
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
            # Already registered -> update status, bind session and CC agent ID
            update_fields: dict = {
                "status": "busy",
                "cc_tool_use_id": cc_agent_id,
                "session_id": session_id,
                "last_active_at": datetime.now(),
            }
            # If existing role contains " — ", auto-split into role + current_task
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

        # 4. Not registered -> find/create OS team by cc_team_name, then register agent
        if not team and cc_team_name:
            team = await self._resolve_cc_team(cc_team_name, session_id)

        if not team:
            if not leader:
                leader = await self._find_leader(session_id)
            if leader:
                team = await self.repo.find_active_team_by_leader(leader.id)

        if not team:
            logger.info(
                "SubagentStart: agent '%s' not registered and no active team found, skipping",
                agent_name,
            )
            return {"status": "skipped", "reason": "no active team"}

        # Final name dedup before creation (race condition: MCP may have completed registration during lookup)
        team_agents = await self.repo.list_agents(team.id)
        late_match = [a for a in team_agents if a.name == agent_name]
        if late_match:
            existing = late_match[0]
            await self.repo.update_agent(
                existing.id,
                status="busy",
                cc_tool_use_id=cc_agent_id,
                session_id=session_id,
                last_active_at=datetime.now(),
            )
            logger.info(
                "SubagentStart: concurrent dedup hit for agent '%s' (id=%s)",
                agent_name,
                existing.id,
            )
            return {"status": "updated", "agent_id": existing.id}

        # Extract role and current_task from agent_name (if contains " — " separator)
        if " — " in agent_name:
            parts = agent_name.split(" — ", 1)
            auto_role = parts[0].strip()
            auto_task = parts[1].strip()
        else:
            auto_role = agent_name
            auto_task = None

        # Auto-fill standardized prompt template
        project_path = ""
        if team.project_id:
            project = await self.repo.get_project(team.project_id)
            if project:
                project_path = project.root_path or ""
        auto_system_prompt = self._render_prompt(auto_role, project_path)

        new_agent = await self.repo.create_agent(
            team_id=team.id,
            name=agent_name,
            role=auto_role,
            source="hook",
            session_id=session_id,
            cc_tool_use_id=cc_agent_id,
            system_prompt=auto_system_prompt,
        )
        # create_agent defaults to status=waiting, immediately set to busy
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
        # Decision event: Agent created (cockpit Phase 1)
        await self.event_bus.emit(
            "decision.agent_created",
            f"team:{team.id}",
            {
                "agent_id": new_agent.id,
                "agent_name": agent_name,
                "role": auto_role,
                "team_id": team.id,
                "team_name": team.name,
                "rationale": "auto_registered_via_hook",
                "alternatives": [],
                "outcome": "pending",
            },
        )
        logger.info(
            "SubagentStart: auto-registered agent '%s' -> team '%s' (cc_id=%s)",
            agent_name,
            team.name,
            cc_agent_id[:8] if cc_agent_id else "?",
        )
        return {"status": "created", "agent_id": new_agent.id}

    async def _on_subagent_stop(self, payload: dict) -> dict:
        """Handle sub-agent stop event.

        CC SubagentStop payload contains agent_id for exact matching.
        """
        cc_agent_id = payload.get("agent_id", "")
        agent_name = payload.get("agent_type", "")
        session_id = payload.get("session_id", "")

        updated: list[str] = []
        if cc_agent_id:
            # Find via _resolve_agent (supports late binding fallback)
            agent = await self._resolve_agent(cc_agent_id, agent_name, session_id)
            if agent:
                # Only update last_active_at, don't change status or current_task
                # CC's SubagentStop only means "one turn ended", agent may still be working
                # State changes are handled by StateReaper: 5min inactive->waiting, 30min->offline
                await self.repo.update_agent(
                    agent.id,
                    last_active_at=datetime.now(),
                )
                # Strict 1:1 — SubagentStop carries agent_transcript_path with the wf_id;
                # promote workflow subagents out of the session-fallback team into their run team.
                await self._promote_workflow_team(agent, payload)
                updated.append(agent.id)
        else:
            # Fallback: find BUSY agents in this session, only update last_active_at without changing status
            agents = await self.repo.find_agents_by_session(session_id)
            for agent in agents:
                if agent.status == "busy":
                    await self.repo.update_agent(
                        agent.id,
                        last_active_at=datetime.now(),
                    )
                    updated.append(agent.id)
        return {"status": "updated", "agents_waiting": updated}

    async def _resolve_cc_team(self, cc_team_name: str, session_id: str) -> object | None:
        """Find or create the corresponding OS team for a CC team name.

        1. Exact name match on existing OS teams (prefer active status)
        2. Not found -> auto-create a same-name OS team
        """
        if not cc_team_name:
            return None

        # 1. Find existing team by name
        existing_team = await self.repo.get_team_by_name(cc_team_name)
        if existing_team:
            logger.info(
                "CC team mapping: '%s' -> existing OS team (id=%s, status=%s)",
                cc_team_name,
                existing_team.id,
                existing_team.status,
            )
            # Auto-revive completed team if a new agent is joining — keeps
            # team.status consistent with reality (busy member exists).
            # Without this, agents end up "in a historical team" on the
            # dashboard, which confused the user when ecosystem-indexer
            # registered into a closed phase1-impl.
            if existing_team.status == "completed":
                await self.repo.update_team(existing_team.id, status="active")
                logger.warning(
                    "Team '%s' was completed; auto-revived to active because "
                    "a new agent (cc_team_name=%s) is registering.",
                    cc_team_name, cc_team_name,
                )
                await self.event_bus.emit(
                    "team.auto_revived",
                    f"team:{existing_team.id}",
                    {"team_id": existing_team.id, "team_name": cc_team_name,
                     "reason": "new agent registration on completed team"},
                )
                existing_team.status = "active"
            return existing_team

        # 2. Auto-create same-name OS team
        new_team = await self.repo.create_team(
            name=cc_team_name,
            mode="coordinate",
        )
        logger.info(
            "CC team mapping: auto-created OS team '%s' (id=%s)",
            cc_team_name,
            new_team.id,
        )

        # Link to project — prefer the session's Leader as authority, fall back
        # to cwd matching only when no Leader exists yet. The Leader's project_id
        # is locked in when the session first opened, so it's robust against
        # ambiguous cwd (multiple CC windows whose cwds overlap by prefix).
        project_id = None

        # 1) Authoritative: session_id -> Leader -> project_id
        if session_id:
            leader = await self._find_leader(session_id)
            if leader and leader.project_id:
                project_id = leader.project_id
                logger.info(
                    "CC team mapping: session %s -> leader '%s' -> project %s",
                    session_id[:8], leader.name, project_id,
                )

        # 2) Fallback: cwd longest-prefix match (only when no Leader yet)
        if not project_id:
            cwd = ""
            if hasattr(self, '_current_event_cwd') and self._current_event_cwd:
                cwd = self._current_event_cwd.replace("\\", "/").rstrip("/").lower()
            if not cwd:
                import os as _os
                cwd = _os.getcwd().replace("\\", "/").rstrip("/").lower()
            if cwd:
                projects = await self.repo.list_projects()
                # Pick the most specific (longest) matching root_path. Several
                # projects can match via prefix (e.g. C:/Users/TUF and
                # C:/Users/TUF/Desktop/AI...) — earlier code took the first
                # match, which frequently picked the broader parent by mistake.
                best_match = None
                best_len = -1
                for p in projects:
                    rp = (p.root_path or "").replace("\\", "/").rstrip("/").lower()
                    if rp and (cwd == rp or cwd.startswith(rp + "/")):
                        if len(rp) > best_len:
                            best_match = p
                            best_len = len(rp)
                if best_match is not None:
                    project_id = best_match.id
                    logger.info(
                        "CC team mapping: cwd '%s' -> project %s (root_path=%s)",
                        cwd, best_match.name, best_match.root_path,
                    )

        if project_id:
            await self.repo.update_team(new_team.id, project_id=project_id)
            logger.info(
                "CC team mapping: team '%s' linked to project %s",
                cc_team_name,
                project_id,
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

    @staticmethod
    def _read_session_model(transcript_path: str) -> str:
        """尾读主会话 transcript，取最后一条 assistant 消息的 model。

        主会话模型 hook 事件里没有，但 transcript 每条 assistant 行都带
        message.model——尾部 200KB 内向后覆盖扫描即可（与观测层 lastCtx 同思路）。
        失败/缺失一律返回空串，绝不抛出。
        """
        try:
            if not transcript_path:
                return ""
            p = Path(transcript_path)
            if not p.is_file():
                return ""
            size = p.stat().st_size
            with open(p, "rb") as f:
                if size > 200_000:
                    f.seek(size - 200_000)
                data = f.read().decode("utf-8", errors="replace")
            model = ""
            for line in data.splitlines():
                try:
                    d = json.loads(line)
                except Exception:  # noqa: BLE001 — seek 截断的首行等
                    continue
                if d.get("type") == "assistant":
                    m = (d.get("message") or {}).get("model")
                    if m:
                        model = str(m)
            return model
        except Exception:  # noqa: BLE001
            return ""

    async def _find_leader(self, session_id: str) -> object | None:
        """Find the leader agent for the current session.

        Lookup strategy:
        1. Exact match by session_id (fastest)
        2. Cross-session fallback by role="leader" (covers DB migration/API restart where session_id is stale)
        After finding the Leader, auto-bind current session_id (self-heal).
        """
        # 1. Exact match by session_id
        if session_id:
            agents = await self.repo.find_agents_by_session(session_id)
            if agents:
                # Prefer leader role agent
                leaders = [a for a in agents if a.role == "leader"]
                if leaders:
                    return leaders[0]

                # Then prefer api-source agent
                api_matches = [a for a in agents if a.source == "api"]
                if api_matches:
                    return api_matches[0]

                # Finally return any matching agent (BUSY first)
                agents.sort(key=lambda a: 0 if a.status == "busy" else 1)
                return agents[0]

        # 2. FALLBACK: cross-session lookup by role="leader"
        # Covers DB migration, API restart where session_id doesn't match
        all_leaders = await self.repo.find_agents_by_role("leader")
        if not all_leaders:
            return None

        # Prefer Leader with an active team
        chosen = None
        for leader in all_leaders:
            team = await self.repo.find_active_team_by_leader(leader.id)
            if team:
                chosen = leader
                break

        if not chosen:
            chosen = all_leaders[0]

        # Self-heal: bind current session_id so subsequent lookups can use the fast path
        if session_id and chosen.session_id != session_id:
            await self.repo.update_agent(chosen.id, session_id=session_id)
            logger.info(
                "Leader self-heal: '%s' session bound to %s",
                chosen.name,
                session_id[:8],
            )

        return chosen

    async def _self_heal_agent(self, agent, trigger: str = "self_heal") -> None:
        """Self-heal: WAITING agent receives tool event -> correct to BUSY."""
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
        logger.info("Self-heal: %s WAITING->BUSY (trigger=%s)", agent.name, trigger)

    @staticmethod
    def _extract_file_path(tool_input: dict | str) -> str:
        """Extract file path from tool input."""
        if isinstance(tool_input, dict):
            return tool_input.get("file_path", "") or tool_input.get("path", "")
        return ""

    def _extract_input_summary(self, tool_name: str, tool_input: dict | str) -> str:
        """Extract summary from tool input — file edit tools prioritize storing file_path."""
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
        self,
        tool_name: str,
        tool_input: dict | str,
        target_agent_id: str,
        target_agent_name: str,
        session_id: str,
    ) -> None:
        """Detect file edit conflicts — O(1) query via in-memory tracker + DB fallback.

        Enhancements:
        1. In-memory tracker first: O(1) query, no DB scan needed
        2. Exact file_path matching: no longer relies on input_summary substring matching
        3. Conflict severity grading: 2 agents editing same file vs 3+ agents
        4. Records to tracker for hotspot statistics
        """
        if tool_name not in self._FILE_EDIT_TOOLS:
            return

        file_path = self._extract_file_path(tool_input)
        if not file_path:
            return

        # Periodically clean up expired records (piggyback on each detection, negligible overhead)
        self._file_tracker.cleanup()

        # Record this edit
        self._file_tracker.record(file_path, target_agent_id, target_agent_name)

        # Use in-memory tracker to find conflicts (O(1) lookup)
        conflicts = self._file_tracker.find_conflicts(
            file_path,
            target_agent_id,
            window_minutes=5,
        )

        if not conflicts:
            # In-memory tracker has no conflicts -> DB fallback (covers cold start after tracker restart)
            conflicts = await self._db_fallback_conflict_check(
                file_path,
                target_agent_id,
                session_id,
            )

        if not conflicts:
            return

        # Dedup: report each agent only once
        seen_agents: dict[str, _FileEditRecord] = {}
        for c in conflicts:
            if c.agent_id not in seen_agents:
                seen_agents[c.agent_id] = c

        # Conflict severity
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
            "File edit conflict[%s]: %s — %s (prior) vs %s (current)",
            severity,
            file_path,
            agent_names,
            target_agent_name,
        )

    async def _db_fallback_conflict_check(
        self,
        file_path: str,
        current_agent_id: str,
        session_id: str,
    ) -> list[_FileEditRecord]:
        """DB fallback conflict detection — when in-memory tracker has no data (cold start).

        Improved: directly matches file_path instead of substring matching input_summary.
        """
        session_agents = await self.repo.find_agents_by_session(session_id)
        other_busy = [a for a in session_agents if a.id != current_agent_id and a.status == "busy"]
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
                # Improved: exact file_path matching (normalized path separators)
                act_summary = (act.input_summary or "").replace("\\", "/")
                normalized_path = file_path.replace("\\", "/")
                if normalized_path == act_summary or normalized_path in act_summary:
                    record = _FileEditRecord(
                        agent_id=other.id,
                        agent_name=other.name,
                        timestamp=act.timestamp,
                    )
                    conflicts.append(record)
                    # Also populate in-memory tracker
                    self._file_tracker.record(
                        file_path,
                        other.id,
                        other.name,
                    )
                    break  # Only take most recent per agent
        return conflicts

    def get_file_hotspots(self, window_minutes: int = 10) -> list[dict]:
        """Get hotspot file info — used by team_briefing.

        Returns:
            List of files edited by multiple agents, with agents and edit_count.
        """
        self._file_tracker.cleanup()
        return self._file_tracker.get_hotspots(window_minutes=window_minutes)

    def get_agent_editing_files(self, agent_id: str) -> list[str]:
        """Get files recently being edited by an agent — used when registering agents."""
        return self._file_tracker.get_agent_files(agent_id)

    async def _resolve_agent(
        self,
        cc_agent_id: str,
        agent_name: str,
        session_id: str,
    ) -> object | None:
        """Resolve which agent a tool call belongs to — supports cc_id exact match + name fallback.

        CC team agents have a race condition: SubagentStart may fire before MCP registration,
        leaving cc_tool_use_id unbound. This method falls back to name matching within team
        when cc_id lookup fails, and binds cc_tool_use_id (late binding) to fix all subsequent lookups.
        """
        # 1. Priority: exact match via cc_tool_use_id
        if cc_agent_id:
            agent = await self.repo.find_agent_by_cc_id(cc_agent_id)
            if agent:
                return agent

        # 2. Fallback: cc_agent_id exists but unbound (race condition), find by name within team
        if cc_agent_id and agent_name:
            leader = await self._find_leader(session_id)
            if leader:
                team = await self.repo.find_active_team_by_leader(leader.id)
                if team:
                    team_agents = await self.repo.list_agents(team.id)
                    matches = [a for a in team_agents if a.name == agent_name and a.id != leader.id]
                    if matches:
                        agent = matches[0]
                        # Late binding: bind cc_tool_use_id to fix all subsequent lookups
                        await self.repo.update_agent(
                            agent.id,
                            cc_tool_use_id=cc_agent_id,
                            session_id=session_id,
                        )
                        logger.info(
                            "Late binding: agent '%s' bound cc_id=%s",
                            agent_name,
                            cc_agent_id[:8],
                        )
                        return agent

        # 3. No agent_id -> main session tool call (Leader)
        if not cc_agent_id:
            return await self._find_leader(session_id)

        return None

    async def _on_pre_tool_use(self, payload: dict) -> dict:
        """Record tool use event.

        CC PreToolUse payload:
        - agent_id/agent_type: present when from a sub-agent
        - tool_name, tool_input: tool information
        - tool_input.description: tool call description
        """
        tool_name = payload.get("tool_name", "unknown")
        session_id = payload.get("session_id", "")
        cc_agent_id = payload.get("agent_id", "")
        agent_name = payload.get("agent_type", "")
        tool_input = payload.get("tool_input", {})

        input_summary = self._extract_input_summary(tool_name, tool_input)

        # Step 4 — pre-register the planned roster when a Workflow is launched.
        # The script is in tool_input.script (confirmed); parse meta.phases (declarative,
        # 100% knowable) + literal agent() calls so the run's skeleton is visible BEFORE
        # any agent starts working (esp. serial later phases). Dynamic fan-out size is runtime.
        if tool_name == "Workflow" and isinstance(tool_input, dict):
            try:
                plan = self._parse_workflow_plan(str(tool_input.get("script", "")))
                # I3a: 暂存计划供 PostToolUse 回执补齐（wf_id 此刻还不可见）。
                if session_id:
                    self._workflow_plans[session_id] = plan
                await self.event_bus.emit(
                    "workflow.planned",
                    f"session:{session_id}",
                    {
                        "session_id": session_id,
                        "name": plan["name"],
                        "phases": plan["phases"],
                        "literal_agent_count": plan["literal_agent_count"],
                        "dynamic_nodes": plan["dynamic_nodes"],
                    },
                )
            except Exception:  # noqa: BLE001 — pre-registration must never block the call
                pass

        # Resolve which agent this tool call belongs to (supports cc_id exact match + name fallback)
        target_agent = await self._resolve_agent(cc_agent_id, agent_name, session_id)

        if target_agent:
            # Self-heal: IDLE agent receives tool event -> correct to BUSY
            await self._self_heal_agent(target_agent)

            # Update last active time + heal missing project binding.
            # Project binding used to happen only at SessionStart; a Leader created
            # before its project was registered (or before cwd resolved) stayed
            # project_id=None forever, so project liveness ("工作中") never saw it
            # despite constant activity. Heal here so any tool call repairs it.
            update_fields: dict = {"last_active_at": datetime.now()}
            if (
                getattr(target_agent, "role", None) == "leader"
                and not getattr(target_agent, "project_id", None)
            ):
                healed_pid = await self._resolve_project_id_by_cwd(payload.get("cwd", ""))
                if healed_pid:
                    update_fields["project_id"] = healed_pid
            await self.repo.update_agent(target_agent.id, **update_fields)

            # Strict 1:1 — a workflow subagent's own tool call may carry agent_transcript_path
            # with the wf_id; promote it out of the session-fallback team as early as possible.
            await self._promote_workflow_team(target_agent, payload)

            start_time = datetime.now()
            activity = await self.repo.create_activity(
                agent_id=target_agent.id,
                session_id=session_id,
                tool_name=tool_name,
                input_summary=input_summary,
                status="running",
            )
            # Record pending span for PostToolUse correlation
            span_key = f"{target_agent.id}:{session_id}:{tool_name}"
            self._pending_spans[span_key] = (activity.id, start_time)
            # current_task is set by Leader via API, hook does not auto-override

            # Intent event: only emit for substantive tools and when throttle threshold exceeded
            if tool_name in self._INTENT_TOOLS:
                last_emit = self._intent_last_emit.get(target_agent.id)
                elapsed = (start_time - last_emit).total_seconds() if last_emit else float("inf")
                if elapsed >= self._INTENT_THROTTLE_SECS:
                    self._intent_last_emit[target_agent.id] = start_time
                    await self.event_bus.emit(
                        "intent.agent_working",
                        f"agent:{target_agent.id}",
                        {
                            "agent_id": target_agent.id,
                            "agent_name": target_agent.name,
                            "tool_name": tool_name,
                            "intent_summary": f"正在使用 {tool_name}",
                            "input_preview": input_summary[:100],
                        },
                    )

            # File edit conflict detection (only records events, does not block operations)
            try:
                await self._check_file_edit_conflict(
                    tool_name,
                    tool_input,
                    target_agent.id,
                    target_agent.name,
                    session_id,
                )
            except Exception as exc:
                logger.warning("Conflict detection error (does not affect tool use): %s", exc)

        # Decision event: meeting created (meeting_start tool call)
        if tool_name == "meeting_start" and isinstance(tool_input, dict):
            await self.event_bus.emit(
                "decision.meeting_started",
                f"session:{session_id}",
                {
                    "agent_name": payload.get("agent_type", ""),
                    "topic": tool_input.get("topic", ""),
                    "participants": tool_input.get("participants", []),
                    "rationale": tool_input.get("purpose", "")[:200],
                    "alternatives": [],
                    "outcome": "pending",
                    "session_id": session_id,
                },
            )

        # Decision event: task assigned (task_run tool call)
        if tool_name == "task_run" and isinstance(tool_input, dict):
            await self.event_bus.emit(
                "decision.task_assigned",
                f"session:{session_id}",
                {
                    "agent_name": payload.get("agent_type", ""),
                    "task_title": tool_input.get("title", tool_input.get("task", "")),
                    "assigned_to": tool_input.get("agent_name", tool_input.get("assigned_to", "")),
                    "rationale": tool_input.get("description", "")[:200],
                    "alternatives": [],
                    "outcome": "pending",
                    "session_id": session_id,
                },
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
        """Record tool completion event, including output summary.

        CC PostToolUse payload additionally contains:
        - tool_response: {stdout, stderr} or other tool output
        """
        tool_name = payload.get("tool_name", "unknown")
        session_id = payload.get("session_id", "")
        cc_agent_id = payload.get("agent_id", "")
        tool_input = payload.get("tool_input", {})
        tool_response = payload.get("tool_response", {})

        input_summary = self._extract_input_summary(tool_name, tool_input)

        # Extract output summary
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

        # I3a: Workflow 启动回执 → run 骨架(running) + workflow.started（关联锚点，非完成态）。
        if tool_name == "Workflow":
            try:
                await self._ingest_workflow_receipt(payload, session_id, tool_response)
            except Exception:  # noqa: BLE001 — 观测摄取绝不阻塞 hook 返回
                logger.warning("workflow receipt ingest failed", exc_info=True)

        # Resolve which agent this tool call belongs to (supports cc_id exact match + name fallback)
        agent_name = payload.get("agent_type", "")
        target_agent = await self._resolve_agent(cc_agent_id, agent_name, session_id)

        if target_agent:
            # Self-heal: IDLE agent receives tool completion event -> correct to BUSY
            await self._self_heal_agent(target_agent, trigger="self_heal_post")

            # Update last active time
            now = datetime.now()
            await self.repo.update_agent(target_agent.id, last_active_at=now)

            # Try to correlate with the running activity created by PreToolUse
            span_key = f"{target_agent.id}:{session_id}:{tool_name}"
            pending = self._pending_spans.pop(span_key, None)

            if pending:
                activity_id, start_time = pending
                duration_ms = int((now - start_time).total_seconds() * 1000)
                await self.repo.update_activity(
                    activity_id,
                    status="completed",
                    output_summary=output_summary,
                    duration_ms=duration_ms,
                )
            else:
                # Backward compat: no pending span found, create new completed record
                await self.repo.create_activity(
                    agent_id=target_agent.id,
                    session_id=session_id,
                    tool_name=tool_name,
                    input_summary=input_summary,
                    output_summary=output_summary,
                    status="completed",
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

    async def _ingest_workflow_receipt(
        self, payload: dict, session_id: str, tool_response: object
    ) -> None:
        """PostToolUse(Workflow) 回执 → run 骨架(running) + workflow.started + best-effort 文件 ingest.

        回执只当「启动回执/关联锚点」不当完成态：实测回执约 7s 返回而 run≈56min，
        此刻 wf_<id>.json 多半未落地，故只建 running 骨架，完成态靠 reaper/对账补。
        一次拿齐 wf_id+cc_task_id+script_path+name，根治 wf_id 在 SubagentStart 不可见。
        """
        text = tool_response if isinstance(tool_response, str) else ""
        if not text and isinstance(tool_response, dict):
            text = (
                tool_response.get("stdout")
                or tool_response.get("content")
                or str(tool_response)
            )
        receipt = workflow_ingest.parse_workflow_receipt(text or "")
        wf_id = receipt.get("wf_id")
        if not wf_id:
            return

        plan = self._workflow_plans.get(session_id, {})

        # 关联既有 workflow-<wf_id> 团队；没有就认养本会话的 session 兜底队——
        # 本 run 早到的 agent 都挂在那里，回执是第一个拿到 wf_id 的时点，就地补链，
        # 否则 live 全程 run.team_id 与 team.config.workflow_run_id 双向皆空
        #（2026-07-06 监控实录：完整 run 期间 team=NULL）。
        team = await self.repo.get_team_by_name(f"workflow-{wf_id}")
        if team is None and session_id:
            fallback = await self.repo.get_team_by_name(
                f"workflow-session-{session_id[:8]}"
            )
            fb_wf = (fallback.config or {}).get("workflow_run_id") if fallback else None
            if fallback is not None and fb_wf in (None, "", wf_id):
                team = fallback
        team_id = team.id if team else None
        project_id = (getattr(team, "project_id", None) or "") if team else ""
        if not project_id:
            # 跨项目修复C（严格归属）：只按发起会话 cwd → 已注册项目最长前缀解析，
            # 次选同会话 Leader；解析不到留空绝不猜。旧 _find_leader 的跨会话回退
            # 曾把未注册项目的 run 归到别的项目 Leader 名下（隔离违规实录）。
            project_id = (
                await self._resolve_project_id_by_cwd(payload.get("cwd") or "") or ""
            )
            if not project_id and session_id:
                try:
                    same = await self.repo.find_agents_by_session(session_id)
                except Exception:  # noqa: BLE001
                    same = []
                leaders = [a for a in same if a.role == "leader"]
                project_id = (leaders[0].project_id or "") if leaders else ""

        # phases：计划里是 title 字符串列表，归一为 [{index,title}]。
        phases = [
            {"index": i, "title": t}
            for i, t in enumerate(plan.get("phases", []) or [], start=1)
        ]

        run = WorkflowRun(
            wf_id=wf_id,
            project_id=project_id,
            team_id=team_id,
            session_id=session_id or None,
            cc_task_id=receipt.get("cc_task_id") or None,
            name=receipt.get("name") or plan.get("name", ""),
            status="running",
            source="hook",
            phases=phases,
            planned_agent_count=int(plan.get("literal_agent_count", 0) or 0),
            dynamic_nodes=int(plan.get("dynamic_nodes", 0) or 0),
            summary=receipt.get("summary") or "",
            script_path=receipt.get("script_path") or "",
            # 跨项目修复A：持久化回执 Transcript dir——此后 live/终态直接寻址，
            # 不再依赖项目注册（未注册项目的 run 曾误判 interrupted/live 全盲）。
            transcript_dir=receipt.get("transcript_dir") or "",
        )
        await self.repo.upsert_workflow_run(run)

        # link team.config.workflow_run_id → team_id（既有链建团队时已写，这里兜底）。
        if team is not None and (team.config or {}).get("workflow_run_id") != wf_id:
            cfg = dict(team.config or {})
            cfg["workflow_run_id"] = wf_id
            try:
                await self.repo.update_team(team.id, config=cfg)
            except Exception:  # noqa: BLE001
                pass

        await self.event_bus.emit(
            "workflow.started",
            f"workflow:{wf_id}",
            {
                "wf_id": wf_id,
                "name": run.name,
                "status": "running",
                "cc_task_id": run.cc_task_id,
                "script_path": run.script_path,
                "team_id": team_id,
                "project_id": project_id,
                "planned_agent_count": run.planned_agent_count,
                "dynamic_nodes": run.dynamic_nodes,
                "phases": run.phases,
                "source": "hook",
                "session_id": session_id,
            },
            entity_id=wf_id,
            entity_type="workflow",
        )

        # best-effort：回执返回时文件多半未落地 → no-op；万一已落地则直接完成入库。
        jp = workflow_ingest.run_json_path_from_transcript_dir(
            receipt.get("transcript_dir", ""), wf_id
        )
        if jp is not None and jp.exists():
            try:
                await workflow_ingest.ingest_run_from_file(self.repo, self.event_bus, jp)
            except Exception:  # noqa: BLE001
                logger.warning("workflow receipt best-effort ingest failed", exc_info=True)

    async def _resolve_project_id_by_cwd(self, cwd: str) -> str | None:
        """Resolve project_id from a cwd via longest root_path prefix match.

        Several projects can prefix-match the same cwd (e.g. C:/Users/TUF vs
        C:/Users/TUF/Desktop/<proj>); the most specific (longest) wins. Returns
        None when cwd is empty or matches no registered project.
        """
        cwd_norm = (cwd or "").replace("\\", "/").rstrip("/").lower()
        if not cwd_norm:
            return None
        best_id: str | None = None
        best_len = -1
        for proj in await self.repo.list_projects():
            rp = (proj.root_path or "").replace("\\", "/").rstrip("/").lower()
            if rp and (cwd_norm == rp or cwd_norm.startswith(rp + "/")) and len(rp) > best_len:
                best_id = proj.id
                best_len = len(rp)
        return best_id

    async def _on_session_start(self, payload: dict) -> dict:
        """Record CC session start.

        Leader = the CC session opened by the user. Each session corresponds to one Leader.
        Flow:
        1. Find project by cwd
        2. Look for existing Leader in project (role=leader + project_id match)
        3. Found -> reuse, update session_id + status=busy
        4. Not found -> create new Leader
        No longer creates session-xxx ghost agents each time.
        """
        session_id = payload.get("session_id", "")
        cwd = payload.get("cwd", "")
        leader = None

        # 1. Find project by cwd — longest root_path match (see helper).
        project = None
        matched_pid = await self._resolve_project_id_by_cwd(cwd)
        if matched_pid:
            project = await self.repo.get_project(matched_pid)

        # 2. Check if this session already has a Leader
        existing = await self.repo.find_agents_by_session(session_id)
        leaders_in_session = [a for a in existing if a.role == "leader"]

        if leaders_in_session:
            # Reuse existing session Leader
            leader = leaders_in_session[0]
            update_kwargs: dict = {
                "status": "busy",
                "last_active_at": datetime.now(),
            }
            # Heal project binding — project liveness (summary "工作中") keys off
            # leader.project_id. Session leaders were observed unbound (7 orphan
            # rows) or bound to the wrong parent project (first-match bug above);
            # a session has exactly one cwd, so the resolved project is authoritative.
            if project and leader.project_id != project.id:
                update_kwargs["project_id"] = project.id
            await self.repo.update_agent(leader.id, **update_kwargs)
        elif project:
            # 3. Find existing Leader in project (may be an old Leader with empty session_id)
            project_leader = await self.repo.find_leader_by_project(project.id)
            if project_leader:
                # Reuse project Leader, bind new session
                leader = project_leader
                await self.repo.update_agent(
                    leader.id,
                    session_id=session_id,
                    status="busy",
                    last_active_at=datetime.now(),
                )
                logger.info(
                    "SessionStart: reusing project Leader %s (session=%s)",
                    leader.name,
                    session_id[:8],
                )
            else:
                # 4. Project has no Leader -> create one
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
                        # 主会话模型 hook 事件不携带——留空由下方 transcript 尾读回填，
                        # 不落仓库默认值（曾恒显 claude-opus-4-7 误导展示）。
                        model="",
                    )
                    await self.repo.update_agent(
                        leader.id,
                        status="busy",
                        last_active_at=datetime.now(),
                    )
                    logger.info("SessionStart: created project Leader -> team %s", team.name)
        else:
            # No project match -> do NOT auto-create. Log for user prompt.
            logger.info(
                "SessionStart: no project match for cwd=%s. "
                "User can register via project_create MCP tool or Dashboard.",
                cwd,
            )

        # 主会话真实模型回填：hook 事件不带主会话模型，但 SessionStart 携带
        # transcript_path，尾读最后一条 assistant 的 message.model 写入
        # Leader.model（与 workflow_agents 的终态模型回填同思路）。
        try:
            _model = self._read_session_model(payload.get("transcript_path") or "")
            if _model and leader is not None and getattr(leader, "model", "") != _model:
                await self.repo.update_agent(leader.id, model=_model)
        except Exception:  # noqa: BLE001 — leader 未定分支/读文件失败均静默
            pass

        # I3a: 耐久兜底 — 会话启动时对账扫全 session workflows/，补 DB 缺失/未完成的 run。
        # 全 try/except 不阻塞会话启动（OS 离线期发生的运行上线后能全量补回）。
        try:
            await workflow_ingest.reconcile(
                self.repo, self.event_bus, project_dir=cwd or None
            )
        except Exception:  # noqa: BLE001
            logger.warning("SessionStart workflow reconcile failed", exc_info=True)

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
        """Handle CC session end — reconcile and clean up state."""
        session_id = payload.get("session_id", "")
        # Reconcile: set all agents in this session to OFFLINE and clear session_id
        agents = await self.repo.find_agents_by_session(session_id)
        for agent in agents:
            updates: dict = {"session_id": None, "status": "offline", "current_task": None}
            await self.repo.update_agent(agent.id, **updates)

        # Reconciliation stats
        hook_count = await self.repo.count_agents_by_source(
            source="hook",
            session_id=session_id,
        )
        api_count = await self.repo.count_agents_by_source(
            source="api",
            session_id=session_id,
        )

        # Close all active teams (session end = entire work session ended)
        closed_teams = []
        all_teams = await self.repo.list_teams()
        for team in all_teams:
            if team.status == "active":
                await self.repo.update_team(team.id, status="completed")
                closed_teams.append(team.name)
                logger.info("SessionEnd: closed team '%s'", team.name)
        # Set all non-offline agents to offline
        for team in all_teams:
            team_agents = await self.repo.list_agents(team.id)
            for agent in team_agents:
                if agent.status != "offline":
                    await self.repo.update_agent(agent.id, status="offline", current_task=None)

        await self.event_bus.emit(
            "cc.session_end",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "agents_hook": hook_count,
                "agents_api": api_count,
                "sync_warning": hook_count > api_count,
                "closed_teams": closed_teams,
            },
        )
        return {
            "status": "reconciled",
            "hook_agents": hook_count,
            "api_agents": api_count,
            "closed_teams": closed_teams,
        }

    async def _on_stop(self, payload: dict) -> dict:
        """Handle CC Stop event — distinguish between agent idle and actual exit.

        Mode 1 (session match): agent completed a turn -> waiting + update last_active_at
            Sub-agent PreToolUse/PostToolUse hooks don't fire (CC limitation),
            so SubagentStop is the only activity signal from sub-agents.
        Mode 2 (global fallback): entire session ended, no matching agent -> offline
        """
        session_id = payload.get("session_id", "")
        updated: list[str] = []

        # Mode 1: find by session_id -> only update last_active_at, don't change status
        # State changes are handled by StateReaper's config_liveness detection
        recent_cutoff = datetime.now() - timedelta(seconds=30)
        agents = await self.repo.find_agents_by_session(session_id)
        for agent in agents:
            if agent.status == "busy" and agent.source == "hook":
                if agent.created_at and agent.created_at > recent_cutoff:
                    continue  # Recently created agent, skip to prevent old Stop from overriding
                await self.repo.update_agent(
                    agent.id,
                    last_active_at=datetime.now(),
                )
                updated.append(agent.id)

        # Leader 模型实时追踪：用户可随时 /model 切换，切换后下一条 assistant 消息
        # 即新模型——每轮 Stop 尾读 transcript 刷新一次即为实时语义（SessionStart
        # 一次性回填不够，用户 2026-07-07 需求）。变更才写库，稳态零写放大。
        try:
            _model = self._read_session_model(payload.get("transcript_path") or "")
            if _model:
                for agent in agents:
                    if agent.role == "leader" and getattr(agent, "model", "") != _model:
                        await self.repo.update_agent(agent.id, model=_model)
        except Exception:  # noqa: BLE001 — 读文件失败静默，不影响 Stop 主流程
            pass

        # Mode 2: global fallback — only triggers when no session match (actual session end)
        if not updated:
            recent_cutoff = datetime.now() - timedelta(seconds=30)
            cutoff = datetime.now() - timedelta(minutes=10)
            teams = await self.repo.list_teams()
            for team in teams:
                all_agents = await self.repo.list_agents(team.id)
                for agent in all_agents:
                    if agent.status == "busy" and agent.source == "hook":
                        # Skip agents created in last 30 seconds (prevent old Stop from overriding new agent)
                        if agent.created_at and agent.created_at > recent_cutoff:
                            continue
                        # Only clean up recently active or never-active agents
                        if agent.last_active_at and agent.last_active_at < cutoff:
                            continue  # Outside time window, skip (may belong to another session)
                        await self.repo.update_agent(
                            agent.id,
                            status="offline",
                            current_task=None,
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

        # Distinguish heartbeat updates from offline settings
        session_agents = (
            {a.id for a in agents if a.status == "busy" and a.source == "hook"} if agents else set()
        )
        heartbeat_ids = [aid for aid in updated if aid in session_agents]
        offline_ids = [aid for aid in updated if aid not in session_agents]
        logger.info(
            "Stop event: %d heartbeat updates, %d agents set offline",
            len(heartbeat_ids),
            len(offline_ids),
        )
        return {"status": "ok", "heartbeat_updates": heartbeat_ids, "agents_offline": offline_ids}

    async def _find_or_create_session_team(
        self,
        session_id: str,
        payload: dict,
    ):
        """Find team associated with session.

        Strategy:
        1. Find the team where leader is (session_id match)
        2. Return most recently created team (fallback)
        3. Auto-create new team (when no teams exist)
        """
        # Strategy 1: find the team containing the leader
        if session_id:
            agents = await self.repo.find_agents_by_session(session_id)
            if agents:
                # Prefer leader-role agent; fallback to first
                leader_agents = [a for a in agents if a.role == "leader"]
                target = leader_agents[0] if leader_agents else agents[0]
                return await self.repo.get_team(target.team_id)

        # Strategy 2: match project by cwd, find associated team
        cwd = payload.get("cwd", "")
        teams = await self.repo.list_teams()
        if teams and cwd:
            # 2a: try to find team belonging to the project matched by cwd
            projects = await self.repo.list_projects()
            for proj in projects:
                if proj.root_path and cwd.replace("\\", "/").startswith(
                    proj.root_path.replace("\\", "/")
                ):
                    proj_teams = [t for t in teams if t.project_id == proj.id]
                    if proj_teams:
                        # Prefer active teams; fallback to most recently created
                        active_proj_teams = [t for t in proj_teams if t.status == "active"]
                        target_team = active_proj_teams[0] if active_proj_teams else proj_teams[-1]
                        return target_team
            # 2b: no project match, safe to return if only one team
            if len(teams) == 1:
                return teams[0]
            # 2c: multiple teams can't determine, return most recently created (log warning)
            logger.warning(
                "Cannot determine team affiliation (cwd=%s, teams=%d), falling back to most recently created team",
                cwd,
                len(teams),
            )
            teams_sorted = sorted(teams, key=lambda t: t.created_at or "", reverse=True)
            return teams_sorted[0] if teams_sorted else None
        if teams:
            teams_sorted = sorted(teams, key=lambda t: t.created_at or "", reverse=True)
            return teams_sorted[0]

        # Strategy 3: create new team
        cwd = payload.get("cwd", "")
        team = await self.repo.create_team(
            name=f"session-{session_id[:8]}",
            mode="coordinate",
        )
        logger.info("Auto-created team: %s (session=%s, cwd=%s)", team.name, session_id[:8], cwd)
        return team
