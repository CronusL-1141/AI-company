"""Conservative static launch classification. Never execute or retain shell input."""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

_TIERS = {"standard", "default", "fast", "priority", "flex", "batch"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(.*)\Z", re.S)
_OPAQUE_PROMPT = "__AITEAM_OPAQUE_PROMPT__"
_CAT_PROMPT = re.compile(r'"\$\(\s*cat\s+([^"\n$`();|&<>]+?)\s*\)"')
_EXIT_ECHO = re.compile(r';\s*echo\s+"exit \$\?"\s*$')
_VALUE_OPTIONS = {
    "-s", "--sandbox", "-p", "--profile", "-C", "--cd", "-o", "--output-last-message",
    "--output-schema", "--color", "--local-provider", "--thread-source", "--add-dir",
    "--cyber-access-program", "--enable", "--disable",
}
_FLAG_OPTIONS = {
    "--json", "--ephemeral", "--strict-config", "--oss", "--approve-for-me",
    "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
    "--skip-git-repo-check", "--worktree", "--ignore-user-config", "--ignore-rules",
    "--last", "--all", "--uncommitted",
}


def _config_value(raw: str) -> object:
    try:
        return tomllib.loads("value=" + raw)["value"]
    except (tomllib.TOMLDecodeError, KeyError):
        return raw


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def classify_exec_arguments(arguments: list[str]) -> dict:
    """Extract only literal requested settings, with the child's -- boundary."""
    settings: dict[str, object] = {}
    model_option = None
    mode, positional = "new", False
    json_output = ephemeral = False
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--":
            break
        key, equals, attached = value.partition("=")
        option, operand = key, attached if equals else None
        if option.startswith("-c") and option != "-c" and not option.startswith("--"):
            option, operand = "-c", value[2:].lstrip("=")
        if option in {"-c", "--config", "-m", "--model", *_VALUE_OPTIONS}:
            if operand is None:
                index += 1
                operand = arguments[index] if index < len(arguments) else None
            if operand is None:
                mode = "unknown"
                break
            if operand == _OPAQUE_PROMPT:
                mode = "unknown"  # Only a standalone prompt may be opaque.
            if option in {"-c", "--config"}:
                config_key, separator, raw = operand.partition("=")
                if separator and config_key.strip() in {"service_tier", "model", "model_reasoning_effort"}:
                    settings[config_key.strip()] = _config_value(raw.strip())
            elif option in {"-m", "--model"}:
                model_option = _identifier(operand)
        elif value in _FLAG_OPTIONS:
            json_output |= value == "--json"
            ephemeral |= value == "--ephemeral"
        elif value.startswith("-"):
            # Unknown/variadic flags might consume a token that looks like a verb.
            mode = "unknown"
        elif not positional:
            if value in {"resume", "fork", "review"}:
                mode = value if mode != "unknown" else mode
            positional = True
        index += 1
    tier = settings.get("service_tier")
    configured_model = _identifier(settings.get("model"))
    model = model_option or configured_model
    if model_option and configured_model and model_option != configured_model:
        model = None  # Do not guess precedence between two different model requests.
    return {
        "requested_service_tier": tier if isinstance(tier, str) and tier in _TIERS else None,
        "requested_model": model,
        "requested_reasoning_effort": _identifier(settings.get("model_reasoning_effort")),
        "exec_mode": mode,
        "json_output": json_output,
        "ephemeral": ephemeral,
    }


def wrapper_launch_metadata(arguments: list[str]) -> dict:
    result = classify_exec_arguments(arguments)
    result.pop("json_output")
    result.pop("ephemeral")
    return {**result, "requested_metadata_source": "wrapper_argv"}


@dataclass(frozen=True)
class BashLaunch:
    metadata: dict
    binding_reason: str
    codex_home: Path | None
    output_path: Path | None = None
    background: bool = False


def _output_channel(words: list[str]) -> Path | None:
    """Only an overwrite output file plus /dev/null stdin and optional 2>&1."""
    output = None
    index = 0
    while index < len(words):
        if words[index:index + 3] == ["2", ">&", "1"]:
            index += 3
        elif words[index:index + 2] == ["<", "/dev/null"]:
            index += 2
        elif words[index] == ">" and index + 1 < len(words) and output is None:
            raw = words[index + 1]
            if not Path(raw).is_absolute() or len(raw) > 2048 or any(char in raw for char in "$`\x00*?["):
                return None
            output = Path(raw)
            index += 2
        else:
            return None
    return output


def _dynamic_shell(command: str) -> bool:
    quote = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
        elif char == quote:
            quote = None
        elif quote is None and char in "'\"":
            quote = char
        elif quote != "'" and char in "$`":
            return True
    return False


def classify_bash_launches(command: object) -> list[BashLaunch]:
    """Classify direct simple command nodes; scripts and heredocs remain opaque."""
    if (not isinstance(command, str) or "codex" not in command or len(command) > 131072
            or "...(truncated)" in command):
        return []
    command = _EXIT_ECHO.sub("", command)

    def opaque_prompt(match):
        try:
            operands = shlex.split(match[1])
        except ValueError:
            return match[0]
        if len(operands) == 1 and Path(operands[0]).is_absolute() and len(operands[0]) <= 2048:
            return '"' + _OPAQUE_PROMPT + '"'
        return match[0]

    command = _CAT_PROMPT.sub(opaque_prompt, command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()\n")
        lexer.whitespace = " \t\r"
        words = list(lexer)
    except ValueError:
        return []
    if any(word.startswith("<<") for word in words):
        return []  # Never interpret heredoc data as executable commands.
    separators = {";", "&&", "||", "&", "|", "\n"}
    segments: list[list[str]] = [[]]
    for word in words:
        if word in separators:
            segments.append([])
        else:
            segments[-1].append(word)
    segments = [segment for segment in segments if segment]
    launches = []
    for segment in segments:
        index, home = 0, None
        if segment and segment[0] == "env":
            index = 1
        while index < len(segment) and _ASSIGNMENT.fullmatch(segment[index]):
            name, raw = segment[index].split("=", 1)
            if name == "CODEX_HOME" and Path(raw).is_absolute() and not any(c in raw for c in "$`~"):
                home = Path(raw)
            index += 1
        if index >= len(segment) or Path(segment[index]).name != "codex":
            continue
        tail = segment[index + 1:]
        # A direct executable is required. Do not look for `exec` inside prompts.
        verb = 0
        while verb < len(tail) and tail[verb].startswith("-"):
            key = tail[verb].split("=", 1)[0]
            if key not in {"-c", "--config", "-m", "--model", "-p", "--profile"}:
                break
            verb += 1 if "=" in tail[verb] else 2
        if verb >= len(tail) or tail[verb] != "exec":
            continue
        arguments = [*tail[:verb], *tail[verb + 1:]]
        redirection = any(word and all(char in "<>()&" for char in word) for word in segment)
        output_path = None
        if redirection:
            # Redirection targets must not become apparent CLI parameters.
            redirect_index = next(i for i, word in enumerate(arguments)
                                  if word and all(char in "<>()&" for char in word))
            # A numeric fd before an operator belongs to the redirection.
            if redirect_index and arguments[redirect_index - 1].isdigit():
                redirect_index -= 1
            output_path = _output_channel(arguments[redirect_index:])
            arguments = arguments[:redirect_index]
        metadata = classify_exec_arguments(arguments)
        reason = "awaiting_completion"
        if _dynamic_shell(command):
            reason = "dynamic_shell"
        elif output_path and len(segments) == 1 and not any(word in {"&&", "||", "|", ";"} for word in words):
            reason = "output_pending"
        elif redirection or "&" in words:
            reason = "background_or_redirect"
        elif len(segments) != 1 or any(word in separators for word in words):
            reason = "complex_shell"
        elif metadata["ephemeral"]:
            reason = "ephemeral"
        elif not metadata["json_output"]:
            reason = "not_json"
        if reason != "output_pending":
            output_path = None
        launches.append(BashLaunch(metadata, reason, home, output_path, "&" in words))
        if len(launches) > 8:
            return []
    if len(launches) > 1:
        launches = [BashLaunch(item.metadata, "multiple_launches", item.codex_home) for item in launches]
    return launches
