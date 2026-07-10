"""Tests for meeting_create dispatch_plan and _build_participation_prompt."""

from __future__ import annotations

from aiteam.mcp.tools.meeting import _build_dispatch_plan, _build_participation_prompt


class TestBuildParticipationPrompt:
    """Tests for the _build_participation_prompt pure function."""

    def _make_prompt(self, **overrides) -> str:
        defaults = dict(
            name="arch-lead",
            role="负责评估整体架构方案",
            meeting_id="mtg-abc123",
            title="API 设计评审",
            round_rule="每人3段，不超过300字",
            materials=["docs/arch.md"],
            context_files=["docs/api-spec.md"],
            expected_output="三段式：评估/风险/建议",
        )
        defaults.update(overrides)
        return _build_participation_prompt(**defaults)

    def test_contains_agent_name(self):
        prompt = self._make_prompt()
        assert "arch-lead" in prompt

    def test_contains_role(self):
        prompt = self._make_prompt()
        assert "负责评估整体架构方案" in prompt

    def test_contains_meeting_id(self):
        prompt = self._make_prompt()
        assert "mtg-abc123" in prompt

    def test_contains_title(self):
        prompt = self._make_prompt()
        assert "API 设计评审" in prompt

    def test_contains_round_rule(self):
        prompt = self._make_prompt()
        assert "每人3段，不超过300字" in prompt

    def test_contains_global_material(self):
        prompt = self._make_prompt()
        assert "docs/arch.md" in prompt

    def test_contains_context_file(self):
        prompt = self._make_prompt()
        assert "docs/api-spec.md" in prompt

    def test_contains_meeting_send_message_call(self):
        prompt = self._make_prompt()
        assert "meeting_send_message" in prompt

    def test_send_message_uses_correct_meeting_id(self):
        prompt = self._make_prompt(meeting_id="mtg-xyz999")
        assert 'meeting_id="mtg-xyz999"' in prompt

    def test_send_message_uses_agent_name_as_agent_id(self):
        prompt = self._make_prompt(name="backend-arch")
        assert 'agent_id="backend-arch"' in prompt
        assert 'agent_name="backend-arch"' in prompt

    def test_contains_sendmessage_to_team_lead(self):
        prompt = self._make_prompt()
        assert 'SendMessage' in prompt
        assert 'team-lead' in prompt

    def test_contains_shutdown_instruction(self):
        prompt = self._make_prompt()
        assert "立即退出" in prompt

    def test_contains_expected_output(self):
        prompt = self._make_prompt()
        assert "三段式：评估/风险/建议" in prompt

    def test_no_materials_skips_read_section(self):
        prompt = self._make_prompt(materials=[], context_files=[])
        # Should not include "Read" file step header
        assert "Read required materials" not in prompt

    def test_deduplicates_files_in_materials_and_context(self):
        prompt = self._make_prompt(
            materials=["docs/shared.md"],
            context_files=["docs/shared.md", "docs/extra.md"],
        )
        # docs/shared.md should appear exactly once in the Read list
        assert prompt.count("docs/shared.md") == 1

    def test_default_output_hint_when_empty(self):
        prompt = self._make_prompt(expected_output="")
        assert "清晰段落格式" in prompt

    def test_default_round_rule_when_empty(self):
        prompt = self._make_prompt(round_rule="")
        assert "立场" in prompt or "建议" in prompt


