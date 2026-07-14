#!/usr/bin/env python3
"""Pipeline gate hook (PreToolUse).

Activated only when the current task has autopilot_active=True.
Interactive mode (autopilot_active=False) passes through with zero friction.
Self-contained: no aiteam package imports (hook runs as a standalone process).

Exit codes:
    0 — allow
    2 — block (stage mismatch or requires_user_ack)
"""

import json
import logging
import os
import sys
import time
import urllib.request
from datetime import UTC
from pathlib import Path

logger = logging.getLogger(__name__)

# ============================================================
# Tool classification constants (mirrors tool_classifier.py)
# Duplicated here to keep the hook self-contained.
# ============================================================

ALL_EXEMPT: frozenset[str] = frozenset({
    "context_resolve", "pipeline_advance", "pipeline_status",
    "task_memo_add", "task_memo_read", "task_status",
    "team_list", "team_briefing", "team_status",
    "agent_list", "agent_template_list", "agent_activity_query",
    "memory_search", "team_knowledge", "os_health_check",
    "event_list", "cache_stats", "agent_heartbeat",
    "watchdog_check", "verify_completion",
    "decision_log", "prompt_version_list", "prompt_effectiveness",
    "project_list", "meeting_list", "meeting_read_messages",
    "Read", "SendMessage",
})

PLAN_CLASS_TOOLS: frozenset[str] = frozenset({
    "pattern_search", "what_if_analysis", "task_decompose",
    "meeting_template_list", "ecosystem_recipes",
    "agent_template_recommend", "find_skill",
    "briefing_resolve", "briefing_dismiss",
    "project_summary", "taskwall_view", "task_list_project",
    "loop_status", "report_list", "report_read",
    "briefing_list", "phase_list",
    "Glob", "Grep", "WebFetch", "WebSearch", "TeamCreate",
})

EXECUTE_CLASS_TOOLS: frozenset[str] = frozenset({
    "task_run", "task_create", "task_update", "task_auto_match",
    "task_subtasks", "cache_clear", "channel_send", "channel_read",
    "channel_mentions", "agent_trust_update", "report_save",
    "file_lock_acquire", "file_lock_release", "file_lock_check",
    "file_lock_list", "failure_analysis", "pattern_record",
    "loop_start", "loop_pause", "loop_resume", "loop_next_task",
    "loop_advance", "loop_review", "error_budget_update",
    "project_update", "dismiss_project_registration",
    "meeting_attendance_check", "meeting_update", "debate_code_review",
    "git_status_check",
    "meeting_create", "meeting_send_message", "meeting_conclude",
    "debate_start",
    "Edit", "Write", "Bash", "Agent", "TaskCreate",
    "EnterWorktree", "ExitWorktree", "NotebookEdit",
    "Glob", "Grep", "WebFetch",
})

VERIFY_CLASS_TOOLS: frozenset[str] = frozenset({
    "task_replay", "task_compare", "task_execution_trace",
    "diagnose_task_failure", "error_budget_status",
    "verify_run",
    "Bash",
})

REQUIRES_USER_ACK: frozenset[str] = frozenset({
    "agent_register", "team_create", "team_delete", "team_close",
    "project_create", "project_delete", "phase_create",
    "scheduler_create", "scheduler_delete", "scheduler_pause",
    "git_auto_commit", "git_create_pr",
    "send_notification", "cross_project_send",
    "TeamCreate",
})

STAGE_CLASS_WHITELIST: dict[str, frozenset[str]] = {
    "Plan": ALL_EXEMPT | PLAN_CLASS_TOOLS,
    "Execute": ALL_EXEMPT | EXECUTE_CLASS_TOOLS,
    "Verify": ALL_EXEMPT | VERIFY_CLASS_TOOLS,
}

# ============================================================
# Config
# ============================================================

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os", "api_port.txt")
_SUPERVISOR_STATE_FILE = os.path.join(
    os.path.expanduser("~"), ".claude", "data", "ai-team-os", "supervisor-state.json"
)
_API_TIMEOUT = 2
_PROJECT_CACHE_TTL = 300


