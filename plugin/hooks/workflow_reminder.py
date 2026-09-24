#!/usr/bin/env python3
"""Workflow reminder - PreToolUse guard + reminder hook.

Everything here is about the call that is about to run, so it runs on
PreToolUse only. A PostToolUse invocation exits at once, silently: running the
same checks again after the tool injected every advisory a second time and
reported a "block" for a command that had already run.

Two phases, in this order:
  1. Local guards (S3-S6 may exit(2); S1 only warns): pure Python plus read-only git.
     They run before any HTTP, so a stalled OS API can never push a blocking
     verdict past Claude Code's 5s hook limit - a killed hook is a silent allow.
  2. Advisory reminders. Only the task-wall check on a named Agent dispatch asks
     the OS API (project resolve cached per cwd for 5 min in supervisor-state.json).
Prints nothing at all when there is nothing to say.

Every block also shows the user one red line (user_notice.emit_block) and tells
the model, at the end of its stderr, that the user saw it; a branch switched
under a session shows one line too. Both are rendered locally, no HTTP.
Usage: python -m aiteam.hooks.workflow_reminder <PreToolUse|PostToolUse>
"""

import contextlib
import copy
import importlib.util
import json
import os
import random
import re
import sys
import time
import urllib.request

_SUPERVISOR_STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "data", "ai-team-os")
_SUPERVISOR_STATE_FILE = os.path.join(_SUPERVISOR_STATE_DIR, "supervisor-state.json")
_PORT_FILE = os.path.join(_SUPERVISOR_STATE_DIR, "api_port.txt")

# ── Process deadline ─────────────────────────────────────────────────────────
#
# Claude Code kills a PreToolUse hook at 5s and then runs the tool anyway, so a
# guard that is still probing at 5s has allowed the command without assessing
# it. The deadline bounds the local guard phase (S1, S3-S6) only: its git probes
# are capped by it and S4 blocks once it has passed, so that phase answers within
# ~3.5s of process start, leaving room for interpreter cold start and exit. The
# API-backed phase after it is not covered - each request there has only its own
# timeout. When run as a script the deadline counts from module import (the
# closest thing to process start this file can see); main() called in-process
# counts from its own start. Only main() arms it, and disarms it on the way out;
# direct calls (tests, other importers) keep the per-call git timeouts alone.
_HOOK_T0 = time.monotonic()
_HOOK_BUDGET_S = 3.5
_hook_deadline: float | None = None


# The event being judged, for the user line a block shows (session dedup, language).
_EVENT_CTX: dict = {"session_id": "", "cwd": ""}
# Branch switches found by S5 in this run: (checkout, recorded branch, current HEAD).
_BRANCH_SWITCHES: list = []


