"""Wake Agent Manager — manages claude -p subprocess lifecycle for scheduled agent waking."""

import asyncio
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

from aiteam.clock import utc_now
from aiteam.config import settings

logger = logging.getLogger(__name__)

# UUID validation pattern
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# Credentials a --bare run can authenticate with. Bare mode reads Anthropic auth
# strictly from ANTHROPIC_API_KEY (or an apiKeyHelper passed via --settings, which
# this module never passes) and never reads OAuth or the keychain, so a
# subscription login has no credential there. Third-party providers use their own.
_BARE_API_KEY_ENV = "ANTHROPIC_API_KEY"
_BARE_PROVIDER_FLAGS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def _validate_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


def _bare_auth_available(env: Mapping[str, str]) -> bool:
    """True when a --bare subprocess with this env has a credential it can read."""
    if (env.get(_BARE_API_KEY_ENV) or "").strip():
        return True
    return any(
        (env.get(flag) or "").strip().lower() in ("1", "true", "yes", "on")
        for flag in _BARE_PROVIDER_FLAGS
    )


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


# Fleet dispatch operational preamble (fleet-layer design §4.3): a dispatch may only
# carry OPERATIONAL work (advance a task, collect status, record a memo). It must never
# make strategic decisions or decide on the user's behalf. The instruction is wrapped in
# XML tags to keep the data section separable from this template (injection mitigation).
_FLEET_DISPATCH_PREAMBLE = (
    "你收到一次 AI Team OS 舰队定向下发（fleet dispatch）。这是对本会话主线的一次"
    "自动化恢复，只做被指派的**操作级**工作：推进指定任务 / 收集你的当前状态 / "
    "把结果用 task_memo_add 记录。\n"
    "红线：不做战略决策、不替用户拍板项目方向或重大架构；遇到需要用户定夺的事，用 "
    "briefing_add 记录待决事项后结束，不要自行拍板。完成后简述你做了什么并结束。"
)


def _build_dispatch_prompt(instruction: str) -> str:
    """Build a fleet-dispatch prompt: operational preamble + XML-wrapped instruction."""
    return (
        f"{_FLEET_DISPATCH_PREAMBLE}\n\n"
        f"<dispatch-instruction>\n{instruction}\n</dispatch-instruction>"
    )


def _cleanup_prompt_file(prompt_file: str | None) -> None:
    """Remove temp prompt file if it exists, silently ignore errors."""
    if prompt_file:
        try:
            Path(prompt_file).unlink(missing_ok=True)
        except Exception:
            pass


