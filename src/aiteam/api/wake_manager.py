"""Wake Agent Manager — manages claude -p subprocess lifecycle for scheduled agent waking."""

import asyncio
import logging
import os
import re
from datetime import datetime

from aiteam.config import settings

logger = logging.getLogger(__name__)

# UUID validation pattern
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# Tool presets for --allowedTools
WAKE_TOOL_PRESETS: dict[str, list[str]] = {
    "safe": [
        "Read", "Glob", "Grep", "Edit", "Write", "SendMessage",
        "mcp__ai-team-os__task_memo_add",
        "mcp__ai-team-os__task_memo_read",
        "mcp__ai-team-os__task_update",
        "mcp__ai-team-os__task_status",
        "mcp__ai-team-os__taskwall_view",
        "mcp__ai-team-os__meeting_send_message",
    ],
    "with_bash": [],  # populated at module load
}
WAKE_TOOL_PRESETS["with_bash"] = [*WAKE_TOOL_PRESETS["safe"], "Bash"]


def _validate_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


def _clean_env(cwd: str = "") -> dict[str, str]:
    """Build a safe env for subprocess — inherit most, exclude secrets."""
    env = os.environ.copy()
    # Remove known sensitive variables
    for key in ("DATABASE_URL", "SECRET_KEY", "AITEAM_API_URL"):
        env.pop(key, None)
    # Ensure CLAUDE_PROJECT_DIR is set for MCP initialization
    if cwd and "CLAUDE_PROJECT_DIR" not in env:
        env["CLAUDE_PROJECT_DIR"] = cwd
    return env


def _build_prompt(sched_task) -> str:
    """Build prompt with strict template/data separation."""
    cfg = sched_task.action_config or {}
    agent_name = cfg.get("agent_name", "unknown")
    prompt_template = cfg.get("prompt_template", "")

    # If no custom template, use default
    if not prompt_template:
        prompt_template = (
            "你是AI Team OS的调度Agent '{agent_name}'。你被自动唤醒来推进待办任务。\n"
            "请查看任务墙，找到分配给你的pending任务，选择最高优先级的一个来推进。\n"
            "完成后通过task_update更新任务状态，并通过task_memo_add记录你的工作进展。\n"
            "如果没有可推进的任务，直接结束即可。"
        )

    prompt_template = prompt_template.replace("{agent_name}", agent_name)

    # Data section wrapped in XML tags (prompt injection mitigation)
    task_context = cfg.get("task_context", "")
    if task_context:
        return f"{prompt_template}\n\n<task-context>\n{task_context}\n</task-context>"
    return prompt_template


