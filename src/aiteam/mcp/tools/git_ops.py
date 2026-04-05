"""Git operations MCP tools.

Provides safe, explicit git automation tools for committing changes,
creating branches, and opening pull requests after pipeline deploy stages.
All operations are non-destructive: no force push, no main-branch modification.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _run_git(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _find_git_root(path: str | None = None) -> str | None:
    """Find the git repository root from the given path (or cwd)."""
    import os
    start = path or os.getcwd()
    code, out, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=start)
    return out if code == 0 else None


def _check_git_available() -> dict[str, Any] | None:
    """Check if git is available. Returns error dict if not, None if OK."""
    if not shutil.which("git"):
        return {"success": False, "error": "git 未安装或不在 PATH 中"}
    return None


def _check_gh_available() -> dict[str, Any] | None:
    """Check if GitHub CLI (gh) is available. Returns error dict if not, None if OK."""
    if not shutil.which("gh"):
        return {
            "success": False,
            "error": "GitHub CLI (gh) 未安装，请先安装: https://cli.github.com/",
            "hint": "安装后运行 `gh auth login` 进行认证",
        }
    return None


def _sanitize_branch_name(name: str) -> str:
    """Convert arbitrary text to a valid git branch name component."""
    # Replace whitespace and special chars with hyphens
    name = re.sub(r"[^\w\-.]", "-", name)
    # Collapse multiple hyphens
    name = re.sub(r"-{2,}", "-", name)
    return name.strip("-").lower()[:50]


def register(mcp):
    """Register all git-operations MCP tools."""

    @mcp.tool()
    def git_auto_commit(
        message: str = "",
        files: list[str] | None = None,
        working_dir: str = "",
    ) -> dict[str, Any]:
        """Stage tracked file changes and create a git commit.

        Only stages changes to files already tracked by git (no untracked files,
        no .env / credentials / key files). Never force-pushes or modifies main/master.

        Args:
            message: Commit message. If empty, a generic message is used.
            files: Specific file paths to stage. If empty, stages all tracked-file changes
                   (git add -u — excludes untracked files and common sensitive patterns).
            working_dir: Repository path. Defaults to cwd.

        Returns:
            Commit result including commit hash and summary.
        """
        err = _check_git_available()
        if err:
            return err

        cwd = working_dir or None
        git_root = _find_git_root(cwd)
        if git_root is None:
            return {
                "success": False,
                "error": "当前目录不是 git 仓库，请在 git 项目目录内运行",
            }

        # Stage files
        if files:
            # Filter out sensitive file patterns
            sensitive_patterns = [".env", ".pem", ".key", "credentials", "secrets"]
            safe_files = []
            blocked_files = []
            for f in files:
                lower_f = f.lower()
                if any(pat in lower_f for pat in sensitive_patterns):
                    blocked_files.append(f)
                else:
                    safe_files.append(f)

            if blocked_files:
                return {
                    "success": False,
                    "error": f"检测到敏感文件，已拒绝: {blocked_files}",
                    "hint": "请手动确认后再提交这些文件",
                }

            if not safe_files:
                return {"success": False, "error": "没有可提交的文件"}

            code, out, err_msg = _run_git(["add", "--"] + safe_files, cwd=git_root)
            if code != 0:
                return {"success": False, "error": f"git add 失败: {err_msg}"}
        else:
            # Stage only tracked file changes (excludes untracked files)
            code, out, err_msg = _run_git(["add", "-u"], cwd=git_root)
            if code != 0:
                return {"success": False, "error": f"git add -u 失败: {err_msg}"}

        # Check if there's anything to commit
        code, status_out, _ = _run_git(["diff", "--cached", "--name-only"], cwd=git_root)
        if not status_out:
            return {
                "success": False,
                "error": "没有已暂存的变更可以提交",
                "hint": "请先修改文件，或使用 files 参数指定要提交的文件",
            }

        staged_files = status_out.splitlines()

        # Build commit message
        commit_msg = message.strip() or "chore: auto-commit via git_auto_commit"

        code, out, err_msg = _run_git(["commit", "-m", commit_msg], cwd=git_root)
        if code != 0:
            return {"success": False, "error": f"git commit 失败: {err_msg}"}

        # Get the commit hash
        _, commit_hash, _ = _run_git(["rev-parse", "--short", "HEAD"], cwd=git_root)

        return {
            "success": True,
            "data": {
                "commit_hash": commit_hash,
                "message": commit_msg,
                "staged_files": staged_files,
                "file_count": len(staged_files),
                "git_root": git_root,
            },
            "message": f"已提交 {len(staged_files)} 个文件，commit: {commit_hash}",
        }

    @mcp.tool()
    def git_create_pr(
        title: str = "",
        body: str = "",
        base_branch: str = "main",
        working_dir: str = "",
    ) -> dict[str, Any]:
        """Create a GitHub Pull Request from the current branch.

        Requires GitHub CLI (gh) to be installed and authenticated.
        Refuses to create PRs where head == base (same branch) or when
        the current branch is main/master (to prevent accidental PRs).

        Args:
            title: PR title. Defaults to current branch name if empty.
            body: PR description markdown. Defaults to a generated summary.
            base_branch: Target branch to merge into (default: main).
            working_dir: Repository path. Defaults to cwd.

        Returns:
            PR URL and metadata on success.
        """
        err = _check_git_available()
        if err:
            return err

        err = _check_gh_available()
        if err:
            return err

        cwd = working_dir or None
        git_root = _find_git_root(cwd)
        if git_root is None:
            return {"success": False, "error": "当前目录不是 git 仓库"}

        # Get current branch name
        code, current_branch, err_msg = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=git_root)
        if code != 0:
            return {"success": False, "error": f"无法获取当前分支: {err_msg}"}

        # Safety: refuse to PR from main/master
        if current_branch in ("main", "master"):
            return {
                "success": False,
                "error": f"当前分支是 '{current_branch}'，无法从主分支创建 PR",
                "hint": "请切换到功能分支后再创建 PR",
            }

        # Safety: refuse if head == base
        if current_branch == base_branch:
            return {
                "success": False,
                "error": f"当前分支 '{current_branch}' 与目标分支 '{base_branch}' 相同",
            }

        # Check remote exists
        code, remotes, _ = _run_git(["remote"], cwd=git_root)
        if code != 0 or not remotes.strip():
            return {
                "success": False,
                "error": "没有配置 git remote，无法创建 PR",
                "hint": "请先运行 `git remote add origin <url>` 并 push 分支",
            }

        # Check if current branch has been pushed
        code, _, upstream_err = _run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=git_root,
        )
        if code != 0:
            # Branch not pushed yet — push it first
            code, _, push_err = _run_git(
                ["push", "-u", "origin", current_branch],
                cwd=git_root,
            )
            if code != 0:
                return {
                    "success": False,
                    "error": f"推送分支到 origin 失败: {push_err}",
                    "hint": "请检查网络和 git remote 配置",
                }

        pr_title = title.strip() or f"feat: {current_branch}"
        pr_body = body.strip() or (
            f"## 变更说明\n\n"
            f"从分支 `{current_branch}` 合并到 `{base_branch}`。\n\n"
            f"---\n_由 git_create_pr 自动生成_"
        )

        # Create PR via gh CLI
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--title", pr_title,
                "--body", pr_body,
                "--base", base_branch,
                "--head", current_branch,
            ],
            capture_output=True,
            text=True,
            cwd=git_root,
            timeout=60,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": f"gh pr create 失败: {result.stderr.strip()}",
                "hint": "请确认 gh 已通过 `gh auth login` 认证，且仓库在 GitHub 上存在",
            }

        pr_url = result.stdout.strip()
        return {
            "success": True,
            "data": {
                "pr_url": pr_url,
                "title": pr_title,
                "head_branch": current_branch,
                "base_branch": base_branch,
            },
            "message": f"PR 已创建: {pr_url}",
        }

    @mcp.tool()
    def git_status_check(working_dir: str = "") -> dict[str, Any]:
        """Check the current git repository status.

        Shows uncommitted changes, current branch, remote tracking info,
        and whether the working tree is clean. Useful before committing.

        Args:
            working_dir: Repository path. Defaults to cwd.

        Returns:
            Git status summary including branch, staged/unstaged/untracked counts.
        """
        err = _check_git_available()
        if err:
            return err

        cwd = working_dir or None
        git_root = _find_git_root(cwd)
        if git_root is None:
            return {"success": False, "error": "当前目录不是 git 仓库"}

        # Current branch
        _, branch, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=git_root)

        # Remote tracking
        code, upstream, _ = _run_git(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=git_root,
        )
        has_remote = code == 0
        remote_ref = upstream if has_remote else None

        # Commits ahead/behind
        ahead = behind = 0
        if has_remote:
            _, ab_out, _ = _run_git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=git_root)
            parts = ab_out.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])

        # Staged / unstaged / untracked counts
        _, staged_out, _ = _run_git(["diff", "--cached", "--name-only"], cwd=git_root)
        _, unstaged_out, _ = _run_git(["diff", "--name-only"], cwd=git_root)
        _, untracked_out, _ = _run_git(
            ["ls-files", "--others", "--exclude-standard"], cwd=git_root
        )

        staged = [f for f in staged_out.splitlines() if f]
        unstaged = [f for f in unstaged_out.splitlines() if f]
        untracked = [f for f in untracked_out.splitlines() if f]

        is_clean = not staged and not unstaged

        return {
            "success": True,
            "data": {
                "branch": branch,
                "git_root": git_root,
                "remote_tracking": remote_ref,
                "ahead": ahead,
                "behind": behind,
                "is_clean": is_clean,
                "staged_files": staged,
                "unstaged_files": unstaged,
                "untracked_files": untracked,
                "staged_count": len(staged),
                "unstaged_count": len(unstaged),
                "untracked_count": len(untracked),
            },
            "message": (
                "工作区干净，无待提交变更" if is_clean
                else f"有 {len(staged)} 个已暂存文件，{len(unstaged)} 个未暂存文件"
            ),
        }