def _build_cmd(
    prompt: str,
    max_turns: str,
    cfg: dict,
    env: Mapping[str, str] | None = None,
) -> tuple[list[str], str | None]:
    """Build the claude subprocess command array.

    Returns (cmd, prompt_file) where prompt_file is a path to a temp file
    that must be deleted after the subprocess finishes, or None if the prompt
    was passed inline.

    The session runs with its full environment (CLAUDE.md, hooks, MCP discovery)
    and the login the user already has, OAuth or API key. No --allowedTools is
    passed: the session keeps its own permission configuration.

    bare_mode=True is an explicit opt-in that trims startup; it skips CLAUDE.md,
    plugins, hooks, auto memory and MCP discovery, so we pair it with
    --mcp-config pointing to the project's .mcp.json. Bare mode authenticates
    only with ANTHROPIC_API_KEY or a third-party provider, so when `env` (the
    subprocess environment, default os.environ) has neither, the opt-in is
    dropped with a warning rather than starting a run that can only fail with
    "Not logged in".

    Fleet dispatch (fleet-layer design §4.1): when cfg carries resume_session_id, we
    append `--resume <sid>` to target an existing ship (CC session).
    output_format (e.g. "json") is appended when requested so the caller can capture the
    resumed session_id/result.
    """
    resume_session_id: str = str(cfg.get("resume_session_id", "") or "")
    output_format: str = str(cfg.get("output_format", "") or "")
    bare_mode: bool = bool(cfg.get("bare_mode", False))
    if bare_mode and not _bare_auth_available(os.environ if env is None else env):
        logger.warning(
            "wake: bare_mode requested but the subprocess env has no %s or "
            "third-party provider flag; bare mode never reads OAuth or keychain "
            "logins, so running without --bare",
            _BARE_API_KEY_ENV,
        )
        bare_mode = False

    # Resolve .mcp.json path relative to cwd or project root
    mcp_config_path: str = cfg.get("mcp_config", "")
    if not mcp_config_path and bare_mode:
        # Attempt to locate .mcp.json next to the project working directory
        cwd = cfg.get("cwd", "")
        candidate = Path(cwd) / ".mcp.json" if cwd else None
        if candidate and candidate.exists():
            mcp_config_path = str(candidate)

    # Handle Windows 8191-char cmdline limit: use a temp file for long prompts
    prompt_file: str | None = None
    prompt_arg: str = prompt
    if len(prompt) > 4000:
        tf = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        tf.write(prompt)
        tf.close()
        prompt_file = tf.name
        prompt_arg = f"@{prompt_file}"

    cmd: list[str] = ["claude", "-p", prompt_arg]

    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    if output_format:
        cmd += ["--output-format", output_format]

    if bare_mode:
        cmd += ["--bare", "--exclude-dynamic-system-prompt-sections"]
        if mcp_config_path:
            cmd += ["--mcp-config", mcp_config_path]

    cmd += ["--max-turns", max_turns]

    return cmd, prompt_file


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
                session.id, outcome="skipped_triage", finished_at=utc_now(),
                triage_result=triage_summary,
            )
            return "skipped_triage"
        logger.debug("wake_agent: %s triage pass — %s", agent_name, triage_summary)

        # Validate IDs
        task_id = getattr(sched_task, "id", "")
        if task_id and not _validate_uuid(task_id):
            logger.error("wake_agent: invalid scheduled_task_id: %s", task_id)
            return "error_config"

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
        env = _clean_env(cwd)

        # Build command args (array form, no shell=True)
        max_turns = str(cfg.get("max_turns", settings.WAKE_MAX_TURNS))
        prompt = _build_prompt(sched_task)

        cmd, prompt_file = _build_cmd(prompt, max_turns, cfg, env=env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd or None,
            )
        except Exception as e:
            logger.error("wake_agent: failed to start subprocess for %s: %s", agent_name, e)
            _cleanup_prompt_file(prompt_file)
            # Record failed session
            session = await self._repo.create_wake_session(
                scheduled_task_id=task_id, agent_name=agent_name,
                team_id=cfg.get("team_id", ""),
            )
            await self._repo.update_wake_session(
                session.id, outcome="error", finished_at=utc_now(),
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
            self._track_session(proc, sched_task, agent_name, session.id, prompt_file),
            name=f"wake-{agent_name}",
        )
        self._active_sessions[agent_name] = track_task
        logger.info("wake_agent: started %s (pid=%s, session=%s)", agent_name, proc.pid, session.id)
        return "started"

    async def dispatch_to_session(
        self,
        target_session_id: str,
        instruction: str,
        cwd: str = "",
        team_id: str = "",
        max_turns: int | None = None,
    ) -> dict:
        """Fleet dispatch: drive an existing ship via headless `claude -p --resume`.

        Reuses the whole wake machine (global semaphore, circuit breaker, ledger,
        subprocess tracking); the only difference from a scheduled wake is the target
        is an existing CC session resumed by id rather than a fresh one. Concurrency is
        deduped per-session so two dispatches to the same ship never race. The caller
        (the /api/fleet/dispatch route) is responsible for the reachability gate
        (resumable + not user-live); this method assumes that gate already passed.

        Returns a status dict: {"status": <started|skipped_*|fused|error_*>, ...}.
        The instruction is constrained to operational work by the preamble in
        _build_dispatch_prompt; tool permissions are the resumed session's own.
        """
        if not target_session_id:
            return {"status": "error_config", "reason": "missing target_session_id"}
        if not instruction or not instruction.strip():
            return {"status": "error_config", "reason": "empty instruction"}

        # Per-session dedup: the ledger identity encodes the target so a second dispatch
        # to the same ship collides here instead of racing a competing turn.
        dispatch_name = f"fleet-dispatch-{target_session_id}"
        if dispatch_name in self._active_sessions:
            logger.info("fleet_dispatch: %s already in flight, skipping", target_session_id[:8])
            return {"status": "skipped_concurrent", "reason": "dispatch already in flight for this session"}

        # Global concurrency: shared with scheduled wakes so the fleet can't flood.
        if self._semaphore.locked():
            return {"status": "skipped_max_concurrent", "reason": "max concurrent subprocesses reached"}

        # Circuit breaker: repeated dispatch failures to the same ship trip the fuse.
        failures = await self._repo.get_consecutive_failures(dispatch_name)
        if failures >= settings.WAKE_FUSE_THRESHOLD:
            logger.warning("fleet_dispatch: %s fused (%d failures)", target_session_id[:8], failures)
            return {"status": "fused", "reason": f"{failures} consecutive failures"}

        turns = str(max_turns if max_turns is not None else settings.WAKE_MAX_TURNS)
        prompt = _build_dispatch_prompt(instruction)

        cfg = {
            "resume_session_id": target_session_id,
            "output_format": "json",
            "cwd": cwd,
        }
        env = _clean_env(cwd)
        cmd, prompt_file = _build_cmd(prompt, turns, cfg, env=env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd or None,
            )
        except Exception as e:
            logger.error("fleet_dispatch: failed to start subprocess for %s: %s", target_session_id[:8], e)
            _cleanup_prompt_file(prompt_file)
            session = await self._repo.create_wake_session(
                scheduled_task_id="", agent_name=dispatch_name, team_id=team_id,
            )
            await self._repo.update_wake_session(
                session.id, outcome="error", finished_at=utc_now(),
                stdout_summary=str(e)[:500], exit_code=-1,
                triage_result=self._dispatch_ledger_meta(target_session_id, instruction),
            )
            return {"status": "error_start", "reason": str(e)[:200]}

        # Ledger record (fleet-layer §4.3: every dispatch is auditable). Reuses the
        # wake_sessions table: agent_name = fleet-dispatch-<sid>, triage_result carries
        # the dispatch metadata (target + mode + instruction summary) as JSON.
        session = await self._repo.create_wake_session(
            scheduled_task_id="", agent_name=dispatch_name, team_id=team_id,
        )
        await self._repo.update_wake_session(
            session.id,
            triage_result=self._dispatch_ledger_meta(target_session_id, instruction),
        )

        track_task = asyncio.create_task(
            self._track_session(proc, None, dispatch_name, session.id, prompt_file),
            name=f"fleet-dispatch-{target_session_id[:8]}",
        )
        self._active_sessions[dispatch_name] = track_task
        logger.info(
            "fleet_dispatch: started resume of %s (pid=%s, session=%s)",
            target_session_id[:8], proc.pid, session.id,
        )
        return {
            "status": "started",
            "target_session_id": target_session_id,
            "wake_session_id": session.id,
            "mode": "resume",
        }

    @staticmethod
    def _dispatch_ledger_meta(target_session_id: str, instruction: str) -> str:
        """Serialize dispatch metadata for the wake_sessions.triage_result ledger field."""
        import json as _json

        return _json.dumps(
            {
                "kind": "fleet_dispatch",
                "mode": "resume",
                "target_session_id": target_session_id,
                "instruction_summary": (instruction or "")[:200],
            },
            ensure_ascii=False,
        )

    async def _track_session(self, proc, sched_task, agent_name: str, session_id: str, prompt_file: str | None = None):
        """Independent task: waits for subprocess, handles timeout, records outcome."""
        start_time = utc_now()
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
            _cleanup_prompt_file(prompt_file)
            self._active_sessions.pop(agent_name, None)
            finished = utc_now()
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
