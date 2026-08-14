#!/usr/bin/env python3
"""Workflow reminder — lightweight PreToolUse/PostToolUse hook.

Only reads/writes local state files and outputs reminders to stdout, no HTTP calls.
Goal is to complete within 100ms to avoid CC hook timeout.
Usage: python -m aiteam.hooks.workflow_reminder <PreToolUse|PostToolUse>
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

_SUPERVISOR_STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os")
_SUPERVISOR_STATE_FILE = os.path.join(_SUPERVISOR_STATE_DIR, "supervisor-state.json")
_PORT_FILE = os.path.join(_SUPERVISOR_STATE_DIR, "api_port.txt")
_SUBAGENT_MARKER_DIR = os.path.join(_SUPERVISOR_STATE_DIR, "subagent_sessions")


def _safe_session_id(session_id: str) -> str:
    """Strip anything that isn't alphanumeric, hyphen, or underscore to prevent path traversal."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", session_id)


def _is_subagent_session(session_id: str) -> bool:
    """Return True if the given session was marked as a sub-agent by SubagentStart."""
    if not session_id:
        return False
    safe_id = _safe_session_id(session_id)
    if not safe_id:
        return False
    try:
        return os.path.isfile(os.path.join(_SUBAGENT_MARKER_DIR, safe_id))
    except Exception:
        return False


# Returned by _run_git_readonly when the probe never ran to completion (git
# missing, timeout, OS error). Distinct from a non-zero exit code, which means
# git ran and answered - see the docstring below for why collapsing the two is
# how a hard-block guard turns into a silent pass-through.
_GIT_UNAVAILABLE = -1


def _run_git_readonly(
    args: list[str], cwd: str, timeout: float = 5.0, stdin_text: str | None = None
) -> tuple[int, str]:
    """Run a read-only git command, returning (returncode, stripped stdout).

    Short timeout and narrow scope: this only ever runs after a rare, already-matched
    dangerous command pattern (S4 below), never on every Bash call, so it does not
    threaten this hook's general 100ms budget.

    Three-state on purpose: `_GIT_UNAVAILABLE` means "the probe did not run", any
    other non-zero code means "git ran and said no". Those are not the same fact
    and callers must not merge them - the old two-state version made
    "git is not on PATH" and "this directory is not a repository" indistinguishable,
    so a hung or missing git read as "nothing to protect here" and the guard let
    dirty worktrees be deleted.

    `stdin_text` feeds `git rev-list --stdin`: the exclusion list can hold hundreds
    of refs in a real repo (210 in the sample that motivated this), well past what
    is safe to splice into an argv.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin_text,
        )
        return result.returncode, result.stdout.strip()
    except Exception:
        return _GIT_UNAVAILABLE, ""


# ── S4 criterion: git reachability, NOT "did this land on the default branch" ──
#
# 2026-08-14 rework (user-authorized, both defects measured end-to-end first).
# The question this guard actually has to answer is "if this teardown happens,
# does any commit become unreachable?", and git answers exactly that question
# exactly. The previous criterion asked "is this merged into master?" instead,
# and got two things wrong:
#
#   (1) Wrong target. `git worktree remove` does not delete branches. Measured
#       on a throwaway repo: after removing a worktree whose HEAD was attached,
#       the branch ref was still there and
#       `git rev-list <sha> --not --branches --remotes --tags` printed nothing.
#       Only a DETACHED worktree can orphan commits on removal. So hard-blocking
#       committed work on an attached branch protected nothing while blocking
#       routine cleanup.
#   (2) Wrong baseline. `origin/HEAD` -> master/main is not where development
#       lives in every repo. Measured on a real work repo where development sits
#       on a long-lived feature branch ~1090 commits ahead of master: a chore
#       branch cut from that parent shows 810 "not equivalent" commits against
#       master and 0 against its actual parent - an 810x false positive that
#       hard-blocked a legitimate cleanup.
#
# The old helpers (_main_branch_name / _cherry_lines / _all_commits_patch_equivalent
# / _cherry_breakdown) were deleted with this change, deliberately: patch-id
# equivalence against a guessed base branch is a proxy for reachability, and the
# proxy is what broke. Do not reintroduce them here.
#
# ASYMMETRY (the rule every branch below obeys): a false ALLOW loses work
# silently and irreversibly; a false BLOCK costs one human `--force` after
# reading the message. So every "cannot determine" path - git missing, timeout,
# for-each-ref empty, rev-list failing, HEAD state unreadable, a teardown target
# the tokenizer cannot pin down - resolves to BLOCK, never to ALLOW. The single
# exception is "this path has nothing to do with git at all" (no .git present
# and git says it is not a working tree), where there is no committed or
# uncommitted git state to lose.
#
# 2026-08-14 adversarial-review round 2 closed the places where that rule was
# only claimed. Each was reproduced on a real repo before being changed:
#   - `git status` failing was read as "nothing to say", so a pruned/corrupt
#     worktree full of uncommitted work was handed to `rm -rf` (git errors, rm
#     does not). Now: an uninspectable directory that still carries .git blocks.
#   - "HEAD is attached, so the branch survives" is only true for a LINKED
#     worktree. A self-contained clone under .claude/worktrees/ keeps its refs
#     inside the directory being deleted, so the branch dies with it.
#   - `git branch -D a a-backup` let the two operands alibi each other: each
#     probe excluded only its own ref, so each found the other still reaching
#     the commits, and both were deleted. Exclusion is now the union of every
#     ref the whole command line destroys.
#   - Refname exclusion compared raw strings, so on a case-insensitive or
#     Unicode-normalizing filesystem the doomed ref failed to match its own
#     stored spelling, the exclusion silently did nothing, and the orphan set
#     came back empty for every branch. Comparison is now normalized (wide
#     exclusion errs toward blocking).
#   - Ignored-but-precious files (.env, data/, *.db) are not in
#     `git status --porcelain`, so "clean" worktrees whose only unique content
#     was ignored files were waved through. They are now weighed.

_REACHABILITY_REF_NAMESPACES = ("refs/heads", "refs/remotes", "refs/tags")

# Enough hashes to make the message actionable without turning a block into a wall.
_ORPHAN_SAMPLE_LIMIT = 10

# Ignored paths that a teardown may destroy without losing anything: caches and
# build output that a command regenerates. Anything else that is ignored (.env,
# data/, *.db, local notes) is by definition tracked by nothing and pushed
# nowhere - git cannot get it back, so it weighs the same as uncommitted work.
_REGENERABLE_IGNORED_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".cache",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "htmlcov",
        ".coverage",
        "coverage",
        ".next",
        ".turbo",
        ".parcel-cache",
        ".gradle",
        ".DS_Store",
    }
)
_REGENERABLE_IGNORED_SUFFIXES = (".pyc", ".pyo", ".egg-info", ".log", ".tmp")

# Enough names to make the message actionable; the count carries the rest.
_IGNORED_SAMPLE_LIMIT = 5


def _norm_ref(name: str) -> str:
    """Fold a refname for comparison on filesystems that fold it for storage.

    macOS (APFS) matches refs case-insensitively and normalizes Unicode, so
    `git branch -D worktree-wf_case` really deletes `refs/heads/worktree-Wf_Case`
    while an exact string comparison fails to recognize the two as the same ref.
    That miss used to leave the doomed ref inside the exclusion list, which makes
    the orphan set empty for every branch - a blanket false allow. Folding wide
    can only over-exclude, i.e. over-block, which is the safe direction.
    """
    import unicodedata

    return unicodedata.normalize("NFC", name).casefold()


def _format_orphans(orphans: list[str]) -> str:
    listed = ", ".join(orphans[:_ORPHAN_SAMPLE_LIMIT])
    return listed + ("…" if len(orphans) >= _ORPHAN_SAMPLE_LIMIT else "")


def _orphan_commits(
    path: str,
    rev: str,
    exclude_refs: tuple[str, ...] = (),
    namespaces: tuple[str, ...] = _REACHABILITY_REF_NAMESPACES,
) -> tuple[bool, list[str]]:
    """Commits reachable from `rev` but from no other ref. Returns (determined, shas).

    `determined` is False when the probe could not be completed (git failure,
    timeout, no refs at all) - callers must treat that as "block", never as
    "nothing found". `exclude_refs` holds full refnames (refs/heads/<branch>)
    that are about to disappear, e.g. every branch a `git branch -D` line would
    delete; they are filtered out in Python instead of relying on git's own
    `--not --branches`, which would include the doomed refs themselves and make
    the orphan set come back empty for EVERY branch - a blanket false allow.

    `namespaces` narrows what counts as surviving evidence. The default is every
    local ref; a directory that carries its own git dir passes ("refs/remotes",)
    instead, because its local refs are inside what is about to be deleted.
    """
    code, out = _run_git_readonly(
        ["for-each-ref", "--format=%(refname)"] + list(namespaces),
        cwd=path,
        timeout=8.0,
    )
    if code != 0 or not out:
        return False, []

    excluded = {_norm_ref(ref) for ref in exclude_refs}
    keep = [
        ln.strip()
        for ln in out.splitlines()
        if ln.strip() and _norm_ref(ln.strip()) not in excluded
    ]

    # One rev per line on stdin, exclusions prefixed with '^'; argv would overflow
    # on a repo with hundreds of remote branches and tags.
    stdin_text = "\n".join([rev] + [f"^{ref}" for ref in keep]) + "\n"
    rc, revs = _run_git_readonly(
        ["rev-list", "--stdin", f"--max-count={_ORPHAN_SAMPLE_LIMIT}", "--abbrev-commit"],
        cwd=path,
        timeout=8.0,
        stdin_text=stdin_text,
    )
    if rc != 0:
        return False, []
    return True, [ln.strip() for ln in revs.splitlines() if ln.strip()]


def _precious_ignored_paths(status_lines: list[str]) -> list[str]:
    """Ignored entries that no ref and no remote can bring back.

    `git status --porcelain --ignored` marks them with '!!'. Caches and build
    output are filtered out; what remains (.env, data/, *.db, scratch notes) is
    content git was never asked to hold, which makes a teardown the one and only
    way to lose it.
    """
    precious: list[str] = []
    for line in status_lines:
        if not line.startswith("!!"):
            continue
        rel = line[2:].strip().strip('"').rstrip("/")
        if not rel:
            continue
        parts = [p for p in rel.split("/") if p]
        if any(
            p in _REGENERABLE_IGNORED_NAMES or p.endswith(_REGENERABLE_IGNORED_SUFFIXES)
            for p in parts
        ):
            continue
        precious.append(rel)
    return precious


def _path_is_inside(child: str, parent: str) -> bool:
    """True when `child` is `parent` or lives under it (symlinks resolved)."""
    try:
        c = os.path.realpath(child)
        p = os.path.realpath(parent)
        return c == p or c.startswith(p.rstrip(os.sep) + os.sep)
    except Exception:
        return True  # cannot tell -> treat as "refs die with the directory"


def _assess_worktree_teardown(path: str) -> tuple[bool, str | None, str | None]:
    """Read-only "is it safe to tear down this worktree" assessment.

    Returns (dirty, block_reason, advisory):
      - dirty: uncommitted/untracked changes exist. Tearing the worktree down
        destroys them with no ref to fall back on, so this stays a hard block.
      - block_reason: set when the teardown would really lose content - orphaned
        commits, ignored-and-unrecoverable files, refs that live inside the
        directory - or when the probe could not be completed (conservative).
      - advisory: non-blocking note. For an attached HEAD it states the branch
        survives the removal, and says outright when that branch is the only
        thing still reaching those commits, because the follow-up cleanup
        (`git branch -D`) is exactly where they would be lost.

    Never raises. Only a path with no git state at all returns (False, None, None).
    """
    # 1. Is this something git can inspect? "not a working tree" and "the probe
    #    did not run" are different answers and only the first one is silence.
    inside_code, inside = _run_git_readonly(["rev-parse", "--is-inside-work-tree"], cwd=path)
    if inside_code == _GIT_UNAVAILABLE:
        return False, "git 探测无法完成（git 不可用/超时），无法确认该目录里没有未提交工作", None
    if inside_code != 0 or inside != "true":
        if os.path.exists(os.path.join(path, ".git")):
            # A worktree whose admin entry was pruned, or a damaged repo: git
            # refuses to look, but the files - including never-committed ones -
            # are still on disk and `rm -rf` will not complain about any of this.
            return (
                False,
                "该目录带有 .git 但 git 无法检查它（worktree 已被 prune / 管理目录损坏 / 权限问题），"
                "无法确认里面没有未提交内容",
                None,
            )
        return False, None, None

    # 2. Uncommitted work outranks everything else and is cheap to find.
    #    The '.' pathspec keeps the question about the directory being deleted:
    #    at a worktree root it changes nothing, but for a plain subdirectory of
    #    some other repo it stops the containing repo's unrelated edits from
    #    being reported as this target's uncommitted work.
    #    --no-optional-locks so a read-only probe never writes to the target.
    status_code, status_out = _run_git_readonly(
        ["--no-optional-locks", "status", "--porcelain", "--ignored", "."], cwd=path
    )
    if status_code != 0:
        # Step 1 already proved this IS a working tree, so a failure here is a
        # failed probe, not an absence of findings.
        return (
            False,
            "无法读取该 worktree 的 git 状态（探测失败/超时），不能确认其中没有未提交工作",
            None,
        )
    lines = [ln for ln in status_out.splitlines() if ln.strip()]
    if any(not ln.startswith("!!") for ln in lines):
        return True, None, None

    precious = _precious_ignored_paths(lines)
    precious_reason = None
    if precious:
        listed = "、".join(precious[:_IGNORED_SAMPLE_LIMIT])
        more = f" 等 {len(precious)} 项" if len(precious) > _IGNORED_SAMPLE_LIMIT else ""
        precious_reason = (
            f"存在被 .gitignore 忽略、git 完全兜不住的文件（{listed}{more}）——"
            "它们不在任何 commit 里，删掉即永久消失"
        )

    # 3. Linked worktree, or a self-contained repo? The whole "the branch
    #    survives the removal" argument rests on the refs living somewhere else.
    common_code, common_dir = _run_git_readonly(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path
    )
    if common_code != 0:
        # git < 2.31 has no --path-format; its answer is relative to the worktree.
        common_code, rel_common = _run_git_readonly(["rev-parse", "--git-common-dir"], cwd=path)
        common_dir = os.path.abspath(os.path.join(path, rel_common)) if rel_common else ""
    if common_code != 0 or not common_dir:
        return False, "无法确定该 worktree 的 git 目录位置（探测失败/超时）", None

    if _path_is_inside(common_dir, path):
        # Its refs are inside the directory being torn down, so nothing local
        # outlives it. Only a remote-tracking ref proves a copy exists elsewhere.
        determined, orphans = _orphan_commits(path, "HEAD", namespaces=("refs/remotes",))
        if not determined:
            return (
                False,
                "该目录是自带 .git 的独立仓库（不是链接 worktree），删除会连同它的全部分支/引用一起消失，"
                "且无法确认远端还有副本（探测失败/无远端跟踪引用）",
                None,
            )
        if orphans:
            return (
                False,
                "该目录是自带 .git 的独立仓库（不是链接 worktree），删除会连同它的全部分支/引用一起消失，"
                f"而这些 commit 在任何远端跟踪引用上都没有副本（{_format_orphans(orphans)}）",
                None,
            )
        return False, precious_reason, None

    # 4. Attached HEAD: the branch outlives the worktree. Detached HEAD: nothing does.
    head_code, head_branch = _run_git_readonly(
        ["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=path
    )
    if head_code == _GIT_UNAVAILABLE:
        return False, "无法读取 HEAD 状态（探测失败/超时），不能确认移除后 commit 仍可找回", None
    if head_code == 0 and head_branch:
        self_ref = f"refs/heads/{head_branch}"
        determined, orphans = _orphan_commits(path, self_ref, exclude_refs=(self_ref,))
        if not determined:
            advisory = (
                f"HEAD 附着在分支「{head_branch}」上，移除 worktree 不会删除该分支；"
                "但无法确认这些 commit 是否还有别的引用（探测失败），删该分支前请自行确认"
            )
        elif orphans:
            # The advisory is also an instruction, so it must not point at a
            # cleanup that destroys the work: this branch is the only ref left.
            advisory = (
                f"HEAD 附着在分支「{head_branch}」上，移除 worktree 不会删除该分支；"
                f"但该分支是这些 commit 的唯一引用（{_format_orphans(orphans)}），"
                f"之后再 git branch -d/-D {head_branch} 就会真的丢掉它们"
            )
        else:
            advisory = (
                f"HEAD 附着在分支「{head_branch}」上，移除 worktree 不会删除该分支"
                f"（commit 仍可从 {head_branch} 到达）；要连分支一起清掉需另行 git branch -d/-D"
            )
        return False, precious_reason, advisory

    determined, orphans = _orphan_commits(path, "HEAD")
    if not determined:
        return False, "HEAD 处于游离状态（detached），且无法完成可达性探测（git 探测失败/超时）", None
    if orphans:
        return (
            False,
            f"HEAD 处于游离状态（detached），有 commit 不被任何分支/远端/tag 引用"
            f"（{_format_orphans(orphans)}），移除后将无法找回",
            None,
        )
    return False, precious_reason, None


# ── S4 command recognition: tokens, not one regex per spelling ────────────
#
# Regex-per-spelling was measured to leak, badly. `-ff`, `-f --`,
# `--delete --force`, `-D -f`, `git -C <dir> branch -D`, `cd <dir> && git branch -D`,
# a trailing `xargs git branch -D`, a backslash line continuation and a second
# operand after the first all slipped through while really destroying refs or
# worktrees. The failure mode is structural: every new spelling is a new hole,
# and a hole in a hard-block guard is a silent loss.
#
# So the command line is tokenized once and the teardown verbs are recognized on
# tokens: any leading-dash token is a flag, `--` ends the flag section, every
# operand is examined (not just the first), and an operand the tokenizer cannot
# pin down (variable, command substitution) blocks instead of being skipped.

_WORKTREES_MARKER = ".claude/worktrees"


def _split_shell_segments(cmd: str) -> list[str]:
    """Split a command line into simple commands on ; | & && || and newlines.

    Quote-aware, deliberately not a shell parser: the split only decides where
    one command's operands stop. Splitting too eagerly costs an unparsed
    operand, which routes to the conservative branch; the reverse would let a
    second command hide inside the first one's operand list.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
        elif ch in ";\n&|":
            segments.append("".join(buf))
            buf = []
            i += 2 if cmd[i : i + 2] in ("&&", "||") else 1
        else:
            buf.append(ch)
            i += 1
    segments.append("".join(buf))
    cleaned = []
    for seg in segments:
        # Subshell/group brackets are noise here: `(cd x && git branch -D y)`
        # must tokenize to the same thing as the bare command.
        seg = re.sub(r"^[\s({]+", "", seg)
        seg = re.sub(r"[\s)}]+$", "", seg)
        if seg.strip():
            cleaned.append(seg.strip())
    return cleaned


