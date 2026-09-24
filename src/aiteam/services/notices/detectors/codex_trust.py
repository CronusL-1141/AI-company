"""E16: compare the user's saved trust with normalized Codex declarations.

Algorithm evidence: local official openai/codex checkout at
0a2eb4696c26ac33204bcd255721ab30220a4774:
* hooks/src/engine/discovery.rs:505-566, 737-819 normalizes command handlers,
  hashes one matcher group, and distinguishes trusted/modified/untrusted.
* config/src/hook_config.rs:154-190 defines the serialized fields; conversion
  through TOML omits absent Options (no JSON nulls).
* config/src/fingerprint.rs:54-86 hashes recursively sorted compact UTF-8 JSON.
* hooks/src/lib.rs:92-124 defines event labels and positional state keys.

These paths are relative to codex-rs/. This is deliberately NOT the project's
hook-trust.lock hash. Unsupported declarations use only the documented weak
signal, and can never clear or downgrade a previously confirmed trust issue.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import time
import tomllib
from datetime import timedelta
from pathlib import Path

from sqlalchemy import exists, or_, select

from aiteam.services.notices.detectors import DetectContext, Finding, NoDataError, Scope
from aiteam.services.notices.detectors.codex_copies import (
    OBSERVER_DIRNAME,
    codex_home,
    file_signature,
    read_bounded,
)
from aiteam.storage.connection import get_session
from aiteam.storage.models import AgentModel, EventModel

_CACHE_SECONDS = 600
_MAX_CONFIG_BYTES = 1024 * 1024
_EVENTS = {
    "PreToolUse": "pre_tool_use", "PermissionRequest": "permission_request",
    "PostToolUse": "post_tool_use", "PreCompact": "pre_compact", "PostCompact": "post_compact",
    "SessionStart": "session_start", "SessionEnd": "session_end",
    "UserPromptSubmit": "user_prompt_submit", "SubagentStart": "subagent_start",
    "SubagentStop": "subagent_stop", "Stop": "stop", "Interrupt": "interrupt",
}
_NO_MATCHER = {"user_prompt_submit", "stop", "interrupt"}
_CONTEXT = {"pre_tool_use", "post_tool_use", "session_start", "user_prompt_submit", "subagent_start"}
_COMMAND_FIELDS = {"type", "command", "commandWindows", "command_windows", "timeout", "async",
                   "statusMessage", "additionalContextLimit"}


class UnsupportedDeclarationError(ValueError):
    """A declaration whose current host semantics we cannot establish."""


def trusted_hash(event: str, group: dict, handler: dict, *, windows: bool = False) -> str:
    """Codex's source-proven fingerprint for supported command declarations."""
    event = _EVENTS.get(event, event)
    if event not in _EVENTS.values() or handler.get("type") != "command":
        raise UnsupportedDeclarationError
    if set(group) - {"matcher", "hooks"} or set(handler) - _COMMAND_FIELDS:
        raise UnsupportedDeclarationError
    matcher = group.get("matcher")
    if matcher is not None and not isinstance(matcher, str):
        raise UnsupportedDeclarationError
    if event in _NO_MATCHER:
        matcher = None
    if matcher not in (None, "", "*"):
        # Codex uses Rust regex. Decline constructs whose semantics differ
        # instead of labeling a declaration the host may reject as untrusted.
        if "(?" in matcher or re.search(r"\\[1-9]", matcher):
            raise UnsupportedDeclarationError
        try:
            re.compile(matcher)
        except re.error as exc:
            raise UnsupportedDeclarationError from exc
    command = handler.get("command")
    other = handler.get("commandWindows", handler.get("command_windows"))
    if "commandWindows" in handler and "command_windows" in handler:
        raise UnsupportedDeclarationError
    if not isinstance(command, str) or (other is not None and not isinstance(other, str)):
        raise UnsupportedDeclarationError
    if windows and other is not None:
        command = other
    if not command.strip():
        raise UnsupportedDeclarationError
    timeout = handler.get("timeout")
    if timeout is not None and (type(timeout) is not int or not 0 <= timeout <= 2**63 - 1):
        raise UnsupportedDeclarationError
    if event in {"session_end", "interrupt"}:
        timeout = min(3, max(1, 1 if timeout is None else timeout))
    else:
        timeout = max(1, 600 if timeout is None else timeout)
    asynchronous = handler.get("async", False)
    status = handler.get("statusMessage")
    limit = handler.get("additionalContextLimit")
    if (type(asynchronous) is not bool or (status is not None and not isinstance(status, str))
            or (limit is not None and (type(limit) is not int or not 0 <= limit <= 2**63 - 1))):
        raise UnsupportedDeclarationError
    normalized = {"type": "command", "command": command, "timeout": timeout, "async": asynchronous}
    if status is not None:
        normalized["statusMessage"] = status
    if limit is not None and event in _CONTEXT and limit != 2500:
        normalized["additionalContextLimit"] = limit
    identity = {"event_name": event, "hooks": [normalized]}
    if matcher is not None:
        identity["matcher"] = matcher
    serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(serialized.encode()).hexdigest()


