"""The parts of config files that are assets, as canonical JSON (virtual files).

Each extractor reads only the keys it names and projects them through a field
whitelist; every other key of the file is never kept, logged or returned. The
result is a set of virtual assets, ``virtual path -> bytes``, versioned like files:

* ``settings_hooks``: the ``hooks`` key of settings.json without OS's own
  registrations, as ``hooks/registration.json``; nothing when none is left;
* ``mcp_servers``: each MCP server of the global config file, top level and per
  project, as ``mcp/<name>.json``: ``type``, ``command``, ``args``, ``url``, and the
  names of ``env`` and ``headers`` (never their values); OS's own server is left out;
* ``plugin_manifest``: installed plugins (name, marketplace, version) and the
  marketplaces' sources, as ``plugins/manifest.json``; local install paths dropped.

Values that carry credentials are replaced by ``REDACTED`` before anything else
sees them (``redact``): the value after a secret-named flag in ``args`` (``--token v``
and ``--token=v``, ``-H``/``--header`` included), the credential after ``Bearer`` or
``Basic``, a URL's user and password, every query value and any path segment that
looks like a key, and header and environment values of hook entries. The caller
still scans the result and refuses what it hits: redaction is the first line, the
scan the second.

A source that is present but does not parse makes the extractor answer None, never
an empty list: a file caught mid-write must not read as "everything was removed".

Canonical JSON (sorted keys, two-space indent, trailing newline) makes equal
settings hash equal, whatever order the host wrote them in.

Standard library only: the backup child process imports this module.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass

HOOK_REGISTRATION = "hooks/registration.json"
PLUGIN_MANIFEST = "plugins/manifest.json"
MCP_PREFIX = "mcp/"
REDACTED = "<redacted>"
_MCP_FIELDS = ("type", "command", "args", "url")
_MARKETPLACE_SOURCE_FIELDS = ("source", "repo", "url", "path", "ref")
# Keys of a hook entry whose values are a map of secrets (header and environment values).
_SECRET_MAPS = ("headers", "env")

_SECRET_WORDS = r"api[-_]?key|apikey|token|secret|password|passwd|pwd|credentials?|auth|authorization|key"
# A flag whose value is a credential: -H, --header(s), and any --*api-key/--*token/... flag.
_SECRET_FLAG = re.compile(rf"^(?:-H|--headers?|--?[A-Za-z0-9_-]{{0,40}}?(?:{_SECRET_WORDS}))$", re.IGNORECASE)
_SECRET_FLAG_ASSIGNMENT = re.compile(
    rf"^(--?[A-Za-z0-9_-]{{0,40}}?(?:{_SECRET_WORDS})|--headers?|-H)=(.+)$", re.IGNORECASE)
_SCHEME_CREDENTIAL = re.compile(r"\b(Bearer|Basic|Token)\s+[^\s\"',;]{1,512}", re.IGNORECASE)
_URL = re.compile(r"\b[a-z][a-z0-9+.-]{0,30}://[^\s\"']{1,2048}", re.IGNORECASE)
_SEGMENT_MIN_CHARS = 16


@dataclass(frozen=True)
class Extracted:
    """One virtual asset. ``owner_path`` is the project folder a per-project entry belongs to."""

    rel_path: str
    content: bytes
    owner_path: str = ""


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _object(data: bytes | None) -> dict | None:
    """A JSON object from a config file: {} when absent or empty, None when it does not parse."""
    if not data or not data.strip():
        return {}
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ----------------------------------------------------------------------------
# Redaction
# ----------------------------------------------------------------------------


def _looks_like_key(segment: str) -> bool:
    """A path segment that reads as a credential: long, mixing letters and digits, varied."""
    if len(segment) < _SEGMENT_MIN_CHARS or not any(c.isdigit() for c in segment) \
            or not any(c.isalpha() for c in segment):
        return False
    counts: dict[str, int] = {}
    for char in segment:
        counts[char] = counts.get(char, 0) + 1
    entropy = -sum(n / len(segment) * math.log2(n / len(segment)) for n in counts.values())
    return entropy >= 3.0


def redact_url(url: str) -> str:
    """``url`` without user and password, query values, or key-like path segments."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return REDACTED
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    path = "/".join(REDACTED if _looks_like_key(segment) else segment for segment in parts.path.split("/"))
    query = "&".join(f"{key}={REDACTED}" if value else key
                     for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, parts.fragment and REDACTED))


def redact_urls(text: str) -> str:
    """Every URL in ``text`` through ``redact_url``; the rest of the text as it is."""
    return _URL.sub(lambda m: redact_url(m.group(0)), text)


def redact_text(text: str) -> str:
    """Free text (a shell command, a header line): scheme credentials and URLs redacted."""
    return redact_urls(_SCHEME_CREDENTIAL.sub(lambda m: f"{m.group(1)} {REDACTED}", text))