def _shell_tokens(segment: str) -> list[str]:
    """Tokenize one simple command, falling back to a quote-aware split."""
    import shlex

    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        return [t.strip("'\"") for t in re.findall(r'"[^"]*"|\'[^\']*\'|\S+', segment)]


def _is_indeterminate_token(tok: str) -> bool:
    """A token whose real value only exists at runtime.

    Variables, command substitution, and `find -exec`'s `{}` placeholder: the
    guard cannot resolve any of them, which is exactly why they must not be
    quietly skipped.
    """
    return "$" in tok or "`" in tok or "{}" in tok


def _resolve_path(raw: str, cwd: str) -> str:
    return os.path.abspath(os.path.join(cwd, os.path.expanduser(raw)))


def _program_name(token: str) -> str:
    """Basename of a command token, Windows spellings included (`C:\\git.exe`)."""
    name = re.split(r"[\\/]", token)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _rm_invocations(tokens: list[str]) -> list[tuple[list[str], list[str]]]:
    """Every `rm ...` in a token list, as (flags, operands).

    Scanned at any position, not only the first: `sudo rm -rf x`,
    `... | xargs rm -rf` and `find <dir> -exec rm -rf {} +` all put rm in the
    middle, and the last two carry their targets outside the token list
    entirely - which the caller resolves conservatively rather than skipping.
    """
    calls: list[tuple[list[str], list[str]]] = []
    for idx, tok in enumerate(tokens):
        if _program_name(tok) == "rm":
            calls.append(_split_flags_operands(tokens[idx + 1 :]))
    return calls


def _git_calls(tokens: list[str], cwd: str) -> list[tuple[str, list[str]]]:
    """Every `git ...` inside a token list, as (working directory, argument tokens).

    `git -C <dir>` and `--git-dir=` are honoured: they are how an agent avoids a
    `cd`, and probing the wrong repository answers a question nobody asked -
    measured as a full bypass of the branch-deletion guard.
    """
    calls: list[tuple[str, list[str]]] = []
    for idx, tok in enumerate(tokens):
        if _program_name(tok) != "git":
            continue
        call_cwd = cwd
        i = idx + 1
        while i < len(tokens):
            t = tokens[i]
            if t in ("-C", "--work-tree") and i + 1 < len(tokens):
                call_cwd = _resolve_path(tokens[i + 1], call_cwd)
                i += 2
                continue
            if t.startswith("--work-tree="):
                call_cwd = _resolve_path(t.split("=", 1)[1], call_cwd)
                i += 1
                continue
            if t == "--git-dir" and i + 1 < len(tokens):
                t = f"--git-dir={tokens[i + 1]}"
                i += 1
            if t.startswith("--git-dir="):
                git_dir = _resolve_path(t.split("=", 1)[1], call_cwd)
                call_cwd = (
                    os.path.dirname(git_dir) if os.path.basename(git_dir) == ".git" else git_dir
                )
                i += 1
                continue
            if t == "-c" and i + 1 < len(tokens):
                i += 2
                continue
            if t.startswith("-"):
                i += 1
                continue
            break
        calls.append((call_cwd, tokens[i:]))
    return calls


def _split_flags_operands(args: list[str]) -> tuple[list[str], list[str]]:
    """Split argument tokens into (flags, operands). `--` ends the flag section."""
    flags: list[str] = []
    operands: list[str] = []
    after_ddash = False
    for tok in args:
        if after_ddash:
            operands.append(tok)
        elif tok == "--":
            after_ddash = True
        elif tok.startswith("-") and len(tok) > 1:
            flags.append(tok)
        else:
            operands.append(tok)
    return flags, operands


def _branch_delete_refs(args: list[str]) -> tuple[bool, list[str], list[str]]:
    """Parse `branch ...` tokens -> (is_delete, doomed refnames, indeterminate operands).

    Covers -d/-D/--delete in any order or combination with --force/-f/-q, plus
    -r/--remotes (which deletes a remote-tracking ref, equally destructible).
    """
    flags, operands = _split_flags_operands(args[1:])
    is_delete = False
    remote = False
    for flag in flags:
        if flag.startswith("--"):
            name = flag.split("=", 1)[0]
            if name == "--delete":
                is_delete = True
            elif name == "--remotes":
                remote = True
        else:
            for ch in flag[1:]:
                if ch in "dD":
                    is_delete = True
                elif ch == "r":
                    remote = True
    if not is_delete:
        return False, [], []
    namespace = "refs/remotes/" if remote else "refs/heads/"
    refs: list[str] = []
    unknown: list[str] = []
    for op in operands:
        if _is_indeterminate_token(op):
            unknown.append(op)
        elif op.startswith("refs/"):
            refs.append(op)
        else:
            refs.append(namespace + op)
    if not refs and not unknown:
        # `... | xargs git branch -D`: the operands arrive at runtime.
        unknown.append("(无操作数)")
    return True, refs, unknown


def _update_ref_delete_refs(args: list[str]) -> tuple[list[str], list[str]]:
    """`git update-ref -d <ref>` deletes a ref just as thoroughly as branch -D."""
    flags, operands = _split_flags_operands(args[1:])
    if not any(f == "-d" or f == "--delete" for f in flags):
        return [], []
    refs: list[str] = []
    unknown: list[str] = []
    for op in operands[:1]:
        if _is_indeterminate_token(op):
            unknown.append(op)
        elif op.startswith("refs/"):
            refs.append(op)
    return refs, unknown


