"""Tests for worktree_probe.py — read-only worktree observation.

See docs/worktree-governance-design.md section 4 ((c) worktree registry and
observation). All scenarios use real throwaway git repos (no mocking of git
itself), mirroring the convention in tests/test_workflow_reminder.py.
"""

from __future__ import annotations

import subprocess

from aiteam.api import worktree_probe


def _git(args: list[str], cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo_with_worktree(tmp_path, *, dirty: bool = False, merged: bool = True):
    """Build a real git repo with one subordinate worktree.

    merged=True: the worktree branch's HEAD stays exactly at the same commit as
    master (trivially an ancestor). merged=False: the worktree gets one extra
    local commit master never sees.
    """
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git(["init", "-b", "master"], main_repo)
    _git(["config", "user.email", "test@example.com"], main_repo)
    _git(["config", "user.name", "Test"], main_repo)
    (main_repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], main_repo)
    _git(["commit", "-m", "initial"], main_repo)

    wt_path = tmp_path / "wt"
    _git(["worktree", "add", str(wt_path), "-b", "worktree-scenario"], main_repo)

    if not merged:
        (wt_path / "extra.txt").write_text("local only\n")
        _git(["add", "extra.txt"], wt_path)
        _git(["commit", "-m", "local unlanded work"], wt_path)

    if dirty:
        (wt_path / "README.md").write_text("changed but not committed\n")

    return str(main_repo), str(wt_path)


class TestParsePorcelain:
    def test_parses_main_plus_two_worktrees(self):
        output = (
            "worktree /repo\n"
            "HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "branch refs/heads/master\n"
            "\n"
            "worktree /repo/.claude/worktrees/wf_1\n"
            "HEAD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
            "branch refs/heads/worktree-wf_1\n"
            "\n"
            "worktree /repo/.claude/worktrees/wf_2\n"
            "HEAD cccccccccccccccccccccccccccccccccccccccc\n"
            "branch refs/heads/worktree-wf_2\n"
            "locked stale\n"
        )
        entries = worktree_probe._parse_porcelain(output)
        assert len(entries) == 3
        assert entries[0]["path"] == "/repo"
        assert entries[1]["branch"] == "worktree-wf_1"
        assert entries[2]["locked"] is True
        assert entries[2]["locked_reason"] == "stale"

    def test_detached_worktree_has_no_branch(self):
        output = (
            "worktree /repo\n"
            "HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "branch refs/heads/master\n"
            "\n"
            "worktree /repo/.claude/worktrees/detached\n"
            "HEAD dddddddddddddddddddddddddddddddddddddddd\n"
            "detached\n"
        )
        entries = worktree_probe._parse_porcelain(output)
        assert entries[1]["branch"] is None


class TestDetectWorktrees:
    def test_nonexistent_root_returns_empty(self):
        assert worktree_probe.detect_worktrees("/no/such/path/at/all") == []

    def test_empty_root_returns_empty(self):
        assert worktree_probe.detect_worktrees("") == []

    def test_repo_with_no_subordinate_worktrees_returns_empty(self, tmp_path):
        main_repo = tmp_path / "solo"
        main_repo.mkdir()
        _git(["init", "-b", "master"], main_repo)
        _git(["config", "user.email", "test@example.com"], main_repo)
        _git(["config", "user.name", "Test"], main_repo)
        (main_repo / "README.md").write_text("hello\n")
        _git(["add", "README.md"], main_repo)
        _git(["commit", "-m", "initial"], main_repo)

        assert worktree_probe.detect_worktrees(str(main_repo)) == []

    def test_clean_merged_worktree(self, tmp_path):
        main_repo, wt_path = _init_repo_with_worktree(tmp_path, dirty=False, merged=True)
        result = worktree_probe.detect_worktrees(main_repo)
        assert len(result) == 1
        entry = result[0]
        assert entry["path"] == wt_path
        assert entry["branch"] == "worktree-scenario"
        assert entry["dirty"] is False
        assert entry["merged"] is True
        assert entry["locked"] is False
        assert len(entry["head"]) == 8

    def test_dirty_worktree_flagged(self, tmp_path):
        main_repo, _ = _init_repo_with_worktree(tmp_path, dirty=True, merged=True)
        result = worktree_probe.detect_worktrees(main_repo)
        assert result[0]["dirty"] is True

    def test_unlanded_commit_flagged_unmerged(self, tmp_path):
        main_repo, _ = _init_repo_with_worktree(tmp_path, dirty=False, merged=False)
        result = worktree_probe.detect_worktrees(main_repo)
        assert result[0]["merged"] is False
        assert result[0]["dirty"] is False

    def test_main_worktree_never_reported_as_subordinate(self, tmp_path):
        main_repo, _ = _init_repo_with_worktree(tmp_path)
        result = worktree_probe.detect_worktrees(main_repo)
        paths = [e["path"] for e in result]
        assert main_repo not in paths


class TestResolveBaseBranch:
    def test_falls_back_when_no_symbolic_ref(self, tmp_path):
        main_repo = tmp_path / "solo"
        main_repo.mkdir()
        _git(["init", "-b", "master"], main_repo)
        # No origin remote configured at all -> must fall back, not raise.
        assert worktree_probe._resolve_base_branch(str(main_repo)) == "master"
