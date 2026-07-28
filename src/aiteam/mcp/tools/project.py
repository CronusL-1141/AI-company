"""Project and phase management MCP tools."""

from __future__ import annotations

from typing import Any

from aiteam.clock import utc_now
from aiteam.mcp._base import _api_call, _resolve_project_id


def register(mcp):
    """Register all project-related MCP tools."""

    @mcp.tool()
    def project_create(
        name: str,
        description: str = "",
        root_path: str = "",
    ) -> dict[str, Any]:
        """Create a new project with a default Phase automatically created.

        ⚠️ IMPORTANT: Projects are automatically registered by the OS when
        a CC session starts. You should NOT manually create projects unless
        the auto-registered project is missing. The root_path MUST match
        the current CC session's working directory — do NOT create projects
        pointing to other directories.

        Args:
            name: Project name
            description: Project description
            root_path: Project root directory path (must match current cwd)

        Returns:
            Created project info including project_id
        """
        import os

        cwd = os.getcwd().replace("\\", "/")
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
                        f"created for the current session directory. "
                        f"The OS auto-registers projects on session start "
                        f"— use project_list to find existing projects."
                    ),
                    "_recovery": "Use project_list to find auto-registered projects.",
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
            root_path: New root directory path (optional)

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
        """Delete a project.

        Args:
            project_id: Project ID to delete

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

        Args:
            cwd: Directory path to dismiss (empty = use current cwd)

        Returns:
            Status dict with dismissed_count and normalized cwd
        """
        import json as _json
        import os as _os
        from pathlib import Path as _Path

        if not cwd:
            cwd = _os.getcwd()
        cwd_norm = str(_Path(cwd).resolve()).replace("\\", "/").lower()

        dismissed_file = _Path.home() / ".claude" / "data" / "ai-team-os" / "dismissed_projects.json"
        dismissed_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            data = (
                _json.loads(dismissed_file.read_text(encoding="utf-8"))
                if dismissed_file.exists()
                else {"dismissed": []}
            )
        except Exception:
            data = {"dismissed": []}

        if cwd_norm not in data["dismissed"]:
            data["dismissed"].append(cwd_norm)
        data["updated_at"] = utc_now().isoformat()

        dismissed_file.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "dismissed_count": len(data["dismissed"]), "cwd": cwd_norm}