def _child_dirs(path: str) -> list[str]:
    try:
        return sorted(
            os.path.join(path, name)
            for name in os.listdir(path)
            if os.path.isdir(os.path.join(path, name))
        )
    except Exception:
        return []


def _rm_worktree_targets(operand: str, cwd: str) -> tuple[list[str], str | None]:
    """Worktree directories a recursive `rm` operand would destroy.

    Returns (directories to assess, indeterminate reason). Covers the shapes the
    old regex missed: the worktrees directory itself, a glob over it, and a
    parent that merely contains it - each of which takes every worktree with it.
    """
    norm = operand.replace("\\", "/")
    touches_worktrees = _WORKTREES_MARKER in norm
    if _is_indeterminate_token(operand):
        if touches_worktrees:
            return [], f"删除目标含变量/命令替换（{operand}），无法确定会删掉哪些 worktree"
        return [], None
    if any(ch in operand for ch in "*?["):
        if not touches_worktrees:
            return [], None
        import glob

        pattern = operand if os.path.isabs(operand) else os.path.join(cwd, operand)
        return [p for p in glob.glob(os.path.expanduser(pattern)) if os.path.isdir(p)], None

    target = _resolve_path(operand, cwd)
    if touches_worktrees:
        if norm.rstrip("/").endswith(_WORKTREES_MARKER):
            return _child_dirs(target), None
        return ([target] if os.path.isdir(target) else []), None
    # A linked worktree anywhere on disk, recognized without spawning git: only
    # a linked worktree has `.git` as a FILE (a gitdir pointer) instead of a
    # directory. Worktrees are not always parked under .claude/worktrees - the
    # real repo that motivated this guard keeps one in /tmp - and a plain
    # `rm -rf` there loses uncommitted work exactly the same way.
    if os.path.isfile(os.path.join(target, ".git")):
        return [target], None
    # A parent that contains the worktrees directory takes all of them with it.
    nested = os.path.join(target, ".claude", "worktrees")
    if os.path.isdir(nested):
        return _child_dirs(nested), None
    return [], None


def _check_worktree_teardown_guard(cmd: str, base_cwd: str) -> list[str]:
    """S4 driver: plan every teardown in the command line, then assess each.

    Planning happens before assessment for one reason that is not stylistic: the
    exclusion set for the reachability probe has to be the union of every ref the
    WHOLE line destroys. Probing one ref at a time, excluding only itself, lets
    `git branch -D worktree-x worktree-x-backup` pass because each one is still
    reachable from the other - both were then deleted and the commits orphaned
    (measured, and a regression against the pre-2026-08-14 guard).

    Returns advisories; blocks by exiting with code 2.
    """
    advisories: list[str] = []
    # Backslash-newline is a continuation, not a command boundary.
    joined = re.sub(r"\\[ \t]*\n", " ", cmd)

    cwd = base_cwd
    teardowns: list[tuple[str, str]] = []  # (directory, how it is being torn down)
    deletions: list[tuple[str, str]] = []  # (probe cwd, doomed refname)
    undetermined: list[str] = []  # parse-level "cannot tell" -> block

    # A worktree path arriving through a pipe (`... | xargs rm -rf`,
    # `find ... -exec rm -rf {} +`) is not in the token list at all, so the only
    # honest reading of "recursive rm, no resolvable target, but this line is
    # about the worktrees directory" is "cannot determine".
    mentions_worktrees = _WORKTREES_MARKER in joined.replace("\\", "/")

    for segment in _split_shell_segments(joined):
        try:
            tokens = _shell_tokens(segment)
            if not tokens:
                continue
            if tokens[0] == "cd" and len(tokens) >= 2:
                # `cd x && git branch -D y` probes the repo at x, not the event cwd.
                if not _is_indeterminate_token(tokens[1]):
                    cwd = _resolve_path(tokens[1], cwd)
                continue

            for flags, operands in _rm_invocations(tokens):
                if not any(
                    f == "--recursive"
                    or (not f.startswith("--") and any(c in "rR" for c in f[1:]))
                    for f in flags
                ):
                    continue  # non-recursive rm cannot take a worktree down
                found = 0
                for operand in operands:
                    targets, reason = _rm_worktree_targets(operand, cwd)
                    if reason:
                        undetermined.append(f"用 rm -rf 删除 worktree 目录：{reason}")
                        found += 1
                    found += len(targets)
                    teardowns.extend((t, "用 rm -rf 删除 worktree 目录") for t in targets)
                runtime_operands = not operands or any(
                    _is_indeterminate_token(op) for op in operands
                )
                if not found and runtime_operands and mentions_worktrees:
                    undetermined.append(
                        "用 rm -rf 删除 worktree 目录：命令提到了 .claude/worktrees，"
                        "但删除目标来自管道/find/变量（解析不出具体路径）"
                    )

            for call_cwd, args in _git_calls(tokens, cwd):
                if not args:
                    continue
                if args[0] == "worktree" and len(args) > 1 and args[1] == "remove":
                    _, operands = _split_flags_operands(args[2:])
                    usable = [op for op in operands if not _is_indeterminate_token(op)]
                    if not usable:
                        undetermined.append("删除 worktree：命令里解析不出可用的 worktree 路径")
                        continue
                    for op in usable:
                        target = _resolve_path(op, call_cwd)
                        if os.path.isdir(target):
                            teardowns.append((target, "删除 worktree"))
                elif args[0] == "branch":
                    is_delete, refs, unknown = _branch_delete_refs(args)
                    if not is_delete:
                        continue
                    for op in unknown:
                        undetermined.append(
                            f"强删分支：操作数 {op} 解析不出确定的分支名，请改成显式分支名后重试"
                        )
                    deletions.extend((call_cwd, ref) for ref in refs)
                elif args[0] == "update-ref":
                    refs, unknown = _update_ref_delete_refs(args)
                    for op in unknown:
                        undetermined.append(f"删除引用：操作数 {op} 解析不出确定的引用名")
                    deletions.extend((call_cwd, ref) for ref in refs)
        except Exception:
            # A parser defect must not brick every Bash call, but it also must
            # not silently disarm the guard: only a segment that looks like a
            # teardown resolves to BLOCK, anything else is left alone.
            lowered = segment.lower()
            if _WORKTREES_MARKER in lowered.replace("\\", "/") or re.search(
                r"\b(worktree\s+remove|branch\s+-{1,2}\w*d|update-ref)", lowered
            ):
                undetermined.append(f"解析这段命令时出错，无法确认它会删掉什么：{segment[:80]}")

    for reason in undetermined:
        sys.stderr.write(
            f"[OS BLOCK] 拒绝{reason}。"
            "解析不出确定目标时一律按最坏情况处理（放行可能静默丢工作，拦下只是多一步人工确认）。"
        )
        sys.exit(2)

    # `rm -rf .claude/worktrees .claude/worktrees/wf_a` names wf_a twice; assess
    # each directory once, in the order the command line reaches it.
    seen: set[str] = set()
    for target, via in teardowns:
        key = os.path.realpath(target)
        if key in seen:
            continue
        seen.add(key)
        dirty, blocked, advisory = _assess_worktree_teardown(target)
        if dirty or blocked:
            reason = "存在未提交/未跟踪变更" if dirty else blocked
            sys.stderr.write(
                f"[OS BLOCK] 拒绝{via} {target}：{reason}。"
                "先提交/推送备份，或 git branch <名字> <commit> 给它一个引用；"
                "确认要放弃这些改动需本人手动处理，不要重放这条被拦的命令。"
            )
            sys.exit(2)
        if advisory:
            advisories.append(f"[安全] 注意：worktree {target} {advisory}")

    # Union of every ref this command line destroys, per repository. The key is
    # realpath'd so `/tmp/x` and `/private/tmp/x` cannot end up as two separate
    # repositories, each vouching for the other's doomed refs.
    doomed: dict[str, set[str]] = {}
    for probe_cwd, ref in deletions:
        doomed.setdefault(os.path.realpath(probe_cwd), set()).add(ref)

    checked: set[tuple[str, str]] = set()
    for probe_cwd, ref in deletions:
        repo_key = os.path.realpath(probe_cwd)
        if (repo_key, _norm_ref(ref)) in checked:
            continue
        checked.add((repo_key, _norm_ref(ref)))
        name = ref.split("/", 2)[-1]
        repo_code, _ = _run_git_readonly(["rev-parse", "--git-dir"], cwd=probe_cwd)
        if repo_code == _GIT_UNAVAILABLE:
            _block_ref_deletion(name, "git 探测无法完成（git 不可用/超时），无法确认删除后 commit 仍可找回")
        if repo_code != 0:
            continue  # git says this is not a repository: its own error is the answer
        exists_code, _ = _run_git_readonly(["rev-parse", "--verify", "--quiet", ref], cwd=probe_cwd)
        if exists_code == _GIT_UNAVAILABLE:
            _block_ref_deletion(name, "git 探测无法完成（git 不可用/超时），无法确认这条引用指向什么")
        if exists_code != 0:
            continue  # confirmed repo, no such ref: deleting it cannot lose anything
        determined, orphans = _orphan_commits(
            probe_cwd, ref, exclude_refs=tuple(doomed.get(repo_key) or {ref})
        )
        if not determined:
            _block_ref_deletion(
                name, "无法完成可达性探测（git 探测失败/超时），不能确认删除后 commit 仍可找回"
            )
        if orphans:
            _block_ref_deletion(
                name,
                f"有 commit 只能从这条引用到达（排除本次命令会删掉的全部引用后，"
                f"无任何分支/远端/tag 引用：{_format_orphans(orphans)}），删除后将无法找回",
            )

    return advisories


def _block_ref_deletion(name: str, reason: str) -> None:
    sys.stderr.write(
        f"[OS BLOCK] 拒绝强删分支 {name}：{reason}。"
        "先合并或推送备份；确认要放弃这些改动需本人手动处理，不要重放这条被拦的命令。"
    )
    sys.exit(2)


def _get_api_url() -> str:
    """Return current API URL. AITEAM_API_URL env var takes highest priority."""
    env_url = os.environ.get("AITEAM_API_URL")
    if env_url:
        return env_url
    try:
        port = int(open(_PORT_FILE).read().strip())
        return f"http://localhost:{port}"
    except (FileNotFoundError, ValueError):
        return "http://localhost:8000"

# Threshold for Leader delegation check
_LEADER_CONSECUTIVE_THRESHOLD = 8

# Tool names considered "delegation" actions (calling these resets the counter)
# Workflow = CC ultracode 编排工具。Leader 调用它就是在委派执行（交给 CC 内置工作流），
# 与 Agent 直派同属委派动作，应重置 B0.9「连续自己干」计数器，
# 不再催 Leader「为什么不委派」。任务上墙(task_create)提醒不受影响，照常保留。
# TeamCreate 于 CC v2.1.219 已不存在（2026-07-25 工具面核对），保留在集合里
# 无害但已无意义，故移除；派发唯一入口是 Agent。
_DELEGATION_TOOLS = {"Agent", "SendMessage", "Workflow"}

# Infrastructure tools only Leader can do — don't count toward B0.9 threshold
_INFRA_TOOLS = {
    # MCP task management (Leader managing task wall)
    "mcp__ai-team-os__task_create", "mcp__ai-team-os__task_update",
    "mcp__ai-team-os__task_list_project", "mcp__ai-team-os__task_status",
    # MCP team/project management
    "mcp__ai-team-os__team_create", "mcp__ai-team-os__team_list",
    "mcp__ai-team-os__agent_template_recommend", "mcp__ai-team-os__agent_template_list",
    # MCP meeting management
    "mcp__ai-team-os__meeting_create", "mcp__ai-team-os__meeting_conclude",
}


_API_TIMEOUT = 2


def _api_call(method: str, path: str, body: dict | None = None, project_id: str | None = None) -> dict | None:
    """Make a JSON API call to the OS backend. Returns parsed response or None on failure."""
    api_url = _get_api_url()
    url = f"{api_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if project_id:
        headers["X-Project-Id"] = project_id
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


_PROJECT_ID_CACHE_TTL = 300  # 5 minutes


