"""Project and phase management MCP tools."""

from __future__ import annotations

from typing import Any

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
            # Reject if root_path doesn't match current working directory
            if given.lower() != cwd_norm.lower() and not cwd_norm.lower().startswith(given.lower()):
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
    def phase_create(
        project_id: str,
        name: str,
        description: str = "",
        order: int = 0,
    ) -> dict[str, Any]:
        """Create a new development phase in a project.

        Args:
            project_id: Project ID
            name: Phase name
            description: Phase description
            order: Sort order, default 0

        Returns:
            Created phase info including phase_id
        """
        return _api_call(
            "POST",
            f"/api/projects/{project_id}/phases",
            {
                "name": name,
                "description": description,
                "order": order,
            },
        )

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
        from datetime import datetime
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
        data["updated_at"] = datetime.now().isoformat()

        dismissed_file.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "dismissed_count": len(data["dismissed"]), "cwd": cwd_norm}

    @mcp.tool()
    def phase_list(project_id: str) -> dict[str, Any]:
        """List all Phases and their statuses for a project.

        Args:
            project_id: Project ID

        Returns:
            Phase list with name, status, and sort order for each Phase
        """
        return _api_call("GET", f"/api/projects/{project_id}/phases")
