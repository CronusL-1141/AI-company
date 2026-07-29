"""transcript 路径派生器 —— 三类样本 + 边界。

样本按设计 §7 阶段1 的验收要求取：**无 wf 段 / 含 worktree 路径 / 非 ASCII slug**，
三者都是生产里真实出现过（或必然出现）的形态：

* 无 wf：直派子 agent，生产库 1,923 条已登记路径里占 106 条；
* worktree：worktree 隔离的 agent 其 cwd 在 ``.claude/worktrees/...`` 之下，slug 会
  把整条 worktree 路径编进去，段数比普通项目多得多；
* 非 ASCII slug：``~/Desktop/文档`` 这类目录被 ``[^a-zA-Z0-9] -> '-'`` 塌成一串
  连字符，多个不同目录会塌成**同一个** slug（生产实测 ``-Users-dev-Desktop----``
  525 行 / ``-Users-dev-Desktop---`` 191 行）。这正是"slug 只作交叉校验、归属以
  agents.project_id 为准"这条规矩的来由，因此单独钉一组断言。
"""

from __future__ import annotations

import pytest

from aiteam.services.transcript_path import (
    derive_session_id,
    parse_transcript_path,
    slug_matches_root,
)

HOME = "/Users/dev"
SESSION = "80d0cc5e-186a-4948-9e99-39ecfcf17730"


class TestSubagentPaths:
    def test_plain_subagent_has_no_wf_id(self):
        path = (
            f"{HOME}/.claude/projects/-Users-dev-Desktop-AI-team-OS/"
            f"{SESSION}/subagents/agent-a672b51dd77cd8dd0.jsonl"
        )
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.kind == "subagent"
        assert ref.project_slug == "-Users-dev-Desktop-AI-team-OS"
        assert ref.session_id == SESSION
        assert ref.wf_id is None
        assert ref.cc_agent_id == "a672b51dd77cd8dd0"

    def test_workflow_subagent_carries_wf_id(self):
        path = (
            f"{HOME}/.claude/projects/-Users-dev-Desktop-AI-team-OS/"
            f"{SESSION}/subagents/workflows/wf_8cd4fced-95a/agent-af2d08f9a2862fe29.jsonl"
        )
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.wf_id == "wf_8cd4fced-95a"
        assert ref.cc_agent_id == "af2d08f9a2862fe29"
        assert ref.session_id == SESSION

    def test_named_agent_id_with_dashes_survives(self):
        """cc_agent_id 不是纯 hex —— 生产里有 ``agent-autc-final-gate-<hex>.jsonl``。"""
        path = (
            f"{HOME}/.claude/projects/-Users-dev-Desktop-AI-team-OS/"
            f"{SESSION}/subagents/agent-autc-final-gate-4ada3682116587f3.jsonl"
        )
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.cc_agent_id == "autc-final-gate-4ada3682116587f3"


class TestWorktreePaths:
    """worktree 隔离的 agent：cwd 更深，slug 更长，但结构不变。"""

    SLUG = "-Users-dev-Desktop-AI-team-OS--claude-worktrees-agent-a45e819443a34dfb9"

    def test_worktree_slug_parses_and_keeps_session(self):
        path = (
            f"{HOME}/.claude/projects/{self.SLUG}/"
            f"{SESSION}/subagents/agent-ae25a929059984b04.jsonl"
        )
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.project_slug == self.SLUG
        assert ref.session_id == SESSION
        assert ref.cc_agent_id == "ae25a929059984b04"

    def test_worktree_slug_cross_check_against_its_own_root(self):
        root = "/Users/dev/Desktop/AI team OS/.claude/worktrees/agent-a45e819443a34dfb9"
        assert slug_matches_root(self.SLUG, root) is True
        # 与主 checkout 对不上 —— 这正是 mismatch 该被标出来的场景
        assert slug_matches_root(self.SLUG, "/Users/dev/Desktop/AI team OS") is False


class TestNonAsciiSlug:
    def test_non_ascii_directory_collapses_but_still_parses(self):
        # ~/Desktop/文档 → -Users-dev-Desktop---（三个汉字各一个连字符）
        slug = "-Users-dev-Desktop---"
        path = f"{HOME}/.claude/projects/{slug}/{SESSION}/subagents/agent-abc123.jsonl"
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.project_slug == slug
        assert ref.session_id == SESSION

    def test_two_different_roots_collapse_to_one_slug(self):
        """归属不能靠 slug：不同目录塌成同一个 slug，反查是多对一的。"""
        slug = "-Users-dev-Desktop---"
        assert slug_matches_root(slug, "/Users/dev/Desktop/文档") is True
        assert slug_matches_root(slug, "/Users/dev/Desktop/资料") is True

    def test_mismatch_is_the_only_informative_answer(self):
        slug = "-Users-dev-Desktop-AI-team-OS"
        assert slug_matches_root(slug, "/Volumes/ext-disk/other-project") is False
        assert slug_matches_root(slug, None) is False
        assert slug_matches_root("", "/Users/dev") is False


class TestMainSessionPaths:
    def test_main_session_transcript(self):
        path = f"{HOME}/.claude/projects/-Users-dev-Desktop-AI-team-OS/{SESSION}.jsonl"
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.kind == "main"
        assert ref.session_id == SESSION
        assert ref.wf_id is None
        assert ref.cc_agent_id is None

    def test_uuid_v7_style_session_id_is_accepted(self):
        """CC 新会话 id 已出现 v7 形态（生产实测 019f8b2f-…-71d1-…），不能按 v4 卡死。"""
        sid = "019f8b2f-1617-71d1-a5fa-7f828d177065"
        path = f"{HOME}/.claude/projects/-Users-dev/{sid}.jsonl"
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.session_id == sid


class TestRefusals:
    @pytest.mark.parametrize(
        "path",
        [
            "",
            None,
            "/tmp/random.jsonl",
            # 少了 /projects/ 段
            f"{HOME}/.claude/-Users-dev/{SESSION}/subagents/agent-x.jsonl",
            # session 段不是 uuid 形态
            f"{HOME}/.claude/projects/-slug/not-a-session/subagents/agent-x.jsonl",
            # 不是 .jsonl
            f"{HOME}/.claude/projects/-slug/{SESSION}/subagents/agent-x.txt",
        ],
    )
    def test_unparseable_returns_none_never_guesses(self, path):
        assert parse_transcript_path(path) is None
        assert derive_session_id(path) is None


class TestPurity:
    def test_parsing_touches_no_disk(self):
        """纯函数：路径指向的文件根本不存在也照样解析。"""
        path = f"/nowhere/projects/-slug/{SESSION}/subagents/agent-ghost.jsonl"
        ref = parse_transcript_path(path)
        assert ref is not None and ref.cc_agent_id == "ghost"

    def test_windows_backslashes_are_normalized(self):
        path = rf"C:\Users\x\.claude\projects\-slug\{SESSION}\subagents\agent-w1.jsonl"
        ref = parse_transcript_path(path)
        assert ref is not None
        assert ref.session_id == SESSION
        assert ref.cc_agent_id == "w1"

    def test_repeat_parse_is_identical(self):
        path = f"{HOME}/.claude/projects/-slug/{SESSION}/subagents/agent-a1.jsonl"
        assert parse_transcript_path(path) == parse_transcript_path(path)


class TestDeriveSessionIdShortcut:
    def test_matches_full_parse(self):
        path = (
            f"{HOME}/.claude/projects/-slug/{SESSION}"
            "/subagents/workflows/wf_abc-1/agent-a1.jsonl"
        )
        assert derive_session_id(path) == SESSION