def _owned(command: object, installed: Path) -> bool:
    if not isinstance(command, str):
        return False
    try:
        lexer = shlex.shlex(command, posix=os.name != "nt", punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        parts = list(lexer)
    except ValueError as exc:
        raise UnsupportedDeclarationError from exc
    if not parts:
        return False

    def installed_script(part: str) -> bool:
        candidate = Path(part)
        if not candidate.is_absolute() or candidate.suffix not in {".py", ".sh"}:
            return False
        try:
            # Installer commands may retain the original CODEX_HOME spelling,
            # while native state keys use its canonical path. Resolve only the
            # parent: a missing script still has a meaningful declaration.
            return candidate.parent.resolve() == installed.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise UnsupportedDeclarationError from exc

    def references_install(text: str) -> bool:
        configured = os.environ.get("CODEX_HOME", "")
        aliases = [str(installed)]
        if configured:
            aliases.append(str(Path(configured) / "hooks" / OBSERVER_DIRNAME))
        return any(alias in text for alias in aliases)

    # The installer writes '<python> <absolute script> args'. A path merely
    # echoed or passed as another program's input is not an owned hook.
    if any(token and all(char in ";&|<>()" for char in token) for token in parts) and references_install(command):
        raise UnsupportedDeclarationError
    if installed_script(parts[0]):
        return True
    if re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(parts[0]).name):
        if len(parts) > 1 and not parts[1].startswith("-"):
            if installed_script(parts[1]):
                return True
            if references_install(parts[1]):
                raise UnsupportedDeclarationError
            return False
    elif Path(parts[0]).name in {"echo", "printf"}:
        return False
    # Expansion, flags and shell/env wrappers are unknown, not unrelated.
    if references_install(command):
        raise UnsupportedDeclarationError
    return False


