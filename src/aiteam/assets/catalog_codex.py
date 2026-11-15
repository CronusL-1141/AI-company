"""The Codex home's catalog: a skeleton until the Codex line's own stage.

The deny table is complete already (the deny check applies to every host and is
machine-checked); no category is managed yet, so nothing of a Codex home is read.
``config.toml`` is an extraction source for its MCP and plugin tables, not a denied
path: the projection, not the deny table, keeps the rest of it out.
"""

from __future__ import annotations

from aiteam.assets.catalog import DenyRule, HostCatalog
from aiteam.types import AssetHost

DENY: tuple[DenyRule, ...] = (
    DenyRule("auth.json", ("auth.json",), "login credentials"),
    DenyRule("installation_id", ("installation_id",), "this installation's id"),
    DenyRule("*-auth-bootstrap.json", ("chatgpt-auth-bootstrap.json",), "login bootstrap"),
    DenyRule("*-models.json", ("remote-models.json",), "model list cache"),
    DenyRule(".codex-global-state.json", (".codex-global-state.json",), "Codex app state"),
    DenyRule("**/*.sqlite", ("state_5.sqlite", "memories_1.sqlite"), "Codex databases, derived or pipeline state"),
    DenyRule("plugins/**/*", ("plugins/cache/p/plugin.json",), "installed plugin files"),
    DenyRule("packages/**/*", ("packages/x/package.json",), "installed packages"),
    DenyRule("log/**/*", ("log/codex-tui.log",), "logs"),
    DenyRule("tmp/**/*", ("tmp/a",), "temporary files"),
    DenyRule(".tmp/**/*", (".tmp/a",), "temporary files"),
    DenyRule("ipc/**/*", ("ipc/socket.json",), "process channels"),
    DenyRule("app-server-*", ("app-server-state.json",), "app server state"),
    DenyRule("app-server-*/**/*", ("app-server-cache/a.json",), "app server state"),
    DenyRule("mcp-oauth-locks/**/*", ("mcp-oauth-locks/server.json",), "MCP login state"),
    DenyRule("archives/**/*", ("archives/a.tar",), "archives"),
    DenyRule("ai-team-os/**/*", ("ai-team-os/state.json",), "OS's own files"),
    DenyRule("memories/.git/**/*", ("memories/.git/HEAD",), "the memory pipeline's own repository"),
    DenyRule("memories/.codex-baseline/**/*", ("memories/.codex-baseline/MEMORY.md",),
             "the memory pipeline's baseline"),
    DenyRule("claude-memory-import/**/*", ("claude-memory-import/x/MEMORY.md",),
             "converted from Claude Code, derived"),
    DenyRule("skills/.system/**/*", ("skills/.system/s/SKILL.md",), "skills that ship with Codex"),
)

CATALOG = HostCatalog(host=AssetHost.CODEX, version=0, categories=(), deny=DENY)
