"""Infrastructure and OS-level MCP tools."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from aiteam.mcp._base import API_URL, _api_call
from aiteam.mcp.tools.views import (
    EVENT_HINT,
    FIELDS_ERROR,
    compact_event_row,
    resolve_view,
)


def _restart_pid_alive(pid: int) -> bool:
    """Return True if *pid* refers to a live (non-zombie) process.

    POSIX 上优雅停机成功后子进程可能滞留为 defunct 僵尸（父 MCP 尚未收尸）：
    僵尸不占端口、也永远不会再"退出"，必须视为已死——否则重启守卫会把成功的
    停机误报成 shutdown_timeout（2026-07-06 巡检实录）。Windows 无僵尸语义。
    Prefers psutil and falls back to os.kill(pid, 0) + ps state check.
    """
    try:
        import psutil

        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
    except ImportError:
        pass
    try:
        os.kill(pid, 0)  # signal 0 = existence check only
    except (ProcessLookupError, OSError, SystemError):
        # OSError/SystemError (WinError 87) on Windows when the process is gone
        return False
    except PermissionError:
        # Process exists but is owned by another user — still "alive"
        return True
    if sys.platform != "win32":
        import subprocess

        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "state="],
                text=True, stderr=subprocess.DEVNULL, timeout=3,
            ).strip()
            if out.startswith("Z"):
                return False  # defunct 僵尸 = 已死
        except Exception:  # noqa: BLE001 — ps 不可用时保守视为存活
            pass
    return True


def _restart_local_get(path: str, port: int, timeout: float = 3.0) -> dict[str, Any] | None:
    """GET a localhost API path directly (no project headers), returning JSON or None.

    Used by os_restart_api for raw health/version probes that must not be subject to
    project-scoping headers. Returns None on any connection/parse failure.
    """
    url = f"http://localhost:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _restart_local_post(path: str, port: int, timeout: float = 5.0) -> dict[str, Any] | None:
    """POST (empty body) to a localhost API path directly, returning JSON or None."""
    url = f"http://localhost:{port}{path}"
    req = urllib.request.Request(
        url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _restart_spawn_on_port(autostart, port: int) -> dict[str, Any]:
    """Spawn a fresh uvicorn API subprocess on *port*, reusing _autostart bookkeeping.

    Mirrors the spawn step of _autostart._ensure_api_running_locked (same uvicorn
    factory invocation), but pinned to the caller-supplied port and without any
    port-drift fallback — the os_restart_api guards already ensured the port is free.

    Updates the shared PID file and port file so other MCP sessions discover the new
    process. Returns {success, new_pid} or {success: False, error, detail}.
    """
    import subprocess
    import sys

    # Detach fully from the MCP server's stdio. Spawning from inside an MCP
    # *tool call* (unlike _autostart's init-time spawn) inherits the live MCP
    # stdio pipes; an inherited stdin/stderr handle made the child hang before
    # imports (observed: stuck at 9MB forever). stderr goes to the SAME
    # persistent log as _autostart — a tmpdir file survives neither reboots
    # nor the selfcheck loop's gaze (it only watches api-stderr.log).
    from aiteam.mcp._autostart import _API_STDERR_LOG

    stderr_log = _API_STDERR_LOG
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    try:
        with open(stderr_log, "ab") as log_fh:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "aiteam.api.app:create_app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--factory",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_fh,
                close_fds=True,
                creationflags=creationflags,
            )
    except Exception as exc:
        return {
            "success": False,
            "error": "spawn_failed",
            "detail": f"无法启动 uvicorn 子进程: {exc}",
        }

    autostart._write_pid_file(proc.pid)
    autostart._save_api_port(port)
    return {"success": True, "new_pid": proc.pid}


def register(mcp):
    """Register all infrastructure MCP tools."""

    @mcp.tool()
    def context_resolve() -> dict[str, Any]:
        """Get the current active OS context — active project, active team, member list, loop status.

        This is the infrastructure for all simplified operations. A single call returns
        the complete context of the current working environment, allowing Leader or other
        tools to auto-fill parameters like project_id, team_id, etc.

        Returns:
            Context dict containing project / team / agents / loop
        """
        result: dict[str, Any] = {"project": None, "team": None, "agents": [], "loop": None}

        try:
            projects_data = _api_call("GET", "/api/projects")
            projects = projects_data.get("data", [])
            if projects:
                cwd = os.getcwd().replace("\\", "/").rstrip("/").lower()
                # Longest-prefix match — pick the most specific project
                best_p = None
                best_len = -1
                for p in projects:
                    rp = (p.get("root_path") or "").replace("\\", "/").rstrip("/").lower()
                    if rp and (cwd == rp or cwd.startswith(rp + "/")) and len(rp) > best_len:
                        best_p = p
                        best_len = len(rp)
                if best_p is not None:
                    result["project"] = {"id": best_p["id"], "name": best_p.get("name", "")}

            # v1.5.2 fix: project-aware active team resolution.
            # Filter teams by current project_id so a Leader in cwd=A doesn't pick up
            # another project B's active team (root cause of 2026-05-08 cross-project agent dispatch).
            current_project_id = result["project"]["id"] if result["project"] else None
            teams_data = _api_call("GET", "/api/teams")
            all_active = [t for t in teams_data.get("data", []) if t.get("status") == "active"]
            if current_project_id:
                project_teams = [t for t in all_active if t.get("project_id") == current_project_id]
            else:
                project_teams = []  # No project resolved → no team (avoid cross-project leak)
            if project_teams:
                team = project_teams[0]
                result["team"] = {"id": team["id"], "name": team["name"]}
                agents_data = _api_call("GET", f"/api/teams/{team['id']}/agents")
                result["agents"] = [
                    {"name": a["name"], "status": a["status"], "role": a.get("role", "")}
                    for a in agents_data.get("data", [])
                ]

            if result["team"]:
                loop_data = _api_call("GET", f"/api/teams/{result['team']['id']}/loop/status")
                if loop_data.get("success") is not False:
                    result["loop"] = loop_data.get("data") or loop_data

        except Exception as e:
            result["error"] = str(e)

        return result

    @mcp.tool()
    def os_health_check() -> dict[str, Any]:
        """Check the health status of the AI Team OS API service.

        Verifies the API service is running normally by accessing the team list endpoint.

        Returns:
            Health status info including API reachability and team count
        """
        result = _api_call("GET", "/api/teams")
        if result.get("success") is False:
            return {
                "status": "unhealthy",
                "api_url": API_URL,
                "error": result.get("error", "未知错误"),
                "hint": result.get("hint", "请确保 FastAPI 服务已启动: aiteam serve"),
            }
        return {
            "status": "healthy",
            "api_url": API_URL,
            "teams_count": result.get("total", 0),
        }

    @mcp.tool()
    def os_restart_api(force: bool = False) -> dict[str, Any]:
        """Restart the AI Team OS FastAPI process safely (standardized restart flow).

        Use this after backend code changes to pick up the new version without
        manually killing processes. The flow has three safety guards:

        1. Busy-agent guard — refuses to restart while any agent is working
           (status=busy) unless force=True.
        2. Port-pin guard — only ever restarts on the ORIGINAL port (default 8000,
           read from api_port.txt). If that port is held by an unrelated process it
           aborts rather than drifting to a random port.
        3. Dead-before-spawn guard — waits until the old process has fully exited and
           released the port before spawning the new one; never spawns on a timeout.

        If the API is already down, steps 2-4 are skipped and this becomes a plain
        "start" of the API on its configured port.

        Args:
            force: Bypass the busy-agent guard and restart even while agents work.

        Returns:
            On success: {success, old_version, new_version, old_pid, new_pid, elapsed_ms}.
            On refusal/failure: {success: False, error, detail}.
        """
        from aiteam.mcp import _autostart

        t0 = time.monotonic()
        port = _autostart._get_api_port()

        # --- 1. Probe current API + read old version (raw localhost, no project headers) ---
        health = _restart_local_get("/api/health", port, timeout=2.0)
        api_was_up = health is not None
        old_version = health.get("version") if health else None
        old_pid = _autostart._read_pid_file()  # None if stale/missing/dead

        if api_was_up:
            # --- 2. Guard: refuse while agents are busy (unless force) ---
            if not force:
                busy_total = 0
                teams = _restart_local_get("/api/teams", port, timeout=3.0)
                for team in (teams or {}).get("data", []):
                    if team.get("status") != "active":
                        continue
                    agents = _restart_local_get(
                        f"/api/teams/{team['id']}/agents?limit=200", port, timeout=3.0
                    )
                    for agent in (agents or {}).get("data", []):
                        if agent.get("status") == "busy":
                            busy_total += 1
                if busy_total > 0:
                    return {
                        "success": False,
                        "error": "busy_agents",
                        "detail": f"{busy_total} 个 agent 工作中，确需重启传 force=true",
                    }

            # --- 3. Request graceful shutdown ---
            resp = _restart_local_post("/api/system/shutdown", port, timeout=5.0)
            if resp is None or not resp.get("success"):
                return {
                    "success": False,
                    "error": "shutdown_failed",
                    "detail": "POST /api/system/shutdown 未成功返回，已中止重启",
                }

            # --- 4. Guard: wait for old process to die AND port to release (≤10s) ---
            # Iteration cap: even if the monotonic clock misbehaves (frozen/mocked),
            # this loop must terminate — a runaway here once ate 32GB via mock recording.
            deadline = time.monotonic() + 10.0
            _iters = 0
            while time.monotonic() < deadline and _iters < 200:
                _iters += 1
                pid_dead = old_pid is None or not _restart_pid_alive(old_pid)
                port_free = not _autostart._is_port_open(port=port)
                if pid_dead and port_free:
                    break
                time.sleep(0.3)
            else:
                still_alive = old_pid is not None and _restart_pid_alive(old_pid)
                return {
                    "success": False,
                    "error": "shutdown_timeout",
                    "detail": (
                        f"旧进程未在 10s 内退出/释放端口 {port} "
                        f"(pid={old_pid}, still_alive={still_alive})，未拉起新进程"
                    ),
                }
        else:
            # API already down — make sure the port isn't held by an unrelated process.
            if _autostart._is_port_open(port=port):
                return {
                    "success": False,
                    "error": "port_occupied",
                    "detail": f"端口 {port} 被无关进程占用，无法在原端口拉起，已中止",
                }

        # --- 5. Spawn fresh API on the ORIGINAL port (pinned, never drift) ---
        if _autostart._is_port_open(port=port):
            return {
                "success": False,
                "error": "port_occupied",
                "detail": f"端口 {port} 仍被占用，拒绝漂移到随机端口，已中止",
            }
        spawned = _restart_spawn_on_port(_autostart, port)
        if not spawned.get("success"):
            return spawned

        # --- 6. Poll new API health (≤15s) for new version ---
        # Iteration cap mirrors step 4 — terminate even under a frozen clock.
        new_version = None
        new_deadline = time.monotonic() + 15.0
        _iters = 0
        while time.monotonic() < new_deadline and _iters < 200:
            _iters += 1
            health = _restart_local_get("/api/health", port, timeout=2.0)
            if health is not None:
                new_version = health.get("version")
                break
            time.sleep(0.5)
        if new_version is None:
            return {
                "success": False,
                "error": "health_timeout",
                "detail": f"新进程在 15s 内未通过 /api/health（端口 {port}）",
                "new_pid": spawned.get("new_pid"),
            }

        return {
            "success": True,
            "old_version": old_version,
            "new_version": new_version,
            "old_pid": old_pid,
            "new_pid": spawned.get("new_pid"),
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }

    @mcp.tool()
    def os_report_issue(
        team_id: str,
        title: str,
        description: str = "",
        severity: str = "medium",
        category: str = "bug",
    ) -> dict[str, Any]:
        """Report an issue to the team. Issues are created as high-priority tasks, auto-tagged as issue type.

        Severity maps to task priority: critical->critical, high->high, medium->high, low->medium.

        Args:
            team_id: Team ID or name
            title: Issue title
            description: Detailed issue description
            severity: Severity level, one of "critical" / "high" / "medium" / "low"
            category: Issue category, e.g., "bug" / "performance" / "security" / "ux"

        Returns:
            Created Issue task info
        """
        return _api_call(
            "POST",
            f"/api/teams/{team_id}/issues",
            {
                "title": title,
                "description": description,
                "severity": severity,
                "category": category,
            },
        )

    @mcp.tool()
    def os_resolve_issue(issue_id: str, resolution: str) -> dict[str, Any]:
        """Mark an Issue as resolved with a resolution description.

        Updates the Issue status to resolved and records the resolution.
        The corresponding task is also marked as completed.

        Args:
            issue_id: Issue (task) ID
            resolution: Resolution description

        Returns:
            Updated Issue info
        """
        return _api_call(
            "PUT",
            f"/api/issues/{issue_id}/status",
            {
                "status": "resolved",
                "resolution": resolution,
            },
        )

    @mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
    def event_list(limit: int = 50, fields: str = "compact") -> dict[str, Any]:
        """List recent events in the system.

        Default response is a COMPACT projection (marked by view="compact" +
        hint — it is a trimmed view, NOT missing fields): each row keeps
        id/type/source/ts plus a one-line summary derived from the event
        payload. Use fields="all" for full payloads.

        Args:
            limit: Maximum number of events to return, default 50
            fields: "compact" (default, trimmed projection) / "all" (full rows)

        Returns:
            Event list with event type, source, timestamp and derived summary;
            compact view adds view + hint self-identification
        """
        view = resolve_view(fields)
        if view is None:
            return {"success": False, "error": FIELDS_ERROR}
        result = _api_call("GET", f"/api/events?limit={limit}")
        if view == "all" or not isinstance(result, dict) or "data" not in result:
            return result
        return {
            "success": result.get("success", True),
            "total": result.get("total"),
            "data": [compact_event_row(e) for e in result.get("data") or []],
            "view": "compact",
            "hint": EVENT_HINT,
        }

    @mcp.tool()
    def find_skill(
        task_description: str = "",
        level: int = 1,
        category: str = "",
        skill_id: str = "",
    ) -> dict[str, Any]:
        """Find ecosystem skills/plugins using a 3-layer progressive loading system.

        Layer 1 (quick recommend): Describe your task and get top 3-5 matching skills
            with one-line descriptions and install commands.
        Layer 2 (category browse): Browse all skills grouped by category
            (memory / code-quality / frontend / security / dev-workflow / etc.).
        Layer 3 (full detail): Get complete documentation for a single skill
            including features, OS complement relationship, and variants.

        Args:
            task_description: What you want to accomplish (used for level=1 matching).
                              Examples: "frontend ui design", "security audit web app",
                              "data science jupyter", "code review PR".
            level: Discovery depth — 1=quick (default), 2=category, 3=full detail.
            category: Category filter for level=2 (e.g., "frontend", "security").
                      Empty string returns all categories.
            skill_id: Skill identifier for level=3 detail lookup
                      (e.g., "vibesec", "superpowers", "claude-mem").

        Returns:
            Dict with level info, results, and hints for deeper exploration.
        """
        from aiteam.mcp.skill_registry import (
            find_skill_category,
            find_skill_detail,
            find_skill_quick,
        )

        if level == 3:
            if not skill_id:
                return {
                    "error": "level=3 requires skill_id parameter.",
                    "hint": "Use level=1 with task_description to discover skill IDs first.",
                }
            return find_skill_detail(skill_id)

        if level == 2:
            return find_skill_category(category)

        if not task_description:
            return {
                "error": "level=1 requires task_description parameter.",
                "hint": "Describe what you want to do, e.g. 'build a secure REST API'.",
            }
        return find_skill_quick(task_description)

    @mcp.tool()
    def send_notification(message: str, urgency: str = "medium") -> dict[str, Any]:
        """Send a notification to the configured Slack webhook.

        Requires SLACK_WEBHOOK_URL to be configured via PUT /api/settings/webhook.

        Args:
            message: Notification message text.
            urgency: Urgency level — "low", "medium" (default), or "high".

        Returns:
            Result dict with ok/error fields.
        """
        return _api_call(
            "POST",
            "/api/settings/webhook/send",
            {"message": message, "urgency": urgency},
        )

    @mcp.tool()
    def ecosystem_recipes(recipe_id: str = "") -> dict[str, Any]:
        """List available ecosystem integration recipes for combining AI Team OS with external tools.

        Recipes describe how to integrate external MCP servers (GitHub, Slack, Linear, etc.)
        with AI Team OS workflows. Each recipe includes: recommended MCP server, install config,
        and concrete collaboration scenarios with AI Team OS tools.

        Args:
            recipe_id: Optional recipe identifier to get details for a specific recipe.
                       Leave empty to list all available recipes.
                       Valid IDs: "github", "slack", "linear", "fullstack-team".

        Returns:
            Dict with recipe list (overview) or single recipe detail.
        """
        recipes = {
            "github": {
                "id": "github",
                "name": "GitHub 集成",
                "mcp_server": "@modelcontextprotocol/server-github",
                "oneliner": "Code management, PR review, and Issue tracking with AI Team OS orchestration",
                "install_hint": 'npx -y @modelcontextprotocol/server-github (set GITHUB_PERSONAL_ACCESS_TOKEN)',
                "scenarios": [
                    "Pipeline deploy stage -> git_auto_commit + git_create_pr",
                    "Code review -> debate_code_review + GitHub PR comments",
                    "Issue tracking -> GitHub Issue <-> AI Team OS task wall sync",
                ],
                "os_tools_used": [
                    "git_auto_commit", "git_create_pr", "git_status_check",
                    "debate_code_review", "task_create", "task_update",
                ],
            },
            "slack": {
                "id": "slack",
                "name": "Slack 集成",
                "mcp_server": "@modelcontextprotocol/server-slack",
                "oneliner": "Team notifications, alerts, and standup summaries via Slack channels",
                "install_hint": 'npx -y @modelcontextprotocol/server-slack (set SLACK_BOT_TOKEN, SLACK_TEAM_ID)',
                "scenarios": [
                    "Leader briefing -> team_briefing + Slack channel push",
                    "Error budget RED -> error_budget_status alert to #ops-alerts",
                    "Daily standup -> taskwall_view summary to #standup",
                ],
                "os_tools_used": [
                    "team_briefing", "send_notification", "briefing_list",
                    "error_budget_status", "taskwall_view", "meeting_conclude",
                ],
            },
            "linear": {
                "id": "linear",
                "name": "Linear 集成",
                "mcp_server": "@modelcontextprotocol/server-linear",
                "oneliner": "Sync Linear Issues with AI Team OS tasks and map Sprints to Pipelines",
                "install_hint": 'npx -y @modelcontextprotocol/server-linear (set LINEAR_API_KEY)',
                "scenarios": [
                    "Linear Issue <-> AI Team OS task sync",
                    "Sprint stages -> Pipeline stages mapping (Backlog->plan, In Progress->develop, etc.)",
                    "Bi-directional status sync on task completion",
                ],
                "os_tools_used": [
                    "task_create", "task_update", "pipeline_create",
                    "pipeline_advance", "pipeline_status", "task_memo_add",
                ],
            },
            "fullstack-team": {
                "id": "fullstack-team",
                "name": "全栈开发团队模板",
                "mcp_server": "AI Team OS + GitHub + Slack (combo)",
                "oneliner": "Pre-configured fullstack team with frontend, backend, QA, and DevOps roles",
                "install_hint": "See docs/ecosystem-recipes.md for full .mcp.json config",
                "scenarios": [
                    "Sprint start -> sync GitHub Issues to task wall",
                    "Dev phase -> frontend + backend + VibeSec security scan",
                    "Review phase -> debate_code_review + GitHub PR + E2E tests",
                    "Deploy phase -> Docker build + git_auto_commit + pipeline_advance",
                    "Retrospective -> loop_review + team_briefing -> Slack push",
                ],
                "os_tools_used": [
                    "project_create", "team_create", "agent_register",
                    "loop_start", "taskwall_view", "debate_code_review",
                    "git_auto_commit", "git_create_pr", "pipeline_advance",
                    "loop_review", "team_briefing", "meeting_conclude",
                ],
                "recommended_skills": ["Superpowers", "VibeSec", "Frontend-Design"],
            },
        }

        if recipe_id:
            rid = recipe_id.lower().strip()
            recipe = recipes.get(rid)
            if recipe is None:
                return {
                    "error": f"Recipe '{recipe_id}' not found.",
                    "available_recipes": [
                        {"id": r["id"], "name": r["name"], "oneliner": r["oneliner"]}
                        for r in recipes.values()
                    ],
                }
            return {
                "recipe": recipe,
                "docs": "See docs/ecosystem-recipes.md for full setup guide with .mcp.json examples.",
            }

        return {
            "recipes": [
                {"id": r["id"], "name": r["name"], "oneliner": r["oneliner"]}
                for r in recipes.values()
            ],
            "hint": "Use ecosystem_recipes(recipe_id='github') for detailed setup info.",
            "docs": "See docs/ecosystem-recipes.md for complete integration guides.",
        }

    @mcp.tool()
    def cross_project_send(
        content: str,
        to_project_id: str = "",
        message_type: str = "notification",
        sender_name: str = "system",
    ) -> dict[str, Any]:
        """Send a message to another project (or broadcast to all).

        Messages are stored in the shared global DB so any project can read them.
        Requires PROJECT_DIR env var (set automatically by Claude Code via CLAUDE_PROJECT_DIR).

        Args:
            content: Message body text.
            to_project_id: Recipient's 12-char project ID. Leave empty to broadcast to all projects.
            message_type: One of "notification" / "request" / "response" / "broadcast".
            sender_name: Sender name shown in the recipient's inbox (default "system").

        Returns:
            Created cross-project message info including id, from_project_id, created_at.
        """
        payload: dict[str, Any] = {
            "content": content,
            "sender_name": sender_name,
            "message_type": message_type,
            "metadata": {},
        }
        if to_project_id:
            payload["to_project_id"] = to_project_id
        return _api_call("POST", "/api/cross-messages", payload)

    @mcp.tool()
    def cross_project_inbox(
        unread_only: bool = True,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Read the cross-project message inbox for the current project.

        Returns direct messages sent to this project plus any broadcasts.
        Requires PROJECT_DIR env var (set automatically by Claude Code via CLAUDE_PROJECT_DIR).

        Args:
            unread_only: If True (default), only return unread messages.
            limit: Maximum number of messages to return (default 20).

        Returns:
            Inbox message list sorted newest-first, plus unread_count.
        """
        params = urllib.parse.urlencode({"unread_only": str(unread_only).lower(), "limit": limit})
        inbox = _api_call("GET", f"/api/cross-messages?{params}")
        count = _api_call("GET", "/api/cross-messages/count")
        if isinstance(inbox, dict) and isinstance(count, dict):
            inbox["unread_count"] = count.get("data", 0)
        return inbox

    @mcp.tool()
    def model_config_get(usage_days: int = 7) -> dict[str, Any]:
        """Get model governance state: available models (auto-discovered from
        local CC transcripts — the models you actually used), the current
        default startup model (~/.claude/settings.json "model" key), and
        per-model workflow agent usage over the last N days (orchestration
        charter observability: how much fable vs opus the fleet burned).

        Args:
            usage_days: Aggregation window for usage stats (default 7, max 90)
        """
        avail = _api_call("GET", "/api/models/available")
        default = _api_call("GET", "/api/models/default")
        usage = _api_call("GET", f"/api/models/usage?days={usage_days}")
        return {
            "available": avail.get("data") if isinstance(avail, dict) else avail,
            "default": (default.get("data") or {}).get("model", "")
            if isinstance(default, dict)
            else "",
            "usage": usage.get("data") if isinstance(usage, dict) else usage,
        }

    @mcp.tool()
    def model_config_set(model: str) -> dict[str, Any]:
        """Set the default startup model for new CC sessions (writes the
        "model" key in ~/.claude/settings.json; empty string removes the key,
        restoring CC's own default). Takes effect on NEW sessions.

        Args:
            model: Full model ID (e.g. "claude-fable-5") or "" to reset.
        """
        return _api_call("PUT", "/api/models/default", {"model": model})
