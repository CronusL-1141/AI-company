"""Project and phase management MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call, _current_cwd, _resolve_project_id


def _dismiss_registration_notice(cwd: str) -> str:
    """Dismiss the folder's E07 notice in the ledger; queue it locally when the API is away.

    Without this the notice stayed active after the user said no: the
    Dashboard banner and /os-doctor kept asking about a folder the user had
    already declined.
    """
    import time
    import urllib.parse
    import uuid

    from aiteam.mcp.tools.infra import _append_local_record, _caller_host
    from aiteam.services.notices.detectors.registration import notice_key, real_dir

    key = notice_key(real_dir(cwd))
    answer = _api_call("POST", f"/api/notices/{urllib.parse.quote(key, safe=':')}/dismiss")
    if answer.get("key") == key:
        return "dismissed"
    if str(answer.get("error", "")).startswith("HTTP 404"):
        return "none"
    record = {
        "uuid": uuid.uuid4().hex,
        "kind": "notice_dismiss",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts": round(time.time(), 3),
        "source": "dismiss_project_registration",
        "key": key,
    }
    return "queued" if _append_local_record(_caller_host(), record) else "failed"


def register(mcp):
    """Register all project-related MCP tools."""

    @mcp.tool()
    def project_create(
        name: str,
        description: str = "",
        root_path: str = "",
    ) -> dict[str, Any]:
        """Create a new project with a default Phase automatically created.

        The OS never registers a directory on its own. For an unregistered
        working directory the session-start briefing asks the user; call this
        when the user agrees to register, and dismiss_project_registration
        when they decline. The project must be for the current session's
        working directory: unrelated directories and ancestors of the home
        directory are rejected.

        Args:
            name: Project name
            description: Project description
            root_path: Project root directory path; must be the current working
                directory (empty = use it)

        Returns:
            Created project info including project_id
        """
        import os

        cwd = _current_cwd().replace("\\", "/")
        if root_path:
            given = root_path.replace("\\", "/").rstrip("/")
            cwd_norm = cwd.rstrip("/")
            g = given.lower()
            c = cwd_norm.lower()
            # 分隔符边界（照抄 _base._init_session_project 的写法）：裸 startswith
            # 是假前缀，root_path='/Users/cron' 能骗过 cwd='/Users/cronus/...'，
            # 于是可以在别人的目录上立项，冲撞归属铁律。
            if not (c == g or c.startswith(g + "/")):
                return {
                    "success": False,
                    "error": (
                        f"root_path '{root_path}' does not match current "
                        f"working directory '{cwd}'. Projects must be "
                        f"created for the current session directory; "
                        f"use project_list to find existing projects."
                    ),
                    "_recovery": "Use project_list to find existing projects.",
                }
            # 家目录的严格祖先（'/'、'/Users' 这类）永远不是合法项目根：注册后它会
            # 按前缀认领此后每一个未注册目录，一个项目吞掉整台机器。
            home = os.path.expanduser("~").replace("\\", "/").rstrip("/").lower()
            if home and g != home and (home == g or home.startswith(g + "/")):
                return {
                    "success": False,
                    "error": (
                        f"root_path '{root_path}' 是家目录的祖先目录，不能作为项目根"
                        f"（它会把此后每个未注册目录都前缀认领走）。"
                        f"请用本会话真实的工作目录 '{cwd}'。"
                    ),
                    "_recovery": "Use the session cwd as root_path, or project_list to find it.",
                }

        result = _api_call(
            "POST",
            "/api/projects",
            {
                "name": name,
                "description": description,
                "root_path": root_path or cwd,
            },
        )
        # 注册转正提示（记忆隔离升级路径）：该目录在未注册期写入的方向记忆落在
        # 目录指纹临时桶("dir:<sha1>")；转正后这些条目可迁入项目桶。本批不做自动
        # 收编，仅提示，如需迁移用 memory_reconcile 的 promote/merge 手动处理。
        if isinstance(result, dict) and result.get("success") is not False:
            result.setdefault(
                "hint",
                "该目录此前未注册期写入的临时桶记忆（dir:<指纹>，如有）可迁入本项目桶——"
                "本批不自动收编，如需迁移请用 memory_reconcile 手动处理。",
            )
        return result

    @mcp.tool()
    def project_list() -> dict[str, Any]:
        """List all projects in the system.

        Returns:
            projects: List of all projects with id, name, description, root_path, etc.
        """
        return _api_call("GET", "/api/projects")

    @mcp.tool()
    def project_update(
        project_id: str,
        name: str = "",
        description: str = "",
        root_path: str = "",
    ) -> dict[str, Any]:
        """Update a project's name, description, or root_path.

        Args:
            project_id: Project ID to update
            name: New project name (optional)
            description: New description (optional)
            root_path: New root directory (optional). Must be an existing
                absolute directory that is not the home directory, one of its
                ancestors, or another project's root.

        Returns:
            Updated project info
        """
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        if description:
            body["description"] = description
        if root_path:
            body["root_path"] = root_path
        if not body:
            return {"success": False, "error": "No fields to update"}
        return _api_call("PUT", f"/api/projects/{project_id}", body)

    @mcp.tool()
    def project_delete(project_id: str) -> dict[str, Any]:
        """Delete a project and everything filed under it. Irreversible.

        One transaction removes the project's tasks and task memos, its teams,
        meetings and meeting messages, phases, reports, leader briefings,
        project- and team-scoped memories (including the project's
        direction-layer entries), cross-project messages, and the teams' events.
        Agent rows (they carry the token attribution), workflow run archives and
        channel messages are kept.

        Args:
            project_id: Project ID to delete (exact id; names are not resolved)

        Returns:
            Deletion result
        """
        return _api_call("DELETE", f"/api/projects/{project_id}")

    @mcp.tool()
    def project_summary(project_id: str = "") -> dict[str, Any]:
        """Get a quick project summary: status (active/inactive), teams, top tasks.

        Args:
            project_id: Project ID (optional, auto-uses active project if empty)

        Returns:
            Project status, active team count, pending/running task counts, top 3 tasks
        """
        resolved = _resolve_project_id(project_id)
        if not resolved:
            return {"success": False, "error": "No project context"}
        return _api_call("GET", f"/api/projects/{resolved}/summary")

    @mcp.tool()
    def dismiss_project_registration(cwd: str = "") -> dict[str, Any]:
        """Mark current cwd as dismissed for project registration — won't ask again.

        The session-start briefing asks whether to register an unregistered
        working directory; after this call it stops asking for that directory,
        and its "not a registered project" notice is dismissed on the Dashboard
        too. The choice is stored in a local file (~/.claude/data/ai-team-os/
        dismissed_projects.json) that no tool reverses. No project is created,
        changed, or deleted.

        Args:
            cwd: Directory path to dismiss (empty = use current cwd)

        Returns:
            Status dict with dismissed_count, normalized cwd and ``notice``:
            dismissed / none (no open notice) / queued (API unreachable; applied
            on the next hook fetch) / failed
        """
        from aiteam.services.notices.detectors.registration import dismiss_dir_sync

        if not cwd:
            cwd = _current_cwd()
        # Same writer as the Dashboard's "skip" on the unregistered-folder notice.
        result = dismiss_dir_sync(cwd)
        result["notice"] = _dismiss_registration_notice(cwd)
        return result