def _get_api_url() -> str:
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"


def _load_supervisor_state() -> dict:
    try:
        with open(_SUPERVISOR_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ============================================================
# Tool classification helpers
# ============================================================

def _normalize(tool_name: str) -> str:
    return tool_name.removeprefix("mcp__ai-team-os__")


def _is_allowed(tool_name: str, stage_class: str) -> bool:
    name = _normalize(tool_name)
    whitelist = STAGE_CLASS_WHITELIST.get(stage_class, frozenset())
    return name in whitelist


def _requires_user_ack(tool_name: str) -> bool:
    return _normalize(tool_name) in REQUIRES_USER_ACK


# ============================================================
# API helpers
# ============================================================

def _resolve_project_id(cwd: str) -> str | None:
    """Resolve project_id from cwd via API, with supervisor-state cache."""
    state = _load_supervisor_state()
    cached = state.get("cached_project_id")
    cached_at = state.get("cached_project_id_at", 0)
    if cached and (time.time() - cached_at) < _PROJECT_CACHE_TTL:
        return cached

    api_url = _get_api_url()
    try:
        req = urllib.request.Request(
            f"{api_url}/api/context/resolve",
            data=json.dumps({"cwd": cwd, "auto_create": False}).encode(),  # 归属铁律：绝不自动立项
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("project_id") or (data.get("project") or {}).get("id")
    except Exception:
        return None


def _find_current_autopilot_task(project_id: str) -> dict | None:
    """Return the first autopilot_active=True task in the project, or None.

    Uses task-wall endpoint and filters locally for autopilot_active=True.
    """
    api_url = _get_api_url()
    try:
        req = urllib.request.Request(
            f"{api_url}/api/projects/{project_id}/task-wall",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tasks = data.get("data") or data.get("items") or data.get("tasks") or []
        for task in tasks:
            config = task.get("config") or {}
            pipeline = config.get("pipeline") or {}
            if pipeline.get("autopilot_active"):
                return task
    except Exception as e:
        logger.warning("_find_current_autopilot_task failed: %s", e)
        return None
    return None


def _get_pipeline_state(task_id: str) -> dict | None:
    """Fetch pipeline state for a task from the v2 status endpoint."""
    api_url = _get_api_url()
    try:
        req = urllib.request.Request(
            f"{api_url}/api/tasks/{task_id}/pipeline/v2",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("success"):
            return data.get("data") or {}
    except Exception as e:
        logger.warning("_get_pipeline_state failed for %s: %s", task_id, e)
    return None


def _create_briefing(project_id: str, title: str, description: str) -> None:
    """Create a leader briefing for user acknowledgement (fire-and-forget)."""
    api_url = _get_api_url()
    try:
        payload = json.dumps({
            "title": title,
            "description": description,
            "urgency": "high",
            "project_id": project_id,
        }).encode()
        req = urllib.request.Request(
            f"{api_url}/api/leader-briefings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            resp.read()
    except Exception as e:
        logger.warning("_create_briefing failed: %s", e)


def _inline_evaluate(
    task_id: str,
    current_stage: str,
    template: str,
    stage_started_at_iso: str | None,
    tool_name: str,
    tool_output: dict,
    project_root: str,
) -> tuple[str, str | None, str]:
    """Inline evaluator for the hook process (self-contained, no aiteam imports).

    Returns (decision_str, target_stage_or_None, reason).
    decision_str: "advance" | "suggest" | "fall_back" | "no_decision"

    Implements §6.1 objective rules directly to keep the hook standalone.
    """
    import re
    from datetime import datetime, timedelta

    _pass_patterns = [
        re.compile(r'\b\d+ passed\b', re.IGNORECASE),
        re.compile(r'\ball tests passed\b', re.IGNORECASE),
        re.compile(r'\bOK\s*\(\d+ tests\)', re.IGNORECASE),
        re.compile(r'✓'),
        re.compile(r'\bPASS\b'),
        re.compile(r'\b100%\s+passed\b', re.IGNORECASE),
        re.compile(r'\bbuild\s+success(?:ful)?\b', re.IGNORECASE),
        re.compile(r'\bAll \d+ tests? (?:pass|pass(?:ed)?)\b', re.IGNORECASE),
        re.compile(r'\bTests run: \d+.*Failures: 0.*Errors: 0\b', re.IGNORECASE),
        re.compile(r'\bno failures\b', re.IGNORECASE),
    ]
    _fail_patterns = [
        re.compile(r'\bfailed\b', re.IGNORECASE),
        re.compile(r'\bfailure\b', re.IGNORECASE),
        re.compile(r'[Ee]rror\b'),
        re.compile(r'\bTraceback\b'),
        re.compile(r'\bException\b'),
        re.compile(r'\bFAIL\b'),
        re.compile(r'\bERROR\b'),
        re.compile(r'\bAborted\b', re.IGNORECASE),
    ]

    lifecycle: dict[str, list[str]] = {
        "feature":   ["research", "meeting", "decompose", "implement", "test", "review", "retest"],
        "hotfix":    ["diagnose", "fix", "test"],
        "quick-fix": ["fix", "test"],
        "research":  ["research", "report"],
        "spike":     ["research", "implement"],
        "refactor":  ["decompose", "implement", "test", "review"],
        "debate":    ["meeting", "decision"],
    }

    def _aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    tool_bare = tool_name.removeprefix("mcp__ai-team-os__")
    stages = lifecycle.get(template, [])

    # implement → test: src/ mtime check on any Edit/Write tool call
    if current_stage == "implement" and tool_bare in ("Edit", "Write"):
        if stage_started_at_iso:
            try:
                started = _aware(datetime.fromisoformat(stage_started_at_iso))
                threshold = started + timedelta(seconds=2)
                src_root = os.path.join(project_root, "src")
                if os.path.isdir(src_root):
                    for dp, _, fnames in os.walk(src_root):
                        for fn in fnames:
                            if fn.endswith(".py"):
                                try:
                                    mt = os.path.getmtime(os.path.join(dp, fn))
                                    if datetime.fromtimestamp(mt, tz=UTC) > threshold:
                                        return ("advance", "test", "src/ modified after stage start")
                                except OSError:
                                    pass
            except Exception as e:
                logger.warning("_inline_evaluate implement mtime check failed: %s", e)
        return ("no_decision", None, "implement: no qualifying src/ modification")

    # test / retest → done or fix: inspect last Bash output
    # CC PostToolUse does not emit exit_code; use stdout/stderr heuristics only
    if current_stage in ("test", "retest") and tool_bare == "Bash":
        stdout: str = (tool_output or {}).get("stdout", "") or ""
        stderr: str = (tool_output or {}).get("stderr", "") or ""
        combined = stdout + "\n" + stderr

        if any(p.search(combined) for p in _pass_patterns):
            terminal = stages[-1] if stages else "done"
            return ("advance", terminal, f"bash pass signal → {terminal}")

        if any(p.search(combined) for p in _fail_patterns):
            if "fix" in stages:
                return ("fall_back", "fix", "bash fail signal → fix")
            return ("fall_back", None, "bash fail signal, no fix stage")

        return ("no_decision", None, "bash: no pass/fail signal matched")

    return ("no_decision", None, f"stage '{current_stage}' has no inline objective rule for {tool_bare}")


# ============================================================
# Main
# ============================================================

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.exit(0)

    hook_type = payload.get("hook_event_name", "PreToolUse")
    tool_name = payload.get("tool_name", "")
    cwd = payload.get("cwd") or os.getcwd()

    # Resolve project
    project_id = _resolve_project_id(cwd)
    if not project_id:
        sys.exit(0)

    # Find an autopilot task in this project
    task = _find_current_autopilot_task(project_id)
    if not task:
        sys.exit(0)

    task_id = task.get("id", "")
    pipeline_state = _get_pipeline_state(task_id)
    if not pipeline_state or not pipeline_state.get("autopilot_active"):
        sys.exit(0)

    stage_class = pipeline_state.get("current_stage_class", "")
    current_stage = pipeline_state.get("current_stage", "?")

    # ----------------------------------------------------------------
    # PostToolUse: fact-stream auto-advance (Phase 2)
    # ----------------------------------------------------------------
    if hook_type == "PostToolUse":
        tool_output = payload.get("tool_response") or {}
        template = pipeline_state.get("template", "")
        stage_started_at_iso = pipeline_state.get("stage_started_at")
        project_root = os.environ.get("AITEAM_PROJECT_ROOT", cwd)

        decision, target, reason = _inline_evaluate(
            task_id=task_id,
            current_stage=current_stage,
            template=template,
            stage_started_at_iso=stage_started_at_iso,
            tool_name=tool_name,
            tool_output=tool_output,
            project_root=project_root,
        )

        if decision in ("advance", "fall_back"):
            # pipeline 退役 Phase1（设计文档 §7）：停用事实流自动推进——不再 POST
            # pipeline/v2/advance 变更服务端状态，降级为建议输出。回滚见 git 历史。
            sys.stderr.write(
                f"[OS GATE] (retired) {decision} suggestion for task {task_id}: "
                f"{current_stage} → {target} ({reason}) — pipeline 退役期，不再自动推进\n"
            )
        elif decision == "suggest":
            sys.stderr.write(
                f"[OS GATE] Suggest advance for task {task_id} from {current_stage}: {reason}\n"
            )
        # no_decision: silent

        sys.exit(0)  # PostToolUse never blocks (exit 0)

    # ----------------------------------------------------------------
    # PreToolUse: whitelist gate (existing logic)
    # ----------------------------------------------------------------

    # Escalation trust root — these tools can only be invoked by user/Leader, not sub-agents.
    escalation_tools: frozenset[str] = frozenset({
        "pipeline_advance",  # only when force=True
        "briefing_resolve",
        "briefing_dismiss",
    })

    _subagent_sessions_dir = Path(
        os.path.expanduser("~/.claude/data/ai-team-os/subagent_sessions")
    )
    session_id = payload.get("session_id", "")
    is_subagent = bool(
        session_id and (_subagent_sessions_dir / session_id).exists()
    )

    tool_name_norm = _normalize(tool_name)
    if is_subagent and tool_name_norm in escalation_tools:
        tool_input = payload.get("tool_input", {}) or {}
        if tool_name_norm == "pipeline_advance" and not tool_input.get("force"):
            pass  # regular (non-forced) advance is allowed from sub-agents
        else:
            sys.stderr.write(
                f"[OS GATE] {tool_name_norm} 在 sub-agent session 不允许"
                f"（escalation 必须用户/Leader 操作）\n"
            )
            sys.exit(2)

    # High-risk tools: create briefing and block in autonomous mode
    if _requires_user_ack(tool_name):
        _create_briefing(
            project_id=project_id,
            title=f"[Autopilot] 工具 {_normalize(tool_name)} 需要用户确认",
            description=(
                f"当前任务 {task_id} 处于 autopilot 模式（阶段 {current_stage}），"
                f"工具 {tool_name} 属于高风险操作，需要用户批准后才能继续。"
            ),
        )
        sys.stderr.write(
            f"[OS GATE] 工具 {tool_name} 需要用户确认，已创建 briefing，暂时阻断。\n"
        )
        sys.exit(2)

    # Stage class whitelist check
    if not _is_allowed(tool_name, stage_class):
        sys.stderr.write(
            f"[OS GATE] 当前任务处于 {current_stage} 阶段（{stage_class} 类），"
            f"不允许使用 {tool_name}。"
            f"如需使用，请先 pipeline_advance 到正确阶段。\n"
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
