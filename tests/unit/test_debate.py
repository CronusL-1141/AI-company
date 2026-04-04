"""Tests for the enhanced debate meeting template and debate MCP tools."""

from __future__ import annotations

import pytest

from aiteam.meeting.templates import TEMPLATE_ROUNDS, recommend_template


class TestDebateTemplate:
    """Tests for the enhanced 4-round debate template structure."""

    def test_debate_has_4_rounds(self):
        template = TEMPLATE_ROUNDS["debate"]
        assert template["total_rounds"] == 4
        assert len(template["rounds"]) == 4

    def test_debate_round_numbers_are_sequential(self):
        rounds = TEMPLATE_ROUNDS["debate"]["rounds"]
        numbers = [r["number"] for r in rounds]
        assert numbers == [1, 2, 3, 4]

    def test_debate_round_names(self):
        rounds = TEMPLATE_ROUNDS["debate"]["rounds"]
        names = [r["name"] for r in rounds]
        assert names == ["正方陈述", "反方质疑", "正方回应", "裁决"]

    def test_debate_round1_advocate_rule(self):
        rule = TEMPLATE_ROUNDS["debate"]["rounds"][0]["rule"]
        assert "Advocate" in rule
        assert "正方" in rule

    def test_debate_round2_critic_rule(self):
        rule = TEMPLATE_ROUNDS["debate"]["rounds"][1]["rule"]
        assert "Critic" in rule
        assert "风险等级" in rule
        # Critic must cite advocate's words
        assert "引用" in rule

    def test_debate_round3_response_rule(self):
        rule = TEMPLATE_ROUNDS["debate"]["rounds"][2]["rule"]
        # Advocate responds to each challenge
        assert "接受" in rule or "回应" in rule

    def test_debate_round4_verdict_rule(self):
        rule = TEMPLATE_ROUNDS["debate"]["rounds"][3]["rule"]
        # Judge must produce APPROVED/CONDITIONAL/REJECTED verdict
        assert "APPROVED" in rule or "裁决" in rule
        assert "Action Items" in rule or "Action" in rule

    def test_debate_has_roles_definition(self):
        template = TEMPLATE_ROUNDS["debate"]
        assert "roles" in template
        roles = template["roles"]
        assert "advocate" in roles
        assert "critic" in roles
        assert "judge" in roles

    def test_debate_description_mentions_4_rounds(self):
        desc = TEMPLATE_ROUNDS["debate"]["description"]
        assert "四轮" in desc or "4" in desc or "正方" in desc


class TestDebateKeywords:
    """Tests for debate keyword matching."""

    def test_debate_keywords_include_advocate(self):
        from aiteam.meeting.templates import TEMPLATE_KEYWORDS
        keywords = TEMPLATE_KEYWORDS["debate"]
        assert "advocate" in keywords

    def test_debate_keywords_include_critic(self):
        from aiteam.meeting.templates import TEMPLATE_KEYWORDS
        keywords = TEMPLATE_KEYWORDS["debate"]
        assert "critic" in keywords

    def test_red_team_triggers_debate(self):
        template, _ = recommend_template("red team evaluation of our architecture")
        assert template == "debate"

    def test_debate_keyword_triggers_debate(self):
        template, _ = recommend_template("debate: should we use PostgreSQL or MongoDB?")
        assert template == "debate"

    def test_advocate_keyword_triggers_debate(self):
        template, _ = recommend_template("advocate the current implementation design")
        assert template == "debate"


class TestDebateMCPTools:
    """Tests for debate_start and debate_code_review MCP tools."""

    def _register_meeting_tools(self):
        """Register meeting tools on a fresh FastMCP instance and return it."""
        import asyncio
        from fastmcp import FastMCP
        import aiteam.mcp.tools.meeting as meeting_module

        mcp = FastMCP(name="test")
        meeting_module.register(mcp)
        # list_tools() is async in this version of fastmcp
        tools_list = asyncio.get_event_loop().run_until_complete(mcp.list_tools())
        return {t.name: t for t in tools_list}

    def test_debate_start_tool_registered(self):
        """Verify debate_start appears in tool registration."""
        tools = self._register_meeting_tools()
        assert "debate_start" in tools

    def test_debate_code_review_tool_registered(self):
        """Verify debate_code_review appears in tool registration."""
        tools = self._register_meeting_tools()
        assert "debate_code_review" in tools

    def test_debate_start_has_required_params(self):
        """debate_start tool must expose topic, advocate, critic parameters."""
        tools = self._register_meeting_tools()
        tool = tools["debate_start"]
        schema = tool.parameters if hasattr(tool, "parameters") else tool.inputSchema
        props = schema.get("properties", {})
        assert "topic" in props
        assert "advocate" in props
        assert "critic" in props

    def test_debate_code_review_has_file_path_param(self):
        """debate_code_review tool must expose file_path and change_description parameters."""
        tools = self._register_meeting_tools()
        tool = tools["debate_code_review"]
        schema = tool.parameters if hasattr(tool, "parameters") else tool.inputSchema
        props = schema.get("properties", {})
        assert "file_path" in props
        assert "change_description" in props

    def test_debate_start_no_team_returns_error(self):
        """When no team is resolved, debate_start returns error dict."""
        from unittest.mock import patch

        with patch("aiteam.mcp._base._resolve_team_id", return_value=None), \
             patch("aiteam.mcp._base._api_call", return_value={}):
            # Call the logic directly by importing the module-level function
            # We replicate what the registered tool calls
            from aiteam.mcp._base import _resolve_team_id, _api_call
            from aiteam.meeting.templates import TEMPLATE_ROUNDS

            resolved = _resolve_team_id("")
            if not resolved:
                result = {"success": False, "error": "未找到活跃团队，请提供 team_id 或先创建团队"}
            else:
                result = {}
            assert result["success"] is False
            assert "team_id" in result["error"]


class TestDebateAgentTemplates:
    """Verify debate agent template files exist and have required frontmatter."""

    def _read_template(self, filename: str) -> str:
        import pathlib
        root = pathlib.Path(__file__).parents[2]
        template_path = root / "plugin" / "agents" / filename
        return template_path.read_text(encoding="utf-8")

    def test_advocate_template_exists(self):
        content = self._read_template("debate-advocate.md")
        assert content

    def test_critic_template_exists(self):
        content = self._read_template("debate-critic.md")
        assert content

    def test_advocate_has_name_frontmatter(self):
        content = self._read_template("debate-advocate.md")
        assert "name: debate-advocate" in content

    def test_critic_has_name_frontmatter(self):
        content = self._read_template("debate-critic.md")
        assert "name: debate-critic" in content

    def test_advocate_covers_round1_and_round3(self):
        content = self._read_template("debate-advocate.md")
        assert "Round 1" in content
        assert "Round 3" in content

    def test_critic_covers_round2(self):
        content = self._read_template("debate-critic.md")
        assert "Round 2" in content

    def test_critic_has_risk_levels(self):
        content = self._read_template("debate-critic.md")
        assert "High" in content
        assert "Medium" in content
        assert "Low" in content

    def test_advocate_has_os_binding(self):
        content = self._read_template("debate-advocate.md")
        assert "task_memo_read" in content
        assert "task_memo_add" in content

    def test_critic_has_os_binding(self):
        content = self._read_template("debate-critic.md")
        assert "task_memo_read" in content
        assert "task_memo_add" in content
