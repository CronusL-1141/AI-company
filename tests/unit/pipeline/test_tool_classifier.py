"""Unit tests for tool_classifier — TC-CLS-01 through TC-CLS-10."""

from aiteam.pipeline.tool_classifier import (
    ALL_EXEMPT,
    EXECUTE_CLASS_TOOLS,
    PLAN_CLASS_TOOLS,
    REQUIRES_USER_ACK,
    VERIFY_CLASS_TOOLS,
    is_allowed,
    normalize_tool_name,
    requires_user_ack,
)


class TestNormalizeToolName:
    def test_tc_cls_01_strips_mcp_prefix(self):
        assert normalize_tool_name("mcp__ai-team-os__task_create") == "task_create"

    def test_tc_cls_01_cc_builtin_unchanged(self):
        assert normalize_tool_name("Read") == "Read"

    def test_tc_cls_01_no_prefix_unchanged(self):
        assert normalize_tool_name("pipeline_advance") == "pipeline_advance"

    def test_tc_cls_01_double_prefix_strips_once(self):
        # removeprefix only strips one occurrence
        raw = "mcp__ai-team-os__mcp__ai-team-os__task_run"
        assert normalize_tool_name(raw) == "mcp__ai-team-os__task_run"


class TestAllExemptTools:
    def test_tc_cls_02_exempt_allowed_in_plan(self):
        for tool in ALL_EXEMPT:
            assert is_allowed(tool, "Plan"), f"{tool} should be allowed in Plan"

    def test_tc_cls_02_exempt_allowed_in_execute(self):
        for tool in ALL_EXEMPT:
            assert is_allowed(tool, "Execute"), f"{tool} should be allowed in Execute"

    def test_tc_cls_02_exempt_allowed_in_verify(self):
        for tool in ALL_EXEMPT:
            assert is_allowed(tool, "Verify"), f"{tool} should be allowed in Verify"

    def test_tc_cls_02_read_is_exempt(self):
        assert "Read" in ALL_EXEMPT

    def test_tc_cls_02_send_message_is_exempt(self):
        assert "SendMessage" in ALL_EXEMPT


class TestPlanOnlyTools:
    def test_tc_cls_03_plan_tool_not_in_execute(self):
        plan_only = PLAN_CLASS_TOOLS - EXECUTE_CLASS_TOOLS - ALL_EXEMPT
        for tool in plan_only:
            assert not is_allowed(tool, "Execute"), f"{tool} should NOT be allowed in Execute"

    def test_tc_cls_03_plan_tool_not_in_verify(self):
        plan_only = PLAN_CLASS_TOOLS - VERIFY_CLASS_TOOLS - ALL_EXEMPT
        for tool in plan_only:
            assert not is_allowed(tool, "Verify"), f"{tool} should NOT be allowed in Verify"

    def test_tc_cls_03_web_search_plan_only(self):
        assert is_allowed("WebSearch", "Plan")
        assert not is_allowed("WebSearch", "Execute")
        assert not is_allowed("WebSearch", "Verify")

    def test_tc_cls_03_task_decompose_plan_only(self):
        assert is_allowed("task_decompose", "Plan")
        assert not is_allowed("task_decompose", "Execute")
        assert not is_allowed("task_decompose", "Verify")


class TestExecuteTools:
    def test_tc_cls_04_execute_tool_not_in_plan(self):
        execute_only = EXECUTE_CLASS_TOOLS - PLAN_CLASS_TOOLS - ALL_EXEMPT
        for tool in execute_only:
            assert not is_allowed(tool, "Plan"), f"{tool} should NOT be allowed in Plan"

    def test_tc_cls_04_edit_not_in_plan(self):
        assert not is_allowed("Edit", "Plan")

    def test_tc_cls_04_write_not_in_plan(self):
        assert not is_allowed("Write", "Plan")

    def test_tc_cls_04_edit_allowed_in_execute(self):
        assert is_allowed("Edit", "Execute")

    def test_tc_cls_04_mcp_prefixed_task_create(self):
        assert is_allowed("mcp__ai-team-os__task_create", "Execute")
        assert not is_allowed("mcp__ai-team-os__task_create", "Plan")


class TestVerifyTools:
    def test_tc_cls_05_verify_tools_not_in_plan(self):
        verify_only = VERIFY_CLASS_TOOLS - PLAN_CLASS_TOOLS - ALL_EXEMPT
        for tool in verify_only:
            assert not is_allowed(tool, "Plan"), f"{tool} should NOT be allowed in Plan"

    def test_tc_cls_05_task_replay_not_in_plan(self):
        assert not is_allowed("task_replay", "Plan")

    def test_tc_cls_05_task_replay_allowed_in_verify(self):
        assert is_allowed("task_replay", "Verify")

    def test_tc_cls_05_diagnose_task_failure_not_in_plan(self):
        assert not is_allowed("diagnose_task_failure", "Plan")


class TestRequiresUserAck:
    def test_tc_cls_06_scheduler_create_needs_ack(self):
        assert requires_user_ack("scheduler_create") is True

    def test_tc_cls_06_mcp_prefixed_scheduler_create_needs_ack(self):
        assert requires_user_ack("mcp__ai-team-os__scheduler_create") is True

    def test_tc_cls_07_read_does_not_need_ack(self):
        assert requires_user_ack("Read") is False

    def test_tc_cls_07_task_create_does_not_need_ack(self):
        assert requires_user_ack("task_create") is False

    def test_tc_cls_06_git_auto_commit_needs_ack(self):
        assert requires_user_ack("git_auto_commit") is True

    def test_tc_cls_06_project_delete_needs_ack(self):
        assert requires_user_ack("mcp__ai-team-os__project_delete") is True


class TestUnknownInputs:
    def test_tc_cls_08_unknown_stage_class_returns_false(self):
        assert is_allowed("task_create", "UnknownStage") is False

    def test_tc_cls_08_empty_stage_class_returns_false(self):
        assert is_allowed("Read", "") is False

    def test_tc_cls_09_unknown_tool_all_stages_false(self):
        for stage in ("Plan", "Execute", "Verify"):
            assert is_allowed("nonexistent_super_tool", stage) is False

    def test_tc_cls_09_unknown_tool_not_requires_ack(self):
        assert requires_user_ack("totally_unknown_tool") is False


class TestTeamCreate:
    def test_tc_cls_10_team_create_in_plan_whitelist(self):
        assert is_allowed("TeamCreate", "Plan") is True

    def test_tc_cls_10_team_create_requires_user_ack(self):
        assert requires_user_ack("TeamCreate") is True

    def test_tc_cls_10_team_create_in_requires_user_ack_set(self):
        assert "TeamCreate" in REQUIRES_USER_ACK

    def test_tc_cls_10_team_create_not_in_execute(self):
        assert not is_allowed("TeamCreate", "Execute")