def _user_notice():
    """Load the shared notice module next to this file; None if it cannot load."""
    module = sys.modules.get("user_notice")
    if module is not None:
        return module
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_notice.py")
        spec = importlib.util.spec_from_file_location("user_notice", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["user_notice"] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop("user_notice", None)
        return None


def _block(message: str, catalog_id: str, params: dict, variant: str = "", key_variant: str = "") -> None:
    """Refuse the tool call: one red user line on stdout, the reason on stderr, exit 2.

    The user line is shown once per session per target; the model learns from the
    end of stderr that the user saw it.
    """
    note = ""
    notice = _user_notice()
    if notice is not None:
        session_id = _EVENT_CTX.get("session_id") or ""
        key = ""
        if key_variant:
            key = f"{catalog_id}:{session_id}:{key_variant}"
        try:
            note = notice.emit_block(catalog_id, params, session_id=session_id,
                                     cwd=_EVENT_CTX.get("cwd") or os.getcwd(),
                                     variant=variant, key=key)
        except Exception:
            note = ""
    sys.stderr.write(message + note)
    sys.exit(2)


def _teardown_variant(reason: str) -> str:
    """unverified when the probe could not answer, unsaved when work would really be lost.

    Every "could not determine" reason in S4 says 无法 (cannot); every real-loss
    reason names what would be lost instead. The test pins each reason text.
    """
    return "unverified" if "无法" in reason else "unsaved"


def _arm_deadline(started_at: float, budget_s: float = _HOOK_BUDGET_S) -> None:
    global _hook_deadline
    _hook_deadline = started_at + budget_s


def _disarm_deadline() -> None:
    global _hook_deadline
    _hook_deadline = None


def _deadline_remaining() -> float | None:
    """Seconds left before the process deadline, or None when none is armed."""
    if _hook_deadline is None:
        return None
    return _hook_deadline - time.monotonic()


def _deadline_passed() -> bool:
    remaining = _deadline_remaining()
    return remaining is not None and remaining <= 0


def _safe_session_id(session_id: str) -> str:
    """Strip anything that isn't alphanumeric, hyphen, or underscore to prevent path traversal."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", session_id)


# Returned by _run_git_readonly when the probe never ran to completion (git
# missing, timeout, OS error). Distinct from a non-zero exit code, which means
# git ran and answered - see the docstring below for why collapsing the two is
# how a hard-block guard turns into a silent pass-through.
_GIT_UNAVAILABLE = -1


def _run_git_readonly(
    args: list[str], cwd: str, timeout: float = 5.0, stdin_text: str | None = None
) -> tuple[int, str]:
    """Run a read-only git command, returning (returncode, stripped stdout).

    Narrow scope: this only ever runs after a rare, already-matched command
    pattern (S4 teardown, S5 commit), never on every Bash call. The timeout is
    capped by the process deadline, so a chain of probes cannot outlive the
    5s limit Claude Code puts on this hook; once the deadline has passed no new
    probe is started at all.

    Three-state on purpose: `_GIT_UNAVAILABLE` means "the probe did not run", any
    other non-zero code means "git ran and said no". Those are not the same fact
    and callers must not merge them - the old two-state version made
    "git is not on PATH" and "this directory is not a repository" indistinguishable,
    so a hung or missing git read as "nothing to protect here" and the guard let
    dirty worktrees be deleted.

    `stdin_text` feeds `git rev-list --stdin`: the exclusion list can hold hundreds
    of refs in a real repo (210 in the sample that motivated this), well past what
    is safe to splice into an argv.

    A timed-out probe is killed together with its process group: killing only the
    direct child leaves any grandchild holding the output pipes open, and reading
    them to EOF would then block past the timeout that was supposed to end it.
    """
    import subprocess

    remaining = _deadline_remaining()
    if remaining is not None:
        if remaining <= 0:
            return _GIT_UNAVAILABLE, ""
        timeout = min(timeout, remaining)
    try:
        proc = subprocess.Popen(
            ["git", "-C", cwd] + args,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=(os.name == "posix"),
        )
    except Exception:
        return _GIT_UNAVAILABLE, ""
    try:
        out, _err = proc.communicate(input=stdin_text, timeout=timeout)
    except Exception:
        _kill_probe(proc)
        return _GIT_UNAVAILABLE, ""
    return proc.returncode, (out or "").strip()


def _kill_probe(proc) -> None:
    """Kill a git probe and everything it spawned, then reap it briefly."""
    import signal

    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
    with contextlib.suppress(Exception):
        proc.communicate(timeout=0.2)


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
    undetermined: list[tuple[str, str]] = []  # parse-level "cannot tell" -> block: (reason, target)

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
                        undetermined.append((f"用 rm -rf 删除 worktree 目录：{reason}", operand))
                        found += 1
                    found += len(targets)
                    teardowns.extend((t, "用 rm -rf 删除 worktree 目录") for t in targets)
                runtime_operands = not operands or any(
                    _is_indeterminate_token(op) for op in operands
                )
                if not found and runtime_operands and mentions_worktrees:
                    undetermined.append((
                        "用 rm -rf 删除 worktree 目录：命令提到了 .claude/worktrees，"
                        "但删除目标来自管道/find/变量（解析不出具体路径）",
                        _WORKTREES_MARKER,
                    ))

            for call_cwd, args in _git_calls(tokens, cwd):
                if not args:
                    continue
                if args[0] == "worktree" and len(args) > 1 and args[1] == "remove":
                    _, operands = _split_flags_operands(args[2:])
                    usable = [op for op in operands if not _is_indeterminate_token(op)]
                    if not usable:
                        undetermined.append(("删除 worktree：命令里解析不出可用的 worktree 路径", "worktree"))
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
                        undetermined.append((
                            f"强删分支：操作数 {op} 解析不出确定的分支名，请改成显式分支名后重试", op
                        ))
                    deletions.extend((call_cwd, ref) for ref in refs)
                elif args[0] == "update-ref":
                    refs, unknown = _update_ref_delete_refs(args)
                    for op in unknown:
                        undetermined.append((f"删除引用：操作数 {op} 解析不出确定的引用名", op))
                    deletions.extend((call_cwd, ref) for ref in refs)
        except Exception:
            # A parser defect must not brick every Bash call, but it also must
            # not silently disarm the guard: only a segment that looks like a
            # teardown resolves to BLOCK, anything else is left alone.
            lowered = segment.lower()
            if _WORKTREES_MARKER in lowered.replace("\\", "/") or re.search(
                r"\b(worktree\s+remove|branch\s+-{1,2}\w*d|update-ref)", lowered
            ):
                undetermined.append(
                    (f"解析这段命令时出错，无法确认它会删掉什么：{segment[:80]}", segment[:80])
                )

    for reason, what in undetermined:
        _block(
            f"[OS BLOCK] 拒绝{reason}。"
            "解析不出确定目标时一律按最坏情况处理（放行可能静默丢工作，拦下只是多一步人工确认）。",
            "blocked_teardown", {"target": what}, "unverified",
        )

    # `rm -rf .claude/worktrees .claude/worktrees/wf_a` names wf_a twice; assess
    # each directory once, in the order the command line reaches it.
    # The deadline is checked on both sides of every assessment: a verdict that
    # was reached with the budget exhausted may rest on a probe the deadline cut
    # short, so it is not trusted in either direction.
    n_targets = len({os.path.realpath(t) for t, _ in teardowns}) + len(
        {(os.path.realpath(c), _norm_ref(r)) for c, r in deletions}
    )
    seen: set[str] = set()
    for target, via in teardowns:
        key = os.path.realpath(target)
        if key in seen:
            continue
        seen.add(key)
        _block_teardown_on_deadline(n_targets, target)
        dirty, blocked, advisory = _assess_worktree_teardown(target)
        _block_teardown_on_deadline(n_targets, target)
        if dirty or blocked:
            reason = "存在未提交/未跟踪变更" if dirty else blocked
            _block(
                f"[OS BLOCK] 拒绝{via} {target}：{reason}。"
                "先提交/推送备份，或 git branch <名字> <commit> 给它一个引用；"
                "确认要放弃这些改动需本人手动处理，不要重放这条被拦的命令。",
                "blocked_teardown", {"target": target}, _teardown_variant(reason),
            )
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
        _block_teardown_on_deadline(n_targets, name)
        repo_code, _ = _run_git_readonly(["rev-parse", "--git-dir"], cwd=probe_cwd)
        _block_teardown_on_deadline(n_targets, name)
        if repo_code == _GIT_UNAVAILABLE:
            _block_ref_deletion(name, "git 探测无法完成（git 不可用/超时），无法确认删除后 commit 仍可找回")
        if repo_code != 0:
            continue  # git says this is not a repository: its own error is the answer
        exists_code, _ = _run_git_readonly(["rev-parse", "--verify", "--quiet", ref], cwd=probe_cwd)
        _block_teardown_on_deadline(n_targets, name)
        if exists_code == _GIT_UNAVAILABLE:
            _block_ref_deletion(name, "git 探测无法完成（git 不可用/超时），无法确认这条引用指向什么")
        if exists_code != 0:
            continue  # confirmed repo, no such ref: deleting it cannot lose anything
        determined, orphans = _orphan_commits(
            probe_cwd, ref, exclude_refs=tuple(doomed.get(repo_key) or {ref})
        )
        _block_teardown_on_deadline(n_targets, name)
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
    _block(
        f"[OS BLOCK] 拒绝强删分支 {name}：{reason}。"
        "先合并或推送备份；确认要放弃这些改动需本人手动处理，不要重放这条被拦的命令。",
        "blocked_teardown", {"target": name}, _teardown_variant(reason),
    )


def _block_teardown_on_deadline(n_targets: int, target: str = "") -> None:
    """S4 ran out of the process budget: block instead of being killed mid-probe.

    Being killed by Claude Code at 5s is not a neutral outcome - the tool then
    runs unassessed, which is exactly the silent false ALLOW the asymmetry rule
    above forbids. Reaching the deadline is "cannot determine", so it blocks.

    `n_targets` is how many distinct worktrees/refs the command tears down. With
    only one, splitting the command cannot help - git itself is too slow to be
    assessed in time - so the way out is a manual check, not a retry.
    """
    if not _deadline_passed():
        return
    head = (
        "[OS BLOCK] 拒绝本次 worktree/分支拆除：安全评估超时——本 hook 必须在 "
        f"Claude Code 的 5 秒上限内答复，{_HOOK_BUDGET_S:.1f} 秒预算内没评估完，已按拦截处理。"
    )
    manual = (
        "请本人在终端用 git status / git log 确认没有会丢的改动后手动操作，不要重放这条被拦的命令。"
    )
    if n_targets <= 1:
        message = f"{head}这条命令只拆一个目标仍然超时，说明 git 本身响应太慢，分批也过不去。{manual}"
    else:
        message = (
            f"{head}一条命令里要检查的 worktree/分支太多，或 git 响应太慢。"
            "请分批拆除：每条命令只带一两个 worktree 或分支，逐批重试；"
            f"若只带一个仍超时，说明 git 本身太慢，{manual}"
        )
    _block(message, "blocked_teardown", {"target": target or "worktree"}, "timeout")


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


_API_TIMEOUT = 2


_PROJECT_ID_CACHE_TTL = 300  # 5 minutes
# cwd -> {"id", "at"}。键是 realpath(cwd)：
# 多个项目的会话在本机并行，全局单值缓存会把 A 项目的 id 借给 B（跨项目守卫据此
# 误拦）；/tmp 与 /private/tmp 这类同一目录的两种写法则应当命中同一条。
_PROJECT_CACHE_KEY = "project_id_by_cwd"
# 24h 没被刷新的 cwd 条目在下次写入时剪掉，state 文件不随访问过的目录无限膨胀。
_PROJECT_CACHE_PRUNE_TTL = 24 * 3600


def _resolve_project_id(
    state: dict | None = None,
    cwd: str | None = None,
    *,
    http: dict | None = None,
) -> str | None:
    """Resolve the project for `cwd` via the OS API, cached per cwd for 5 minutes.

    The cache lives in `state`, the same dict main() loads once and saves once.
    It used to be written through a second load/save of its own, which main()
    then overwrote with its older copy - so from 2026-04 on the cache never
    survived a single call and every Pre/Post paid a synchronous resolve.

    "No project here" ("") is cached as well: an unregistered directory would
    otherwise pay the round trip on every call. It goes stale the moment the
    directory is registered, which only delays an advisory by up to 5 minutes.

    `http` is main()'s per-invocation memo: an answer the API gave during this
    call (or its failure) is reused, never asked for twice. A failed request
    caches nothing and falls back to the stale entry. Without a `state`
    (standalone callers) the cache is only read.
    """
    if state is None:
        state = _load_supervisor_state()
    cwd = cwd or os.getcwd()
    key = os.path.realpath(cwd)
    by_cwd = state.get(_PROJECT_CACHE_KEY)
    if not isinstance(by_cwd, dict):
        by_cwd = {}
    entry = by_cwd.get(key)
    cached_id = entry.get("id") if isinstance(entry, dict) else None
    memo_key = ("resolve", key)
    if http is not None and memo_key in http:
        answered = http[memo_key]  # None: this call's request already failed
        return (answered if answered is not None else cached_id) or None
    now = time.time()
    if isinstance(entry, dict) and 0 <= now - entry.get("at", 0) < _PROJECT_ID_CACHE_TTL:
        return cached_id or None

    api_url = _get_api_url()
    try:
        req = urllib.request.Request(
            f"{api_url}/api/context/resolve",
            data=json.dumps({"cwd": cwd, "auto_create": False}).encode(),  # 归属铁律：绝不自动立项
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        project_id = data.get("project_id") or (data.get("project") or {}).get("id") or ""
    except Exception:
        if http is not None:
            http[memo_key] = None
        return cached_id or None
    if http is not None:
        http[memo_key] = project_id

    for k in list(by_cwd.keys()):
        e = by_cwd.get(k)
        if not isinstance(e, dict) or now - e.get("at", 0) > _PROJECT_CACHE_PRUNE_TTL:
            by_cwd.pop(k, None)
    by_cwd[key] = {"id": project_id, "at": now}
    state[_PROJECT_CACHE_KEY] = by_cwd
    # 旧的全局单值缓存键（从未生效过，且会跨项目串用），换成按 cwd 分键后就地清掉。
    state.pop("cached_project_id", None)
    state.pop("cached_project_id_at", None)
    return project_id or None


def _get_json_once(api_url: str, path: str, project_id: str | None, http: dict | None):
    """GET `api_url + path` (X-Project-Id when known) at most once per hook call.

    `http` is the per-invocation memo main() threads through every advisory
    check. A failure is remembered and re-raised as well: an API that just timed
    out once is not asked the same question again two checks later.
    """
    key = (api_url + path, project_id or "")
    if http is not None and key in http:
        ok, value = http[key]
        if ok:
            return value
        raise value
    headers: dict[str, str] = {}
    if project_id:
        headers["X-Project-Id"] = project_id
    req = urllib.request.Request(f"{api_url}{path}", method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT) as resp:
            value = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        if http is not None:
            http[key] = (False, exc)
        raise
    if http is not None:
        http[key] = (True, value)
    return value


def _get_data_list(api_url: str, path: str, project_id: str | None, http: dict | None) -> list:
    """The "data" list of a memoised GET (see _get_json_once)."""
    return _get_json_once(api_url, path, project_id, http).get("data", [])


def _read_state_file() -> dict | None:
    """Parsed state file; {} when absent, None when present but unreadable."""
    return _read_state_snapshot()[0]


def _file_identity(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _read_state_snapshot() -> tuple[dict | None, tuple[int, int, int] | None]:
    """(parsed state, identity of the very file that was parsed).

    State as in _read_state_file. The identity comes from fstat on the open
    file, so it describes what was parsed even if the path is replaced right
    after; None when there was no file (or nothing usable was read).
    """
    try:
        f = open(_SUPERVISOR_STATE_FILE, encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError:
        return None, None
    with f:
        try:
            identity = _file_identity(os.fstat(f.fileno()))
            data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None, None
    return (data if isinstance(data, dict) else None), identity


def _load_supervisor_state() -> dict:
    """Load supervisor state file; return default value if missing or corrupted."""
    return _read_state_file() or {}


# ── supervisor-state.json 的并发写 ─────────────────────────────────────────────
#
# 这个文件是全机共享的：所有会话、所有子 agent 的 PreToolUse 都在读改写它。
# 旧写法是 open("w") 截断后再写，别的进程恰好在这一瞬间
# 读到空文件或半截 JSON，就当成 {}，然后把"空状态 + 自己这一次的改动"整份写回——
# 计数器归零，会话节流桶和 S5 分支认领整批消失（并发实测：读侧大量见到半截文件，
# 预置的 S5 认领被清空，计数器终值远低于调用次数）。
#
# 现在三层：
#   1. 原子替换：写到同目录临时文件再 os.replace，读者只可能看到旧的或新的整份。
#   2. 保存时三方合并：load 时留一份 base，保存前一刻重读磁盘上的当前内容，
#      只把"本次调用相对 base 改了的键"合进去。读改写窗口里夹着 HTTP（慢时数秒），
#      整份覆盖会把这段时间里别的会话写下的会话节流记录和 S5 认领一起冲掉。
#   3. 替换前核对：合并结果写进临时文件后，os.replace 之前再 stat 一次，文件已不是
#      刚才读的那份（inode/mtime/size 变了）就放弃这次写，随机退避后重读重合并。
#      只靠第 2 层时"重读 → 替换"的窗口是整段解析 + 合并 + 序列化（空闲约 1.6ms，
#      12 路并发争 CPU 时更长），实测计数增量丢了约一半；核对把窗口压到 stat 与
#      rename 两个系统调用之间。_SAVE_ATTEMPTS 次都没抢到就放弃本次改动：丢的只是
#      自己这一次的增量，强行写反而会冲掉刚抢到的那几方。
#
# 刻意不加文件锁：运行期新造文件锁被裁定禁止（S5 的认领合并正属于所有权仲裁，
# 见 _check_commit_branch_ownership）。第 3 层是乐观重试，不留任何锁文件、进程
# 死了也没有东西要清。代价是极端并发下仍会丢少量更新（stat 与 rename 之间的一瞬、
# 或重试用尽）。丢的只是一条会话节流记录（最多让一条提醒多出一次）；S5 认领丢了，
# 下次提交会重新认领；并发首认领仍恰好留下一条（见 _reconcile_branch_claims）。
# 这里的状态里没有计数器：按调用次数取模的提醒已全部退役，它们的全局计数器正是
# 多会话串计的来源，所以合并也不再为 int 做增量叠加。
_SAVE_ATTEMPTS = 12
_SAVE_BACKOFF_S = 0.002  # 第 n 次重试前随机退避 0 ~ n * 此值


def _merge_state(base: object, mine: object, theirs: object) -> object:
    """Three-way merge of one state value: this call's edits (base -> mine) onto theirs.

    - dict: recurse per key; keys this call did not touch keep theirs; a key this
      call deleted is dropped only if nobody else changed it meanwhile.
    - anything else this call changed: this call's value wins.
    """
    if isinstance(mine, dict):
        if not isinstance(theirs, dict):
            # A copy: _reconcile_branch_claims edits the merged result in place,
            # and a save may merge the same `mine` again on retry.
            return copy.deepcopy(mine)
        b = base if isinstance(base, dict) else {}
        out = dict(theirs)
        for key in b.keys() | mine.keys():
            if key not in mine:
                if key in out and out[key] == b.get(key):
                    out.pop(key)
                continue
            if key in b and b[key] == mine[key]:
                continue
            out[key] = _merge_state(b.get(key), mine[key], theirs.get(key))
        return out
    return mine


def _reconcile_branch_claims(base: dict, mine: dict, merged: dict) -> None:
    """Concurrent first commits on one branch: the claim already on disk wins.

    S5 records a claim only when it saw no other agent's active claim on that
    branch. Two calls that overlap can both see "nobody" and both record, and a
    plain per-key merge would keep both - after which each agent finds its own
    claim first and S5 never blocks either of them again. Run one after the
    other, the second call would have been blocked and recorded nothing. So a
    claim this call added is dropped when the file now holds another agent's
    active claim on the same branch; that agent's next commit is then blocked as
    usual. In the rare case the two saves themselves overlap past the
    pre-replace check (see the block comment above), the later replace carries
    only its own claim - the old whole-file overwrite's "last writer wins".
    Either way exactly one claim survives, which is what keeps the conflict
    visible.
    """
    mine_root = mine.get("branch_ownership")
    merged_root = merged.get("branch_ownership")
    if not isinstance(mine_root, dict) or not isinstance(merged_root, dict):
        return
    base_root = base.get("branch_ownership")
    if not isinstance(base_root, dict):
        base_root = {}
    now = time.time()
    for checkout, claims in mine_root.items():
        out = merged_root.get(checkout)
        if not isinstance(claims, dict) or not isinstance(out, dict):
            continue
        seen = base_root.get(checkout)
        seen = seen if isinstance(seen, dict) else {}
        for agent, rec in list(claims.items()):
            if agent in seen or not isinstance(rec, dict):
                continue  # not a claim this call created
            for other, other_rec in out.items():
                if (
                    other != agent
                    and isinstance(other_rec, dict)
                    and other_rec.get("branch") == rec.get("branch")
                    and now - other_rec.get("ts", 0) <= _BRANCH_OWNERSHIP_ACTIVE_TTL
                ):
                    out.pop(agent, None)
                    break


_ANY_FILE = object()


def _path_identity(path: str) -> tuple[int, int, int] | None:
    try:
        return _file_identity(os.stat(path))
    except FileNotFoundError:
        return None


def _atomic_write_json(path: str, data: dict, expect: object = _ANY_FILE) -> bool:
    """Write to a sibling temp file, then os.replace: readers see old or new, never half.

    With `expect` (an identity from _read_state_snapshot, None meaning "no file
    yet"), the replace happens only if the file at `path` is still that one;
    otherwise nothing is written and False comes back so the caller can merge
    again. The temp file sits in the same directory (os.replace must not cross
    filesystems) under a pid + monotonic-ns name opened with O_EXCL, so two
    writers can never share one.
    """
    tmp = f"{path}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        if expect is not _ANY_FILE and _path_identity(path) != expect:
            os.unlink(tmp)
            return False
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return True


def _save_supervisor_state(state: dict, base: dict | None = None) -> None:
    """Save supervisor state atomically.

    With `base` (the state as loaded at the start of this call), only this
    call's edits are merged onto whatever is on disk at save time, re-merged if
    another save lands first, and dropped if that keeps happening - see the
    block comment above. Without it, or when the file on disk is unreadable,
    the whole dict is written as is.
    """
    try:
        os.makedirs(_SUPERVISOR_STATE_DIR, exist_ok=True)
        if base is None:
            _atomic_write_json(_SUPERVISOR_STATE_FILE, state)
            return
        for attempt in range(_SAVE_ATTEMPTS):
            if attempt:
                time.sleep(random.uniform(0, _SAVE_BACKOFF_S * attempt))
            theirs, identity = _read_state_snapshot()
            if theirs is None:
                _atomic_write_json(_SUPERVISOR_STATE_FILE, state)
                return
            merged = _merge_state(base, state, theirs)
            _reconcile_branch_claims(base, state, merged)
            if _atomic_write_json(_SUPERVISOR_STATE_FILE, merged, identity):
                return
        # Every attempt lost the race: drop this call's edits rather than
        # overwrite whatever the winners just wrote.
    except (OSError, TypeError, ValueError):
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

def _commit_probe_cwd(cmd: str, base_cwd: str) -> str | None:
    """The directory a `git commit` on this line actually runs in, or None.

    Both ways an agent aims a commit at a worktree other than the session cwd
    are honoured, and they compose: a leading `cd <dir> &&` chain and a per-call
    `git -C <dir>`. This reuses the exact segment/token/cd/`_git_calls` machinery
    the S4 teardown guard uses, so the two commit-time guards read one command
    line identically instead of via a second, weaker regex.

    Why this replaced a regex (2026-08-19, user-reported false block): the old
    `_GIT_COMMIT_RE` only saw `git -C <dir> commit`; a `cd /tmp/wt && git commit`
    left the probe on the session cwd (the main checkout), so a commit made
    inside an isolated worktree was judged against the main repo's HEAD and hard-
    blocked as "landing on someone else's branch" - the very isolation the guard
    tells you to set up. Structurally that commit cannot reach the main branch;
    the guard was answering a question about the wrong repository.

    The quoted-literal protection is preserved for free: `echo "git commit"`
    tokenizes the quoted string into a single token whose program name is not
    `git`, so `_git_calls` never sees it. An indeterminate `cd $DIR` leaves the
    running cwd unchanged (same conservative choice as S4) rather than guessing.
    """
    joined = re.sub(r"\\[ \t]*\n", " ", cmd)
    cwd = base_cwd
    for segment in _split_shell_segments(joined):
        tokens = _shell_tokens(segment)
        if not tokens:
            continue
        if tokens[0] == "cd" and len(tokens) >= 2:
            if not _is_indeterminate_token(tokens[1]):
                cwd = _resolve_path(tokens[1], cwd)
            continue
        for call_cwd, args in _git_calls(tokens, cwd):
            if args and args[0] == "commit":
                return call_cwd
    return None


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
    call, no lock file - that includes the save path, which also guards nothing
    with a lock. Ownership arbitration through a new runtime lock file is
    forbidden by the same ruling; a claim record is an observation, not a lock -
    it never gates anything by itself, it only decides warn vs. block. When two
    agents' first commits on one branch overlap, the save-time merge keeps
    exactly one of the two claims (_reconcile_branch_claims), so the other
    agent's next commit meets it and is blocked as in a serial run.

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
    probe_cwd = _commit_probe_cwd(cmd, base_cwd)
    if probe_cwd is None:
        return []

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
            _block(
                f"[OS BLOCK] 分支所有权冲突：{checkout} 当前 HEAD 是「{branch}」，"
                f"而这条分支由 agent {other_id} 在 {age_h:.1f} 小时前认领且仍在有效期内。"
                "你正要把提交落到别人的分支上（2026-07-10 就是这样丢过代码）。"
                "请先 git worktree add 开自己的隔离工作区，或与对方确认后由本人操作——"
                "不要重放这条被拦的命令。",
                "blocked_foreign_branch", {"branch": branch},
            )
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
        _BRANCH_SWITCHES.append((checkout, str(mine.get("branch") or ""), branch))
    else:
        bucket[agent_id] = {"branch": branch, "ts": now}

    return warnings


# ── S6: dispatch model tier gate ────────────────────────────────────────────
#
# Every dispatch carries an explicit model, and a fable dispatch carries a
# written reason. Which tier suits which work is the user's own dispatch
# policy; this gate only makes the choice visible.
#
# The trap being closed is inheritance, not a wrong default: an Agent call or a
# workflow `agent()` with no model argument does not fall back to a cheap tier,
# it runs at the CALLER's tier. Dispatched from a fable session,
# a whole fan-out of mechanical workers silently bills at fable rates, and
# nothing in the transcript says so. Only the absence of an argument is visible,
# which is why the gate is on absence rather than on any observed cost.
_MODEL_TIERS = ("fable", "opus", "sonnet", "haiku")
# Both the short aliases CC accepts ('opus') and full ids ('claude-fable-5-1',
# 'claude-haiku-4-5-20251001') must classify alike: the question is which tier
# is being dispatched, not which spelling was typed.
_TIER_ALIAS_RE = {t: re.compile(rf"^{t}|claude-{t}") for t in _MODEL_TIERS}
# Reason marker, Agent tool: first line of the prompt. Tolerant about padding
# and the full-width colon a Chinese keyboard produces by default.
_FABLE_REASON_RE = re.compile(r"\[\s*fable\s*理由\s*[:：]")
# Reason marker, workflow script: one `//` line comment per fable call.
_FABLE_REASON_COMMENT_RE = re.compile(r"//\s*fable\s*理由\s*[:：]")
_AGENT_CALL_RE = re.compile(r"\bagent\s*\(")
_MODEL_KEY_RE = re.compile(r"\bmodel\s*:")
# `{ "model": "opus" }` is legal JS, and the quoted key does not survive literal
# stripping - matched against the raw text so valid syntax is never a false block.
_QUOTED_MODEL_KEY_RE = re.compile(r"""["']model["']\s*:""")
_FABLE_VALUE_RE = re.compile(r"^\s*['\"`]?\s*(claude-)?fable", re.IGNORECASE)
# How far into a prompt the reason marker still counts as "first line": room for
# a leading blank line or a short preamble, not enough for a mention buried in
# the task body to pass as a declared reason.
_FABLE_REASON_SCAN = 300


def _model_tier(model: object) -> str:
    """Classify a raw `model` argument into a dispatch tier.

    Returns one of _MODEL_TIERS, "missing" when nothing usable was passed, or
    "other" for an id this hook does not recognise. "other" is never blocked -
    an unfamiliar id is not evidence of a violation, and a gate that blocks on
    unfamiliarity would have to be disarmed the next time a model ships.
    """
    m = str(model or "").strip().lower()
    if not m:
        return "missing"
    for tier, pattern in _TIER_ALIAS_RE.items():
        if pattern.search(m):
            return tier
    return "other"


def _has_fable_reason(prompt: object) -> bool:
    return bool(_FABLE_REASON_RE.search(str(prompt or "")[:_FABLE_REASON_SCAN]))


def _block_dispatch(message: str, why: str = "no_model") -> None:
    """S6 refusal. ``why`` names the case for the once-per-session user line."""
    variant = "" if why in ("no_model", "workflow_model") else "no_reason"
    _block(f"[OS BLOCK] {message}不要重放这条被拦的命令。", "blocked_dispatch_model", {},
           variant, key_variant=why)


def _check_agent_dispatch_model(tool_input: dict) -> list[str]:
    """S6-A: the Agent tool must name its tier out loud."""
    prompt = tool_input.get("prompt", "")
    subagent_type = str(tool_input.get("subagent_type") or "").strip().lower()

    # fork is checked first and on its own terms: it ignores the model argument
    # entirely and always inherits the parent session, so demanding a model
    # here would be asking for a value with no effect - a lie the guard would
    # then have to keep believing. What a fork actually needs is the same
    # justification a fable dispatch needs.
    if subagent_type == "fork":
        if not _has_fable_reason(prompt):
            _block_dispatch(
                "fork 派工未写理由：subagent_type='fork' 会忽略 model 参数、总是继承父会话模型"
                "（在 fable 会话里就是按 fable 派工）。若确需继承本会话上下文，"
                "请在 prompt 首行写 `[fable 理由: …]`；只是想派活就改用普通 subagent_type "
                "并显式写 model。改完再发，",
                "fork_reason",
            )
        return []

    tier = _model_tier(tool_input.get("model"))
    if tier == "missing":
        _block_dispatch(
            "派工未指定 model：不写 model 不是走默认值，而是继承当前会话模型——"
            "在 fable 会话里等于整场按 fable 派工。请按你的派工策略显式写 model；"
            "用 fable 则同时在 prompt 首行写 `[fable 理由: …]`。补上参数再发，"
        )
    if tier == "fable" and not _has_fable_reason(prompt):
        _block_dispatch(
            f"派 fable 未写理由：model='{tool_input.get('model')}' 属 fable 档。"
            "请在 prompt 首行写 `[fable 理由: …]` 说明这件事为何要用 fable，"
            "或改用其他档。改完再发，",
            "fable_reason",
        )
    if tier in ("sonnet", "haiku"):
        return [
            f"[安全] 派工档位提醒：model='{tool_input.get('model')}' 属 {tier} 档，"
            "请确认这一档符合你的派工策略再继续。"
        ]
    return []


def _strip_script_noise(script: str) -> str:
    """Blank out comments and string literals, preserving every offset.

    A workflow script carries whole prompts as string literals, and those
    prompts routinely quote the words `agent(` and `model:` - scanning raw text
    counts prose as code. Stripped characters become spaces (newlines kept) so
    offsets still map onto the original: call spans are located in this masked
    view, then the VALUE after each `model:` is read back out of the raw script,
    where it still exists. Splitting the two lookups that way is the point -
    keys are code and must not come from prose, values are string literals and
    cannot be found anywhere else.

    Deliberately not a JS parser. A template literal is masked whole, `${...}`
    included; an `agent(` call written inside an interpolation would be missed,
    which no real script does and which costs one un-gated call rather than a
    crash.
    """
    out: list[str] = []
    mode: str | None = None  # None | "line" | "block" | "'" | '"' | "`"
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""
        if mode is None:
            if ch == "/" and nxt == "/":
                mode, i = "line", i + 2
                out.append("  ")
            elif ch == "/" and nxt == "*":
                mode, i = "block", i + 2
                out.append("  ")
            elif ch in "'\"`":
                mode, i = ch, i + 1
                out.append(" ")
            else:
                out.append(ch)
                i += 1
            continue
        if mode == "line":
            if ch == "\n":
                mode = None
            out.append("\n" if ch == "\n" else " ")
            i += 1
            continue
        if mode == "block":
            if ch == "*" and nxt == "/":
                mode, i = None, i + 2
                out.append("  ")
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
            continue
        # inside a string literal
        if ch == "\\" and nxt:
            out.append("  ")
            i += 2
            continue
        if ch == mode:
            mode = None
        out.append("\n" if ch == "\n" else " ")
        i += 1
    return "".join(out)


def _agent_call_spans(code: str) -> list[tuple[int, int]]:
    """Locate each `agent(` call in masked code as a (start, end) span.

    End is the matching close paren by depth counting, so nested calls in the
    arguments (`agent(build(x), {...})`) stay inside one span instead of cutting
    the options object off. An unbalanced tail (truncated script) extends to the
    end of the text rather than raising - CC rejects such a script anyway.
    """
    spans: list[tuple[int, int]] = []
    for match in _AGENT_CALL_RE.finditer(code):
        depth, i, n = 0, match.end() - 1, len(code)
        while i < n:
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        spans.append((match.start(), min(i + 1, n)))
    return spans


def _check_workflow_dispatch_model(tool_input: dict) -> list[str]:
    """S6-B: every `agent()` in an inline workflow script names its tier."""
    script = tool_input.get("script")
    if not isinstance(script, str) or not script.strip():
        return [
            "[安全] S6 无法静态检查本次 workflow：调用没带内联 script"
            "（走 scriptPath/已存工作流时读不到脚本正文）。"
            "派工纪律照旧——每个 agent() 显式写 model，fable 那处配 `// fable 理由: …`。"
        ]

    # Counted before stripping on purpose: the reason markers live in `//`
    # comments, which the mask is about to erase.
    reasons = len(_FABLE_REASON_COMMENT_RE.findall(script))
    code = _strip_script_noise(script)

    missing: list[int] = []
    fable: list[int] = []
    spans = _agent_call_spans(code)
    for idx, (start, end) in enumerate(spans, 1):
        code_slice, raw_slice = code[start:end], script[start:end]
        key_ends = [m.end() for m in _MODEL_KEY_RE.finditer(code_slice)]
        key_ends += [m.end() for m in _QUOTED_MODEL_KEY_RE.finditer(raw_slice)]
        if not key_ends:
            missing.append(idx)
        elif any(_FABLE_VALUE_RE.match(raw_slice[k:]) for k in key_ends):
            fable.append(idx)

    if missing:
        where = "、".join(str(i) for i in missing)
        _block_dispatch(
            f"workflow 脚本有 {len(missing)} 处 agent() 未写 model"
            f"（第 {where} 处，共 {len(spans)} 处调用）。不写 model 不是走默认值，"
            "而是继承当前会话模型——fable 会话里整场按 fable 价率跑。"
            "每个 agent() 须按你的派工策略显式写 model；用 fable 的那处在上一行补 "
            "`// fable 理由: …`。改完脚本再发，",
            "workflow_model",
        )
    if len(fable) > reasons:
        where = "、".join(str(i) for i in fable)
        _block_dispatch(
            f"workflow 脚本有 {len(fable)} 处 fable agent()（第 {where} 处），"
            f"却只有 {reasons} 条 `// fable 理由: …` 注释——每处 fable 调用须配一条。"
            "补齐注释，或把不必要的那几处改成其他档。改完脚本再发，",
            "workflow_reason",
        )
    if fable:
        return [
            f"[安全] 派工档位提醒：本次 workflow 有 {len(fable)} 处 fable agent()，"
            f"已配 {reasons} 条理由注释。确认每处都落在你的派工策略留给 fable 的关口上。"
        ]
    return []


def _check_dispatch_model_tier(event_data: dict) -> list[str]:
    """S6 driver: no dispatch leaves this session without naming its tier.

    Sibling of S4/S5 in kind - a PreToolUse assertion that blocks - but on the
    dispatch path rather than the git one. Two surfaces, one rule: the Agent
    tool (`model` argument, `[fable 理由: …]` in the prompt) and the Workflow
    tool (`model:` per `agent()`, one `// fable 理由: …` comment each).

    Fails open by construction. Any defect in the masking or span logic degrades
    to a "could not check" advisory, the same shape S5 uses when its git probe
    cannot run: a block must always be a positive finding, never the fallout of
    a parser bug. sys.exit(2) raises SystemExit, a BaseException, so a real
    verdict passes straight through this net.
    """
    tool_name = event_data.get("tool_name", "")
    if tool_name not in ("Agent", "Workflow"):
        return []
    tool_input = event_data.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    try:
        if tool_name == "Agent":
            return _check_agent_dispatch_model(tool_input)
        return _check_workflow_dispatch_model(tool_input)
    except Exception:
        return [
            f"[安全] S6 静态检查未能执行（解析 {tool_name} 参数时出错）。"
            "本次派工既未放行也未拦截——请自己确认显式带了 model"
            "（fable 须写理由）再继续。"
        ]


# ── S3: sensitive files in `git add` ─────────────────────────────────────────
#
# Only the path operands of a real `git add` are judged, read with the same
# segment/token/`_git_calls` machinery S4 and S5 use. The old check matched
# substrings of the whole command line, so `.env` in a commit message, in an
# echo or grep pattern, or in `.env.example` blocked just as hard as the real
# file (measured: 27 blocks over two months, none of them a real secret; one
# of them made the agent throw away a legitimate template edit).
_S3_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist")
# `.env`, `.env.local`, and globs such as `.env*`; not `.environment.ts`.
_S3_ENV_NAME_RE = re.compile(r"^\.env(?:$|[.*?\[])")
# Default ssh-keygen private key names; the `.pub` half of the pair is public.
_S3_SSH_KEY_RE = re.compile(r"^id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?")
# `git stage` is git's built-in synonym for `git add`.
_S3_ADD_VERBS = frozenset({"add", "stage"})
# A backslash in front of a path character is a Windows separator, not a shell
# escape: nobody escapes a plain letter on purpose, and Git Bash would drop the
# backslash, which turns `.\config\.env` into an operand that names no secret.
_S3_BACKSLASH_SEPARATOR_RE = re.compile(r"\\(?=[\w.-])")


def _s3_basename(operand: str) -> str:
    return operand.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()


def _s3_sensitive_kind(operand: str) -> str | None:
    """Which sensitive pattern a `git add` path operand hits, judged on its basename."""
    base = _s3_basename(operand)
    if not base or base.endswith(_S3_TEMPLATE_SUFFIXES):
        return None
    if _S3_ENV_NAME_RE.match(base):
        return ".env"
    key = _S3_SSH_KEY_RE.match(base)
    if key and not base.endswith(".pub"):
        return key.group(0)
    for suffix in (".pem", ".key"):
        if base.endswith(suffix):
            return suffix
    return None


def _s3_is_unseen(operand: str) -> bool:
    """True when the file a `git add` operand names only exists at runtime.

    Judged on the basename, like the sensitive check: `$DIR/app.py` still shows
    which file it is, `$F`, `$(cat list)` and `find`'s `{}` do not. A runtime
    name with a template suffix is a template whatever the variable holds.
    """
    if operand == _S3_LIST_OPERAND:
        return True
    base = _s3_basename(operand)
    if base.endswith(_S3_TEMPLATE_SUFFIXES):
        return False
    return _is_indeterminate_token(base) or base.endswith(")")


_S3_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
# Shell options that consume the next token as their value.
_S3_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
_S3_NESTING_LIMIT = 3
# Stands for the paths a `git add` reads from stdin (`xargs git add`) or from a
# list file (`--pathspec-from-file`): real operands, just not on the command line.
_S3_LIST_OPERAND = "xargs 或 --pathspec-from-file 传入的路径"


def _nested_shell_scripts(tokens: list[str]) -> list[str]:
    """Command strings a token list hands to another shell.

    `bash -c '...'`, `eval ...` and `bash <<< '...'`. A quoted argument is a
    single token, so a `git add` inside `bash -lc "..."` would otherwise be as
    invisible as one inside an echo string. Only a shell's actual script counts:
    the command operand of -c (the first non-option token, `--` skipped), or a
    here-string when the shell has no other script to run. Any other quoted
    text stays text.
    """
    scripts: list[str] = []
    for idx, tok in enumerate(tokens):
        name = _program_name(tok)
        if name == "eval":
            scripts.append(" ".join(tokens[idx + 1 :]))
            continue
        if name not in _S3_SHELLS:
            continue
        wants_command = False  # -c seen: the next operand is the script
        j = idx + 1
        while j < len(tokens):
            opt = tokens[j]
            if opt.startswith("<<<") and not wants_command:
                here = opt[3:] or (tokens[j + 1] if j + 1 < len(tokens) else "")
                if here:
                    scripts.append(here)
                break
            if opt in _S3_SHELL_VALUE_OPTIONS:
                j += 2  # `bash -o pipefail -c ...`: the option takes a value
                continue
            if opt == "--":
                if wants_command and j + 1 < len(tokens):
                    scripts.append(tokens[j + 1])
                break
            if opt[:1] not in ("-", "+"):
                if wants_command:
                    scripts.append(opt)
                break  # otherwise a script file: not readable from here
            if not opt.startswith("--") and "c" in opt[1:]:
                wants_command = True
            j += 1
    return scripts


def _git_add_operands(cmd: str, base_cwd: str, depth: int = 0) -> list[str]:
    """Path operands of every `git add` in a command line, nested shells included.

    Paths that arrive through xargs or `--pathspec-from-file` are represented
    by one `_S3_LIST_OPERAND`.
    """
    operands: list[str] = []
    cmd = _S3_BACKSLASH_SEPARATOR_RE.sub("/", re.sub(r"\\[ \t]*\n", " ", cmd))
    for segment in _split_shell_segments(cmd):
        try:
            tokens = _shell_tokens(segment)
            calls = _git_calls(tokens, base_cwd)
        except Exception:
            continue
        via_xargs = any(_program_name(t) == "xargs" for t in tokens)
        for _call_cwd, args in calls:
            if not args or args[0] not in _S3_ADD_VERBS:
                continue
            flags, paths = _split_flags_operands(args[1:])
            operands.extend(paths)
            if via_xargs or any(f.startswith("--pathspec-from-file") for f in flags):
                operands.append(_S3_LIST_OPERAND)
        if depth < _S3_NESTING_LIMIT:
            for script in _nested_shell_scripts(tokens):
                operands.extend(_git_add_operands(script, base_cwd, depth + 1))
    return operands


def _check_git_add_sensitive(cmd: str, base_cwd: str) -> list[str]:
    """S3: block `git add` of secret-bearing files. Returns advisories; blocks via exit(2).

    Operands that only exist at runtime (`$FILE`, `xargs git add`, `find -exec
    git add {}`) are never blocked on a guess; they get one advisory, since the
    file names this check exists for cannot be seen. A command S3 cannot see as
    a `git add` at all (`eval "$CMD"`) gets nothing: a reminder on every eval
    would be noise. A parser defect skips its segment rather than crash the
    hook, which would take the S4-S6 guards after it down too.
    """
    warnings: list[str] = []
    unseen: list[str] = []
    for operand in _git_add_operands(cmd, base_cwd):
        kind = _s3_sensitive_kind(operand)
        if kind:
            _block(
                f"[OS BLOCK] 拒绝 git add 敏感文件 {operand}（命中 {kind}）："
                "密钥与本地配置不进版本库。模板文件请用 .example/.sample/.template/.dist 后缀；"
                "确需提交请由用户本人手动执行，不要重放这条被拦的命令。",
                "blocked_secret_add", {"file": operand},
            )
        if _s3_is_unseen(operand):
            if operand not in unseen:
                unseen.append(operand)
            continue
        if "credentials" in operand.lower():
            # A name, not proof of a secret: warn, never block.
            warnings.append(
                f"[安全] 安全：git add 的目标 {operand} 像凭据文件，"
                "请确认它不含密钥且已在 .gitignore 中"
            )
    if unseen:
        warnings.append(
            f"[安全] 注意：git add 的目标要到运行时才确定（{'、'.join(unseen)}），"
            "敏感文件拦截看不到真实文件名，请确认其中没有 .env、私钥或 .pem/.key 文件"
        )
    return warnings


def _check_local_guards(event_data: dict, state: dict) -> list[str]:
    """S3, S4, S5, S6 (blocking) and the S1 command warnings: no OS API needed.

    Runs before any HTTP this hook makes. Claude Code kills the hook at 5s and
    then runs the tool anyway, so a verdict queued behind a stalled API call is
    a verdict that never happens (measured: a model-less Agent dispatch went
    through unblocked while the API stalled). Returns advisories; blocks by
    exiting with code 2.
    """
    tool_name = event_data.get("tool_name", "")
    warnings: list[str] = []
    tool_input = event_data.get("tool_input", {})
    _EVENT_CTX["session_id"] = str(event_data.get("session_id") or "")
    _EVENT_CTX["cwd"] = str(event_data.get("cwd") or os.getcwd())

    # S1: dangerous command warnings (Bash). Recursive delete of the root or
    # home directory itself is left to Claude Code's own dangerous-removal
    # check, which parses the command and asks the user even in bypass mode;
    # the regex that used to hard-block it here fired on home subdirectories
    # and on quoted text instead (measured: 9 blocks, all false). One spelling
    # is covered by neither side: a bare `$HOME` operand with no trailing slash
    # (Claude Code's variable-path rule needs the slash, and the removed regex
    # never matched it either). Not closed here on purpose: a PreToolUse
    # warning is only read after the deletion has run, and a block would fire
    # on sandbox scripts that point HOME at a temp directory first.
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        # Strip heredoc blocks (<<'EOF'...EOF, <<"EOF"...EOF, <<EOF...EOF) so that
        # text inside commit messages or other string literals does not trigger S1.
        # Only the executable shell syntax outside heredoc delimiters is scanned.
        cmd_for_s1 = re.sub(r"<<['\"]?(\w+)['\"]?.*?\n.*?\1", "", cmd, flags=re.DOTALL)
        cmd_lower = cmd_for_s1.lower()
        # Recursive delete of a wildcard -> warning
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

        base_cwd = event_data.get("cwd") or os.getcwd()
        # S3: sensitive files in `git add` -> exit(2) hard block
        warnings.extend(_check_git_add_sensitive(cmd_for_s1, base_cwd))

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

    # S6: dispatch model tier gate - see the driver above.
    # PreToolUse only: a model argument checked after the agent already started
    # answers a question nobody can act on any more.
    if event_data.get("hook_event_name") == "PreToolUse":
        warnings.extend(_check_dispatch_model_tier(event_data))

    return warnings


# ── Task-wall match for a dispatched agent ───────────────────────────────────
#
# Splitting on whitespace and asking for two shared words never matched a
# Chinese prompt against a Chinese title, so the "not on the task wall" advisory
# fired on work that was on the wall (87 false reminders in two weeks). Latin
# words stay whole; a CJK run is cut at common function characters and read as
# overlapping character pairs, which is what a Chinese word boundary cannot
# otherwise be found from without a dictionary. A title matches when the prompt
# covers enough of it; a task id quoted in the prompt matches outright.
_MATCH_WORD_RE = re.compile(r"[a-z0-9_]{2,}")
_MATCH_CJK_RUN_RE = re.compile("[\u3400-\u9fff\uf900-\ufaff]+")
_MATCH_CJK_FUNCTION_CHARS = re.compile(r"[的了和与]")
_MATCH_STOP_WORDS = frozenset({"the", "to", "for", "and", "of", "in", "on", "an", "is"})
_TASK_ID_PREFIX_LEN = 8


def _match_tokens(text: str) -> set[str]:
    text = text.lower()
    tokens = {w for w in _MATCH_WORD_RE.findall(text) if w not in _MATCH_STOP_WORDS}
    for run in _MATCH_CJK_RUN_RE.findall(text):
        for part in _MATCH_CJK_FUNCTION_CHARS.split(run):
            if len(part) == 1:
                tokens.add(part)
            tokens.update(part[i : i + 2] for i in range(len(part) - 1))
    return tokens


def _dispatch_matches_task(task: dict, text: str, text_tokens: set[str]) -> bool:
    """True when the dispatched work reads as this wall item.

    Covering at least two of the title's tokens and 30% of them errs toward a
    match: a missed advisory costs nothing, a false one teaches the model to
    ignore the hook.
    """
    task_id = str(task.get("id") or "")
    if len(task_id) >= _TASK_ID_PREFIX_LEN and task_id[:_TASK_ID_PREFIX_LEN].lower() in text:
        return True
    title_tokens = _match_tokens(task.get("title") or "")
    if not title_tokens:
        return False
    shared = len(title_tokens & text_tokens)
    return shared >= min(2, len(title_tokens)) and shared * 10 >= len(title_tokens) * 3


def _wants_task_wall_check(event_data: dict) -> bool:
    """An Agent dispatched with a name or team: the one advisory that asks the OS API."""
    tool_input = event_data.get("tool_input")
    return (
        event_data.get("tool_name") == "Agent"
        and isinstance(tool_input, dict)
        and bool(tool_input.get("team_name") or tool_input.get("name"))
    )


def _check_agent_task_wall(
    input_dict: dict, state: dict, session_id: str, project_id: str | None, http: dict
) -> list[str]:
    """An Agent dispatched with a name: is the work it carries on the task wall?"""
    warnings: list[str] = []
    has_active_task = False
    active_teams: list[dict] = []
    api_url = _get_api_url()
    try:
        teams = _get_data_list(api_url, "/api/teams", project_id, http)
        active_teams = [t for t in teams if t.get("status") == "active"]
        if active_teams:
            team_id = active_teams[0].get("id", "")
            if team_id:
                tasks = _get_data_list(api_url, f"/api/teams/{team_id}/tasks", project_id, http)
                has_active_task = any(
                    t.get("status") in ("running", "in_progress") for t in tasks
                )
    except Exception:
        has_active_task = True  # API unavailable, don't nag

    # Fallback: project-level tasks (team_id=None) when no team task is running
    if not has_active_task:
        try:
            if active_teams and active_teams[0].get("project_id"):
                pid = active_teams[0]["project_id"]
                proj_data = _get_json_once(
                    api_url, f"/api/projects/{pid}/tasks/running-count", project_id, http
                )
                if proj_data.get("count", 0) > 0:
                    has_active_task = True
        except Exception:
            pass

    if not has_active_task:
        warnings.append(
            "[OS提醒] 当前无进行中任务。派 Agent 干活前先 task_create 把这件事上墙，"
            "否则产出无处记账。"
        )
        return warnings

    text = f"{input_dict.get('prompt', '')} {input_dict.get('description', '')}".lower()
    if not text.strip() or not project_id:
        return warnings
    try:
        wall = _get_json_once(
            api_url,
            f"/api/projects/{project_id}/task-wall?limit=20&include_completed=false",
            project_id,
            http,
        )
    except Exception:
        return warnings  # advisory only
    if not isinstance(wall, dict):
        return warnings
    wall_tasks: list[dict] = []
    for group in (wall.get("wall") or {}).values():
        if isinstance(group, list):
            wall_tasks.extend(t for t in group if isinstance(t, dict))
    open_tasks = [t for t in wall_tasks if t.get("status") in ("pending", "running")]
    if not open_tasks:
        return warnings
    text_tokens = _match_tokens(text)
    if any(_dispatch_matches_task(t, text, text_tokens) for t in open_tasks):
        return warnings
    # Once an hour per session: the throttle key used to be global, so one
    # session's reminder silenced every other session for the hour.
    bucket = _session_bucket(state, session_id)
    now = time.time()
    if now - bucket.get("wall_match_reminder_at", 0) >= 3600:
        bucket["wall_match_reminder_at"] = now
        titles = "、".join(str(t.get("title") or "?")[:20] for t in open_tasks[:3])
        warnings.append(
            f"[OS提醒] 此Agent工作未匹配到任务墙项（墙上有：{titles}）。"
            "确认此工作已在任务墙登记？→ task_create 上墙"
        )
    return warnings


def _check_workflow_reminders(
    event_data: dict,
    state: dict,
    project_id: str | None = None,
    http: dict | None = None,
    guard_warnings: list[str] | None = None,
) -> list[str]:
    """Advisory reminders for the call about to run, local guard output included.

    `guard_warnings`: output of _check_local_guards when the caller already ran
    it (main() does, before any HTTP); otherwise the guards run here, first.
    `http`: per-invocation GET memo, see _get_json_once.
    """
    if guard_warnings is None:
        guard_warnings = _check_local_guards(event_data, state)
    if http is None:
        http = {}
    tool_name = event_data.get("tool_name", "")
    session_id = event_data.get("session_id", "")
    tool_input = event_data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    warnings: list[str] = []

    # Workflow (CC orchestration) - soft reminder so the run's output flows back
    # into OS. Once per session: the two steps do not change between runs.
    if tool_name == "Workflow":
        bucket = _session_bucket(state, session_id)
        if not bucket.get("workflow_reminder_shown"):
            bucket["workflow_reminder_shown"] = True
            warnings.append(
                "[OS提醒] Workflow 运行已自动追踪成团队。仍需你做两件事："
                "① task_create 把总任务上墙；② 在每个 workflow agent 的 prompt 里嵌回写指令"
                "（ToolSearch 加载 task_memo_add/report_save 后调用），否则内部产出不入 OS。"
                "模板见 skill /os-workflow。"
            )

    # Agent dispatched with a name: the work should be on the task wall.
    if _wants_task_wall_check(event_data):
        warnings.extend(_check_agent_task_wall(tool_input, state, session_id, project_id, http))

    warnings.extend(guard_warnings)

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
        # Reports data directory: only writes to the actual reports data dirs
        # under ~/.claude/data/ are pointed at report_save. Any other .md write
        # (README, docs, src) is left alone.
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

    return warnings


# Keys earlier versions kept at the top level of supervisor-state.json for
# reminders that no longer exist, or whose throttle moved into the per-session
# bucket. Dropped on the next save so the shared file stops carrying them.
_RETIRED_STATE_KEYS = (
    "leader_consecutive_calls",
    "last_taskwall_view",
    "bottleneck_check_count",
    "team_cleanup_check_count",
    "last_template_reminder",
    "last_memo_reminder",
    "pipeline_pending_warnings",
    "ultracode_hint_at",
    "last_dispatched_task_id",
    "last_dispatched_task_title",
    "workflow_reminder_at",
    "wall_match_reminder_at",
)


def main(started_at: float | None = None) -> None:
    """Hook entry. `started_at`: monotonic time the hook process started.

    The script entry passes the module import time; an in-process caller that
    passes nothing gets a deadline counted from this call. Either way the
    deadline is disarmed on the way out (sys.exit included), so a later
    in-process caller never inherits an expired one.
    """
    _arm_deadline(time.monotonic() if started_at is None else started_at)
    try:
        _main()
    finally:
        _disarm_deadline()


def _branch_switch_lines(payload: dict) -> tuple[list[str], list[str]]:
    """One user line per branch switch S5 found (once per session per switch), plus model notes."""
    lines: list[str] = []
    notes: list[str] = []
    switches = list(_BRANCH_SWITCHES)
    _BRANCH_SWITCHES.clear()
    if not switches:
        return lines, notes
    notice = _user_notice()
    if notice is None:
        return lines, notes
    for checkout, old, new in switches:
        got = notice.claim_local(
            "branch_switched",
            {"repo": os.path.basename(checkout.rstrip("/\\")) or checkout, "ob": old, "nb": new},
            host="cc", session_id=str(payload.get("session_id") or ""),
            cwd=str(payload.get("cwd") or os.getcwd()), event="PreToolUse",
            key=f"branch_switched:{notice.sha8(checkout)}:{old}:{new}", immediate=True,
        )
        if got:
            lines.append(got[0])
            notes.append(got[1])
    return lines, notes


def _main() -> None:
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
    if not isinstance(payload, dict):
        return

    # CC hook payload doesn't include event type name; inject via CLI arg
    if len(sys.argv) > 1 and "hook_event_name" not in payload:
        payload["hook_event_name"] = sys.argv[1]
    # Everything below is about the call that is about to run. PostToolUse
    # (still registered) stops here: no state read, no HTTP, no output.
    if payload.get("hook_event_name") != "PreToolUse":
        return

    _BRANCH_SWITCHES.clear()
    state = _load_supervisor_state()
    base = copy.deepcopy(state)
    for key in _RETIRED_STATE_KEYS:
        state.pop(key, None)

    # 1. Local guards first (S1 warnings, S3-S6): no HTTP may run ahead of a blocking
    #    verdict. Their advisories are handed to _check_workflow_reminders below
    #    so they keep their place in the output.
    guard_warnings = _check_local_guards(payload, state)

    # 2. Only the task-wall check on a named Agent dispatch needs the project
    #    (resolved once, cached per cwd); every other call makes no HTTP at all.
    #    `http` memoises the resolve and every advisory GET for this invocation.
    http: dict = {}
    project_id = None
    if _wants_task_wall_check(payload):
        project_id = _resolve_project_id(state, os.getcwd(), http=http)

    warnings = _check_workflow_reminders(
        payload, state, project_id=project_id, http=http, guard_warnings=guard_warnings
    )

    if state != base:
        _save_supervisor_state(state, base)

    user_lines, notes = _branch_switch_lines(payload)

    # Nothing to say -> print nothing. An empty hookSpecificOutput object on
    # every call only added a blank attachment to the transcript.
    if not warnings and not user_lines:
        return

    notice = _user_notice()
    if notice is not None:
        # Never fills permissionDecision either (see below): emit only carries
        # the user line and the model-only reminders.
        notice.emit("cc", "PreToolUse", user_text="\n".join(user_lines),
                    model_text="\n".join(warnings + notes))
        return

    # 绝不填 permissionDecision（2026-07-27 用户裁定）：该字段是**可选**的表态位
    # （allow 直接放行 / deny 拒绝 / ask 询问 / 不给=不表态走 CC 默认流程），旧实现
    # 把它当成"PreToolUse 必须带的输出格式"每次填 allow，其优先级高于用户选的权限
    # 模式——default/plan/acceptEdits 一律被覆盖，Agent|Bash|Edit|Write|Workflow 五类
    # 工具的权限询问全被静音（30 天 43,605 次调用无一询问）。CC 自己有完整的权限模式
    # 供用户选择，OS 不替它做决定：本 hook 只注入提醒文本，权限交回 CC。
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "\n".join(warnings),
        }
    }
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
    main(started_at=_HOOK_T0)