def _trust_state(home: Path, config: dict, manifest: dict) -> str:
    """Return trusted/untrusted/unverified, or no-data for no applicable hooks."""
    hooks = manifest.get("hooks")
    settings = config.get("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(settings, dict):
        raise NoDataError
    states = settings.get("state", {})
    if not isinstance(states, dict):
        raise NoDataError
    features = config.get("features", {})
    if not isinstance(features, dict):
        raise NoDataError
    # Native features applies aliases first and canonical entries afterwards.
    enabled = features.get("hooks", features.get("codex_hooks"))
    if enabled is not None and type(enabled) is not bool:
        raise NoDataError
    if enabled is False:
        raise NoDataError  # deliberate feature disable is not missing trust
    installed = home / "hooks" / OBSERVER_DIRNAME
    count = 0
    unknown = False
    untrusted = False
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise NoDataError
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise NoDataError
            for handler_index, handler in enumerate(group["hooks"]):
                if not isinstance(handler, dict):
                    raise NoDataError
                try:
                    command = (handler.get("commandWindows", handler.get("command_windows", handler.get("command")))
                               if os.name == "nt" else handler.get("command"))
                    if not _owned(command, installed):
                        continue
                    count += 1
                    event_key = _EVENTS.get(event, event)
                    key = f"{home / 'hooks.json'}:{event_key}:{group_index}:{handler_index}"
                    state = states.get(key, {})
                    if not isinstance(state, dict) or ("enabled" in state and type(state["enabled"]) is not bool):
                        raise UnsupportedDeclarationError
                    if state.get("enabled") is False:
                        # /hooks disabling is an explicit host choice, not a
                        # reason to tell the user to grant trust again.
                        continue
                    current = trusted_hash(event, group, handler, windows=os.name == "nt")
                    saved = state.get("trusted_hash")
                    if saved is not None and not isinstance(saved, str):
                        raise UnsupportedDeclarationError
                    if saved != current:
                        untrusted = True
                except UnsupportedDeclarationError:
                    unknown = True
    if untrusted:
        return "untrusted"
    if unknown:
        return "unverified"
    if not count:
        raise NoDataError
    return "trusted"


class CodexTrustDetector:
    name = "codex_trust"
    catalog_ids = ("codex_untrusted",)
    timing = frozenset({"session_start", "demand"})
    hosts = frozenset({"cc"})
    timeout_s = 0.3

    def __init__(self) -> None:
        self._cache: tuple[object, float, tuple[str, str]] | None = None

    def applies(self, ctx: DetectContext) -> bool:
        return True

    def scope(self, ctx: DetectContext) -> Scope:
        return Scope(prefixes=("codex_untrusted:",))

    def _inspect(self) -> tuple[str, str]:
        home = codex_home()
        paths = (home / "config.toml", home / "hooks.json")
        signatures = tuple(file_signature(path) for path in paths)
        identity = str(home), signatures
        now = time.monotonic()
        if self._cache is not None and self._cache[0] == identity and now - self._cache[1] < _CACHE_SECONDS:
            return self._cache[2]
        try:
            config = tomllib.loads(read_bounded(paths[0], _MAX_CONFIG_BYTES).decode("utf-8"))
            raw = read_bounded(paths[1], _MAX_CONFIG_BYTES)
            manifest = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise NoDataError from exc
        if not isinstance(manifest, dict):
            raise NoDataError
        state = _trust_state(home, config, manifest)
        if signatures != tuple(file_signature(path) for path in paths):
            raise NoDataError
        digest = hashlib.sha256(str(paths[1]).encode() + b"\0" + raw).hexdigest()[:8]
        result = state, f"codex_untrusted:{digest}"
        self._cache = identity, now, result
        return result

    async def detect(self, ctx: DetectContext) -> list[Finding]:
        state, key = await asyncio.to_thread(self._inspect)
        if state == "trusted":
            return []
        if state == "unverified":
            existing, _ = await ctx.repo.list_notices(
                statuses=("active", "snoozed"), catalog_ids=self.catalog_ids,
            )
            if any(not row.variant for row in existing):
                raise NoDataError  # weak evidence cannot downgrade a confirmed issue
            # Direct SQL keeps this bounded even when other hosts dominate the
            # event stream; taking the latest N events can give a false absence.
            async with get_session(ctx.repo._db_url) as session:
                recent = await session.scalar(select(EventModel.id).where(
                    EventModel.timestamp >= ctx.now - timedelta(days=7),
                    EventModel.timestamp <= ctx.now,
                    or_(
                        EventModel.data["harness"].as_string() == "codex",
                        exists(select(AgentModel.id).where(
                            AgentModel.harness == "codex",
                            AgentModel.session_id == EventModel.data["session_id"].as_string(),
                            EventModel.source == "session:" + AgentModel.session_id,
                        )),
                    ),
                    EventModel.type.startswith("cc.", autoescape=True),
                ).limit(1))
            if recent is not None:
                raise NoDataError  # event arrival does not prove all hooks are trusted
        return [Finding(catalog_id="codex_untrusted", key=key,
                        variant="unverified" if state == "unverified" else "")]
