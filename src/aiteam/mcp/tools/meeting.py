"""Meeting MCP tools."""

from __future__ import annotations

import urllib.parse
from typing import Any

from aiteam.mcp._base import _api_call, _resolve_team_id


def register(mcp):
    """Register all meeting-related MCP tools."""

    @mcp.tool()
    def meeting_create(
        topic: str,
        team_id: str = "",
        participants: list[str] | None = None,
        template: str = "free",
    ) -> dict[str, Any]:
        """Create a team meeting for multi-Agent collaborative discussion.

        Rule: Dynamically add suitable participants based on the topic; recruit experts
        when new directions emerge during discussion. Meeting conclusions should be
        converted to tasks and placed on the task wall.

        Available templates: brainstorm (4 rounds) / decision (3 rounds) / review (3 rounds) /
                  retrospective (3 rounds) / standup (1 round) / debate (3 rounds) /
                  lean_coffee (3 rounds) / council (3 rounds) / free (default, auto-recommends based on topic)

        Args:
            topic: Meeting discussion topic
            team_id: Team ID or name (optional, auto-uses active team if empty)
            participants: List of participant Agent IDs; all members join if empty
            template: Meeting template, default "free"

        Returns:
            Meeting info including meeting_id, operation guide, and template round structure
        """
        from aiteam.meeting.templates import TEMPLATE_ROUNDS, recommend_template

        resolved = _resolve_team_id(team_id)
        if not resolved:
            return {"success": False, "error": "未找到活跃团队，请提供 team_id 或先创建团队"}
        result = _api_call(
            "POST",
            f"/api/teams/{resolved}/meetings",
            {
                "topic": topic,
                "participants": participants or [],
            },
        )

        auto_selected = False
        if template == "free" and topic:
            recommended, reason = recommend_template(topic)
            if recommended != "brainstorm" or "brainstorm" in topic.lower():
                template = recommended
                auto_selected = True
                result["_auto_selected"] = {"template": recommended, "reason": reason}

        if template and template != "free" and template in TEMPLATE_ROUNDS:
            result["_template"] = {
                "name": template,
                "auto_selected": auto_selected,
                **TEMPLATE_ROUNDS[template],
            }
        else:
            result["_template"] = {
                "name": "free",
                "description": "自由讨论——无预设结构，按需进行多轮讨论",
                "total_rounds": None,
                "rounds": [],
            }
        return result

    @mcp.tool()
    def meeting_send_message(
        meeting_id: str,
        agent_id: str,
        agent_name: str,
        content: str,
        round_number: int = 1,
    ) -> dict[str, Any]:
        """Send a discussion message in a meeting.

        Discussion rules:
        - Round 1: Each participant presents their views
        - Round 2+: Must read previous speakers' messages first, cite and respond to specific points
        - Final round: Summarize consensus and disagreements

        Args:
            meeting_id: Meeting ID
            agent_id: ID of the speaking Agent
            agent_name: Name of the speaking Agent
            content: Message content
            round_number: Discussion round number, default 1

        Returns:
            Successfully sent message info
        """
        return _api_call(
            "POST",
            f"/api/meetings/{meeting_id}/messages",
            {
                "agent_id": agent_id,
                "agent_name": agent_name,
                "content": content,
                "round_number": round_number,
            },
        )

    @mcp.tool()
    def meeting_read_messages(meeting_id: str, limit: int = 100) -> dict[str, Any]:
        """Read all discussion messages in a meeting.

        Args:
            meeting_id: Meeting ID
            limit: Maximum number of messages to return, default 100

        Returns:
            Message list in chronological order
        """
        return _api_call("GET", f"/api/meetings/{meeting_id}/messages?limit={limit}")

    @mcp.tool()
    def meeting_conclude(meeting_id: str) -> dict[str, Any]:
        """Conclude a meeting, marking it as completed.

        Args:
            meeting_id: Meeting ID

        Returns:
            Updated meeting info
        """
        result = _api_call("PUT", f"/api/meetings/{meeting_id}/conclude")
        result["_hint"] = "会议结论已自动保存到团队记忆。可通过 memory_search 或 team_briefing 检索历史决策。"
        return result

    @mcp.tool()
    def meeting_template_list() -> dict[str, Any]:
        """List available meeting templates and their round structures.

        Returns:
            templates: All available templates with round structure details
        """
        from aiteam.meeting.templates import TEMPLATE_ROUNDS

        return {"templates": TEMPLATE_ROUNDS}

    @mcp.tool()
    def meeting_list(
        team_id: str = "",
        status: str = "",
    ) -> dict[str, Any]:
        """List meetings for a team, optionally filtered by status.

        Args:
            team_id: Team ID or name (optional, auto-uses active team if empty)
            status: Filter by meeting status: "active" or "concluded" (optional, returns all if empty)

        Returns:
            Meeting list with topic, status, participant count, etc.
        """
        resolved = _resolve_team_id(team_id)
        if not resolved:
            return {"success": False, "error": "未找到活跃团队，请提供 team_id 或先创建团队"}
        path = f"/api/teams/{resolved}/meetings"
        if status:
            path += f"?status={urllib.parse.quote(status)}"
        return _api_call("GET", path)

    @mcp.tool()
    def meeting_update(
        meeting_id: str,
        topic: str = "",
        participants: list[str] | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        """Update meeting fields (topic, participants, notes).

        Use this to add conclusions/notes to a meeting or update its topic.
        To formally conclude a meeting (mark as concluded), use meeting_conclude instead.

        Args:
            meeting_id: Meeting ID (required)
            topic: New topic text (optional)
            participants: Updated participant list (optional)
            notes: Meeting notes or conclusion summary to store (optional)

        Returns:
            Updated meeting info
        """
        payload: dict[str, Any] = {}
        if topic:
            payload["topic"] = topic
        if participants is not None:
            payload["participants"] = participants
        if notes:
            payload["notes"] = notes
        if not payload:
            return {"success": False, "error": "至少需要提供一个更新字段（topic / participants / notes）"}
        return _api_call("PUT", f"/api/meetings/{meeting_id}", payload)
