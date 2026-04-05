"""Channel communication MCP tools (v1.0 P1-6).

Provides cross-team channel messaging with @mention semantics.
Channel formats: "team:<name>" / "project:<id>" / "global"
"""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call


def register(mcp):
    """Register channel MCP tools."""

    @mcp.tool()
    def channel_send(
        channel: str,
        message: str,
        sender: str = "agent",
        mentions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send a message to a channel.

        Supports cross-team broadcasting and @mention semantics.

        Channel formats:
        - "team:<name>"    — send to a specific team channel
        - "project:<id>"   — send to a project-wide channel
        - "global"         — broadcast to all teams

        Args:
            channel: Target channel (e.g. "team:backend", "project:abc123", "global").
            message: Message content.
            sender: Sender identity, default "agent".
            mentions: List of @mention tags, e.g. ["@agent-name", "@team-name"].

        Returns:
            Created channel message info.
        """
        payload: dict[str, Any] = {
            "sender": sender,
            "content": message,
            "mentions": mentions or [],
        }
        return _api_call("POST", f"/api/channels/{channel}/messages", payload)

    @mcp.tool()
    def channel_read(
        channel: str,
        since: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read messages from a channel.

        Supports incremental pull via 'since' parameter to fetch only new messages.

        Args:
            channel: Target channel (e.g. "team:backend", "global").
            since: ISO 8601 timestamp — only return messages after this time.
                   Example: "2026-04-04T10:00:00". Leave empty to get all recent messages.
            limit: Maximum number of messages to return (default 50, max 200).

        Returns:
            List of channel messages sorted oldest-first.
        """
        import urllib.parse

        params: dict[str, Any] = {"limit": limit}
        if since:
            params["since"] = since
        query = urllib.parse.urlencode(params)
        return _api_call("GET", f"/api/channels/{channel}/messages?{query}")

    @mcp.tool()
    def channel_mentions(
        agent_name: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get channel messages that @mention a specific agent.

        Args:
            agent_name: Agent name to look up mentions for (without '@' prefix).
                        Leave empty to use the current agent's name from context.
            limit: Maximum number of messages to return (default 50).

        Returns:
            List of channel messages that mention the agent, newest-first.
        """
        import urllib.parse

        if not agent_name:
            agent_name = "agent"

        params = urllib.parse.urlencode({"limit": limit})
        return _api_call("GET", f"/api/channels/mentions/{agent_name}?{params}")