class WakeAgentManager:
    """Manages wake_agent subprocess lifecycle, decoupled from StateReaper tick."""

    def __init__(self, repo, event_bus):
        self._repo = repo
        self._event_bus = event_bus
        self._active_sessions: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_WAKES)

    async def try_wake(self, sched_task) -> str:
        """Attempt to wake an agent. Called from StateReaper. Returns immediately."""
        cfg = sched_task.action_config or {}
        agent_name = cfg.get("agent_name", "")

        # Validate agent_name
        if not agent_name:
            logger.warning("wake_agent: missing agent_name in action_config")
            return "error_config"

        # Per-agent concurrency check
        if agent_name in self._active_sessions:
            logger.info("wake_agent: %s already active, skipping", agent_name)
            return "skipped_concurrent"

        # Global concurrency check
        if self._semaphore.locked():
            logger.info("wake_agent: max concurrent reached, skipping %s", agent_name)
            return "skipped_max_concurrent"

        # Circuit breaker check
        failures = await self._repo.get_consecutive_failures(agent_name)
        if failures >= settings.WAKE_FUSE_THRESHOLD:
            logger.warning("wake_agent: %s fused (%d consecutive failures)", agent_name, failures)
            return "fused"

        # Triage: check if agent has actionable work
        has_work, triage_summary = await self._repo.has_actionable_tasks(agent_name)
        if not has_work:
            logger.debug("wake_agent: %s triage skip — %s", agent_name, triage_summary)
            session = await self._repo.create_wake_session(
                scheduled_task_id=getattr(sched_task, "id", ""),
                agent_name=agent_name,
                team_id=cfg.get("team_id", ""),
            )
            await self._repo.update_wake_session(
                session.id, outcome="skipped_triage", finished_at=datetime.now(),
                triage_result=triage_summary,
            )
            return "skipped_triage"
        logger.debug("wake_agent: %s triage pass — %s", agent_name, triage_summary)

        # Validate IDs
        task_id = getattr(sched_task, "id", "")
        if task_id and not _validate_uuid(task_id):
            logger.error("wake_agent: invalid scheduled_task_id: %s", task_id)
            return "error_config"

        # Build command args (array form — no shell=True)
        max_turns = str(cfg.get("max_turns", settings.WAKE_MAX_TURNS))
        tools_level = cfg.get("allowed_tools_level", "safe")
        tools = cfg.get("allowed_tools") or WAKE_TOOL_PRESETS.get(tools_level, WAKE_TOOL_PRESETS["safe"])
        allowed_tools_str = ",".join(tools)
        prompt = _build_prompt(sched_task)

        cmd = ["claude", "-p", prompt, "--max-turns", max_turns, "--allowedTools", allowed_tools_str]

        # Resolve working directory: use action_config.cwd or project root
        cwd = cfg.get("cwd", "")
        if not cwd:
            # Try to find project root from repo
            try:
                projects = await self._repo.list_projects()
                for p in projects:
                    if p.root_path:
                        cwd = p.root_path
                        break
            except Exception:
                pass

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_clean_env(cwd),
                cwd=cwd or None,
            )
        except Exception as e:
            logger.error("wake_agent: failed to start subprocess for %s: %s", agent_name, e)
            # Record failed session
            session = await self._repo.create_wake_session(
                scheduled_task_id=task_id, agent_name=agent_name,
                team_id=cfg.get("team_id", ""),
            )
            await self._repo.update_wake_session(
                session.id, outcome="error", finished_at=datetime.now(),
                stdout_summary=str(e)[:500], exit_code=-1,
            )
            return "error_start"

        # Create session record
        session = await self._repo.create_wake_session(
            scheduled_task_id=task_id, agent_name=agent_name,
            team_id=cfg.get("team_id", ""),
        )

        # Fire-and-forget: register independent tracking task
        track_task = asyncio.create_task(
            self._track_session(proc, sched_task, agent_name, session.id),
            name=f"wake-{agent_name}",
        )
        self._active_sessions[agent_name] = track_task
        logger.info("wake_agent: started %s (pid=%s, session=%s)", agent_name, proc.pid, session.id)
        return "started"

    async def _track_session(self, proc, sched_task, agent_name: str, session_id: str):
        """Independent task: waits for subprocess, handles timeout, records outcome."""
        start_time = datetime.now()
        outcome = "error"
        exit_code = None
        stdout_tail = ""
        try:
            async with self._semaphore:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=settings.WAKE_TIMEOUT_SECONDS,
                )
                exit_code = proc.returncode
                stdout_tail = (stdout_bytes or b"").decode(errors="replace")[-500:]
                # max-turns reached exits with code 1 but is normal behavior
                if exit_code == 0 or "max turns" in stdout_tail.lower():
                    outcome = "completed"
                else:
                    outcome = "error"
        except TimeoutError:
            # Two-phase kill
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except TimeoutError:
                proc.kill()
                await proc.wait()
            exit_code = proc.returncode
            stdout_tail = f"TIMEOUT: killed after {settings.WAKE_TIMEOUT_SECONDS}s"
            outcome = "timeout"
            logger.warning("wake_agent: %s timed out, killed", agent_name)
        except asyncio.CancelledError:
            # Shutdown path
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            outcome = "cancelled"
            raise
        except Exception as e:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            exit_code = proc.returncode
            stdout_tail = str(e)[:500]
            outcome = "error"
            logger.error("wake_agent: %s tracking error: %s", agent_name, e)
        finally:
            self._active_sessions.pop(agent_name, None)
            finished = datetime.now()
            duration = (finished - start_time).total_seconds()
            try:
                await self._repo.update_wake_session(
                    session_id,
                    finished_at=finished,
                    outcome=outcome,
                    exit_code=exit_code,
                    stdout_summary=stdout_tail,
                    duration_seconds=duration,
                )
            except Exception as e:
                logger.error("wake_agent: failed to record session outcome: %s", e)

    async def shutdown(self):
        """Graceful shutdown: cancel all active wake sessions."""
        if not self._active_sessions:
            return
        logger.info("wake_agent: shutting down %d active sessions", len(self._active_sessions))
        tasks = list(self._active_sessions.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active_sessions.clear()

    @property
    def active_count(self) -> int:
        return len(self._active_sessions)

    @property
    def active_agents(self) -> list[str]:
        return list(self._active_sessions.keys())