def redact_args(args: list) -> list:
    """An argument list with every value after (or attached to) a secret-named flag redacted."""
    redacted: list = []
    expect_value = False
    for arg in args:
        if not isinstance(arg, str):
            redacted.append(arg)
            expect_value = False
            continue
        if expect_value:
            redacted.append(REDACTED)
            expect_value = False
            continue
        assignment = _SECRET_FLAG_ASSIGNMENT.match(arg)
        if assignment:
            redacted.append(f"{assignment.group(1)}={REDACTED}")
            continue
        expect_value = bool(_SECRET_FLAG.match(arg))
        redacted.append(arg if expect_value else redact_text(arg))
    return redacted


def redact(value: object) -> object:
    """A hook entry or any nested config value with its credentials redacted (see module doc)."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _SECRET_MAPS and isinstance(item, dict):
                out[key] = {name: REDACTED for name in item}
            elif key == "args" and isinstance(item, list):
                out[key] = redact_args(item)
            elif key == "url" and isinstance(item, str):
                out[key] = redact_url(item)
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value


# ----------------------------------------------------------------------------
# Extractors
# ----------------------------------------------------------------------------


def settings_hooks(data: bytes | None, is_ours: Callable[[str], bool]) -> list[Extracted] | None:
    """The user's hook registrations in settings.json (OS's own dropped, credentials redacted)."""
    settings = _object(data)
    if settings is None:
        return None
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    kept: dict[str, list] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            entries = [entry for entry in group.get("hooks") or []
                       if isinstance(entry, dict) and not is_ours(str(entry.get("command", "")))]
            if entries:
                kept.setdefault(str(event), []).append(redact({**group, "hooks": entries}))
    return [Extracted(HOOK_REGISTRATION, canonical(kept))] if kept else []


def project_server(definition: Mapping) -> dict:
    """An MCP server definition through the whitelist, credentials redacted, ``env``/``headers`` by name."""
    projected: dict = {key: definition[key] for key in _MCP_FIELDS if key in definition}
    if isinstance(projected.get("url"), str):
        projected["url"] = redact_url(projected["url"])
    if isinstance(projected.get("args"), list):
        projected["args"] = redact_args(projected["args"])
    if isinstance(projected.get("command"), str):
        projected["command"] = redact_text(projected["command"])
    for key in ("env", "headers"):
        if isinstance(definition.get(key), dict):
            projected[f"{key}_keys"] = sorted(str(name) for name in definition[key])
    return projected


def _server_path(name: str) -> str:
    return MCP_PREFIX + urllib.parse.quote(name, safe="") + ".json"


def mcp_servers(data: bytes | None, own_server: str) -> list[Extracted] | None:
    """Every MCP server of the global config file but OS's own, one virtual file each."""
    config = _object(data)
    if config is None:
        return None
    found: list[Extracted] = []

    def add(servers: object, owner_path: str) -> None:
        if not isinstance(servers, dict):
            return
        for name, definition in sorted(servers.items()):
            if name == own_server or not isinstance(definition, dict):
                continue
            found.append(Extracted(_server_path(str(name)), canonical(project_server(definition)), owner_path))

    add(config.get("mcpServers"), "")
    projects = config.get("projects")
    if isinstance(projects, dict):
        for folder, entry in sorted(projects.items()):
            if isinstance(entry, dict):
                add(entry.get("mcpServers"), str(folder))
    return found


def plugin_manifest(installed: bytes | None, marketplaces: bytes | None) -> list[Extracted] | None:
    """Which plugins are installed from which marketplaces: enough to reinstall, no local paths."""
    installed_list, market_list = _object(installed), _object(marketplaces)
    if installed_list is None or market_list is None:
        return None
    plugins = []
    entries = installed_list.get("plugins")
    for key, installs in sorted(entries.items()) if isinstance(entries, dict) else ():
        name, _, market = str(key).partition("@")
        versions = sorted({str(i.get("version", "")) for i in installs if isinstance(i, dict)}) \
            if isinstance(installs, list) else []
        plugins.append({"name": name, "marketplace": market, "versions": versions})
    markets = []
    for name, entry in sorted(market_list.items()):
        source = entry.get("source") if isinstance(entry, dict) else None
        if isinstance(source, dict):
            source = {key: source[key] for key in _MARKETPLACE_SOURCE_FIELDS if key in source}
            if isinstance(source.get("url"), str):
                source["url"] = redact_url(source["url"])
        markets.append({"name": str(name), "source": source if isinstance(source, dict) else {}})
    if not plugins and not markets:
        return []
    return [Extracted(PLUGIN_MANIFEST, canonical({"plugins": plugins, "marketplaces": markets}))]
