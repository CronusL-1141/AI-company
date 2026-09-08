"""Channel communication MCP tools (v1.0 P1-6).

Provides cross-team channel messaging with @mention semantics.
Channel formats: "team:<name>" / "project:<id>" / "global"

未读徽章（2026-09-08）：channel_unread 查计数，channel_read_ack 推进水位。
channel_read 保持**纯读无副作用**——把水位推进塞进读取里会同时踩两个坑：只读会话
拿不到清零的办法，而它又确实在写库却不在 WRITE_TOOLS 里。
"""

from __future__ import annotations

from typing import Any

from aiteam.mcp._base import _api_call, _resolve_project_id


def register(mcp):
    """Register channel MCP tools."""

    @mcp.tool()
    def channel_send(
        channel: str,
        message: str,
        sender: str = "agent",
        mentions: list[str] | None = None,
        project_id: str = "",
    ) -> dict[str, Any]:
        """Send a message to a channel.

        Supports cross-team broadcasting and @mention semantics.

        Channel formats:
        - "team:<name>"    — send to a specific team channel
        - "project:<id>"   — send to a project-wide channel
        - "global"         — broadcast to all teams

        收件人写法：mentions 里裸名与 "@名" 都算数，未读判定两种都认。

        Args:
            channel: Target channel (e.g. "team:backend", "project:abc123", "global").
            message: Message content.
            sender: Sender identity, default "agent".
            mentions: List of mention tags, e.g. ["leader-cc"] or ["@leader-cc"].
            project_id: 归属项目；留空按当前工作目录自动归属（与 task_memo / report
                同一套模式）。归属为空的消息照发照存，但**不进任何项目的未读**——
                收件人不会被提示，只能主动读到。

        Returns:
            Created channel message info.
        """
        payload: dict[str, Any] = {
            "sender": sender,
            "content": message,
            "mentions": mentions or [],
        }
        resolved = _resolve_project_id(project_id)
        if resolved:
            payload["project_id"] = resolved
        return _api_call("POST", f"/api/channels/{channel}/messages", payload)

    @mcp.tool()
    def channel_read(
        channel: str,
        since: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read messages from a channel.

        Supports incremental pull via 'since' parameter to fetch only new messages.

        **纯读，不清未读**。读完要消掉徽章须显式调 channel_read_ack，并把本次实际
        读到的最后一条的 created_at 传进去。

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
    def channel_unread(
        reader: str,
        project_id: str = "",
    ) -> dict[str, Any]:
        """某读者在某项目下的逐频道未读计数（谁在叫你、有几条、最新一条讲什么）。

        纯读：查询不会清掉未读，也不会在库里留下水位行。要清零调 channel_read_ack。

        未读 = mentions 整值命中 reader，且消息归属该项目，且晚于该频道的已读水位。
        没有水位时按"全部未读"算。

        Args:
            reader: 读者角色标识，如 "leader-cc" / "leader-codex"。**不要传 session_id**：
                会话是一次性的，按会话记水位会让每开一个新会话就把历史消息重算成未读。
            project_id: 归属项目；留空按当前工作目录自动归属。

        Returns:
            {"reader", "project_id", "total", "channels": [{channel, count,
            latest_sender, latest_excerpt, latest_at}], "truncated"}。
            truncated=true 表示命中扫描上限、计数偏少，不是"就这么多"。
        """
        import urllib.parse

        query = urllib.parse.urlencode(
            {"reader": reader, "project_id": _resolve_project_id(project_id)}
        )
        return _api_call("GET", f"/api/channels/unread?{query}")

    @mcp.tool()
    def channel_read_ack(
        channel: str,
        reader: str,
        last_read_at: str,
        project_id: str = "",
    ) -> dict[str, Any]:
        """把某频道的已读水位推进到你**实际读到**的那一条，清掉对应未读。

        幂等且单调：传入时间早于或等于现有水位时不动，返回 advanced=false。

        Args:
            channel: 频道名，与你刚才 channel_read 用的那个一致。
            reader: 读者角色标识，如 "leader-cc"，须与 channel_unread 用的一致。
            last_read_at: ISO 8601 时间戳，取**本次实际读到的最后一条消息的 created_at**。
                别传当前时间：分页只拿了前 N 条时按 now 推进会把没读到的那些一起标成
                已读，之后再也不会提示，且没有任何机检抓得到。
            project_id: 归属项目；留空按当前工作目录自动归属。

        Returns:
            {"reader", "channel", "project_id", "last_read_at", "advanced"}。
        """
        return _api_call(
            "POST",
            f"/api/channels/{channel}/read-cursor",
            {
                "reader": reader,
                "project_id": _resolve_project_id(project_id),
                "last_read_at": last_read_at,
            },
        )

    @mcp.tool()
    def channel_mentions(
        agent_name: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get channel messages that mention a specific agent.

        裸名与 "@名" 两种书写都能查到（2026-09-08 前这里只匹配 "@"+名，而真实调用方
        写的是裸名，导致对每一条消息都返回 0）。

        Args:
            agent_name: 要查的收件人名，如 "leader-cc"。带不带 "@" 前缀都可以。
                **必填**：早先这个参数可留空并声称"从上下文取当前 agent 名"，实现却是
                硬编码字面量 "agent"，留空等于去查一个真的叫 agent 的收件人。
            limit: Maximum number of messages to return (default 50).

        Returns:
            List of channel messages that mention the agent, newest-first.
        """
        import urllib.parse

        params = urllib.parse.urlencode({"limit": limit})
        return _api_call("GET", f"/api/channels/mentions/{agent_name}?{params}")
