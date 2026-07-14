"""Worktree probe — read-only observation of git worktrees for one project.

Mirrors session_probe.py's design: a pure, on-demand probe with zero background
polling, invoked only when a caller (project_summary) asks for it. No new timer,
no daemon — consistent with this repo's "no background daemons" constraint.

See docs/worktree-governance-design.md section 4 ((c) worktree registry and
observation). This module only reports state; it never mutates a worktree.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Tighter than git_ops._run_git's 30s default: a broken/unreachable worktree
# must not stall the project_summary response (design §4/(c) point 5).
_GIT_TIMEOUT = 8

# Fallback base branch when `origin/HEAD` has no symbolic ref configured
# (true for this repo today; see worktree-governance-design.md section 3
# "前置条件"). Kept as a constant rather than hardcoded inline for clarity.
_DEFAULT_BASE_BRANCH = "master"


def _run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    """Run a read-only git command, bounded by _GIT_TIMEOUT. Never raises."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_GIT_TIMEOUT,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception:  # noqa: BLE001 — probe failure must not affect callers
        return 1, "", ""


def _parse_porcelain(output: str) -> list[dict]:
    """Parse `git worktree list --porcelain` stdout into raw entries.

    Each entry is separated by a blank line. Fields seen per entry: `worktree
    <path>`, `HEAD <sha>`, `branch refs/heads/<name>` (absent when detached),
    `locked [<reason>]`, `prunable [<reason>]`.
    """
    entries: list[dict] = []
    current: dict = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):].strip()
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "detached":
            current["branch"] = None
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
            reason = line[len("locked "):].strip() if line.startswith("locked ") else ""
            current["locked_reason"] = reason
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = True
    if current:
        entries.append(current)
    return entries


def _resolve_base_branch(root_path: str) -> str:
    """Resolve the merge-target base branch, falling back when unconfigured.

    This repo has no `origin/HEAD` symbolic ref today (confirmed by design
    doc §3's "前置条件" note), so the fallback path is the normal path here,
    not an edge case — it must not log noise or fail the probe.
    """
    code, out, _ = _run_git(["rev-parse", "--abbrev-ref", "origin/HEAD"], root_path)
    if code == 0 and out:
        return out.removeprefix("origin/")
    return _DEFAULT_BASE_BRANCH


def _is_dirty(path: str) -> bool:
    code, out, _ = _run_git(["status", "--porcelain"], path)
    return code == 0 and bool(out)


def _is_merged(path: str, base_branch: str) -> bool | None:
    """Whether HEAD is an ancestor of base_branch. None when undeterminable.

    Known limitation (accepted by design, not a bug): squash-merged branches
    will read as unmerged even when their content already landed, because the
    original commit is never an ancestor of base after a squash. This mirrors
    the same accepted limitation documented for the (b) hard-block guard.
    """
    code, _, _ = _run_git(["merge-base", "--is-ancestor", "HEAD", base_branch], path)
    if code == 0:
        return True
    if code == 1:
        return False
    return None  # base branch missing / other git failure — don't guess


def detect_worktrees(root_path: str) -> list[dict]:
    """Detect all non-main worktrees for the project at root_path.

    Returns one dict per subordinate worktree: path, branch, head (short),
    dirty, merged, locked, locked_reason. Returns [] on any failure so a
    broken probe never breaks project_summary (same contract as session_probe).
    """
    try:
        if not root_path or not Path(root_path).is_dir():
            return []
        code, out, _ = _run_git(["worktree", "list", "--porcelain"], root_path)
        if code != 0 or not out:
            return []
        entries = _parse_porcelain(out)
        if len(entries) <= 1:
            return []  # only the main worktree — nothing subordinate to report

        main_path = str(Path(entries[0]["path"]).resolve())
        base_branch = _resolve_base_branch(root_path)

        result: list[dict] = []
        for entry in entries[1:]:
            wt_path = entry.get("path", "")
            if not wt_path or str(Path(wt_path).resolve()) == main_path:
                continue  # defensive: never treat the main worktree as subordinate
            result.append({
                "path": wt_path,
                "branch": entry.get("branch"),
                "head": (entry.get("head") or "")[:8],
                "dirty": _is_dirty(wt_path),
                "merged": _is_merged(wt_path, base_branch),
                "locked": bool(entry.get("locked", False)),
                "locked_reason": entry.get("locked_reason") or None,
            })
        return result
    except Exception:  # noqa: BLE001 — probe failure must not affect callers
        return []