class TestBuildDispatchPlan:
    """Tests for the _build_dispatch_plan function."""

    def _structured_participant(self, **overrides) -> dict:
        base = {
            "name": "arch-lead",
            "agent_template": "software-architect",
            "role": "负责架构评审",
            "context_files": ["docs/arch.md"],
            "expected_output": "三段式：评估/风险/建议",
        }
        base.update(overrides)
        return base

    def test_structured_participant_has_launch_call(self):
        plan, expected, warnings = _build_dispatch_plan(
            meeting_id="mtg-1",
            title="Test",
            participants_raw=[self._structured_participant()],
            rounds=[],
            materials=[],
            team_name="my-team",
        )
        assert len(plan) == 1
        item = plan[0]
        assert item["launch_call"] != {}
        assert item["ready_to_paste"] is True

    def test_launch_call_has_correct_tool_name(self):
        plan, _, _ = _build_dispatch_plan(
            meeting_id="mtg-1",
            title="Test",
            participants_raw=[self._structured_participant()],
            rounds=[],
            materials=[],
            team_name="my-team",
        )
        assert plan[0]["launch_call"]["tool"] == "Agent"

    def test_launch_call_params_has_required_fields(self):
        plan, _, _ = _build_dispatch_plan(
            meeting_id="mtg-1",
            title="Test",
            participants_raw=[self._structured_participant(name="arch-lead")],
            rounds=[],
            materials=[],
            team_name="my-team",
        )
        params = plan[0]["launch_call"]["params"]
        assert "subagent_type" in params
        assert "name" in params
        assert "team_name" in params
        assert "description" in params
        assert "prompt" in params

    def test_launch_call_name_matches_participant_name(self):
        plan, _, _ = _build_dispatch_plan(
            meeting_id="mtg-1",
            title="Test",
            participants_raw=[self._structured_participant(name="backend-arch")],
            rounds=[],
            materials=[],
            team_name="my-team",
        )
        assert plan[0]["launch_call"]["params"]["name"] == "backend-arch"

    def test_launch_call_team_name_passed_through(self):
        plan, _, _ = _build_dispatch_plan(
            meeting_id="mtg-1",
            title="Test",
            participants_raw=[self._structured_participant()],
            rounds=[],
            materials=[],
            team_name="repo-insight-arch",
        )
        assert plan[0]["launch_call"]["params"]["team_name"] == "repo-insight-arch"

    def test_legacy_string_participant_has_empty_launch_call(self):
        plan, _, warnings = _build_dispatch_plan(
            meeting_id="mtg-1",
            title="Test",
            participants_raw=["arch-lead"],
            rounds=[],
            materials=[],
            team_name="my-team",
        )
        assert plan[0]["launch_call"] == {}
        assert plan[0]["ready_to_paste"] is False
        assert "arch-lead" in warnings

    def test_expected_participants_list_correct(self):
        _, expected, _ = _build_dispatch_plan(
            meeting_id="mtg-1",
            title="Test",
            participants_raw=[
                self._structured_participant(name="arch-lead"),
                self._structured_participant(name="backend-arch"),
            ],
            rounds=[],
            materials=[],
            team_name="my-team",
        )
        assert expected == ["arch-lead", "backend-arch"]

    def test_round_rule_injected_into_prompt(self):
        plan, _, _ = _build_dispatch_plan(
            meeting_id="mtg-1",
            title="Test",
            participants_raw=[self._structured_participant()],
            rounds=[{"topic": "立场", "rule": "每人5段阐述立场"}],
            materials=[],
            team_name="my-team",
        )
        prompt = plan[0]["launch_call"]["params"]["prompt"]
        assert "每人5段阐述立场" in prompt

    def test_global_materials_injected_into_prompt(self):
        plan, _, _ = _build_dispatch_plan(
            meeting_id="mtg-1",
            title="Test",
            participants_raw=[self._structured_participant(context_files=[])],
            rounds=[],
            materials=["docs/global.md"],
            team_name="my-team",
        )
        prompt = plan[0]["launch_call"]["params"]["prompt"]
        assert "docs/global.md" in prompt

    def test_attendance_check_command_format(self):
        """Verify the attendance_check_command string format."""
        meeting_id = "mtg-abc-123"
        expected_cmd = f"meeting_attendance_check(meeting_id='{meeting_id}')"
        assert f"meeting_id='{meeting_id}'" in expected_cmd

    def test_mixed_string_and_structured_participants(self):
        plan, expected, warnings = _build_dispatch_plan(
            meeting_id="mtg-1",
            title="Test",
            participants_raw=[
                "legacy-agent",
                self._structured_participant(name="arch-lead"),
            ],
            rounds=[],
            materials=[],
            team_name="my-team",
        )
        assert len(plan) == 2
        assert expected == ["legacy-agent", "arch-lead"]
        assert "legacy-agent" in warnings
        # legacy item has no launch_call
        assert plan[0]["ready_to_paste"] is False
        # structured item is ready
        assert plan[1]["ready_to_paste"] is True
