"""Resolve HTTP MCP project scope per request without changing stdio identity."""

from __future__ import annotations

import os
from urllib.parse import unquote

from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware

from aiteam.mcp import _base

# These tools establish/decline registration and need only a verified folder.
# Read-only discovery tools still require a project, like every other tool.
_REGISTRATION_TOOLS = frozenset({"project_create", "dismiss_project_registration"})


def _normalize_path(value: str) -> str:
    return os.path.realpath(value).replace("\\", "/").rstrip("/").lower() if value else ""


def _verified_directory(directory: str) -> str:
    directory = os.path.realpath(directory)
    if not os.path.isdir(directory):
        raise ValueError("HTTP MCP working directory does not exist or is not a directory")
    return directory


def _project_for_directory(directory: str, projects: list[dict]) -> str:
    current = _normalize_path(directory)
    matches = [
        (len(root), str(project["id"]))
        for project in projects
        if os.path.isabs(project.get("root_path") or "")
        and (root := _normalize_path(project["root_path"]))
        and root != "." and (current == root or current.startswith(root + "/"))
    ]
    if not matches:
        return ""
    longest = max(length for length, _ in matches)
    owners = {project_id for length, project_id in matches if length == longest}
    if len(owners) != 1:
        raise ValueError("HTTP MCP working directory matches multiple projects")
    return owners.pop()


class HTTPProjectContext(Middleware):
    async def on_call_tool(self, context, call_next):
        try:
            request = get_http_request()
        except RuntimeError:
            return await call_next(context)

        # Never inherit the shared API creator's project or CC session identity.
        token = _base._http_request_context.set({})
        try:
            server = request.scope.get("server")
            if not server or len(server) != 2 or type(server[1]) is not int or not 0 < server[1] < 65536:
                raise ValueError("HTTP MCP requires the ASGI server's actual listening address")
            host = "[::1]" if server[0] in {"::", "::1"} else "127.0.0.1"
            api_url = f"http://{host}:{server[1]}"
            _base._http_request_context.set({"api_url": api_url})
            directory = unquote(request.headers.get("x-aiteam-project-dir", ""))
            if not directory or "\x00" in directory or not os.path.isabs(directory):
                raise ValueError("HTTP MCP requires an absolute working directory from its connection helper")
            directory = await _base._run_http_sync(_verified_directory, directory)
            # REST's synchronous dependencies use AnyIO's default thread pool.
            # Waiting for that REST call in the same pool can exhaust it before
            # get_repository gets a thread. Give HTTP clients their own limiter.
            projects = await _base._run_http_sync(_base._api_call, "GET", "/api/projects")
            rows = projects.get("data")
            if not isinstance(rows, list):
                raise ValueError("HTTP MCP could not verify the working directory's project")
            project_id = await _base._run_http_sync(_project_for_directory, directory, rows)
            if not project_id and context.message.name not in _REGISTRATION_TOOLS:
                raise ValueError("HTTP MCP working directory is not a registered project")
            # The connection's own MCP session id (server-issued, one per client
            # connection) is the only per-session identity an HTTP host such as Codex
            # has; the reconcile lease uses it to recognise its holder.
            scope = {
                "project_dir": directory, "api_url": api_url,
                "mcp_session_id": request.headers.get("mcp-session-id", ""),
            }
            if project_id:
                scope["project_id"] = project_id
            _base._http_request_context.set(scope)
            return await call_next(context)
        finally:
            _base._http_request_context.reset(token)
