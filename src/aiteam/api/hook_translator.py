"""AI Team OS — Hook event translator.

Translates Claude Code hook events into OS system operations,
bridging automatic sync between CC sessions and the OS.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from aiteam.api import agent_context, leader_usage, workflow_ingest
from aiteam.api.always_load import normalize_tool_name
from aiteam.api.event_bus import EventBus
from aiteam.clock import utc_now
from aiteam.services import token_attribution, transcript_path
from aiteam.services.agent_identity import pick_reusable_row
from aiteam.storage.repository import StorageRepository
from aiteam.types import EventType, TokenSource, WorkflowRun

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
# Bounded to a single optional dash-suffix (not unbounded `*`): an unbounded group greedily
# swallows the worktree instance suffix CC appends for parallel branches of one run, e.g.
# ".claude/worktrees/wf_a69e7d46-a66-1" would over-match to "wf_a69e7d46-a66-1" instead of
# the true run id "wf_a69e7d46-a66", causing team lookups to miss the real wf_<id>.json
# snapshot and spawn an orphan team (2026-07 task f8207497). Matches link_extract._WF_RE's
# bounded convention.
_WF_RUN_ID_RE = re.compile(r"wf_[0-9a-z]+(?:-[0-9a-z]+)?", re.IGNORECASE)

# Workflow 脚本的「非代码面」：行/块注释 + 三种字符串字面量。静态数 agent( 与动态节点
# 前先剥掉它，否则 prompt 正文里写的 "agent(" / ".map(" 会被算进计划数——实测 178 个
# 真实脚本里 33 个（18.5%）被注释或 prompt 文本抬高过计数。
# 交替顺序即优先级：注释先于字符串（注释里的撇号不会起串），单/双引号不跨行（未闭合
# 引号最多毁一行），模板串与块注释才跨行。计数是启发式，剥离失误只会让下限更保守。
# phases/name 仍从原文取——它们本就住在字符串里。
_JS_NOISE_RE = re.compile(
    r"//[^\n]*"  # line comment
    r"|/\*.*?\*/"  # block comment
    r"|`(?:\\.|[^`\\])*`"  # template literal
    r"|'(?:\\.|[^'\\\n])*'"  # single-quoted string
    r'|"(?:\\.|[^"\\\n])*"',  # double-quoted string
    re.S,
)

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
                timestamp=utc_now(),
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
        cutoff = utc_now() - timedelta(minutes=window_minutes)
        records = self._edits.get(file_path, [])
        return [r for r in records if r.agent_id != current_agent_id and r.timestamp >= cutoff]

    def get_hotspots(self, window_minutes: int = 10, min_agents: int = 2) -> list[dict]:
        """Get hotspot files — files edited by multiple agents within the time window.

        Returns:
            List of hotspot files, each containing file_path, agents, edit_count.
        """
        cutoff = utc_now() - timedelta(minutes=window_minutes)
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
        cutoff = utc_now() - timedelta(minutes=window_minutes)
        files = []
        for file_path, records in self._edits.items():
            if any(r.agent_id == agent_id and r.timestamp >= cutoff for r in records):
                files.append(file_path)
        return files

    def cleanup(self) -> int:
        """Clean up expired records, return count of removed records."""
        cutoff = utc_now() - self._window
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
        # Leader 活性触摸节流（每会话 60s 一次写库；工具事件每分钟可达数十次）
        self._leader_touch: dict[str, datetime] = {}
        # Intent throttle: key = agent_id, value = last_emit_time
        self._intent_last_emit: dict[str, datetime] = {}
        # Last known cwd from hook payload (for project matching)
        self._last_cwd: str = ""
        # I3a: PreToolUse(Workflow) 解析出的静态计划暂存（session_id -> plan），
        # 供 PostToolUse(Workflow) 回执补齐 name/phases/planned_agent_count。
        self._workflow_plans: dict[str, dict] = {}
        # Leader 主会话用量采集的节流器（token 归因 v1 阶段 4）。进程内状态，
        # 丢了不影响正确性——落库是覆写，重测幂等。
        self._usage_meter = leader_usage.SessionUsageMeter()

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
            "TeammateIdle": self._on_teammate_idle,
            "PostCompact": self._on_post_compact,
            "WorktreeCreate": self._on_worktree_event,
            "WorktreeRemove": self._on_worktree_event,
            "TaskCreated": self._on_cc_task_event,
            "TaskCompleted": self._on_cc_task_event,
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
                    last_active_at=utc_now(),
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
            team, _created = await self.repo.get_or_create_team(
                name=team_key,
                mode="coordinate",
                config={"kind": "workflow", "auto_created": True, "workflow_run_id": wf_id},
                project_id=project_id,
            )
            if _created:
                # race-loss（并发同名建队）时跳过 emit——winner 已发 team.created。
                await self.event_bus.emit(
                    "team.created",
                    f"team:{team.id}",
                    {"team_id": team.id, "name": team_key, "kind": "workflow"},
                )
        else:
            await self._reactivate_workflow_team(team)

        # 5. Register this internal agent as a distinct member (unique name per cc id).
        member_name = f"wf-{(cc_agent_id or session_id or 'anon')[:10]}"
        try:
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
        except IntegrityError:
            # 并发 create 竞态（本协程 vs reaper live-tail 收尸协程，审计 B1）：
            # cc_tool_use_id partial unique index 已被先到方占位。吃掉冲突、改走
            # update 既有行，避免同 cc_id 双成员行。约束非本条则 re-fetch 落空、上抛。
            existing = await self.repo.find_agent_by_cc_id(cc_agent_id)
            if existing is None:
                raise
            await self.repo.update_agent(
                existing.id,
                status="busy",
                session_id=session_id,
                project_id=project_id,
                last_active_at=utc_now(),
            )
            return {"status": "updated", "agent_id": existing.id, "kind": "workflow"}
        await self.repo.update_agent(
            new_agent.id,
            status="busy",
            project_id=project_id,
            last_active_at=utc_now(),
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

    async def _reactivate_workflow_team(self, team: object) -> None:
        """Re-open a closed workflow team that a new subagent just joined.

        A ``workflow-session-<sid8>`` shell is reachable by name forever, so a long
        lived session can register into one the reaper already closed (its previous
        runs having settled). Members landing in a ``completed`` team are invisible to
        every active-only view; the reaper's contradiction self-heal catches it within
        a tick, but joining is the exact moment we know the team is in use again.
        Best-effort: a failure here just falls back to that self-heal.
        """
        if team is None or not str(getattr(team, "status", "")).endswith("completed"):
            return
        try:
            await self.repo.update_team(team.id, status="active")
        except Exception:  # noqa: BLE001 — 复活失败由 reaper 的矛盾态自愈兜底
            logger.debug("workflow team reactivation failed team=%s", getattr(team, "id", "?"))

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
            # race-safe find-or-create（并发 regroup 到同一 per-run 队名会撞 UNIQUE）
            team, _ = await self.repo.get_or_create_team(
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

        literal_agent_count is therefore a LOWER BOUND, never a target: a run with
        dynamic_nodes > 0 legitimately registers more agents than planned. Consumers
        must present it as such (UI shows "≥N"); nothing ratchets it up afterwards.
        """
        phases = re.findall(r"title:\s*['\"]([^'\"]+)['\"]", script)
        code = _JS_NOISE_RE.sub(" ", script)  # 只在代码面上计数，见 _JS_NOISE_RE
        literal_agents = len(re.findall(r"\bagent\(", code))
        dynamic_nodes = len(re.findall(r"\.map\(|while\s*\(|\bpipeline\(", code))
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

        Levels 2 and 3 match on **name**, which is not an identity: two dispatches
        of the same agent name are two dispatches, each with its own transcript and
        therefore its own token ledger. Welding the second one onto the first row
        overwrites a ledger that cannot be rebuilt (A-06). So every same-name
        candidate goes through ``pick_reusable_row``, which only ever reuses a row
        that is the same dispatch or has never been a dispatch at all - anything
        else gets a new row.
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
                    existing = pick_reusable_row(matches, cc_agent_id)
            else:
                # No cc_team_name -> legacy compat: global lookup by session_id+name
                existing = pick_reusable_row(
                    await self.repo.find_agents_by_session_and_name(session_id, agent_name),
                    cc_agent_id,
                )

        # 3. Still no match -> find team via Leader, deduplicate by name within team
        if not existing and not team:
            leader = await self._find_leader(session_id)
            if leader:
                team = await self.repo.find_active_team_by_leader(leader.id)
            if team:
                team_agents = await self.repo.list_agents(team.id)
                matches = [a for a in team_agents if a.name == agent_name]
                existing = pick_reusable_row(matches, cc_agent_id)

        if existing:
            # Already registered -> update status, bind session and CC agent ID
            update_fields: dict = {
                "status": "busy",
                "session_id": session_id,
                "last_active_at": utc_now(),
            }
            # Never blank out an existing binding: cc_tool_use_id is this row's
            # dispatch identity, and an empty payload agent_id is "unknown", not
            # "none" - erasing it would strand the row's transcript and ledger.
            if cc_agent_id:
                update_fields["cc_tool_use_id"] = cc_agent_id
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

        if not team and session_id:
            # 自动收编（2026-07-22 缔造者裁定「全面放开+一律自动追踪」，任务 8705dac2）：
            # 直派 agent 无队可归不再 skip 脱管——find-or-create 本会话 session-<sid8>
            # 容器队收编。实锤：闻歌会话直派 agent 实际在跑而前端零显示（memo 8e8b162b）。
            team = await self._find_or_create_session_team(session_id, payload)
            if team:
                # Leader 归位（fleet-layer §3：Leader 之家=会话容器队）：被 workflow
                # 队认养未归还、或家队已 completed 时，随收编一并归位——否则
                # find_active_team_by_leader 持续解析到别人的/已关的队。
                if not leader:
                    leader = await self._find_leader(session_id)
                if leader and leader.team_id != team.id:
                    home = (
                        await self.repo.get_team(leader.team_id)
                        if leader.team_id else None
                    )
                    if (
                        home is None
                        or home.status == "completed"
                        or str(home.name or "").startswith("workflow-")
                    ):
                        await self.repo.update_agent(leader.id, team_id=team.id)
                        logger.info(
                            "SubagentStart: leader '%s' re-homed to session team %s",
                            leader.name, team.name,
                        )

        if not team:
            logger.info(
                "SubagentStart: agent '%s' not registered and no active team found, skipping",
                agent_name,
            )
            return {"status": "skipped", "reason": "no active team"}

        # Final name dedup before creation (race condition: MCP may have completed registration during lookup)
        team_agents = await self.repo.list_agents(team.id)
        late_match = [a for a in team_agents if a.name == agent_name]
        existing = pick_reusable_row(late_match, cc_agent_id)
        if existing:
            late_fields: dict = {
                "status": "busy",
                "session_id": session_id,
                "last_active_at": utc_now(),
            }
            if cc_agent_id:
                late_fields["cc_tool_use_id"] = cc_agent_id
            await self.repo.update_agent(existing.id, **late_fields)
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

        # 归属解析（2026-07-27 事故修复）：Leader 权威优先于 team.project_id。
        # 实录：session-80d0cc5e 容器队被启动回填绑成 Wenge，而同队 Leader 行是
        # AI Team OS——旧代码直接 `project_id=team.project_id`，于是 agent 行误归
        # 且 system_prompt 的 {project_path} 渲染出别人项目的路径（同一个根，非两处
        # bug）。解析不出就留空，绝不用跨会话共享的 cwd 全局态兜底。
        agent_project_id = await self._resolve_agent_project_id(team, session_id)

        # Auto-fill standardized prompt template
        project_path = ""
        if agent_project_id:
            project = await self.repo.get_project(agent_project_id)
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
            "project_id": agent_project_id,
            "last_active_at": utc_now(),
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
                updates: dict = {"last_active_at": utc_now()}
                # P1 context watermark ledger (batch 1B): the SubagentStop payload
                # carries agent_transcript_path (batch0 contract test section 5), so
                # tail-read the sub-agent's last assistant usage and record its exact
                # context watermark. Best-effort: never block the stop path.
                try:
                    tpath = payload.get("agent_transcript_path") or ""
                    if tpath:
                        measured = agent_context.measure(tpath)
                        if measured is not None:
                            updates.update(measured)
                            updates["transcript_path"] = tpath
                except Exception:  # noqa: BLE001 — watermark capture must not break stop
                    pass
                # 归因链的 session 一跳（token 归因 v1 阶段1）：session_id 该在
                # SubagentStart 就写上，但那一列历来被抹（SessionEnd 旧代码 +
                # 启动自愈），所以在 stop 这一刻按**文件真相**再定一次——路径
                # .../projects/<slug>/<session_id>/subagents/... 本身就编码着它。
                # 派生不出才退回 payload 的 session_id；两者都没有就留空不猜。
                _sid = transcript_path.derive_session_id(
                    payload.get("agent_transcript_path") or ""
                ) or session_id
                if _sid:
                    updates["session_id"] = _sid
                # 计费口径 token 归因（与上面的上下文水位是两回事）。同一份
                # transcript 顺带解析：按 requestId 分组取每组末条快照再累加，
                # 逐行裸加会严重虚高（流式的 output_tokens 是递增快照，不是增量）。
                # model 一并采下来——transcript 里是完整型号，这正是"由观测回填"。
                try:
                    tpath = payload.get("agent_transcript_path") or ""
                    if tpath:
                        usage = token_attribution.parse_transcript_usage(tpath)
                        if usage:
                            updates["input_tokens"] = usage["input_tokens"]
                            updates["output_tokens"] = usage["output_tokens"]
                            updates["cache_creation_tokens"] = usage["cache_creation_tokens"]
                            updates["cache_read_tokens"] = usage["cache_read_tokens"]
                            updates["tokens_measured_at"] = utc_now()
                            # 这一行的数是怎么来的必须随行持久化（阶段0 §2.6）：
                            # 走到这里的四层数一律是 transcript 逐行解析定真的，
                            # 与读侧别名兜底推出来的数不是一回事，事后要分得开。
                            updates["tokens_source"] = TokenSource.TRANSCRIPT.value
                            if usage.get("model"):
                                updates["model"] = usage["model"]
                except Exception:  # noqa: BLE001 — 记账绝不阻断 stop 路径
                    logger.debug("token attribution failed", exc_info=True)
                await self.repo.update_agent(agent.id, **updates)
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
                        last_active_at=utc_now(),
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
            # 项目补绑（2026-07-25 实锤：wenge 队 project_id 为空，前端项目视图筛不到，
            # 4 个在跑 agent 全盲）。旧逻辑只在建队时绑项目——建队那一刻若 Leader 尚未
            # 注册（或 owner 会话 id 与 CC 内部团队名不同源）就绑不上，且此后永无补绑
            # 时机。每次解析都补一次：空则补，已绑不动。
            if not getattr(existing_team, "project_id", None):
                # 补绑只认权威路径（session→Leader→project），**禁用 cwd 兜底**：
                # _current_event_cwd 是实例属性，多会话事件共用一个 translator 会互相
                # 覆盖——补绑发生在任意事件到达时，用 cwd 极易把 A 项目的队绑到 B 项目
                # （2026-07-26 实测：wenge 队被绑成 AI Team OS）。建队时才允许 cwd 兜底
                # （那一刻 cwd 与事件同源，且 2026-05-08 跨项目泄漏事故防线仍在）。
                pid = await self._infer_project_id(session_id, allow_cwd_fallback=False)
                if pid:
                    await self.repo.update_team(existing_team.id, project_id=pid)
                    existing_team.project_id = pid
                    logger.info(
                        "CC team mapping: back-filled project %s onto team '%s'",
                        pid, cc_team_name,
                    )
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

        # 2. Auto-create same-name OS team. Stamp owner_session_id so SessionEnd
        # only closes teams owned by the ending session (fleet-layer design §5,
        # fixes 7ae3b7cd: a bystander session's SessionEnd must not clobber it).
        new_team, _created = await self.repo.get_or_create_team(
            name=cc_team_name,
            mode="coordinate",
            config={"owner_session_id": session_id} if session_id else {},
        )
        if not _created:
            # 并发 find-or-create 竞态：另一事件已抢先建同名队（teams.name UNIQUE），
            # get_or_create_team 已吞 IntegrityError 重取既有行。winner 会自行完成
            # 项目链接与 team.created 事件——loser 直接返回既有行，避免 ASGI 500。
            return new_team
        logger.info(
            "CC team mapping: auto-created OS team '%s' (id=%s)",
            cc_team_name,
            new_team.id,
        )

        project_id = await self._infer_project_id(session_id)
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

    async def _touch_session_leader(self, session_id: str) -> None:
        """工具事件驱动的 Leader 活性：对话进行中 busy、last_active 跟进。

        曾因 5 分钟心跳在长回合中把正在对话的 Leader 打成 offline（用户实测
        "我们在对话它却显示关闭"）。工具事件流即活性真相源：每 60s 节流一次
        写库；status 非 busy 时立即复活。回合结束后无事件 → 心跳超时自然衰减，
        语义正确（busy=事件在流，offline=静默超时）。
        """
        if not session_id:
            return
        now = utc_now()
        prev = self._leader_touch.get(session_id)
        if prev is not None and (now - prev).total_seconds() < 60:
            return
        self._leader_touch[session_id] = now
        try:
            for a in await self.repo.find_agents_by_session(session_id):
                if a.role == "leader":
                    kwargs: dict = {"last_active_at": now}
                    if str(getattr(a, "status", "")).lower() != "busy":
                        kwargs["status"] = "busy"
                    await self.repo.update_agent(a.id, **kwargs)
        except Exception:  # noqa: BLE001 — 活性触摸绝不影响事件主流程
            pass

    @staticmethod
    def _read_session_model(path: str) -> str:
        """尾读主会话 transcript 的真实模型 — 实现统一在 session_probe。

        参数名刻意不叫 ``transcript_path``：那是本模块 import 进来的派生器模块名，
        同名参数会在函数体内把它遮住，是个只在有人往这里加一行代码时才会咬人的坑。
        """
        from aiteam.api import session_probe

        return session_probe.read_session_model(path)

    async def _find_leader(self, session_id: str) -> object | None:
        """Find the leader agent for the current session — strictly by session_id.

        session_id is the hard identity boundary (fleet-layer design §3): a leader
        row belongs to exactly one CC session and is never reused or rebound across
        sessions. The former cross-session fallback (global role="leader" lookup +
        self-heal session rebind) was the churn source behind 03fe7cae (leader rows
        shared across sessions, session_id/project_id rewritten back and forth,
        leader parasitizing a workflow team). It is removed: no match -> return None.
        Callers resolve project by cwd or skip; they never guess a leader from
        another session (aligns with the workflow side's "strict ownership, leave
        empty rather than guess").
        """
        if not session_id:
            return None
        agents = await self.repo.find_agents_by_session(session_id)
        if not agents:
            return None
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

    # Tools whose interesting field is neither description nor command, so the
    # generic fallback stringifies the whole input and truncates at 200 chars —
    # which for these is a wall of JSON that says nothing. Each entry names the
    # field that actually identifies the call.
    _SUMMARY_FIELDS: dict[str, tuple[str, ...]] = {
        "Skill": ("skill", "args"),
        "SendMessage": ("summary", "to"),
        "TaskCreate": ("subject", "description"),
        "TaskUpdate": ("subject", "status", "owner"),
        "TaskGet": ("taskId",),
        "TaskStop": ("taskId", "reason"),
        "AskUserQuestion": ("question",),
        "ExitPlanMode": ("plan",),
        "EnterWorktree": ("name", "path"),
        "ExitWorktree": ("name", "path"),
        "WebFetch": ("url", "prompt"),
        "WebSearch": ("query",),
        "ToolSearch": ("query",),
    }

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
            for field in self._SUMMARY_FIELDS.get(tool_name, ()):
                value = tool_input.get(field)
                if value:
                    return str(value)[:200]
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

        cutoff = utc_now() - timedelta(minutes=5)
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

        The name fallback is gated by the same dispatch-identity rule as registration
        (A-06): late binding **writes** cc_tool_use_id onto the row it picks, and the
        SubagentStop path then overwrites that row's token ledger - so binding onto a
        row that already carries an account, or that already belongs to another
        dispatch, destroys a ledger that cannot be rebuilt. When no candidate is
        eligible the call stays unresolved: losing one observation of an unregistered
        agent beats corrupting an existing one (same stance as TeammateIdle).
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
                    agent = pick_reusable_row(matches, cc_agent_id)
                    if agent is not None:
                        # Late binding: bind cc_tool_use_id to fix all subsequent lookups
                        try:
                            await self.repo.update_agent(
                                agent.id,
                                cc_tool_use_id=cc_agent_id,
                                session_id=session_id,
                            )
                        except IntegrityError:
                            # 迟绑定竞态：步骤 1 读空之后 cc_id 已被并发请求绑上
                            # 别行。UNIQUE 拦下的正是双绑毁账（A-06）；败者改认
                            # 胜者行——同一派工身份，观测不丢也不落错行。
                            winner = await self.repo.find_agent_by_cc_id(cc_agent_id)
                            if winner is not None:
                                logger.info(
                                    "Late binding lost race: '%s' cc_id=%s -> row %s",
                                    agent_name,
                                    cc_agent_id[:8],
                                    winner.id[:8],
                                )
                            return winner
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
            update_fields: dict = {"last_active_at": utc_now()}
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

            start_time = utc_now()
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

        # Decision events below match on the BARE tool name: CC sends the client-side
        # full name (mcp__ai-team-os__meeting_create), so the old bare literals could
        # never match — and the old `meeting_start` literal is not even a real tool
        # name (the tool is meeting_create), so that branch was doubly dead.
        # (2026-07-27 batch 3.)
        bare_tool = normalize_tool_name(tool_name)

        # 中止观测:CC 在中止侧不给任何 hook（不存在 TaskStop/TaskAborted 事件,
        # Esc 打断也无声）,唯一能看见"任务被停掉"的地方就是 TaskStop **工具调用**。
        # 实测中止是主路径——TaskStop 40 次且持续在用,TaskCreate 14 次且 7/8 后归零
        # （新版 CC 里后台 subagent 即 task,停 agent 走的就是它）。把它从五万条
        # cc.tool_use 里拎成一等事件,中止信号才查得到。
        if bare_tool == "TaskStop" and isinstance(tool_input, dict):
            await self.event_bus.emit(
                EventType.CC_TASK_STOPPED.value,
                f"session:{session_id}",
                {
                    "session_id": session_id,
                    "cc_task_id": str(tool_input.get("taskId") or ""),
                    "reason": str(tool_input.get("reason") or ""),
                    "agent_name": payload.get("agent_type", ""),
                },
            )

        # Decision event: meeting created (meeting_create tool call)
        if bare_tool == "meeting_create" and isinstance(tool_input, dict):
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
        if bare_tool == "task_run" and isinstance(tool_input, dict):
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

        # Leader 活性：工具事件在流 = 对话进行中（60s 节流，见 _touch_session_leader）
        await self._touch_session_leader(session_id)

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
            now = utc_now()
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
        await self._record_notable_call(payload, session_id, tool_name, tool_response)
        return {"status": "recorded"}

    # SendMessage 的正文上限。队友之间的消息可以很长（派工书动辄数千字），完整
    # 存有价值但不能无界——超出部分截断，真实长度另记 message_chars。
    _MESSAGE_BODY_CAP = 4000

    async def _record_notable_call(
        self, payload: dict, session_id: str, tool_name: str, tool_response: object
    ) -> None:
        """三类工具调用值得从 cc.tool_complete 的洪流里单拎出来完整留档。

        - **ExitPlanMode**：``tool_input.plan`` 是 Leader 已经想清楚、正要请示
          用户的整套方案。它此前只以截断 200 字的 input_summary 存在于活动流里，
          方案正文当场丢失。
        - **AskUserQuestion**：人审裁决的原始现场——问了什么、用户选了哪个。
          30 天 27 次，条条是后来所有工作的依据，此前同样只剩一句截断的摘要。
        - **SendMessage**：CC 原生队友私信，30 天 279 次。邮箱由 CC 自管，
          OS 的定位是**只读镜像**——所以镜到事件流，**不写进
          channel_messages**：那张表是 OS 自有广播频道（已规划未启用），
          与队友私信语义不同，合流会把两条通道的边界糊掉且不可逆。
          收件人只在这里有记录，cc.tool_use 只存工具名与一句摘要。

        三者都低频，正文值得完整留下（对比：心跳一天上万条，本批刚停掉）。
        失败一律静默——记账不能反过来阻塞工具返回。
        """
        if tool_name not in ("ExitPlanMode", "AskUserQuestion", "SendMessage"):
            return
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return
        try:
            if tool_name == "ExitPlanMode":
                plan = str(tool_input.get("plan") or "")
                if not plan:
                    return
                await self.event_bus.emit(
                    "decision.plan_presented",
                    f"session:{session_id}",
                    {
                        "session_id": session_id,
                        "agent_name": payload.get("agent_type", ""),
                        "plan": plan,
                        "plan_chars": len(plan),
                    },
                )
                return
            if tool_name == "SendMessage":
                body = tool_input.get("message")
                body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                await self.event_bus.emit(
                    "cc.message_sent",
                    f"session:{session_id}",
                    {
                        "session_id": session_id,
                        "sender": payload.get("agent_type", ""),
                        "recipient": str(tool_input.get("to") or ""),
                        "summary": str(tool_input.get("summary") or "")[:200],
                        "message": body[: self._MESSAGE_BODY_CAP],
                        "message_chars": len(body),
                    },
                )
                return
            # AskUserQuestion 的真实载荷是**复数 questions 数组**（每项 question /
            # header / options），此前只读单数 tool_input["question"]，于是生产 6 条
            # 人审裁决记录 question/options 全空、真内容全塞在 answer JSON 里。
            # 单数形态保留兜底。
            questions = tool_input.get("questions")
            headers: list[str] = []
            if isinstance(questions, list) and questions:
                texts: list[str] = []
                options: list = []
                for item in questions:
                    if not isinstance(item, dict):
                        continue
                    texts.append(str(item.get("question") or ""))
                    if item.get("header"):
                        headers.append(str(item["header"]))
                    item_options = item.get("options")
                    if isinstance(item_options, list):
                        options.extend(item_options)
                question = "\n".join(t for t in texts if t)
                question_count = len(texts)
            else:
                question = str(tool_input.get("question") or "")
                options = tool_input.get("options") or []
                question_count = 1 if question else 0
            await self.event_bus.emit(
                "decision.user_asked",
                f"session:{session_id}",
                {
                    "session_id": session_id,
                    "agent_name": payload.get("agent_type", ""),
                    "question": question,
                    "question_count": question_count,
                    "headers": headers,
                    "options": options,
                    "answer": tool_response if isinstance(tool_response, (str, dict, list)) else "",
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("decision moment capture failed for %s", tool_name, exc_info=True)

    async def _on_worktree_event(self, payload: dict) -> dict:
        """WorktreeCreate / WorktreeRemove — 隔离工作区的出生与消失。

        本仓的多会话并行纪律要求第二个及之后的会话用 git worktree 隔离，而 OS
        此前对 worktree 的出现和消失完全无感。两个载荷字段**不对称**（已核 CC
        二进制）：WorktreeCreate 只给 ``name``，WorktreeRemove 只给
        ``worktree_path``，所以两边各记各的，不假装能配对。
        """
        event_name = payload.get("hook_event_name", "")
        created = event_name == "WorktreeCreate"
        session_id = payload.get("session_id", "")
        await self.event_bus.emit(
            "cc.worktree_created" if created else "cc.worktree_removed",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "cwd": payload.get("cwd", ""),
                "name": payload.get("name", ""),
                "worktree_path": payload.get("worktree_path", ""),
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
        fallback = None
        if team is None and session_id:
            fallback = await self.repo.get_team_by_name(
                f"workflow-session-{session_id[:8]}"
            )
            fb_wf = (fallback.config or {}).get("workflow_run_id") if fallback else None
            if fallback is not None and fb_wf in (None, "", wf_id):
                team = fallback
                fallback = None  # 已认养整队，无需再迁成员
                await self._reactivate_workflow_team(team)
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

        # 回执建队（2026-07-07 D1 实录）：per-run 队原本只在 SubagentStop 的
        # promote 时刻懒创建——被 kill 在 turn 中途 / 长 turn 未结束的 run 永远
        # 无队，项目页全程隐形。回执是每条 run 最早可靠拿到 wf_id 的时点（~7s），
        # 就地建队并把兜底队里本会话仍活跃的 workflow-subagent 迁入；后续
        # SubagentStop 的 _promote_workflow_team 只需迁移，不再承担建队职责。
        if team is None:
            team, _created = await self.repo.get_or_create_team(
                name=f"workflow-{wf_id}",
                mode="coordinate",
                config={"kind": "workflow", "auto_created": True, "workflow_run_id": wf_id},
                project_id=project_id or None,
            )
            team_id = team.id
            # race-loss（并发已建同名 per-run 队）时跳过迁成员——winner 已迁；漏网的
            # straggler 由后续 SubagentStop 的 _promote_workflow_team 兜底迁移。
            if _created and fallback is not None and session_id:
                try:
                    same = await self.repo.find_agents_by_session(session_id)
                except Exception:  # noqa: BLE001
                    same = []
                for a in same:
                    if (
                        getattr(a, "team_id", None) == fallback.id
                        and getattr(a, "role", "") == WORKFLOW_AGENT_TYPE
                        and str(getattr(a, "status", "")).endswith("busy")
                    ):
                        # 负排除（2026-07-08 误迁实锤 wf-a6a1a875a3）：同会话串行
                        # 多 run 时，兜底队里可能残留上一条 run 尚未 stop 的 busy
                        # agent——观测行已知其真身 wf_id 时，非本 run 的跳过；
                        # 查不到观测行（本 run 头几秒的新 agent）放行，错了有
                        # SubagentStop 的 promote 精确纠正。
                        try:
                            cc_id = getattr(a, "cc_tool_use_id", "") or ""
                            if cc_id:
                                known = await self.repo.find_workflow_agents_by_cc_id(cc_id)
                                if known and all(w.wf_id != wf_id for w in known):
                                    continue
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            await self.repo.update_agent(a.id, team_id=team.id)
                        except Exception:  # noqa: BLE001
                            pass

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

    async def _leader_usage_outcome(
        self,
        payload: dict,
        *,
        force: bool,
        leader: object | None = None,
    ) -> tuple[dict | None, str | None]:
        """Snapshot this session's main-transcript token usage onto its leader row.

        token 归因 v1 阶段 4 的唯一写入口（设计 §3.3）。四个挂载点共用它：
        ``SessionStart`` / ``SessionEnd`` / ``PostCompact`` 传 ``force=True`` 强制
        定格，``Stop`` 传 ``force=False`` 走节流。

        语义要点（细则见 aiteam.api.leader_usage 模块文档）：
        * **覆写**：写进去的是"从会话开始到此刻"的累计快照，不是本轮增量；
        * **口径**：这里落的是 ``usage_sum``（跨 requestId 累加），与
          ``workflow_agents.tokens`` 的 ``ctx_last`` **不是同一个量，不可相加**；
        * **分列**：Leader 量级远超全部子 agent 之和，呈现层必须按 role 分列
          （属阶段 5，此处只作约束标注）；
        * 全程 best-effort：采集绝不阻断任何 hook 主流程。

        Returns ``(summary, skip_reason)`` —— 两者恒有且仅有一个非空。理由字符串
        是 2026-08-03 排查事故补上的：在那之前五种结局（无 session_id / 无 Leader
        行 / 定位不到 transcript / 被节流 / 抛异常）全部塌缩成同一个 ``None``，
        异常还只落 ``logger.debug``，于是"跑了没数据"和"炸了"在库、日志、回执三处
        都无从分辨，整条链不可证伪。判据分级刻意如此：
        * **节流**是稳态里最常见的正常结局 —— 保持安静，不喊也不落事件；
        * **强制定格失手**（``force=True`` 且非节流）一定是异常 —— 喊 WARNING，
          因为那一刻的水位再也补不回来；
        * **抛异常**额外落一条事件 —— 日志随进程重启被截断，事件不会。
        """
        session_id = payload.get("session_id", "")
        if not session_id:
            return None, "no-session-id"
        try:
            if leader is None:
                leader = await self._find_leader(session_id)
            # _find_leader 在没有 leader 行时会退回本会话的其它 agent，而用量快照
            # 只对主会话成立——落到子 agent 行上就是张冠李戴。
            if leader is None or getattr(leader, "role", "") != "leader":
                return None, "no-leader-row"
            transcript = leader_usage.locate_main_transcript(
                payload.get("transcript_path") or "",
                cwd=payload.get("cwd") or "",
                session_id=session_id,
            )
            if transcript is None:
                return None, "no-transcript"
            snapshot, reason = self._usage_meter.capture_or_reason(
                session_id, transcript, force=force
            )
            if snapshot is None:
                return None, reason
            await self.repo.update_agent(leader.id, **snapshot.as_agent_updates())
            return snapshot.as_summary(), None
        except Exception as exc:  # noqa: BLE001 — 记账绝不阻断 hook 主流程
            reason = f"error:{type(exc).__name__}"
            logger.warning(
                "leader usage capture failed (session=%s force=%s): %s",
                session_id[:8],
                force,
                exc,
                exc_info=True,
            )
            await self._emit_leader_usage_failure(session_id, force, f"{reason}: {exc}")
            return None, reason

    async def _emit_leader_usage_failure(self, session_id: str, forced: bool, reason: str) -> None:
        """Leave a durable, queryable trace of a capture that threw.

        只在**真异常**时落 —— 本批刚把 events 表的写入砍掉四成，日常节流跳过绝不
        进这张表。事件本身也是 best-effort：连留痕都失手时更不该反过来炸主流程；
        但那一手也必须是 WARNING —— 「记录失败的记录失败了」若只落 DEBUG，就正好
        复刻了本次要修的病（``EventType`` 缺成员时 ``create_event`` 抛 ValueError，
        实测本修复一开始就踩到了这一脚）。
        """
        try:
            await self.event_bus.emit(
                EventType.LEADER_USAGE_CAPTURE_FAILED.value,
                f"session:{session_id}",
                {"session_id": session_id, "forced": forced, "reason": reason},
            )
        except Exception:  # noqa: BLE001
            logger.warning("leader usage failure event not recorded", exc_info=True)

    async def _capture_leader_usage(
        self,
        payload: dict,
        *,
        force: bool,
        leader: object | None = None,
    ) -> tuple[dict | None, str | None]:
        """采集 + "强制定格失手就喊出来" —— 四个挂载点唯一该调的入口。

        拆成两层而不是塞进一个函数：内层 :meth:`_leader_usage_outcome` 只负责判定
        结局，这里只负责按结局分级发声。挂载点一律走这里，所以"强制定格静默失败"
        在代码里没有第二条路径可走。
        """
        summary, reason = await self._leader_usage_outcome(payload, force=force, leader=leader)
        if (
            force
            and reason is not None
            and reason != leader_usage.SKIP_THROTTLED
            and not reason.startswith("error:")  # 异常分支已经 WARNING 过了
        ):
            logger.warning(
                "leader usage forced capture missed (session=%s): %s",
                str(payload.get("session_id", ""))[:8],
                reason,
            )
        return summary, reason

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
            # Reuse THIS session's leader (session_id match — never another session's).
            leader = leaders_in_session[0]
            update_kwargs: dict = {
                "status": "busy",
                "last_active_at": utc_now(),
            }
            # Heal project binding — project liveness (summary "工作中") keys off
            # leader.project_id. This is now safe from the 03fe7cae rebind churn:
            # a leader row is bound to exactly one session (one cwd), so the resolved
            # project is stable across the session's life (heals only an unbound row,
            # never ping-pongs between projects like the old cross-session reuse did).
            if project and leader.project_id != project.id:
                update_kwargs["project_id"] = project.id
            await self.repo.update_agent(leader.id, **update_kwargs)
            # 会话恢复补队（2026-07-22，8705dac2 病灶②）：SessionEnd 关掉容器队后
            # 同 id 会话恢复（重启/compact 续接）时这里只复用了 Leader——容器队保持
            # completed，后续直派在 find_active_team_by_leader 撞 completed 全部脱管。
            # 确保容器队在位且 active（helper 内含归属校验与 auto-revive）。
            await self._find_or_create_session_team(session_id, payload)
        elif project:
            # No leader for THIS session yet -> always create a fresh per-session
            # leader (fleet-layer design §3: one leader row per session, born bound,
            # never reused across sessions). The old path here reused another
            # session's leader via find_leader_by_project and rebound its session_id
            # -> the exact churn 03fe7cae is about. Removed.
            team = await self._find_or_create_session_team(session_id, payload)
            if team:
                leader = await self.repo.create_agent(
                    team_id=team.id,
                    name="Leader",
                    role="leader",
                    backstory="Project Leader",
                    source="hook",
                    session_id=session_id,
                    # 主会话模型 hook 事件不携带——留空由下方 transcript 尾读回填，
                    # 不落仓库默认值（曾恒显 claude-opus-4-7 误导展示）。
                    model="",
                )
                # Bind project_id at birth (fleet-layer §3 "出生即绑定"). create_agent
                # drops project_id from kwargs, so it must be set via update_agent —
                # the historical omission is exactly why session leaders were observed
                # unbound (the "7 orphan rows"). One cwd per session makes this stable.
                await self.repo.update_agent(
                    leader.id,
                    status="busy",
                    project_id=project.id,
                    last_active_at=utc_now(),
                )
                # Link the container team to its leader so find_active_team_by_leader
                # resolves (used by _on_subagent_start's no-cc_team_name fallback and
                # by SessionEnd ownership inference).
                await self.repo.update_team(team.id, leader_agent_id=leader.id)
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

        # 主会话用量首测（阶段 4 挂载点之一）：会话启动/恢复各一次，为本会话的
        # Leader 行建立基线。恢复的会话 transcript 已有存量，这一测就把历史补上；
        # 全新会话文件里还没有 assistant 行，采集返回 None、不写任何 0。
        usage, usage_skip = await self._capture_leader_usage(payload, force=True, leader=leader)

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
        return {
            "status": "recorded",
            "leader": leader.name if leader else None,
            "leader_usage": usage,
            # 没采到时说得出是为什么 —— 回执里的 None 曾是唯一线索，什么也证明不了。
            "leader_usage_skip": usage_skip,
        }

    @staticmethod
    def _team_owned_by_session(team: object, session_id: str) -> bool:
        """Whether a team is owned by the given CC session (fleet-layer design §5).

        Ownership signals, in order:
        1. team.config.owner_session_id == session_id (stamped at creation — the
           authoritative, immutable key for all teams created after this change).
        2. Legacy fallback: the session-container team name encodes the session id8
           (``session-<sid8>``), covering rows created before owner stamping.

        Deliberately conservative: an unknown-ownership team returns False so
        SessionEnd never closes a team it cannot prove belongs to the ending
        session. Slightly delayed cleanup of legacy teams (reaper handles them) is
        the correct trade against cross-session clobbering (7ae3b7cd).
        """
        if not session_id:
            return False
        owner = (getattr(team, "config", None) or {}).get("owner_session_id")
        if owner:
            return owner == session_id
        return getattr(team, "name", "") == f"session-{session_id[:8]}"

    async def _on_session_end(self, payload: dict) -> dict:
        """Handle CC session end — reconcile and clean up state.

        Sets this session's agents offline but **keeps their session_id**:
        session_id is identity, status is state, and only the latter ends with
        the session. The old code cleared it here, which detached every row from
        the session that owned it and broke ``_on_session_start``'s reuse lookup
        (``find_agents_by_session``) — so each resume of the same session minted
        another leader. Production carried 120 leader rows, all session_id NULL,
        11 of them from a single day and three of those inside one team.
        """
        session_id = payload.get("session_id", "")
        agents = await self.repo.find_agents_by_session(session_id)
        # 主会话用量终测（阶段 4 挂载点之一，force）：会话到此为止，这一笔就是该
        # Leader 行的终值。**在把 agent 置 offline 之前测** —— 顺序无关正确性
        # （覆写幂等），但先测能保证即便下面的清理抛错也已经定格。
        end_leader = next((a for a in agents if a.role == "leader"), None)
        end_usage, end_usage_skip = (
            await self._capture_leader_usage(payload, force=True, leader=end_leader)
            if end_leader is not None
            else (None, "no-leader-row")
        )
        for agent in agents:
            # workflow 子 agent 豁免（与下方 kind=workflow 队豁免对称）：CC Workflow run
            # 可远长于发起会话，其 fan-out 成员注册时带 session_id 但生命周期归 ingest 按
            # run 终态收（WP9 成员收工）。无条件 offline 会误伤仍在跑的 run 成员，造成
            # offline 闪烁（审计 WP6）。成员的 offline 只归 run 终态 ingest 路径管。
            if getattr(agent, "role", "") == WORKFLOW_AGENT_TYPE:
                continue
            await self.repo.update_agent(agent.id, status="offline", current_task=None)

        # Reconciliation stats
        hook_count = await self.repo.count_agents_by_source(
            source="hook",
            session_id=session_id,
        )
        api_count = await self.repo.count_agents_by_source(
            source="api",
            session_id=session_id,
        )

        # Close ONLY the teams owned by THIS session (fleet-layer design §5, fixes
        # 7ae3b7cd). The old code closed every active non-workflow team in the whole
        # DB regardless of ownership, so a bystander session's SessionEnd clobbered
        # teams other live sessions were still using (实录: c4fab878 的 SessionEnd 关了
        # abff40af 的队). Ownership key = team.config.owner_session_id (stamped at
        # creation); legacy teams without it fall back to the session-container name.
        # Teams owned by other sessions (or unknown ownership) are left for the reaper
        # to reap by their own liveness — never cross-session clobbered here.
        # workflow 队仍全豁免（生命周期由 ingest 按 run 状态维护）。
        closed_teams = []
        all_teams = await self.repo.list_teams()
        for team in all_teams:
            if (team.config or {}).get("kind") == "workflow":
                continue
            if team.status != "active":
                continue
            if not self._team_owned_by_session(team, session_id):
                continue
            await self.repo.update_team(team.id, status="completed")
            closed_teams.append(team.name)
            logger.info("SessionEnd: closed owned team '%s'", team.name)
            # Offline any stragglers left in this owned team (the first loop already
            # handled agents still carrying this session_id; this catches members
            # whose session_id was cleared/never set but who live in the closed team).
            for agent in await self.repo.list_agents(team.id):
                if agent.status != "offline":
                    await self.repo.update_agent(
                        agent.id, status="offline", current_task=None
                    )

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
            "leader_usage": end_usage,
            "leader_usage_skip": end_usage_skip,
        }

    async def _on_stop(self, payload: dict) -> dict:
        """Handle CC Stop event — a turn ended in *this* session.

        Touches ``last_active_at`` on the stopping session's own busy agents and
        refreshes the leader's model. Status transitions are not this hook's job:
        StateReaper owns liveness, SessionEnd owns session teardown.

        There used to be a "mode 2 global fallback" here — when the stopping
        session owned no busy agents it walked *every team in the database* and
        offlined anything busy and recently active, on the theory that a Stop
        with no session match must mean a session ended. It is the same
        cross-session clobber 7ae3b7cd fixed for SessionEnd, and it fired in
        production on 2026-07-27: a live sub-agent of session 80d0cc5e was
        flipped offline (``trigger=stop_global``) mid tool-call because an
        unrelated session in another project ended a turn. "No busy agent in
        this session" is the *normal* state of a plain Leader turn end, so the
        premise never held. Removed rather than narrowed: with session_id no
        longer wiped at SessionEnd, the session-scoped path above is reliable,
        and stragglers are already reaped by liveness.
        """
        session_id = payload.get("session_id", "")
        updated: list[str] = []

        # Mode 1: find by session_id -> only update last_active_at, don't change status
        # State changes are handled by StateReaper's config_liveness detection
        recent_cutoff = utc_now() - timedelta(seconds=30)
        agents = await self.repo.find_agents_by_session(session_id)
        for agent in agents:
            if agent.status == "busy" and agent.source == "hook":
                if agent.created_at and agent.created_at > recent_cutoff:
                    continue  # Recently created agent, skip to prevent old Stop from overriding
                await self.repo.update_agent(
                    agent.id,
                    last_active_at=utc_now(),
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

        # 主会话用量（阶段 4 挂载点之一）——**这一个必须节流**。Stop 每轮都触发，
        # 而全量解析随会话线性变贵（实测 45.1 MB / 0.18 s，且只会更大）。节流窗内
        # 直接返回 None，连文件都不读第二遍（判据只用一次 stat）。
        leader_row = next((a for a in agents if a.role == "leader"), None)
        usage, usage_skip = (
            await self._capture_leader_usage(payload, force=False, leader=leader_row)
            if leader_row is not None
            # 本会话没有 Leader 行（未注册项目的会话）——不必再查一次
            else (None, "no-leader-row")
        )

        logger.info("Stop event: %d heartbeat updates (session %s)", len(updated), session_id[:8])
        return {
            "status": "ok",
            "heartbeat_updates": updated,
            "agents_offline": [],
            "leader_usage": usage,
            "leader_usage_skip": usage_skip,
        }

    async def _on_teammate_idle(self, payload: dict) -> dict:
        """CC says a teammate went idle — recorded, deliberately not acted on.

        TeammateIdle is CC's own first-party answer to "is this teammate still
        working", i.e. exactly the signal the OS has been approximating with
        transcript mtime and tool-event touches. It is wired up now so the two
        readings accumulate side by side; the switch to using it as the liveness
        authority is a separate, evidence-gated decision (C13, batch 9).

        Deliberately does **not** write ``status``. CC's idle means "finished a
        turn, awaiting input" — the OS's ``offline`` means "gone". Conflating
        them is precisely the "team dead while the agent is alive" misread this
        batch is unwinding, so the event records both views and changes nothing.
        Payload (CC v2.1.219): ``{teammate_name, team_name(@deprecated)}`` plus
        the common base — no idle reason, no idle duration.
        """
        session_id = payload.get("session_id", "")
        name = payload.get("teammate_name", "")
        # Resolved by *exact name inside this session*, never through
        # _resolve_agent: that helper's last resort is "no cc agent id means the
        # main session, so return the Leader", which is right for tool calls and
        # wrong here — live check attributed worker-b9's idle signal to the
        # Leader row. Misattributed signals would poison the very comparison this
        # event exists to collect, so an unmatched name stays unmatched.
        agent = None
        if name and session_id:
            agent = await self.repo.find_agent_by_session(session_id, name)
        await self.event_bus.emit(
            "cc.teammate_idle",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "teammate_name": name,
                "cc_team_name": payload.get("team_name", ""),
                "agent_id": getattr(agent, "id", ""),
                "os_status": getattr(getattr(agent, "status", None), "value", ""),
                "observed_only": True,
            },
        )
        return {"status": "observed", "agent_id": getattr(agent, "id", "")}

    async def _on_cc_task_event(self, payload: dict) -> dict:
        """TaskCreated / TaskCompleted — CC 原生任务的**观测**面。

        刻意只落事件、不碰任务墙:上墙仍归 cc_task_bridge 在 TaskCompleted 上按
        "有主或有依赖链"过滤后记账（Q1 完成时点记账裁定不动）。这里补的是遥测——
        桥此前挂在一个没有并挂 send_event 的事件上,触发几次、滤掉几条全无记录,
        连"桥是不是活的"都判断不了。
        """
        session_id = payload.get("session_id", "")
        created = payload.get("hook_event_name") == "TaskCreated"
        event_type = (
            EventType.CC_TASK_CREATED.value if created else EventType.CC_TASK_COMPLETED.value
        )
        await self.event_bus.emit(
            event_type,
            f"session:{session_id}",
            {
                "session_id": session_id,
                "cc_task_id": str(payload.get("task_id") or ""),
                "subject": str(payload.get("task_subject") or ""),
                "owner": str(payload.get("teammate_name") or ""),
                "cc_team_name": str(payload.get("team_name") or ""),
                "observed_only": True,
            },
        )
        return {"status": "observed", "cc_task_id": str(payload.get("task_id") or "")}

    async def _on_post_compact(self, payload: dict) -> dict:
        """压缩确实完成了 — 给 PreCompact 存下的检查点收口。

        PreCompact 触发不等于压缩发生（用户可以中断），所以检查点需要一个"真的
        压了"的回执，否则事后无法分辨一条检查点对应的是一次真压缩还是一次取消。

        **不落 compact_summary 正文**：那段摘要压缩后本来就在模型的上下文里，
        再存一份既是重复，又会把大段对话内容灌进 events 表——本批刚把这张表的
        写入砍掉四成，不该从另一头加回来。只记长度，够判断摘要是否正常产出。
        """
        session_id = payload.get("session_id", "")
        # hook 侧已把正文换成长度（send_event._trim_payload）——大摘要场景下正文
        # 根本到不了这里，只认长度才不会落成 0。老形态（正文还在）继续兜底。
        precomputed = payload.get("compact_summary_chars")
        if isinstance(precomputed, int):
            summary_chars = precomputed
        else:
            summary_chars = len(payload.get("compact_summary", "") or "")
        # 主会话用量在压缩点强制定格（阶段 4 挂载点之一）。实测 compact 是**原地
        # 追加**边界行、不截断也不替换文件，所以这一测不是在抢救即将消失的数据；
        # 它定的是"压缩发生在哪个水位"，并覆盖 compact/resume 之后会话另起文件
        # 的交接点（新文件重放的 pre-compact 行 usage 全 0，两份各自解析不重复计数）。
        # PostCompact 的载荷最容易被 hook 侧裁剪，路径缺失时由 slug 反查兜底。
        usage, usage_skip = await self._capture_leader_usage(payload, force=True)
        await self.event_bus.emit(
            "session.compact_completed",
            f"session:{session_id}",
            {
                "session_id": session_id,
                "trigger": payload.get("trigger", ""),
                "summary_chars": summary_chars,
            },
        )
        return {
            "status": "recorded",
            "summary_chars": summary_chars,
            "leader_usage": usage,
            "leader_usage_skip": usage_skip,
        }

    async def _find_or_create_session_team(
        self,
        session_id: str,
        payload: dict,
    ):
        """Find or create THIS session's own container team (fleet-layer design §3).

        Each CC session gets its own container team that anchors its leader row.
        Never reuse another session's team: the old fallbacks (cwd -> an existing
        project team, or "most recently created team") let a session's leader
        parasitize another session's / a workflow team — the 03fe7cae churn. Removed.

        1. If this session already owns a team (its leader/agents point at one), reuse it.
        2. Otherwise create a fresh ``session-<sid8>`` container, tagged kind="session"
           + owner_session_id so its lifecycle is governed by the owning session's file
           mtime (reaper) and closed session-scoped at SessionEnd, and so the reaper's
           CC-config-based liveness check exempts it (it never has a ~/.claude/teams dir).
        """
        # 1. This session's own team (contains its leader / agents)
        container_name = f"session-{session_id[:8]}" if session_id else "session-unknown"
        if session_id:
            agents = await self.repo.find_agents_by_session(session_id)
            if agents:
                leader_agents = [a for a in agents if a.role == "leader"]
                target = leader_agents[0] if leader_agents else agents[0]
                team = await self.repo.get_team(target.team_id)
                # 归属校验（2026-07-22，8705dac2 病灶③）：顺 leader/agents 找到的队
                # 必须真是本会话容器——Leader 被 workflow 队认养未归还时，这里会顺着
                # 漂移的 team_id 寄生到别人的/已关的队。不是本会话容器就落到步骤2
                # 按名 find-or-create。
                if team is not None and (
                    (getattr(team, "config", None) or {}).get("owner_session_id")
                    == session_id
                    or team.name == container_name
                ):
                    return await self._revive_if_completed(team)

        # 2. Create this session's own container team (never reuse another session's).
        # race-safe：同会话两并发 hook 事件都建 session-<sid8> 容器队会撞 teams.name
        # UNIQUE（2026-07-21 实锤 session-e713a6cb → 未捕获 ASGI 500 丢事件）。
        # get_or_create_team 吞冲突重取既有行，loser 复用 winner 建的容器队。
        config: dict = {"kind": "session", "owner_session_id": session_id} if session_id else {}
        # 顺手记下拥有这支队的 CC 进程。必须在**建队此刻**记：CC 每进程一份会话
        # 登记，进程换会话时原地改写 sessionId，旧值不留痕，事后再问就查不到了。
        # 判据是 session_id 精确相等，不掺 cwd / 启动时间窗的近似认亲（本机实测
        # 两个进程可在同一 cwd 下相隔 1 秒启动，近似匹配就是二义的）。查不到就
        # 不盖章——展示层认得出"不知道"，猜错却会把两个进程混成一个。
        if session_id:
            from aiteam.api import session_registry

            try:
                pid = session_registry.pid_for_session(session_id)
            except Exception:  # noqa: BLE001 — 探测失败绝不能挡住建队
                pid = None
            if pid:
                config["cc_pid"] = pid

        team, _created = await self.repo.get_or_create_team(
            name=container_name,
            mode="coordinate",
            config=config,
        )
        if _created:
            logger.info(
                "Auto-created session container team: %s (cwd=%s)",
                team.name,
                payload.get("cwd", ""),
            )
            return team
        # 既有容器队可能已被 SessionEnd/reaper 关闭（同 id 会话恢复的生命周期黑洞，
        # 8705dac2 病灶②）——复活，对齐 _resolve_cc_team 的 auto-revive 先例。
        return await self._revive_if_completed(team)

    async def _infer_project_id(
        self, session_id: str, *, allow_cwd_fallback: bool = True
    ) -> str | None:
        """解析团队应归属的项目：会话 Leader 优先，cwd 最长前缀匹配兜底。

        Leader 的 project_id 在会话开启时就锁定，比 cwd 更可靠（多窗口 cwd 可能
        前缀重叠）。cwd 分支取最长匹配 root_path——早期取首个匹配常误选更宽的父项目。
        2026-07-25 从建队分支提取为 helper，供"已有队补绑项目"复用。
        """
        # 1) Authoritative: session_id -> Leader -> project_id
        if session_id:
            leader = await self._find_leader(session_id)
            if leader and leader.project_id:
                logger.info(
                    "CC team mapping: session %s -> leader '%s' -> project %s",
                    session_id[:8], leader.name, leader.project_id,
                )
                return leader.project_id

        # 2) Fallback: cwd longest-prefix match (only when no Leader yet)
        if not allow_cwd_fallback:
            return None  # 补绑路径：宁可不绑，绝不猜错项目（跨项目污染代价远大于不绑）
        cwd = ""
        if getattr(self, "_current_event_cwd", ""):
            cwd = self._current_event_cwd.replace("\\", "/").rstrip("/").lower()
        if not cwd:
            import os as _os
            cwd = _os.getcwd().replace("\\", "/").rstrip("/").lower()
        if not cwd:
            return None
        projects = await self.repo.list_projects()
        best_match = None
        best_len = -1
        for p in projects:
            rp = (p.root_path or "").replace("\\", "/").rstrip("/").lower()
            if rp and (cwd == rp or cwd.startswith(rp + "/")) and len(rp) > best_len:
                best_match = p
                best_len = len(rp)
        if best_match is None:
            return None
        logger.info(
            "CC team mapping: cwd '%s' -> project %s (root_path=%s)",
            cwd, best_match.name, best_match.root_path,
        )
        return best_match.id

    async def _resolve_agent_project_id(self, team, session_id: str) -> str | None:
        """解析新注册 agent 应归属的项目——只认权威链，拿不到就留空。

        档位（越靠前越权威）：
        1. 本会话 Leader 的 project_id（会话开启时按该会话 cwd 锁定，归属铁律
           「session 启动目录为准」）；
        2. **同队 Leader 行**的 project_id——agents.session_id 历史上大面积为
           NULL（2026-07-27 实测 120/120 个 leader 行皆空），只靠第 1 档会恒失效，
           故补这一档把队内 Leader 也当权威；
        3. team.project_id——队自身绑定，作最后兜底。它可能被历史脏数据污染
           （启动回填的 cwd 批量认领，已在 deps._auto_create_projects 修掉），
           所以排在 Leader 之后。
        4. 都拿不到 → None。绝不用进程 cwd / 跨会话共享的全局态猜。
        """
        # 1) 本会话 Leader
        leader = await self._find_leader(session_id) if session_id else None
        if leader is not None and getattr(leader, "project_id", None):
            return str(leader.project_id)

        # 2) 同队 Leader 行
        if team is not None:
            try:
                members = await self.repo.list_agents(team.id)
            except Exception:  # noqa: BLE001 — 归属解析绝不影响注册主流程
                members = []
            for a in members:
                if getattr(a, "role", "") == "leader" and getattr(a, "project_id", None):
                    return str(a.project_id)

        # 3) 队自身绑定
        return getattr(team, "project_id", None) or None

    async def _revive_if_completed(self, team):
        """completed 的会话容器队复活为 active（新成员/会话恢复即事实上在用）。"""
        if team is not None and team.status == "completed":
            await self.repo.update_team(team.id, status="active")
            logger.warning(
                "Session container team '%s' was completed; auto-revived to active.",
                team.name,
            )
            await self.event_bus.emit(
                "team.auto_revived",
                f"team:{team.id}",
                {"team_id": team.id, "team_name": team.name,
                 "reason": "session container back in use"},
            )
            team.status = "active"
        return team
