#!/usr/bin/env python3
"""Codex harness hook registration surface - the single source of truth.

Pure data plus deterministic renderers. This module imports nothing from this
repository and touches no filesystem path: I20 keeps the adapter one-way. The
CC installer never walks this directory (its source constants are frozen to
hooks / agents / skills / commands / loop.md), and nothing here may reach back
into the CC installer.

Why a second surface exists at all. CC's own table lives in the root installer
and is pinned to the CC manifest by I8. Codex speaks a different event set, a
different tool-name face and a different trust model, so it gets its own table.
Merging the two would make every Codex edit touch a CC distribution file, which
is exactly what the "one core, two adapters" split forbids.

Read README.md in this directory before editing. The group order below is a
trust anchor on the user's machine, not a formatting choice: the host keys each
trust record on (manifest path, snake_case event, group index, handler index), so
inserting a group in the middle silently un-trusts every handler after it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

# ---------------------------------------------------------------------------
# Version constants (I-CDX-R8(c): every version assertion in the adapter and in
# its machine checks reads these two names - no scattered literals anywhere).
#
# The upper bound is a rolling value, refreshed by measurement, not a ceiling we
# believe in. It was measured on the default desktop entry point; the standalone
# binary on PATH reported a different one on the same machine the same day, so
# the multi-core spread is a snapshot and must never be written down as a
# constant. Never derive either value from the host CLI's version subcommand
# (that reports the standalone binary on PATH, not the carrying core) or from a
# model's self-report; the only trusted sources are session_meta.cli_version and
# state_5.threads.cli_version. I-CDX-R8(a) turns that ban into a static scan.
# ---------------------------------------------------------------------------
CODEX_MIN_VERSION: Final[str] = "0.145.0"
CODEX_KNOWN_UPPER_VERSION: Final[str] = "0.153.0-alpha.5"

# D8 degrade code: harness_version came from anywhere other than the two trusted
# sources above. Named here so the adapter, the machine checks and the fixture
# cases all quote one string instead of three copies of a spelling.
DEGRADED_VERSION_SOURCE_UNTRUSTED: Final[str] = "version_source_untrusted"

# ---------------------------------------------------------------------------
# Placeholders. The in-repo manifest is a reference rendering, never a file that
# can be dropped onto a machine: the host needs an absolute interpreter path and
# an absolute script path, and neither is knowable at commit time. The installer
# substitutes both at write time. I15 asserts the pair is present in every
# command and that no home-directory literal ever reaches this file.
# ---------------------------------------------------------------------------
PLACEHOLDER_PY: Final[str] = "{{PY}}"
PLACEHOLDER_HOOKS_DIR: Final[str] = "{{CODEX_HOOKS_DIR}}"

# ---------------------------------------------------------------------------
# Handler kind. observe = records only, never changes the outcome (no permission
# decision, no rewritten input, no non-zero blocking exit). takeover = may deny
# or rewrite. The distinction is a release-review checkpoint, so it is declared
# here rather than inferred from a script that does not exist yet.
# ---------------------------------------------------------------------------
KIND_OBSERVE: Final[str] = "observe"
KIND_TAKEOVER: Final[str] = "takeover"
HANDLER_KINDS: Final[frozenset[str]] = frozenset({KIND_OBSERVE, KIND_TAKEOVER})

# ---------------------------------------------------------------------------
# Events.
#
# CODEX_SUPPORTED_EVENTS is the set this harness is known to deliver. The six we
# actually register were all observed firing on a real machine; the rest are
# documented as deliverable but carry no handler yet.
#
# CC_ONLY_EVENTS never appear in a Codex manifest. The host ignores unknown
# event names silently, so registering one produces a handler that can never
# fire and can never be noticed - I15 turns that into a red check instead.
# ---------------------------------------------------------------------------
CODEX_SUPPORTED_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "SessionEnd",
        "PreCompact",
        "PostCompact",
    }
)

CC_ONLY_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "TaskCreated",
        "TaskCompleted",
        "TeammateIdle",
        "PermissionDenied",
        "WorktreeCreate",
        "WorktreeRemove",
    }
)

# The host truncates a session-end handler hard; anything registered there has
# to finish inside this budget or its output is dropped without a marker.
SESSION_END_MAX_TIMEOUT_SEC: Final[int] = 3

# ---------------------------------------------------------------------------
# Dispatch matcher.
#
# The tool name has three mutually unequal faces (see TOOL_NAME_REGISTRY). The
# matcher may only ever carry the hook-payload face. A dotted model-face name
# written here would be read as a regular expression (the host compares matchers
# literally only while they consist of word characters and pipes), and the dot
# would then demand a character that does not exist between the two segments of
# the payload-face name - the matcher would match nothing at all, silently.
#
# `collaboration.*` covers the concatenated payload-face family; the trailing
# `spawn_agent` alternative covers the older bare payload-face name still seen
# on the oldest supported core. Aliases may only be appended, never reordered.
# ---------------------------------------------------------------------------
CODEX_DISPATCH_MATCHER: Final[str] = "collaboration.*|spawn_agent"

# Matcher tokens that are patterns rather than literal tool names. Registered
# explicitly so I15 can tell "a known family pattern" from "somebody pasted a
# regex into a matcher".
MATCHER_PATTERN_TOKENS: Final[frozenset[str]] = frozenset({"collaboration.*"})

# Wildcard matchers: every tool, or a group that carries no matcher key at all.
MATCHER_WILDCARDS: Final[frozenset[str]] = frozenset({"*", ""})

# ---------------------------------------------------------------------------
# Tool-name registry (design section 6.3, keyed on the hook payload face).
#
# The three faces do not derive from one another: one of them is a prefix strip
# while another is an outright rename, and both were observed in the same
# session. Any implementation that normalises with a single regular expression
# is therefore wrong by construction, which is why this is a table.
#
# model_face is quoted for reading model-side narration and for triage only. It
# must never become a matcher or a registry key. Its evidence grade is weaker
# than the other two columns (self-reported, never reconciled against the tool
# schema source), so it is recorded with that caveat rather than trusted.
#
# rollout_face is None where this batch captured no sample. None means "not
# sampled", never "does not exist".
# ---------------------------------------------------------------------------
TOOL_NAME_REGISTRY: Final[dict[str, dict[str, Any]]] = {
    # Rename, not a prefix strip. This single row is the reason the whole
    # normalisation step has to be a table lookup.
    "Bash": {
        "model_face": "functions.exec",
        "rollout_face": None,
        "disposition": "Bash",
        "note": "rename; the same family also exposes exec_command / write_stdin on the model face",
    },
    "apply_patch": {
        "model_face": "tools.apply_patch",
        "rollout_face": None,
        "disposition": "apply_patch",
        "note": "prefix strip; joins the file-edit and intent tool sets",
    },
    "collaborationspawn_agent": {
        "model_face": "collaboration.spawn_agent",
        "rollout_face": "spawn_agent",
        "disposition": "Agent",
        "note": "dispatch edge; matcher must use the payload face only",
    },
    # Historical payload face on the oldest supported core, kept because the
    # dispatch matcher still carries it as a trailing alternative.
    "spawn_agent": {
        "model_face": "collaboration.spawn_agent",
        "rollout_face": "spawn_agent",
        "disposition": "Agent",
        "note": "bare payload face on the oldest supported core",
    },
    "collaborationwait_agent": {
        "model_face": "collaboration.wait_agent",
        "rollout_face": "wait_agent",
        "disposition": "keep span",
        "note": "same family as spawn; only the payload face was measured",
    },
    "collaborationsend_message": {
        "model_face": "collaboration.send_message",
        "rollout_face": "send_message",
        "disposition": "keep minimal activity row",
        "note": "body is encrypted and dropped; the row keeps the agent id. Not inert",
    },
    "collaborationfollowup_task": {
        "model_face": "collaboration.followup_task",
        "rollout_face": "followup_task",
        "disposition": "keep minimal activity row",
        "note": "same handling as send_message",
    },
    "collaborationlist_agents": {
        "model_face": "collaboration.list_agents",
        "rollout_face": "list_agents",
        "disposition": "keep minimal activity row",
        "note": "same handling as send_message",
    },
    "collaborationinterrupt_agent": {
        "model_face": "collaboration.interrupt_agent",
        "rollout_face": "interrupt_agent",
        "disposition": "keep minimal activity row",
        "note": "same handling as send_message",
    },
    "send_input": {
        "model_face": None,
        "rollout_face": "send_input",
        "disposition": "keep minimal activity row",
        "note": "first-generation name of the send_message family",
    },
    "close_agent": {
        "model_face": None,
        "rollout_face": "close_agent",
        "disposition": "keep minimal activity row",
        "note": "first-generation collaboration name",
    },
    "resume_agent": {
        "model_face": None,
        "rollout_face": "resume_agent",
        "disposition": "keep minimal activity row",
        "note": "first-generation collaboration name",
    },
    "update_plan": {
        "model_face": "tools.update_plan",
        "rollout_face": None,
        "disposition": "inert",
        "note": "host bookkeeping; no observational value",
    },
    "view_image": {
        "model_face": "tools.view_image",
        "rollout_face": None,
        "disposition": "keep minimal activity row",
        "note": "",
    },
    "webrun": {
        "model_face": None,
        "rollout_face": None,
        "disposition": "keep minimal activity row",
        "note": "",
    },
    "image_genimagegen": {
        "model_face": None,
        "rollout_face": None,
        "disposition": "keep minimal activity row",
        "note": "concatenated payload face carried over from the earlier design round",
    },
    "list_mcp_resources": {
        "model_face": "tools.list_mcp_resources",
        "rollout_face": None,
        "disposition": "keep minimal activity row",
        "note": "the resource-template and read variants share this handling",
    },
}

# Prefix rules. A tool name that starts with one of these is registered by rule
# rather than by row - the server side owns the tail and adding a tool there
# must not require a commit here.
#
# The MCP prefix is the underscore form on the hook payload face. The host's own
# configuration key and its settings UI show the hyphen form for the same
# server. Both spellings are correct in their own layer and neither layer may
# "fix" the other; only the tool-name face belongs in this table.
TOOL_NAME_PREFIX_REGISTRY: Final[dict[str, dict[str, str]]] = {
    "mcp__ai_team_os__": {
        "disposition": "normalise to the OS canonical form",
        "note": "payload face is underscored; the config key for the same server is hyphenated",
    },
    "mcp__codex_app__": {
        "disposition": "inert (host application tools)",
        "note": "payload face inferred from the measured server name plus the measured prefix shape",
    },
}

# ---------------------------------------------------------------------------
# Injection handlers. The host truncates injected context silently - no marker,
# no warning, and a spot check on the injected text cannot detect it. A limit of
# 0 disables host-side truncation entirely (measured: a 14,331 character payload
# arrived whole, 400 of 400 lines, with no truncation header), which is why the
# two injection entries below carry 0 rather than a positive budget. The scripts
# stay responsible for their own size budget; that budget is auditable, the
# host's silent cut is not.
# ---------------------------------------------------------------------------
CODEX_INJECTION_SCRIPTS: Final[frozenset[str]] = frozenset(
    {
        "session_bootstrap_codex.py",
        "inject_subagent_context_codex.py",
    }
)

# ---------------------------------------------------------------------------
# The surface.
#
# Entry shape: (event, matcher, kind, [(script, arg, timeout_sec, limit), ...])
# where limit is the additional-context limit and is None for non-injection
# handlers.
#
# Provenance of the twelve handlers: the on-disk sample captured from a real
# installation carries fourteen OS-owned handlers across these same six events;
# the two belonging to a retired mail reminder are dropped, leaving twelve. The
# entry-script names are the harness's own - reusing the CC file names would put
# three same-named scripts in the tree and would blur the one-way boundary that
# I20 checks. The scripts themselves are not written in this batch: this table
# registers the names, the events, the order and the budgets, and nothing else.
#
# Matchers are translated to the Codex payload face rather than copied: the CC
# table scopes its reminder groups with a tool list that does not exist on this
# harness (that alias has matched nothing since the version before the current
# upper bound), and the CC manifest scopes two writeback hooks to MCP tool names
# in the hyphenated spelling. Both become the payload-face spellings here.
#
# Timeouts are carried over from the CC table unchanged. The installed sample on
# disk used a shorter budget for the two tool events; the session-start budget in
# particular is a live suspect in an injection that produces nothing, so this
# table keeps the larger headroom rather than the smaller.
#
# Group order within an event: the stable telemetry group first, the volatile
# tool-scoped groups last, so that a future matcher change appends or truncates
# at the tail instead of renumbering trust keys in the middle.
# ---------------------------------------------------------------------------
CODEX_HOOK_SURFACE: Final[list[tuple[str, str, str, list[tuple[str, str, int, int | None]]]]] = [
    # Full telemetry leg. Its own inert-tool guard caps the cost.
    ("PreToolUse", "*", KIND_OBSERVE, [
        ("send_event_codex.py", "PreToolUse", 5, None),
    ]),
    # Dispatch gate. The CC twin hard-blocks on some branches, so the contract
    # is declared takeover even though every deny branch on this harness is off
    # by default and the entry script lands in a later phase. Declaring it
    # observe and then shipping a script that denies is the failure this field
    # exists to prevent.
    ("PreToolUse", CODEX_DISPATCH_MATCHER, KIND_TAKEOVER, [
        ("workflow_reminder_codex.py", "PreToolUse", 5, None),
    ]),
    ("PostToolUse", "*", KIND_OBSERVE, [
        ("send_event_codex.py", "PostToolUse", 5, None),
    ]),
    ("PostToolUse", "mcp__ai_team_os__report_save", KIND_OBSERVE, [
        ("deep_review_link_codex.py", "", 5, None),
    ]),
    ("PostToolUse", "mcp__ai_team_os__meeting_conclude", KIND_OBSERVE, [
        ("meeting_ecosystem_writeback_codex.py", "", 5, None),
    ]),
    # Nothing can be denied after the fact, so the post leg is observe.
    ("PostToolUse", CODEX_DISPATCH_MATCHER, KIND_OBSERVE, [
        ("workflow_reminder_codex.py", "PostToolUse", 5, None),
    ]),
    ("SessionStart", "", KIND_OBSERVE, [
        ("session_bootstrap_codex.py", "", 15, 0),
        ("send_event_codex.py", "SessionStart", 5, None),
    ]),
    ("SubagentStart", "", KIND_OBSERVE, [
        ("inject_subagent_context_codex.py", "", 10, 0),
        ("send_event_codex.py", "SubagentStart", 5, None),
    ]),
    ("SubagentStop", "", KIND_OBSERVE, [
        ("send_event_codex.py", "SubagentStop", 5, None),
    ]),
    ("Stop", "", KIND_OBSERVE, [
        ("send_event_codex.py", "Stop", 5, None),
    ]),
]

# Every entry script this surface registers, derived from the table so a handler
# can never be registered without its name being visible to I20's same-name ban.
CODEX_HOOK_SCRIPTS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(script for _e, _m, _k, entries in CODEX_HOOK_SURFACE for script, _a, _t, _l in entries)
)


def snake_case_event(event: str) -> str:
    """CamelCase event name -> the snake_case spelling used in trust keys.

    The manifest spells events in CamelCase while the host's trust records spell
    the same event in snake_case. The two spellings must never be mixed: a lock
    computed over the manifest spelling would not line up with anything the host
    ever wrote.
    """
    out: list[str] = []
    for index, char in enumerate(event):
        if char.isupper() and index > 0:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def _command(script: str, arg: str, *, windows: bool) -> str:
    """Render one command line in placeholder form.

    Unix keeps the shape captured from a real installation: a double-quoted
    interpreter path and a single-quoted script path. Windows uses double quotes
    for both, since single quotes are not a quoting character there. The Windows
    column is generated but not verified on a Windows machine in this round.
    """
    quote = '"' if windows else "'"
    command = f'"{PLACEHOLDER_PY}" {quote}{PLACEHOLDER_HOOKS_DIR}/{script}{quote}'
    return f"{command} {arg}" if arg else command


def render_manifest() -> dict[str, Any]:
    """Build the manifest structure from the surface table.

    Top level carries the hooks key and nothing else: no description (the oldest
    supported core rejects it) and no version (adding a versioned manifest file
    would widen the release version-lockstep surface, which this phase does not
    do).
    """
    hooks: dict[str, list[dict[str, Any]]] = {}
    for event, matcher, _kind, entries in CODEX_HOOK_SURFACE:
        group: dict[str, Any] = {}
        if matcher:
            group["matcher"] = matcher
        handlers: list[dict[str, Any]] = []
        for script, arg, timeout, limit in entries:
            handler: dict[str, Any] = {
                "type": "command",
                "command": _command(script, arg, windows=False),
                "command_windows": _command(script, arg, windows=True),
                "timeout": timeout,
            }
            if limit is not None:
                handler["additionalContextLimit"] = limit
            handlers.append(handler)
        group["hooks"] = handlers
        hooks.setdefault(event, []).append(group)
    return {"hooks": hooks}


def render() -> str:
    """The exact text of the in-repo manifest. I15 pins the file to this."""
    return json.dumps(render_manifest(), indent=2, ensure_ascii=False) + "\n"


def trust_lock_entries(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Declaration-level trust digests, computed from a rendered manifest.

    What is hashed is the registration declaration and only that: the five-tuple
    (snake_case event, group index, handler index, command, timeout). Script
    contents are deliberately excluded - replacing a script body without
    touching the manifest was measured to keep every handler trusted, so a lock
    that hashed script bytes would cry wolf on every ordinary code change and
    stay silent on the one change that actually costs the user a re-trust.

    Both platform columns are computed, because the installer picks the column
    at write time and a Windows-only registration change must not slip through
    a Unix-only lock.
    """
    entries: list[dict[str, str]] = []
    for event, groups in manifest.get("hooks", {}).items():
        event_key = snake_case_event(event)
        for group_index, group in enumerate(groups):
            for handler_index, handler in enumerate(group.get("hooks", [])):
                key = f"{event_key}:{group_index}:{handler_index}"
                row = {"key": key}
                for column, field in (("command", "command"), ("command_windows", "command_windows")):
                    payload = json.dumps(
                        [event_key, group_index, handler_index, handler.get(field), handler.get("timeout")],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                    row[f"{column}_sha256"] = f"sha256:{digest}"
                entries.append(row)
    return entries


def render_trust_lock(manifest: dict[str, Any]) -> str:
    """The exact text of the in-repo lock file. I17 pins the file to this."""
    document = {
        "schema": "codex-hook-trust-lock/1",
        "hashed": "snake_case event, group index, handler index, command, timeout - placeholder form",
        "excludes": "hook script contents (replacing a script body costs no trust)",
        "entries": trust_lock_entries(manifest),
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