def _resolve_project_id() -> str | None:
    """Resolve current project ID from cwd via OS API, with file-based cache (TTL 5 min)."""
    # Check cache first
    state = _load_supervisor_state()
    cached = state.get("cached_project_id")
    cached_at = state.get("cached_project_id_at", 0)
    if cached and (time.time() - cached_at) < _PROJECT_ID_CACHE_TTL:
        return cached

    # Resolve via API
    api_url = _get_api_url()
    cwd = os.getcwd()
    try:
        req = urllib.request.Request(
            f"{api_url}/api/context/resolve",
            data=json.dumps({"cwd": cwd, "auto_create": False}).encode(),  # 归属铁律：绝不自动立项
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        project_id = data.get("project_id") or data.get("project", {}).get("id")

        # Cache result
        if project_id:
            state["cached_project_id"] = project_id
            state["cached_project_id_at"] = time.time()
            _save_supervisor_state(state)

        return project_id
    except Exception:
        return cached  # Return stale cache on failure


def _get_running_pipeline_subtask(
    api_url: str, project_id: str | None = None
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (subtask_id, parent_task_id, stage_name, next_stage_name) for the current running pipeline.

    Scans active teams for a running task with a pipeline, finds the current pending/running stage,
    and returns its subtask_id. Returns (None, None, None, None) when not found.
    """
    try:
        headers: dict[str, str] = {}
        if project_id:
            headers["X-Project-Id"] = project_id
        req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            teams = json.loads(resp.read().decode()).get("data", [])
        active_teams = [t for t in teams if t.get("status") == "active"]
        if not active_teams:
            return None, None, None, None

        team_id = active_teams[0].get("id", "")
        if not team_id:
            return None, None, None, None

        req2 = urllib.request.Request(f"{api_url}/api/teams/{team_id}/tasks", method="GET", headers=headers)
        with urllib.request.urlopen(req2, timeout=_API_TIMEOUT) as resp2:
            tasks = json.loads(resp2.read().decode()).get("data", [])

        for task in tasks:
            if task.get("status") not in ("running", "in_progress"):
                continue
            pipeline = (task.get("config") or {}).get("pipeline")
            if not pipeline:
                continue

            stages = pipeline.get("stages", [])
            current_idx = pipeline.get("current_stage_index", 0)
            if current_idx >= len(stages):
                continue

            current_stage = stages[current_idx]
            subtask_id = current_stage.get("subtask_id")
            stage_name = current_stage.get("name", "")

            # Find next stage name
            next_stage_name = None
            for s in stages[current_idx + 1:]:
                if s.get("status") != "skipped":
                    next_stage_name = s.get("name")
                    break

            return subtask_id, task.get("id"), stage_name, next_stage_name

    except Exception:
        pass

    return None, None, None, None


def _bind_subtask_running(api_url: str, project_id: str | None = None) -> str | None:
    """Advisory-only detection of the current pipeline stage subtask on agent dispatch.

    pipeline 已退役（设计文档 §7；对齐 pipeline_gate.py:413-419 的退役口径）：不再自动
    把子任务 PUT running。原写库带 active_teams[0] 启发式错绑风险——派出的 agent 未必
    属于该 pipeline，却会把不相关子任务标 running。现仅只读探测存量 pipeline，返回提示
    文本让 Leader 自行决定；无 pipeline 时返回 None。
    """
    subtask_id, _parent_task_id, stage_name, _ = _get_running_pipeline_subtask(api_url, project_id=project_id)
    if not subtask_id:
        return None
    return (
        f"检测到存量 pipeline 子任务 {subtask_id}（阶段: {stage_name}）。"
        "hook 不会自动置 running，需要跟踪请自己 task_update。"
    )


def _advance_pipeline_on_completion(api_url: str, project_id: str | None = None) -> str | None:
    """Advisory-only detection of a legacy pipeline when an agent reports completion.

    pipeline 已退役（设计文档 §7；对齐 pipeline_gate.py:413-419 的退役口径）：不再自动把
    子任务 PUT completed、也不再 POST advance。原写库有双重缺陷——SendMessage 完成关键词
    误判（Leader 说"完成后汇报"也触发）+ active_teams[0] 启发式错绑；两者叠加会伪造 pipeline
    推进。现仅只读探测存量 pipeline 并提示；无 pipeline 时返回 None。
    """
    subtask_id, parent_task_id, stage_name, next_stage_name = _get_running_pipeline_subtask(
        api_url, project_id=project_id
    )
    if not subtask_id or not parent_task_id:
        return None

    if next_stage_name:
        return (
            f"[OS提醒] 检测到存量 pipeline 阶段 '{stage_name}' → '{next_stage_name}'。"
            "hook 不会自动推进，确认完成请自己 task_update。"
        )
    return (
        f"[OS提醒] 检测到存量 pipeline 最后阶段 '{stage_name}'。"
        "hook 不会自动收尾，确认完成请自己 task_update。"
    )


def _load_supervisor_state() -> dict:
    """Load supervisor state file; return default value if missing or corrupted."""
    try:
        with open(_SUPERVISOR_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_supervisor_state(state: dict) -> None:
    """Save supervisor state to file."""
    try:
        os.makedirs(_SUPERVISOR_STATE_DIR, exist_ok=True)
        with open(_SUPERVISOR_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError:
        pass


# 催办类提醒的会话级节流子桶（supervisor-state.json 是跨会话全局文件；催办计数/
# 已展示标记必须按 session 隔离，否则"每会话最多 N 次"无从谈起）。24h 未触及的
# 会话桶自动剪枝，避免全局 state 文件随会话数无限膨胀。
_SESSION_BUCKET_TTL = 24 * 3600


def _session_bucket(state: dict, session_id: str) -> dict:
    """Return (lazily create) the per-session throttle sub-dict for catch-up reminders."""
    sid = _safe_session_id(session_id) or "unknown"
    buckets = state.get("session_scoped")
    if not isinstance(buckets, dict):
        buckets = {}
        state["session_scoped"] = buckets
    now = time.time()
    # Prune stale session buckets so the global state file stays bounded.
    for key in list(buckets.keys()):
        b = buckets.get(key)
        if not isinstance(b, dict) or (now - b.get("_ts", 0)) > _SESSION_BUCKET_TTL:
            buckets.pop(key, None)
    bucket = buckets.get(sid)
    if not isinstance(bucket, dict):
        bucket = {}
        buckets[sid] = bucket
    bucket["_ts"] = now
    return bucket


# A1'(S5) commit 期分支所有权断言的两条时间线（辩论 503e07f1 议题A 裁决）。
# 刻意是两个不同的值，不是一个：
#   ACTIVE — 认领在这个窗口内算"活人还在这条分支上"，别人撞上去硬拦。
#   PRUNE  — 记录彻底删掉的线，必须显著长于 ACTIVE。若两者取同一个值，过期记录会在
#            剪枝时一并消失，"他人记录过期→降为警告"就永远发不出来（静默放行），
#            合法接手者也就失去了"你接的是别人手里的分支"这一句提示。
_BRANCH_OWNERSHIP_ACTIVE_TTL = 24 * 3600
_BRANCH_OWNERSHIP_PRUNE_TTL = 7 * 24 * 3600

# 只认命令里真正作为命令起头出现的 git commit（行首/管道/分号/子 shell 之后），
# 避免把 `echo "git commit"` 之类的字面量当成一次提交。`-C <dir>` 与 `-c k=v`
# 是提交时真会出现的全局参数，一并吃掉才能拿到正确的探测目录。
_GIT_COMMIT_RE = re.compile(
    r"(?:\A|[\n;&|(])\s*git\s+(?:-C\s+(?P<cdir>\S+)\s+)?(?:-c\s+\S+\s+)*commit\b"
)


def _ownership_bucket(state: dict, checkout: str) -> dict:
    """Return (lazily create) the per-checkout ``{agent_id: {branch, ts}}`` claim map.

    Prunes claims older than _BRANCH_OWNERSHIP_PRUNE_TTL on every access, same
    shape as _session_bucket's TTL sweep - supervisor-state.json is a single
    global file and every agent that ever committed would otherwise stay in it
    forever.
    """
    root = state.get("branch_ownership")
    if not isinstance(root, dict):
        root = {}
        state["branch_ownership"] = root
    now = time.time()
    for ck in list(root.keys()):
        claims = root.get(ck)
        if not isinstance(claims, dict):
            root.pop(ck, None)
            continue
        for aid in list(claims.keys()):
            rec = claims.get(aid)
            if not isinstance(rec, dict) or (now - rec.get("ts", 0)) > _BRANCH_OWNERSHIP_PRUNE_TTL:
                claims.pop(aid, None)
        if not claims and ck != checkout:
            root.pop(ck, None)
    bucket = root.get(checkout)
    if not isinstance(bucket, dict):
        bucket = {}
        root[checkout] = bucket
    return bucket


def _check_commit_branch_ownership(
    event_data: dict, state: dict, cmd: str, base_cwd: str
) -> list[str]:
    """S5: assert, at commit time, that HEAD still belongs to the committing agent.

    Rationale (2026-07-10 incident): two CC sessions shared one checkout, one
    switched branches, and the other's commits silently landed on that branch -
    "code disappeared" until reflog recovery. The cheapest reliable moment to
    catch this is the commit itself: one read-only `git rev-parse` says which
    branch the commit is about to land on, and this hook's own state file says
    who claimed it.

    Deliberately self-contained: state lives in supervisor-state.json, no API
    call, no lock file. Ownership arbitration through a new runtime lock file is
    forbidden by the same ruling; a claim record is an observation, not a lock -
    it never gates anything by itself, it only decides warn vs. block.

    Semantics, in evaluation order:
      * git probe fails / detached HEAD -> fail loud. Neither bless nor block:
        the warning states outright that the check could not run, and nothing is
        recorded (recording a branch we could not read would cement a wrong owner).
      * HEAD == my own claim -> silent, claim refreshed. Refreshing matters: an
        agent working a long stretch on its own branch must not age out and lose
        the branch to whoever wanders in next.
      * HEAD == another agent's claim, claim younger than ACTIVE_TTL -> exit(2).
      * HEAD == another agent's claim, claim older than ACTIVE_TTL -> warn only.
        A dead agent's stale claim must not permanently fence off a branch a
        legitimate successor is picking up.
      * HEAD != my claim (nobody else owns it) -> loud warning showing both the
        recorded branch and current HEAD, never a block. Switching branches on
        purpose is legal; doing it without noticing is the failure mode.
      * no claim yet -> record (checkout, agent) -> branch + timestamp, silent.

    The claim is written once, on first commit, and is never rewritten to a new
    branch - so the self-switch warning keeps firing for as long as the mismatch
    lasts, instead of going quiet after one commit. The cost is that a branch an
    agent switched to is left unclaimed; that is the ruling's shape, and the
    unclaimed side degrades to "no signal", never to a wrong block.
    """
    m = _GIT_COMMIT_RE.search(cmd)
    if not m:
        return []

    cdir = m.group("cdir")
    probe_cwd = (
        os.path.abspath(os.path.join(base_cwd, cdir.strip("'\"")))
        if cdir
        else base_cwd
    )
    agent_id = _safe_session_id(event_data.get("session_id", "")) or "unknown"

    # One read-only call for both facts: repo root (the checkout identity - a cwd
    # deep inside the tree must not fragment into its own key) and branch name.
    code, out = _run_git_readonly(
        ["rev-parse", "--show-toplevel", "--abbrev-ref", "HEAD"], cwd=probe_cwd
    )
    lines = out.splitlines() if out else []
    if code != 0 or len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        return [
            f"[安全] 分支所有权检查未能执行：git 探测失败（cwd={probe_cwd}）。"
            "本次提交既未放行也未拦截——请自己 git rev-parse --abbrev-ref HEAD "
            "确认当前分支确实是你在用的那条，再决定要不要提交。"
        ]

    checkout, branch = lines[0].strip(), lines[1].strip()
    if branch == "HEAD":
        return [
            f"[安全] 分支所有权检查未能执行：{checkout} 处于游离 HEAD（detached），"
            "没有分支名可比对。本次提交既未放行也未拦截——游离态提交不属于任何分支，"
            "请先确认这是你要的状态。"
        ]

    bucket = _ownership_bucket(state, checkout)
    now = time.time()
    warnings: list[str] = []

    mine = bucket.get(agent_id)
    if isinstance(mine, dict) and mine.get("branch") == branch:
        mine["ts"] = now
        return warnings

    for other_id, rec in bucket.items():
        if other_id == agent_id or not isinstance(rec, dict) or rec.get("branch") != branch:
            continue
        age = now - rec.get("ts", 0)
        age_h = age / 3600
        if age <= _BRANCH_OWNERSHIP_ACTIVE_TTL:
            sys.stderr.write(
                f"[OS BLOCK] 分支所有权冲突：{checkout} 当前 HEAD 是「{branch}」，"
                f"而这条分支由 agent {other_id} 在 {age_h:.1f} 小时前认领且仍在有效期内。"
                "你正要把提交落到别人的分支上（2026-07-10 就是这样丢过代码）。"
                "请先 git worktree add 开自己的隔离工作区，或与对方确认后由本人操作——"
                "不要重放这条被拦的命令。"
            )
            sys.exit(2)
        warnings.append(
            f"[安全] 分支所有权提示：{checkout} 的分支「{branch}」原由 agent {other_id} 认领，"
            f"但该记录已过期（{age_h:.1f} 小时前，超过 {_BRANCH_OWNERSHIP_ACTIVE_TTL // 3600}h）——"
            "按合法接手处理，不拦。若对方其实还活着，请先确认再提交。"
        )

    if isinstance(mine, dict):
        warnings.append(
            f"[安全] 分支所有权警告：你在 {checkout} 首次提交时记录的分支是"
            f"「{mine.get('branch')}」，当前 HEAD 却是「{branch}」。"
            "分支在你没留意时被换过（同一 checkout 被多方共用的典型征兆）。"
            "确认这就是你要提交的分支再继续；不是的话先 git checkout 回去。"
        )
    else:
        bucket[agent_id] = {"branch": branch, "ts": now}

    return warnings


def _is_taskwall_tool(tool_name: str) -> bool:
    """True for any OS task-wall operation (task_* / taskwall_*, prefixed or bare).

    Operating the task wall — creating / updating / reading / memoing tasks — should
    reset the "好久没看任务墙" catch-up timer, not only the two explicit view tools.
    Otherwise a session actively managing the wall via task_create/task_update/… still
    gets nagged indefinitely.
    """
    base = tool_name.split("mcp__ai-team-os__", 1)[-1]
    return base.startswith("task_") or base.startswith("taskwall_")


def _check_agent_team_name(event_data: dict) -> str | None:
    """Agent 直派检查（2026-07-22 拦截退役版）。Return warning text or None.

    历史：曾对无 team_name 的实施型直派无条件 exit(2) 硬拦（"本地 agent 不可追踪，
    禁止派发"）。2026-07-22 缔造者裁定「全面放开+一律自动追踪」（任务 8705dac2，
    方向记忆 a67fb0de）：hook_translator._on_subagent_start 现已对无队可归的直派
    agent 自动收编进 session-<sid8> 容器队——"不可追踪"前提消失，硬拦随之退役。
    保留两个既有护栏：
      1. explore/plan + team_name 的误用提醒（内置只读类型不支持 SendMessage）；
      2. 显式 team_name 的跨项目派发拦截（2026-05-08 泄漏事故防线，不动）。
    """
    tool_name = event_data.get("tool_name", "")
    if tool_name != "Agent":
        return None

    tool_input_dict = event_data.get("tool_input", {})

    readonly_builtins = ["explore", "plan"]  # CC built-in read-only
    subagent_type = tool_input_dict.get("subagent_type", "").lower()
    has_team = bool(tool_input_dict.get("team_name"))
    if subagent_type in readonly_builtins and has_team:
        return (
            "[OS提醒] Explore/Plan 是 CC 内置只读类型，不支持 SendMessage 团队通讯；"
            "需要来回沟通请换用其他 subagent_type。"
        )

    # 显式 team_name → 跨项目护栏（防 Leader 在项目 A 往项目 B 的队里派 agent）。
    team_name = tool_input_dict.get("team_name")
    if team_name:
        cross_project_warn = _check_team_cross_project(team_name)
        if cross_project_warn:
            sys.stderr.write(cross_project_warn)
            sys.exit(2)

    # 无 team_name（含实施型）一律放行——SubagentStart 自动收编进本会话容器队。
    return None


def _check_team_cross_project(team_name: str) -> str | None:
    """v1.5.2: Verify the team belongs to the current cwd's project.

    Returns a [OS BLOCK] message string when team.project_id != current project,
    or None when the team is valid for current cwd.

    Bypass: if current cwd has no registered project, skip the check.
    """
    current_pid = _resolve_project_id()
    if not current_pid:
        return None  # No project context — allow (rare, e.g. fresh env)
    api_url = _get_api_url()
    try:
        # Fetch all teams (could be filtered server-side if API supports name)
        req = urllib.request.Request(f"{api_url}/api/teams", method="GET")
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            teams = json.loads(resp.read().decode("utf-8")).get("data", [])
        team = next((t for t in teams if t.get("name") == team_name), None)
        if team is None:
            return None  # Team not found in DB — TeamCreate will handle (let it through)
        team_pid = team.get("project_id")
        if team_pid and team_pid != current_pid:
            return (
                f"[OS BLOCK] 跨项目派发被拦截: team='{team_name}' 属于项目 {team_pid[:8]}, "
                f"但当前 cwd 项目是 {current_pid[:8]}。请用本项目的团队，"
                f"或先 cd 到正确的项目目录。"
            )
        return None
    except Exception:
        return None  # API unavailable — fail open (don't block legit work)


def _norm_team_key(name: str) -> str:
    """Normalize a team name for OS↔CC matching.

    Mirrors state_reaper._check_team_liveness's cc_dir_name convention
    (name.lower().replace(" ", "-")) so a TeamDelete identifier can be matched
    against OS team names regardless of case/space-vs-hyphen differences.
    """
    return (name or "").lower().replace(" ", "-")


def _extract_team_identifier(tool_input: dict) -> str | None:
    """Best-effort extract the target team's name/id from a TeamDelete tool_input.

    CC's TeamDelete parameter schema isn't guaranteed available to this hook, so
    probe the conventional keys (team_name mirrors TeamCreate). Returns the first
    non-empty string value, or None when nothing usable is present — the caller
    must then fall back to advisory-only (never a blind cross-team write).
    """
    if not isinstance(tool_input, dict):
        return None
    for key in ("team_name", "name", "team_id", "id"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _check_leader_doing_too_much(event_data: dict, state: dict) -> str | None:
    """Check if Leader is making too many consecutive tool calls without delegating.

    Returns warning text when consecutive non-delegation tool calls exceed threshold.
    Resets counter when Leader calls Agent/TeamCreate/SendMessage.
    Skipped for sub-agent sessions (marked by SubagentStart hook) — hands-on work
    is their job, not a delegation failure.
    """
    tool_name = event_data.get("tool_name", "")
    if not tool_name:
        return None

    if _is_subagent_session(event_data.get("session_id", "")):
        return None

    consecutive = state.get("leader_consecutive_calls", 0)

    if tool_name in _DELEGATION_TOOLS:
        state["leader_consecutive_calls"] = 0
        return None

    # Infrastructure tools (task wall, team mgmt) don't count toward threshold
    if tool_name in _INFRA_TOOLS:
        return None

    consecutive += 1
    state["leader_consecutive_calls"] = consecutive

    # Remind once at threshold+1, then every 10 calls after (avoid noise)
    over = consecutive - _LEADER_CONSECUTIVE_THRESHOLD
    if over == 1 or (over > 1 and over % 10 == 0):
        return (
            f"[AI Team OS] B0.9提醒：Leader已连续执行{consecutive}次工具调用。"
            "是否应该委派给团队成员？"
        )

    return None




def _check_workflow_reminders(event_data: dict, state: dict, project_id: str | None = None) -> list[str]:
    """Generate workflow reminders based on tool call patterns."""
    tool_name = event_data.get("tool_name", "")
    session_id = event_data.get("session_id", "")
    warnings: list[str] = []
    now = time.time()

    # 1. After TeamCreate: remind whether task is on the task wall
    if tool_name == "TeamCreate":
        warnings.append(
            "[OS提醒] 新团队已创建。此工作方向是否已在任务墙创建对应任务？"
            "→ 使用 task_run 或 task_create 添加任务"
        )

    # 1b. Workflow (CC ultracode 编排) — 软提醒，让产出回流 OS（治理层定位）。
    # OS 不拦 Workflow（CC 平台级 runtime），但提醒 Leader：① 总任务仍要上墙；
    # ② 在 agent prompt 里加 OS 回写指令，让 workflow 成员自己记账。节流 300s 防噪音。
    if tool_name == "Workflow":
        last = state.get("workflow_reminder_at", 0)
        if now - last >= 300:
            state["workflow_reminder_at"] = now
            warnings.append(
                "[OS提醒] Workflow 运行已自动追踪成团队。仍需你做两件事："
                "① task_create 把总任务上墙；② 在每个 workflow agent 的 prompt 里嵌回写指令"
                "（ToolSearch 加载 task_memo_add/report_save 后调用），否则内部产出不入 OS。"
                "模板见 skill /os-workflow。"
            )

    # 1c. 生态调研/自建 pipeline 入口 — 编排层已迁移 ultracode/Workflow（v1.8.1 决策）。
    # ultracode 需用户手动开启（非常驻），自建派发层随时可跑但已退役——
    # 所以在旧入口软提醒：先确认用户开了 ultracode，再用 Workflow 编排。节流 3600s。
    _ultracode_hint_tools = (
        "ecosystem_claim_shallow",
        "ecosystem_claim_review",
        "ecosystem_deep_review_request",
        "ecosystem_deep_review_request_batch",
        "ecosystem_scan",
        "ecosystem_scan_periodic",
    )
    if tool_name.removeprefix("mcp__ai-team-os__") in _ultracode_hint_tools:
        last = state.get("ultracode_hint_at", 0)
        if now - last >= 3600:
            state["ultracode_hint_at"] = now
            warnings.append(
                "[OS提醒] 生态调研的产物必须回写 ecosystem 表"
                "（ecosystem_apply_shallow_summary / ecosystem_apply_quality_review），"
                "否则台账与 /ecosystem 页面看不到这次调研。"
            )

    # 2. Before Agent creation: check task wall, template usage, and historical memos
    if tool_name == "Agent":
        input_dict = event_data.get("tool_input", {})
        input_str = str(input_dict)
        has_team = bool(input_dict.get("team_name") or input_dict.get("name"))

        if has_team:
            # 2a. Check if active team has running/in_progress tasks; also check pipeline
            has_active_task = False
            running_tasks: list[dict] = []
            try:
                import urllib.request

                api_url = _get_api_url()
                _ph: dict[str, str] = {}
                if project_id:
                    _ph["X-Project-Id"] = project_id
                req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_ph)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    teams = json.loads(resp.read().decode("utf-8")).get("data", [])
                active_teams = [t for t in teams if t.get("status") == "active"]
                if active_teams:
                    team_id = active_teams[0].get("id", "")
                    if team_id:
                        req2 = urllib.request.Request(
                            f"{api_url}/api/teams/{team_id}/tasks",
                            method="GET",
                            headers=_ph,
                        )
                        with urllib.request.urlopen(req2, timeout=2) as resp2:
                            tasks = json.loads(resp2.read().decode("utf-8")).get(
                                "data", []
                            )
                        running_tasks = [
                            t
                            for t in tasks
                            if t.get("status") in ("running", "in_progress")
                        ]
                        has_active_task = len(running_tasks) > 0
            except Exception:
                has_active_task = True  # API unavailable, don't block

            # Fallback: check project-level tasks (team_id=None) when no team tasks are running
            if not has_active_task:
                try:
                    if active_teams and active_teams[0].get("project_id"):
                        pid = active_teams[0]["project_id"]
                        proj_req = urllib.request.Request(
                            f"{api_url}/api/projects/{pid}/tasks/running-count",
                            method="GET",
                            headers=_ph,
                        )
                        with urllib.request.urlopen(proj_req, timeout=_API_TIMEOUT) as proj_resp:
                            proj_data = json.loads(proj_resp.read().decode("utf-8"))
                        if proj_data.get("count", 0) > 0:
                            has_active_task = True
                except Exception:
                    pass

            if not has_active_task:
                warnings.append(
                    "[OS提醒] 当前无进行中任务。派 Agent 干活前先 task_create 把这件事上墙，"
                    "否则产出无处记账。"
                )
            else:
                # 2a-TW. Pre-check: verify dispatched work matches a task wall item
                try:
                    agent_prompt = (input_dict.get("prompt", "") + " " + input_dict.get("description", "")).lower()
                    if agent_prompt.strip() and project_id:
                        _tw_path = f"/api/projects/{project_id}/task-wall?limit=20&include_completed=false"
                        tw_data = _api_call("GET", _tw_path, project_id=project_id)
                        if tw_data:
                            tw_tasks: list[dict] = []
                            for hg in (tw_data.get("wall") or {}).values():
                                if isinstance(hg, list):
                                    tw_tasks.extend(hg)
                            tw_pending = [t for t in tw_tasks if t.get("status") in ("pending", "running")]
                            # Quick keyword match check
                            agent_words = set(agent_prompt.replace("—", " ").replace("-", " ").split())
                            agent_words -= {"的", "是", "在", "了", "和", "与", "a", "the", "to", "for", "of"}
                            matched_any = False
                            for t in tw_pending:
                                _raw = (t.get("title") or "").lower().replace("—", " ").replace("-", " ")
                                title_words = set(_raw.split())
                                if len(title_words & agent_words) >= 2:
                                    matched_any = True
                                    break
                            if not matched_any and tw_pending:
                                # 节流 3600s（2026-07-14 审计 P1：同条提醒曾对 Leader 连发 24 次）
                                _wm_last = state.get("wall_match_reminder_at", 0)
                                if now - _wm_last >= 3600:
                                    state["wall_match_reminder_at"] = now
                                    tw_titles = "、".join(t.get("title", "?")[:20] for t in tw_pending[:3])
                                    warnings.append(
                                        f"[OS提醒] 此Agent工作未匹配到任务墙项（墙上有：{tw_titles}）。"
                                        "确认此工作已在任务墙登记？→ task_create 上墙"
                                    )
                except Exception:
                    pass  # Advisory only

            # 2-CP1. Pipeline subtask binding: mark current stage subtask as running
            if has_active_task:
                try:
                    _bind_api_url = _get_api_url()
                    bind_msg = _bind_subtask_running(_bind_api_url, project_id=project_id)
                    if bind_msg:
                        warnings.append(f"[OS提醒] {bind_msg}")
                except Exception:
                    pass  # Binding is optional — never block agent dispatch

            # 2d.（已退役）pipeline 强挂载检查 — pipeline 已定向废弃（设计文档 §7
            # Phase1 断新增入口）：不再催促/强制 task_type，编排改用 CC Workflow，
            # 运行追踪走观测层（Dashboard /workflows）。回滚见 git 历史。
            if has_active_task and running_tasks:
                # 2e. Pipeline pending stage detection（退役期：仅对存量 pipeline 软提醒）
                tasks_with_pipeline = [
                    t for t in running_tasks
                    if (t.get("config") or {}).get("pipeline")
                ]
                pending_stage_info: list[tuple[str, str, str]] = []  # (task_title, stage_name, agent_template)
                for t in tasks_with_pipeline:
                    pipeline = t["config"]["pipeline"]
                    stages = pipeline.get("stages", [])
                    current_idx = pipeline.get("current_stage_index", 0)
                    # Look for pending stages after the current index
                    for stage in stages[current_idx + 1:]:
                        if stage.get("status") == "pending":
                            pending_stage_info.append((
                                t.get("title", t.get("id", "?")),
                                stage["name"],
                                stage.get("agent_template", ""),
                            ))
                            break  # Only report first pending per task

                if pending_stage_info:
                    pending_warnings = state.get("pipeline_pending_warnings", 0)
                    first_title, first_stage, first_tpl = pending_stage_info[0]
                    if pending_warnings == 0:
                        warnings.append(
                            f"[OS提醒] Pipeline 阶段 '{first_stage}' 就绪（任务: {first_title}），"
                            f"推荐 agent_template: {first_tpl}"
                        )
                        state["pipeline_pending_warnings"] = 1
                    elif pending_warnings == 1:
                        warnings.append(
                            f"[OS提醒] 请先推进 pipeline（阶段 '{first_stage}' 等待中），"
                            "再分配其他工作"
                        )
                        state["pipeline_pending_warnings"] = 2
                    else:
                        # 退役期不再硬拦（原 exit(2)）：存量 pipeline 只持续软提醒
                        warnings.append(
                            f"[OS提醒] 存量 pipeline 阶段 '{first_stage}' 仍等待推进"
                        )

                # 2f. Agent type matching check vs pipeline recommended template.
                # Skipped for meeting-mode stages: any role can attend a meeting.
                subagent_type_for_check = input_dict.get("subagent_type", "")
                if subagent_type_for_check and tasks_with_pipeline:
                    # Find the current active stage's recommended template
                    recommended_template: str | None = None
                    current_stage_name: str | None = None
                    current_stage_mode: str = "agent"
                    for t in tasks_with_pipeline:
                        pipeline = t["config"]["pipeline"]
                        stages = pipeline.get("stages", [])
                        current_idx = pipeline.get("current_stage_index", 0)
                        if current_idx < len(stages):
                            current_stage = stages[current_idx]
                            if current_stage.get("status") in ("pending", "running"):
                                recommended_template = current_stage.get("agent_template", "")
                                current_stage_name = current_stage.get("name", "")
                                current_stage_mode = current_stage.get("mode", "agent")
                                break

                    if recommended_template and current_stage_name:
                        if current_stage_mode == "meeting":
                            # Meeting stages allow any role — skip type check entirely
                            pass
                        elif subagent_type_for_check == recommended_template:
                            # Correct template — reset pending warnings counter
                            state["pipeline_pending_warnings"] = 0
                        else:
                            warnings.append(
                                f"[OS提醒] 当前阶段 '{current_stage_name}' 推荐 {recommended_template}，"
                                f"但你派出了 {subagent_type_for_check}。确认使用？"
                            )

        # 2b. Template usage reminder (check if subagent_type is a known template)
        subagent_type = input_dict.get("subagent_type", "")
        if subagent_type in ("general-purpose", "") or not subagent_type:
            last_tpl = state.get("last_template_reminder", 0)
            if now - last_tpl > 600:  # 10-min cooldown
                warnings.append(
                    "[OS提醒] 派通用 Agent 前可看一眼是否有更贴合的模板："
                    "agent_template_recommend(task描述)"
                )
                state["last_template_reminder"] = now

        # 2c. Memo reminder (with 5-min cooldown)
        if has_team:
            last_memo = state.get("last_memo_reminder", 0)
            if now - last_memo > 300:
                warnings.append(
                    "[OS提醒] 分配新成员前：此任务是否有历史工作记录？"
                    "→ 建议先 task_memo_read 查看前置工作"
                )
                state["last_memo_reminder"] = now

    # 3. Before SendMessage(shutdown): remind about task completion
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        if "shutdown" in input_str.lower():
            warnings.append(
                "[OS提醒] 关闭Agent前：此Agent的任务是否已标记完成？"
                "→ 建议更新任务状态并添加总结memo (task_memo_add type=summary)"
            )

    # 4. On TeamDelete: sync-close ONLY the corresponding OS team.
    # 历史缺陷（2026-07-14 审计 A2，high）：这里曾遍历把所有 status=active 团队盲 PUT
    # completed，无范围限定——多会话多团队并行（本仓常态，当时 5+ active）下，删任意一个
    # CC 团队都会把其他会话仍在用的团队全部误标 completed，跨团队状态失真。改为：从
    # tool_input 精确提取被删团队标识，按 OS↔CC 命名约定（_norm_team_key，与 state_reaper.
    # _check_team_liveness 的 cc_dir_name 一致）只关那一个；拿不到可靠标识或匹配不到则纯
    # 提醒不写库——state_reaper._check_team_liveness 会按 CC 配置探活兜底同步关闭，无需盲写。
    if tool_name == "TeamDelete":
        target_ident = _extract_team_identifier(event_data.get("tool_input", {}))
        if not target_ident:
            warnings.append(
                "[OS提醒] 检测到 TeamDelete 但无法从参数解析被删团队标识，未自动同步关闭 OS 团队。"
                "如 OS 侧仍显示该团队 active，请手动 team_close（state_reaper 配置探活亦会兜底）。"
            )
        else:
            try:
                import urllib.request

                api_url = _get_api_url()
                _tdh: dict[str, str] = {}
                if project_id:
                    _tdh["X-Project-Id"] = project_id
                req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_tdh)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    teams = json.loads(resp.read().decode("utf-8")).get("data", [])
                _target_key = _norm_team_key(target_ident)
                matched = [
                    t
                    for t in teams
                    if t.get("status") == "active"
                    and (
                        _norm_team_key(t.get("name", "")) == _target_key
                        or t.get("id") == target_ident
                    )
                ]
                if not matched:
                    warnings.append(
                        f"[OS提醒] TeamDelete 团队「{target_ident}」未匹配到 active 的 OS 团队，未写库"
                        "（可能已关闭/从未在 OS 建队；state_reaper 配置探活会兜底同步）。"
                    )
                for t in matched:
                    _close_h = {"Content-Type": "application/json"}
                    if project_id:
                        _close_h["X-Project-Id"] = project_id
                    close_req = urllib.request.Request(
                        f"{api_url}/api/teams/{t['id']}",
                        data=json.dumps({"status": "completed"}).encode(),
                        headers=_close_h,
                        method="PUT",
                    )
                    urllib.request.urlopen(close_req, timeout=2)
            except Exception:
                pass  # Silently handle — state_reaper._check_team_liveness is the backstop

    # 5. After TeamCreate: check if active teams already exist
    if tool_name == "TeamCreate":
        try:
            import urllib.request

            api_url = _get_api_url()
            _tch: dict[str, str] = {}
            if project_id:
                _tch["X-Project-Id"] = project_id
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_tch)
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            active_teams = [t for t in teams if t.get("status") == "active"]
            # Newly created team is also active, so check if >1 active teams
            if len(active_teams) > 1:
                other = active_teams[0].get("name", "未知")
                warnings.append(
                    f"[OS提醒] 已存在活跃团队「{other}」。"
                    "建议：①在已有团队中添加成员 ②先关闭旧团队再创建新的"
                )
        except Exception:
            pass  # Silently skip when API unavailable

    # 6. After SendMessage: check parallel task assignment (idle Agent + pending task matching)
    if tool_name == "SendMessage":
        try:
            import urllib.request

            api_url = _get_api_url()
            _smh: dict[str, str] = {}
            if project_id:
                _smh["X-Project-Id"] = project_id
            req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_smh)
            with urllib.request.urlopen(req, timeout=2) as resp:
                teams = json.loads(resp.read().decode("utf-8")).get("data", [])
            active_teams = [t for t in teams if t.get("status") == "active"]
            if active_teams:
                team_id = active_teams[0].get("id", "")
                if team_id:
                    req2 = urllib.request.Request(
                        f"{api_url}/api/teams/{team_id}/agents",
                        method="GET",
                        headers=_smh,
                    )
                    with urllib.request.urlopen(req2, timeout=2) as resp2:
                        agents = json.loads(resp2.read().decode("utf-8")).get("data", [])
                    non_leader_agents = [a for a in agents if a.get("role") != "leader"]
                    busy_count = sum(1 for a in non_leader_agents if a.get("status") == "busy")
                    idle_agents = [
                        a for a in non_leader_agents if a.get("status") in ("waiting", "offline")
                    ]
                    if busy_count < 3 and idle_agents:
                        # Try to fetch pending tasks for matching suggestions
                        match_hints: list[str] = []
                        try:
                            req3 = urllib.request.Request(
                                f"{api_url}/api/teams/{team_id}/tasks",
                                method="GET",
                                headers=_smh,
                            )
                            with urllib.request.urlopen(req3, timeout=2) as resp3:
                                tasks = json.loads(resp3.read().decode("utf-8")).get("data", [])
                            pending_tasks = [
                                t
                                for t in tasks
                                if t.get("status") in ("pending",) and not t.get("assigned_to")
                            ]
                            for idle in idle_agents[:3]:  # Show at most 3 idle Agents
                                agent_role = (idle.get("role") or idle.get("name") or "").lower()
                                agent_name = idle.get("name", "?")
                                # Find tasks whose tags overlap with agent role
                                matched = next(
                                    (
                                        t
                                        for t in pending_tasks
                                        if any(
                                            tag.lower() in agent_role or agent_role in tag.lower()
                                            for tag in (t.get("tags") or [])
                                        )
                                    ),
                                    pending_tasks[0] if pending_tasks else None,
                                )
                                if matched:
                                    tags_str = ",".join(matched.get("tags") or [])
                                    hint = (
                                        f"空闲Agent: {agent_name}({idle.get('role', '')}), "
                                        f"待办: {matched['title']}"
                                        + (f"(tags:{tags_str})" if tags_str else "")
                                        + " → 建议分配"
                                    )
                                    match_hints.append(hint)
                        except Exception:
                            pass
                        # 催办类节流：同一会话最多提示 1 次"可并行分配"，之后静默
                        # （高频催办被系统性无视=纯 token 与注意力税）。
                        _idle_bucket = _session_bucket(state, session_id)
                        # 只在能给出具体"谁空着 + 哪条待办"时才提醒；给不出配对
                        # 就不发——"可以并行提高效率"这类空话不带信息量。
                        if match_hints and not _idle_bucket.get("idle_member_reminder_shown"):
                            warnings.append(
                                f"[OS提醒] 仅{busy_count}个成员在工作，空闲成员与待办可配对：\n"
                                + "\n".join(f"  • {h}" for h in match_hints)
                            )
                            _idle_bucket["idle_member_reminder_shown"] = True
        except Exception:
            pass  # Silently skip when API unavailable

    # 7. Task-wall catch-up: nag when a session hasn't touched the wall for a while.
    # 催办类=低频+可静默：①重置计时的事件扩大到全部 task_*/taskwall_* 工具（不再只认
    # 两个 view 工具，否则用 task_create/task_update 管理任务墙的会话仍被无限催）；
    # ②间隔 900s→1800s；③同一会话最多催 2 次，之后静默（计数入会话桶）。
    if _is_taskwall_tool(tool_name):
        state["last_taskwall_view"] = now
    else:
        last_view = state.get("last_taskwall_view", 0)
        if last_view == 0:
            # First tool call in session — start the countdown from now
            state["last_taskwall_view"] = now
        elif (now - last_view) > 1800:
            bucket = _session_bucket(state, session_id)
            catchup_count = bucket.get("taskwall_catchup_count", 0)
            if catchup_count < 2:
                minutes = int((now - last_view) / 60)
                warnings.append(
                    f"[OS提醒] 距上次查看任务墙已{minutes}分钟。→ 建议 task_list_project() "
                    f"查看项目任务墙（只看一支队传 team_id=）"
                )
                bucket["taskwall_catchup_count"] = catchup_count + 1
            state["last_taskwall_view"] = now

    # 9. Handoff reminder: when Agent reports completion, remind to assign follow-up tasks
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["完成", "completed", "done", "finished", "汇报"]
        is_completion = any(kw in input_str.lower() for kw in completion_keywords)
        # Exclude shutdown messages (already handled by rule 3)
        is_shutdown = "shutdown" in input_str.lower()
        if is_completion and not is_shutdown:
            # 9-CP2. Pipeline auto-advance: mark subtask completed and advance pipeline
            try:
                _advance_api_url = _get_api_url()
                advance_msg = _advance_pipeline_on_completion(_advance_api_url, project_id=project_id)
                if advance_msg:
                    warnings.append(advance_msg)
            except Exception:
                pass  # Advancing is optional — never block completion message

            try:
                api_url = _get_api_url()
                _r9h: dict[str, str] = {}
                if project_id:
                    _r9h["X-Project-Id"] = project_id
                req = urllib.request.Request(f"{api_url}/api/teams", method="GET", headers=_r9h)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    teams = json.loads(resp.read().decode("utf-8")).get("data", [])
                active_teams = [t for t in teams if t.get("status") == "active"]
                if active_teams:
                    team_id = active_teams[0].get("id", "")
                    if team_id:
                        req2 = urllib.request.Request(
                            f"{api_url}/api/teams/{team_id}/tasks",
                            method="GET",
                            headers=_r9h,
                        )
                        with urllib.request.urlopen(req2, timeout=2) as resp2:
                            tasks = json.loads(resp2.read().decode("utf-8")).get("data", [])
                        pending = [
                            t
                            for t in tasks
                            if t.get("status") == "pending" and not t.get("assigned_to")
                        ]
                        if pending:
                            pending_titles = "、".join(t["title"] for t in pending[:3])
                            more = f"等{len(pending)}个" if len(pending) > 3 else ""
                            warnings.append(
                                f"[OS提醒] Agent已完成汇报，仍有待分配任务：{pending_titles}{more}。"
                                "→ 是否分配给空闲成员继续推进？"
                            )
            except Exception:
                pass

    # 10. After meeting_create: remind to notify participants and use skills
    if tool_name in ("meeting_create", "mcp__ai-team-os__meeting_create"):
        warnings.append(
            "[OS提醒] 会议已创建，但 OS 不会替你通知任何人——"
            "逐一 SendMessage 告知 meeting_id 与议题，参与者才会到场。"
        )

    # 11. After meeting_conclude: remind to add action items to task wall
    if tool_name in ("meeting_conclude", "mcp__ai-team-os__meeting_conclude"):
        warnings.append(
            "[OS提醒] 会议已结束。结论里的行动项要 task_create 上墙——只写在纪要里等于没提。"
        )

    # 12. When task marked complete: remind QA acceptance testing
    if tool_name in ("task_status", "mcp__ai-team-os__task_status"):
        input_str = str(event_data.get("tool_input", {}))
        if "completed" in input_str.lower():
            warnings.append(
                "[OS提醒] 任务已置完成。若改动影响系统行为或前端显示，请告知 QA 要观测什么"
            )

    # 13. Bottleneck detection: remind to hold meeting when all tasks done or many blocked
    # Check every 50 tool calls (throttled)
    bottleneck_count = state.get("bottleneck_check_count", 0) + 1
    state["bottleneck_check_count"] = bottleneck_count
    # v1.8.1 fix: 按项目级任务墙判断，而非逐 active 团队判空——
    # 团队维度会漏掉 team_id=null 的项目级任务，某团队清零即误报"全完成"
    if bottleneck_count % 50 == 0 and project_id:
        try:
            import urllib.request

            api_url = _get_api_url()
            _b13h: dict[str, str] = {"X-Project-Id": project_id}
            req = urllib.request.Request(
                f"{api_url}/api/projects/{project_id}/task-wall",
                method="GET",
                headers=_b13h,
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            wall_data = payload.get("data", payload)
            by_status = (wall_data.get("stats") or {}).get("by_status", {})
            pending_n = by_status.get("pending", 0)
            running_n = by_status.get("running", 0)
            blocked_n = by_status.get("blocked", 0)
            if pending_n + running_n + blocked_n == 0:
                warnings.append("[OS提醒] 任务墙已清空：无 pending/running/blocked 任务")
            elif blocked_n > running_n and blocked_n >= 2:
                warnings.append(f"[OS提醒] {blocked_n}个任务阻塞中，多于进行中的{running_n}个")
        except Exception:
            pass

    # 14. Report format validation — only for member completion reports.
    # ⑤ 老实现不分消息方向，Leader 发出的派工/验收消息也被要求"完成内容/修改文件/
    # 测试结果"格式。改为仅**子agent会话**（成员向 Leader 汇报）触发，排除主/Leader
    # 会话；并加会话级节流（每会话最多 1 次），替代原全局 3600s 节流。
    if tool_name == "SendMessage" and _is_subagent_session(session_id):
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["完成", "completed", "done", "finished", "汇报"]
        if (
            any(kw in input_str.lower() for kw in completion_keywords)
            and "shutdown" not in input_str.lower()
        ):
            required_fields = ["完成内容", "修改文件", "测试结果"]
            missing = [f for f in required_fields if f not in input_str]
            if missing and len(input_str) > 100:  # Only check longer reports
                _rf_bucket = _session_bucket(state, session_id)
                if not _rf_bucket.get("report_fields_reminder_shown"):
                    _rf_bucket["report_fields_reminder_shown"] = True
                    warnings.append(
                        f"[OS提醒] 汇报可能缺少标准字段：{', '.join(missing)}。"
                        "标准格式：完成内容/修改文件/测试结果/建议任务状态/建议memo"
                    )

    # ── Safety guardrail rules ──────────────────────────────────────────

    tool_input = event_data.get("tool_input", {})

    # S1: Dangerous command interception (Bash)
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        # Strip heredoc blocks (<<'EOF'...EOF, <<"EOF"...EOF, <<EOF...EOF) so that
        # text inside commit messages or other string literals does not trigger S1.
        # Only the executable shell syntax outside heredoc delimiters is scanned.
        cmd_for_s1 = re.sub(r"<<['\"]?(\w+)['\"]?.*?\n.*?\1", "", cmd, flags=re.DOTALL)
        cmd_lower = cmd_for_s1.lower()
        # Recursive delete of root/home directory -> exit(2) hard block
        if re.search(r"rm\s+-[^\s]*[rR][^\s]*\s+(/|~/|~)(\s|$|[^a-zA-Z])", cmd_for_s1):
            sys.stderr.write("[OS BLOCK] Dangerous: recursive delete of root/home directory blocked")
            sys.exit(2)
        # Recursive delete of other dangerous targets -> warning
        if re.search(r"rm\s+-[^\s]*[rR][^\s]*\s+\*", cmd_for_s1):
            warnings.append("[安全] 危险：检测到递归删除通配符命令，请确认操作目标")
        # Destructive database operations
        if re.search(r"\b(DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE)\b", cmd_for_s1, re.IGNORECASE):
            warnings.append("[安全] 危险：检测到数据库破坏性操作（DROP/TRUNCATE），请确认")
        # force push
        if "push" in cmd_lower and "--force" in cmd_lower:
            warnings.append("[安全] 注意：检测到force push，可能覆盖远程历史")
        # Overly permissive file permissions
        if "chmod 777" in cmd_for_s1:
            warnings.append("[安全] 安全：过度开放的文件权限（chmod 777），建议使用更严格的权限")
        # S3: Sensitive file commit interception (git add) -> exit(2) hard block
        if "git add" in cmd_lower:
            block_patterns = [".env", "id_rsa", ".pem", ".key"]
            for pat in block_patterns:
                if pat in cmd_lower:
                    sys.stderr.write(f"[OS BLOCK] 禁止提交敏感文件（{pat}）")
                    sys.exit(2)
            # credentials keep as warning (filename is ambiguous, may not be a key file)
            if "credentials" in cmd_lower:
                warnings.append(
                    "[安全] 安全：检测到尝试提交credentials文件，"
                    "请确认该文件不包含密钥信息且已在.gitignore中"
                )

        # S4: Worktree teardown protection - never tear down work git cannot get
        # back. Covers `git worktree remove`, ref deletion (`git branch -d/-D`,
        # `git update-ref -d`) and raw `rm -rf` against a worktree directory (the
        # last bypasses git's own dirty-tree check entirely). See
        # docs/worktree-governance-design.md §3 for the design and rationale, and
        # the _orphan_commits block above for the 2026-08-14 criterion change
        # (reachability instead of "landed on the default branch") plus the
        # round-2 hardening of the command recognition.
        #
        # Ref deletion is checked for EVERY branch, not only `worktree-*` ones:
        # once a worktree removal no longer hard-blocks committed work, deleting
        # the branch is the only remaining way to lose it, and real work branches
        # are named chore/…, feature/… just as often. Under a reachability
        # criterion the scope costs nothing - a branch whose commits any other ref
        # still reaches passes regardless of its name.
        base_cwd = event_data.get("cwd") or os.getcwd()
        warnings.extend(_check_worktree_teardown_guard(cmd_for_s1, base_cwd))

        # S5: commit-time branch ownership assertion (A1', debate 503e07f1).
        # Same domain as S4 - both are read-only git assertions guarding the
        # multi-session worktree discipline - but at the opposite end: S4 stops
        # finished work from being torn down, S5 stops new work from landing on
        # someone else's branch. PreToolUse only: asserting after the commit has
        # already been made answers a question nobody can act on any more.
        if event_data.get("hook_event_name") == "PreToolUse":
            warnings.extend(
                _check_commit_branch_ownership(event_data, state, cmd_for_s1, base_cwd)
            )

    # 15. Team directory cleanup reminder: check every 100 tool calls.
    # ② 该提醒在 session_bootstrap 启动侧已发一次；此处（工具时）加会话级节流——
    # 每会话最多 1 次，避免同一会话内每 100 次工具调用反复重发（启动侧保留不动）。
    team_cleanup_count = state.get("team_cleanup_check_count", 0) + 1
    state["team_cleanup_check_count"] = team_cleanup_count
    if team_cleanup_count % 100 == 0:
        _td_bucket = _session_bucket(state, session_id)
        if not _td_bucket.get("team_dir_reminder_shown"):
            teams_dir = Path.home() / ".claude" / "teams"
            if teams_dir.exists():
                try:
                    team_dirs = [p for p in teams_dir.iterdir() if p.is_dir()]
                    if len(team_dirs) > 5:
                        warnings.append(
                            f"[OS提醒] ~/.claude/teams/ 下已累积 {len(team_dirs)} 个历史会话团队目录，"
                            "可手动删除旧目录（当前会话的勿删）"
                        )
                        _td_bucket["team_dir_reminder_shown"] = True
                except Exception:
                    pass

    # ── Safety guardrail rules ──────────────────────────────────────────

    # S2: Sensitive information detection (Write/Edit)
    if tool_name in ("Write", "Edit"):
        # Get content to be written
        content = tool_input.get("content", "") or tool_input.get("new_string", "")
        # Hardcoded secret detection
        if re.search(
            r"(password|secret|api_key|token)\s*=\s*['\"][^'\"]+['\"]",
            content,
            re.IGNORECASE,
        ):
            warnings.append("[安全] 安全：检测到可能的硬编码密钥，建议使用环境变量")
        # .env file write reminder
        file_path = tool_input.get("file_path", "")
        if file_path.endswith(".env") or "/.env" in file_path or "\\.env" in file_path:
            warnings.append("[安全] 注意：.env文件不应提交到版本库，请确认.gitignore包含.env")
        # Reports data directory hard block: only block writes to the actual reports data
        # dirs under ~/.claude/data/. Any other .md write (README, docs, src) is allowed.
        # Conditions (both must be true):
        #   1. Path contains the exact data dir prefix for reports
        #   2. File extension is .md
        _fp_normalized = file_path.replace("\\", "/")
        _is_report_data_dir = (
            ".claude/data/ai-team-os/reports/" in _fp_normalized
            or (
                ".claude/data/ai-team-os/projects/" in _fp_normalized
                and "/reports/" in _fp_normalized
            )
        )
        if _is_report_data_dir and file_path.endswith(".md"):
            warnings.append(
                "[OS提醒] 报告应通过 report_save 工具保存到数据库（直接写文件不会被系统追踪）。"
                "→ report_save(author=你的名字, topic=主题, content=markdown内容,"
                " report_type=research/design/analysis/meeting-minutes)"
            )
        # 协作式文件锁的冲突提醒已退役（2026-07-27 批 8b）：锁只能由 file_lock_*
        # 四个 MCP 工具建立，而那四个工具从没人调过——锁文件实测恒为 {}，这段判定
        # 因此永远走不进 if。真正在用的冲突检测是 hook_translator 侧的
        # _check_file_edit_conflict（按最近编辑事件判定，212 条真事件背书）+
        # get_file_hotspots，两者一行未动。

    return warnings


def _post_tool_taskwall_sync(event_data: dict, state: dict, project_id: str | None = None) -> list[str]:
    """PostToolUse: auto-sync task wall when Agent dispatched or completion reported.

    1. After Agent dispatch → find matching pending task on project wall → auto-update to running
    2. After SendMessage(completion) → remind to update task to completed
    """
    tool_name = event_data.get("tool_name", "")
    warnings: list[str] = []

    if not project_id:
        return warnings

    # 1. After Agent dispatch: auto-link to task wall item
    if tool_name == "Agent":
        input_dict = event_data.get("tool_input", {})
        # Only for team agents (non-readonly)
        if not input_dict.get("team_name"):
            return warnings

        agent_prompt = input_dict.get("prompt", "")
        agent_desc = input_dict.get("description", "")
        agent_text = f"{agent_desc} {agent_prompt}".lower()

        if not agent_text.strip():
            return warnings

        try:
            # Query project task wall for pending tasks
            _wall_path = f"/api/projects/{project_id}/task-wall?limit=20&include_completed=false"
            wall_data = _api_call("GET", _wall_path, project_id=project_id)
            if not wall_data:
                return warnings

            wall_tasks: list[dict] = []
            for horizon_group in (wall_data.get("wall") or {}).values():
                if isinstance(horizon_group, list):
                    wall_tasks.extend(horizon_group)

            # Find matching pending task by keyword overlap
            pending_tasks = [t for t in wall_tasks if t.get("status") in ("pending", "running")]
            best_match: dict | None = None
            best_score = 0

            for task in pending_tasks:
                title = (task.get("title") or "").lower()
                tags = [t.lower() for t in (task.get("tags") or [])]
                desc = (task.get("description") or "").lower()

                # Simple keyword overlap scoring
                score = 0
                title_words = set(title.replace("—", " ").replace("-", " ").split())
                agent_words = set(agent_text.replace("—", " ").replace("-", " ").split())
                # Remove common stop words
                stop_words = {"的", "是", "在", "了", "和", "与", "a", "the", "to", "and", "for", "of", "in", "on"}
                title_words -= stop_words
                agent_words -= stop_words

                overlap = title_words & agent_words
                score += len(overlap) * 2

                # Tag matching
                for tag in tags:
                    if tag in agent_text:
                        score += 3

                # Description keyword overlap
                if desc:
                    desc_words = set(desc.replace("—", " ").replace("-", " ").split()) - stop_words
                    score += len(desc_words & agent_words)

                if score > best_score:
                    best_score = score
                    best_match = task

            if best_match and best_score >= 3:
                task_id = best_match["id"]
                task_title = best_match.get("title", "")
                task_status = best_match.get("status", "pending")

                if task_status == "pending":
                    # Auto-update to running
                    _api_call("PUT", f"/api/tasks/{task_id}", {"status": "running"}, project_id=project_id)
                    warnings.append(
                        f"[OS提醒] 已自动关联任务墙：「{task_title}」→ running"
                    )
                else:
                    warnings.append(
                        f"[OS提醒] 当前工作关联任务墙：「{task_title}」（状态: {task_status}）"
                    )

                # Save matched task ID for later completion tracking
                state["last_dispatched_task_id"] = task_id
                state["last_dispatched_task_title"] = task_title
            elif best_score < 3 and pending_tasks:
                # No good match - warn to create on wall（节流 3600s，与 PreToolUse 侧共用键）
                _wm_last = state.get("wall_match_reminder_at", 0)
                _wm_now = time.time()
                if _wm_now - _wm_last >= 3600:
                    state["wall_match_reminder_at"] = _wm_now
                    warnings.append(
                        "[OS提醒] 此Agent工作未匹配到任务墙项。建议先用 task_create 上墙，确保工作可追踪。"
                        f"当前任务墙有 {len(pending_tasks)} 个待办任务"
                    )

        except Exception:
            pass  # Task wall sync is advisory — never block

    # 2. After SendMessage with completion keywords: advisory reminder only.
    # NOTE (2026-07-14): this branch used to auto-PUT the task to completed.
    # That was wrong-direction inference — Leader outbound messages like
    # "完成后向我汇报" hit the substring match and marked in-progress tasks
    # completed, bypassing Leader acceptance. Hook must never write task
    # status here; it only nudges the Leader to use task_update explicitly.
    if tool_name == "SendMessage":
        input_str = str(event_data.get("tool_input", {}))
        completion_keywords = ["完成", "completed", "done", "finished", "汇报"]
        is_completion = any(kw in input_str.lower() for kw in completion_keywords)
        is_shutdown = "shutdown" in input_str.lower()

        if is_completion and not is_shutdown:
            last_task_id = state.get("last_dispatched_task_id")
            last_task_title = state.get("last_dispatched_task_title")
            if last_task_id:
                warnings.append(
                    f"[OS提醒] 检测到完成类消息。若「{last_task_title}」确已完成并验收，"
                    "请用 task_update 将其置 completed（hook 不自动写库）"
                )
                # Clear tracking — remind once, then stop nagging
                state.pop("last_dispatched_task_id", None)
                state.pop("last_dispatched_task_title", None)

    return warnings


def main() -> None:
    # Force UTF-8 output on Windows (default is gbk, causes garbled Chinese)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            return
        payload = json.loads(raw)
    except Exception:
        return

    # CC hook payload doesn't include event type name; inject via CLI arg
    if len(sys.argv) > 1 and "hook_event_name" not in payload:
        payload["hook_event_name"] = sys.argv[1]

    event_name = payload.get("hook_event_name", "")
    state = _load_supervisor_state()
    warnings: list[str] = []

    # Resolve project ID once; propagate to all project-scoped API calls
    project_id = _resolve_project_id()

    if event_name == "PreToolUse":
        w = _check_agent_team_name(payload)
        if w:
            warnings.append(w)
        w = _check_leader_doing_too_much(payload, state)
        if w:
            warnings.append(w)
    if event_name == "PostToolUse":
        # Auto-update task wall when Agent is dispatched or reports completion
        post_warnings = _post_tool_taskwall_sync(payload, state, project_id=project_id)
        warnings.extend(post_warnings)

    # Workflow reminders (checked for both PreToolUse and PostToolUse)
    if event_name in ("PreToolUse", "PostToolUse"):
        wf_warnings = _check_workflow_reminders(payload, state, project_id=project_id)
        warnings.extend(wf_warnings)

    _save_supervisor_state(state)

    # PreToolUse/PostToolUse hooks inject text into conversation via hookSpecificOutput
    #
    # 绝不填 permissionDecision（2026-07-27 缔造者裁定）：该字段是**可选**的表态位
    # （allow 直接放行 / deny 拒绝 / ask 询问 / 不给=不表态走 CC 默认流程），旧实现
    # 把它当成"PreToolUse 必须带的输出格式"每次填 allow，其优先级高于用户选的权限
    # 模式——default/plan/acceptEdits 一律被覆盖，Agent|Bash|Edit|Write|Workflow 五类
    # 工具的权限询问全被静音（30 天 43,605 次调用无一询问）。CC 自己有完整的权限模式
    # 供用户选择，OS 不替它做决定：本 hook 只注入提醒文本，权限交回 CC。
    output = {"hookSpecificOutput": {"hookEventName": event_name}}
    if warnings:
        output["hookSpecificOutput"]["additionalContext"] = "\n".join(warnings)
    sys.stdout.write(json.dumps(output))


def _yield_if_superseded() -> None:
    """Backup-chain yield: exit 0 if the source-install main chain covers this hook.

    When AI Team OS is present both as a marketplace plugin and via the source
    installer, CC fires two byte-identical copies of every hook. To keep exactly
    one chain speaking, the plugin-mode copy exits silently iff ~/.claude/settings.json
    already registers this same script under ~/.claude/hooks/ai-team-os/. Hooks the
    main chain does not register keep running from the plugin (out-of-box backup —
    no coverage gap). Only the plugin-mode copy ever yields: CLAUDE_PLUGIN_ROOT is
    set by CC for plugin hooks only and __file__ lives under it; the runtime
    main-chain copy and any direct/repo/test run lack that and never yield. Pure
    stdlib, one small file read; any error falls through and runs (fail-safe).
    """
    import os
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if not plugin_root:
        return
    try:
        import json
        from pathlib import Path
        here = Path(__file__).resolve()
        if Path(plugin_root).resolve() not in here.parents:
            return
        settings = Path.home() / ".claude" / "settings.json"
        registered = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except Exception:
        return
    name = os.path.basename(__file__)
    for groups in registered.values():
        for group in groups:
            for hook in group.get("hooks", []):
                cmd = hook.get("command", "")
                if "ai-team-os" in cmd and name in cmd:
                    raise SystemExit(0)


if __name__ == "__main__":
    _yield_if_superseded()
    main()
