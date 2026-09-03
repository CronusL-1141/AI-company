#!/usr/bin/env python3
"""Deterministic redactor that builds tests/fixtures/codex from local Codex evidence.

Reads (never writes) a Codex home, the probe evidence directory and the AI Team
OS event database, and emits a structural fixture set: rollout skeletons, hook
payload captures, state-db samples and OS event samples, plus MANIFEST.json.

Every string that is not structural is replaced by "<str:N>"; conversation rows
are dropped entirely; paths, model slugs and agent paths are mapped to stable
placeholders computed at runtime (no real value is ever stored in this file).
Running twice over the same inputs produces byte-identical output.

Usage:
    python3 scripts/redact_codex_fixture.py --evidence <dir> [--codex-home ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# --------------------------------------------------------------- constants

# Keys whose string values are structural and kept verbatim (paths still mapped).
KEEP_KEYS = {
    "type", "role", "name", "kind", "status", "model", "model_provider",
    "model_provider_id", "originator", "source", "cli_version", "thread_source",
    "approval_policy", "approvals_reviewer", "reasoning_effort", "service_tier",
    "collaboration_mode_kind", "trigger", "agent_role",
    "agent_path", "other", "limit_id", "limit_name", "plan_type", "event_id",
    "agent_thread_id", "call_id", "id", "session_id", "turn_id",
    "parent_thread_id", "child_thread_id", "client_id", "thread_id",
    "timestamp", "started_at", "completed_at", "current_date", "timezone",
    "depth", "memory_mode", "history_mode", "version", "fork_turns",
    "agent_type", "personality", "access", "permission_mode",
    "hook_event_name", "tool_name", "tool_use_id", "agent_id", "format",
    "output_format", "stage", "reason", "decision", "phase", "level",
    "item_type", "provider_id", "sandbox", "network_access", "wire_api",
    "trust_level", "enabled", "transport", "compact_reason",
    "model_context_window", "rate_limit_reason", "window_minutes",
    # additions for this fixture set
    "effort", "mode", "network", "file_system", "summary_mode",
    "argv_event", "captured_at", "migration_id", "skip_reason",
    "server", "tool", "ref_id", "comp_hash", "window_number", "window_id",
    "first_window_id", "previous_window_id", "ordinal", "namespace",
    "collaboration_mode", "multi_agent_mode", "multi_agent_version",
    "permission_profile", "sandbox_policy", "file_system_sandbox_policy",
    "realtime_active", "apps_instructions", "full", "trigger_turn",
    "subagent_history_start_ordinal", "context_window", "forked_from_id",
    "spend_control_reached", "rate_limit_reached_type",
}

# Keys that always carry content, redacted even when nested under a kept key.
CONTENT_KEYS = {
    "text", "message", "content", "input", "output", "arguments", "summary",
    "encrypted_content", "instructions", "developer_instructions",
    "base_instructions", "last_agent_message", "last_assistant_message",
    "command", "cwd", "path", "writable_roots", "workspace_roots",
    "rollout_path", "agents_md", "title", "thread_name", "first_user_message",
    "preview", "prompt", "query", "queries", "url", "domain", "snippet",
    "source_path", "target", "images", "local_images", "audio", "local_audio",
    "text_elements", "delta", "reasoning", "description", "stdout", "stderr",
    "aggregated_output", "formatted_output", "diff", "unified_diff",
    "additionalContext", "systemMessage", "permissionDecisionReason",
    "updatedInput", "tool_input", "tool_response", "transcript_path",
    "agent_transcript_path", "hook_command", "env", "args", "argv",
    "revised_prompt", "saved_path", "git_origin_url", "git_branch", "git_sha",
    "filesystem", "shell", "structuredContent", "invocation_arguments",
    "memory_citation", "error", "detail", "cwd_of_hook", "env_plugin_root",
    "input-messages", "last-assistant-message", "agents_instructions",
}

# Rollout rows deleted outright: raw conversation carriers.
DROP_RESPONSE_ITEM_TYPES = {"message", "reasoning"}
DROP_EVENT_MSG_TYPES = {
    "agent_message", "user_message", "agent_reasoning",
    "agent_message_delta", "user_message_delta", "agent_reasoning_delta",
    "agent_reasoning_raw_content", "agent_reasoning_raw_content_delta",
    "agent_reasoning_section_break",
}

# session_meta keys removed wholesale (H1).
DROP_SESSION_META_KEYS = {"base_instructions", "git"}

# Lists that would otherwise reproduce whole histories.
CAPPED_LIST_KEYS = {
    "replacement_history", "results", "entries", "content", "dynamic_tools",
    "tools", "oneOf", "anyOf", "allOf", "required", "enum", "queries",
}
LIST_CAP = 3

# tool_response / tool_input strings at or above this length keep their exact
# length but get compressible filler instead of content (H6).
FILLER_MIN = 16384
FILLER_UNIT = "<filler>"

# Keys naming an agent, a task or a person: only the /root tree shape survives.
IDENTITY_KEYS = {"agent_path", "author", "recipient", "task_name", "nickname",
                 "agent_nickname", "agent_name", "sender", "task", "owner"}

# Row labels ("<type>/<payload.type>") observed while designing this fixture set.
# Anything else is still emitted, but counted in MANIFEST.unknown_row_types.
KNOWN_ROW_TYPES = {
    "session_meta", "turn_context", "compacted", "world_state",
    "inter_agent_communication", "inter_agent_communication_metadata",
    "response_item/message", "response_item/reasoning", "response_item/agent_message",
    "response_item/custom_tool_call", "response_item/custom_tool_call_output",
    "response_item/function_call", "response_item/function_call_output",
    "event_msg/agent_message", "event_msg/user_message", "event_msg/agent_reasoning",
    "event_msg/token_count", "event_msg/task_started", "event_msg/task_complete",
    "event_msg/thread_settings_applied", "event_msg/patch_apply_end",
    "event_msg/item_completed", "event_msg/mcp_tool_call_end",
    "event_msg/sub_agent_activity", "event_msg/turn_aborted",
    "event_msg/context_compacted", "event_msg/thread_rolled_back",
    "event_msg/image_generation_end", "event_msg/web_search_end",
}

IMPORT_TURN_PREFIX = "external-import-turn-"
IMPORT_MARKER = "<EXTERNAL SESSION IMPORTED>"

# Sub-trees of a Codex home whose tail is structural and safe verbatim: rollout
# files (H5 resolves them inside the fixture) and this project's own hook scripts
# (they ship in this repository). Everywhere else under the home a real file name
# can carry a document title or a private script name, so the tail is folded --
# same reason the workspace branch below folds file names.
CODEXHOME_KEEP_TAIL = ("sessions/", "archived_sessions/", "hooks/ai-team-os/")
# Top-level entries of a Codex home: product-owned names, kept so a folded path
# still says which artefact class it pointed at.
CODEXHOME_SAFE_TOP = {
    "sessions", "archived_sessions", "hooks", "hooks.json", "config.toml",
    "memories", "visualizations", "generated_images", "history.jsonl", "log",
    "prompts", "skills", "state_5.sqlite", "external_agent_session_imports.json",
}

# H7: dispatch bodies travel as Fernet tokens. They are already ciphertext and the
# key lives outside this repository, so they are kept verbatim -- that is what lets
# a test assert the dispatch body is unreadable on the Codex side.
FERNET_RE = re.compile(r"gAAAAA[A-Za-z0-9_\-=]+")

_PATH_RE = re.compile(r"^(~|/|[A-Za-z]:\\|\\\\)")
_ENUM_KEY_RE = re.compile(
    r"(_type|_kind|_mode|_policy|_status|_source|_reason|_level|_stage|_id"
    r"|_tier|_effort|_format|Type|Kind|Mode|Policy|Status)$"
)
_FILE_EXT_RE = re.compile(
    r"\.(md|py|txt|json|jsonl|ts|tsx|js|toml|ya?ml|csv|html?|pdf|docx?|xlsx?"
    r"|pptx?|sh|ps1|bat|rs|go|java|c|cpp|h|sql|log|ini|cfg|xml|svg|png|jpe?g)$",
    re.I,
)
_MODEL_KEYS = {"model", "model_slug", "model_name", "default_model"}
_MAX_PATH_LEN = 400
# An absolute path inlined inside a larger string (a sandbox policy blob can carry
# a custom writable root): such values never survive verbatim, even under KEEP_KEYS.
_EMBEDDED_PATH_RE = re.compile(r"(?:^|[\s\"'=:,\[({])(?:~|/)[A-Za-z0-9._-]+/")

# Shapes that must never reach a tracked data file. Complements the literal list,
# which is derived at runtime and therefore machine-specific.
FORBIDDEN_SHAPES = {
    "non_ascii": re.compile(r"[^\x00-\x7F]"),
    "email": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    "http_url": re.compile(r"https?://"),
    "api_key": re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    "bearer": re.compile(r"(?i)bearer[\s\"':=]+[A-Za-z0-9._\-]{16,}"),
}


def is_pathlike(s: str) -> bool:
    """A single filesystem path, not a listing or a prose blob that starts with '/'."""
    return bool(_PATH_RE.match(s)) and "\n" not in s and len(s) <= _MAX_PATH_LEN


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def filler(length: int) -> str:
    """ASCII filler of exactly `length` bytes (ASCII, so bytes == characters)."""
    reps = length // len(FILLER_UNIT) + 1
    return (FILLER_UNIT * reps)[:length]


SEPARATORS = (",", ":")


def dumps(obj) -> str:
    """Compact, stable JSON encoding used for every emitted line."""
    return json.dumps(obj, ensure_ascii=False, separators=SEPARATORS)


def path_shape(s: str) -> str:
    parts = [p for p in re.split(r"[\\/]+", s) if p]
    return f"<path:{len(parts)}>"


def map_codexhome_tail(rest: str) -> str:
    """Map a path below CODEX_HOME, folding every tail that is not structural."""
    parts = [q for q in rest.split("/") if q]
    if not parts:
        return "/codexhome"
    if rest.startswith(CODEXHOME_KEEP_TAIL):
        return "/codexhome/" + "/".join(parts)
    if parts[0] not in CODEXHOME_SAFE_TOP:
        return "/codexhome/" + path_shape(rest)
    if len(parts) == 1:
        return "/codexhome/" + parts[0]
    return "/codexhome/" + parts[0] + "/" + path_shape("/".join(parts[1:]))


# --------------------------------------------------------------- registries


class Registry:
    """Runtime-only mapping of real slugs/paths to stable placeholders."""

    def __init__(self, codex_homes: list[str]):
        # longest first so nested homes win
        self.codex_homes = sorted({h.rstrip("/") for h in codex_homes if h}, key=len, reverse=True)
        self.models: dict[str, str] = {}
        self.workspaces: dict[str, str] = {}
        self.agent_paths: dict[str, str] = {}

    # -- models -----------------------------------------------------------
    def learn_model(self, slug: str) -> None:
        if slug and slug not in self.models:
            self.models[slug] = f"codex-model-{chr(ord('a') + len(self.models))}"

    def map_model(self, slug: str) -> str:
        return self.models.get(slug, f"<str:{len(slug)}>")

    # -- workspaces -------------------------------------------------------
    def learn_workspace(self, raw: str) -> None:
        p = self._canon(raw)
        if not p or p in self.workspaces:
            return
        if self._is_codex_home(p):
            return
        if "codex-probe-proj" in p:
            self.workspaces[p] = "/workspace/probe"
        else:
            n = sum(1 for v in self.workspaces.values() if v.startswith("/workspace/proj-"))
            self.workspaces[p] = f"/workspace/proj-{n + 1:02d}"

    def renumber_workspaces(self) -> None:
        """Assign proj numbers by sorted original path so order is input-independent."""
        real = sorted(p for p, v in self.workspaces.items() if v.startswith("/workspace/proj-"))
        for i, p in enumerate(real, start=1):
            self.workspaces[p] = f"/workspace/proj-{i:02d}"

    # -- agent paths ------------------------------------------------------
    def learn_agent_path(self, raw: str) -> None:
        if raw and raw.startswith("/root") and raw != "/root" and raw not in self.agent_paths:
            self.agent_paths[raw] = ""

    def renumber_agent_paths(self) -> None:
        for i, raw in enumerate(sorted(self.agent_paths), start=1):
            depth = len([p for p in raw.split("/") if p])
            tail = "/".join(f"agent-{i:02d}" for _ in range(depth - 1))
            self.agent_paths[raw] = f"/root/{tail}"

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _canon(raw: str) -> str:
        p = raw.rstrip("/")
        if p.startswith("/private/tmp/"):
            p = p[len("/private"):]
        return p

    def _is_codex_home(self, p: str) -> bool:
        return any(p == h or p.startswith(h + "/") for h in self.codex_homes)

    def map_path(self, raw: str) -> str:
        mapped = self._map_path(raw)
        if isinstance(mapped, str) and not self.scrub_ok(mapped):
            return f"<str:{len(raw)}>"
        return mapped

    def _map_path(self, raw: str) -> str:
        if not isinstance(raw, str) or not raw:
            return raw
        if not is_pathlike(raw):
            return f"<str:{len(raw)}>"
        p = self._canon(raw)
        for h in self.codex_homes:
            if p == h:
                return "/codexhome"
            if p.startswith(h + "/"):
                return map_codexhome_tail(p[len(h) + 1:])
        for orig in sorted(self.workspaces, key=len, reverse=True):
            if p == orig:
                return self.workspaces[orig]
            if p.startswith(orig + "/"):
                # keep the workspace root only: file names inside a real project
                # can carry tool, employer or document names
                return self.workspaces[orig] + "/" + path_shape(p[len(orig) + 1:])
        if raw in self.agent_paths:
            return self.agent_paths[raw]
        if raw == "/root":
            return raw
        return path_shape(raw)

    def scrub_ok(self, mapped: str) -> bool:
        """Reject a mapping that left any real prefix behind."""
        return not any(h in mapped for h in self.codex_homes) and str(Path.home()) not in mapped


# --------------------------------------------------------------- sanitizer


class Sanitizer:
    def __init__(self, reg: Registry, import_sample: bool = False):
        self.reg = reg
        self.import_sample = import_sample
        self.unknown_keys: dict[str, int] = {}

    def string(self, key: str | None, s: str) -> str:
        if self.import_sample and s == IMPORT_MARKER:
            return s
        if s.startswith("gAAAAA") and FERNET_RE.fullmatch(s):
            return s
        if key in _MODEL_KEYS:
            return self.reg.map_model(s)
        if key in IDENTITY_KEYS:
            # agent identities: only the /root tree shape survives
            return self.reg.map_path(s) if s.startswith("/root") else f"<str:{len(s)}>"
        if key == "timezone":
            return "UTC"
        if key in CONTENT_KEYS:
            if is_pathlike(s):
                return self.reg.map_path(s)
            if key == "tool_response" and len(s) >= FILLER_MIN:
                return filler(len(s.encode()))
            if key in {"arguments", "tool_input", "tool_response", "input", "output"} and s[:1] in "{[":
                try:
                    return dumps(self.skeleton(json.loads(s)))
                except Exception:
                    pass
            return f"<str:{len(s)}>"
        if key in KEEP_KEYS:
            if is_pathlike(s) and key not in {"timestamp", "id", "session_id"}:
                return self.reg.map_path(s)
            if key == "name" and (not s.isascii() or _FILE_EXT_RE.search(s) or " " in s):
                return f"<str:{len(s)}>"
            if _EMBEDDED_PATH_RE.search(s):
                return f"<str:{len(s)}>"
            return s if len(s) <= 200 else f"<str:{len(s)}>"
        if key:
            self.unknown_keys[key] = self.unknown_keys.get(key, 0) + 1
        if key and _ENUM_KEY_RE.search(key) and len(s) <= 24 \
                and re.fullmatch(r"[A-Za-z0-9_.:-]+", s) and not _PATH_RE.match(s):
            return s
        if is_pathlike(s):
            return self.reg.map_path(s)
        return f"<str:{len(s)}>"

    def key(self, k: str, i: int) -> str:
        if _PATH_RE.match(k):
            return f"{self.reg.map_path(k)}#{i}"
        if not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,64}", k):
            return f"<key:{len(k)}>#{i}"
        return k

    def walk(self, obj, key: str | None = None):
        if isinstance(obj, dict):
            cmd_ctx = "cmd" in obj or "parsed_cmd" in obj
            out = {}
            for i, (k, v) in enumerate(obj.items()):
                if cmd_ctx and k in {"name", "path", "cmd", "pattern", "query"}:
                    out[self.key(k, i)] = self.walk(v, "text")
                else:
                    out[self.key(k, i)] = self.walk(v, k)
            return out
        if isinstance(obj, list):
            if key in CAPPED_LIST_KEYS and len(obj) > LIST_CAP:
                head = [self.walk(v, key) for v in obj[:LIST_CAP]]
                return head + [f"<+{len(obj) - LIST_CAP}>"]
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            return self.string(key, obj)
        return obj

    def skeleton(self, o, key: str | None = None):
        if isinstance(o, dict):
            return {self.key(k, i): self.skeleton(v, k) for i, (k, v) in enumerate(o.items())}
        if isinstance(o, list):
            return [self.skeleton(v, key) for v in o[:LIST_CAP]] + (
                [f"<+{len(o) - LIST_CAP}>"] if len(o) > LIST_CAP else []
            )
        if isinstance(o, bool) or o is None:
            return o
        if isinstance(o, (int, float)):
            return "<num>"
        if isinstance(o, str):
            if o.startswith("gAAAAA") and FERNET_RE.fullmatch(o):
                return o
            if key in IDENTITY_KEYS:
                return self.reg.map_path(o) if o.startswith("/root") else f"<str:{len(o)}>"
            if key in CONTENT_KEYS or key not in KEEP_KEYS:
                return f"<str:{len(o)}>"
            ok = (len(o) <= 24 and o.replace("_", "").replace("-", "").replace(".", "").isalnum()
                  and o.isascii() and o == o.lower())
            return o if ok else f"<str:{len(o)}>"
        return "<?>"


# --------------------------------------------------------------- discovery


def iter_json_lines(path: Path):
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw), raw
            except Exception:
                yield None, raw


def collect_learnables(obj, reg: Registry, key: str | None = None) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            collect_learnables(v, reg, k)
    elif isinstance(obj, list):
        for v in obj:
            collect_learnables(v, reg, key)
    elif isinstance(obj, str):
        if key in _MODEL_KEYS:
            reg.learn_model(obj)
        elif key in {"cwd", "cwd_of_hook"} and _PATH_RE.match(obj):
            reg.learn_workspace(obj)
        elif key in {"workspace_roots", "writable_roots"} and _PATH_RE.match(obj):
            reg.learn_workspace(obj)
        elif key in {"agent_path", "author", "recipient"} and obj.startswith("/root"):
            reg.learn_agent_path(obj)


def is_imported(path: Path) -> bool:
    for obj, _ in iter_json_lines(path):
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        tid = payload.get("turn_id") if isinstance(payload, dict) else None
        if isinstance(tid, str) and tid.startswith(IMPORT_TURN_PREFIX):
            return True
    return False


# --------------------------------------------------------------- rollout emit


def rollout_rows(path: Path, sanitizer: Sanitizer, row_stats: dict) -> list[str]:
    kept: list[str] = []
    for obj, raw in iter_json_lines(path):
        if obj is None:
            row_stats["kept"]["<unparseable>"] = row_stats["kept"].get("<unparseable>", 0) + 1
            kept.append(dumps({"_unparseable_line_len": len(raw)}))
            continue
        rtype = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        ptype = payload.get("type") if isinstance(payload, dict) else None
        label = f"{rtype}/{ptype}" if ptype else str(rtype)
        keep_marker = sanitizer.import_sample and IMPORT_MARKER in raw
        if not keep_marker and rtype == "response_item" and ptype in DROP_RESPONSE_ITEM_TYPES:
            row_stats["dropped"][label] = row_stats["dropped"].get(label, 0) + 1
            continue
        if not keep_marker and rtype == "event_msg" and ptype in DROP_EVENT_MSG_TYPES:
            row_stats["dropped"][label] = row_stats["dropped"].get(label, 0) + 1
            continue
        if rtype == "session_meta" and isinstance(obj.get("payload"), dict):
            obj = dict(obj)
            obj["payload"] = {k: v for k, v in obj["payload"].items()
                              if k not in DROP_SESSION_META_KEYS}
        compacted_bytes = None
        if rtype == "compacted" and isinstance(payload.get("message"), str):
            compacted_bytes = len(payload["message"].encode())
        row_stats["kept"][label] = row_stats["kept"].get(label, 0) + 1
        clean = sanitizer.walk(obj)
        if compacted_bytes is not None:
            # H9: "message" is a content key, so the walk would map it to <str:N>;
            # a compaction summary keeps its exact length as filler instead.
            clean["payload"]["message"] = filler(compacted_bytes)
        kept.append(dumps(clean))
    return kept


RARE_ROW_MAX = 40


def row_label(line: str) -> str:
    try:
        o = json.loads(line)
    except Exception:
        return "<unparseable>"
    pl = o.get("payload") if isinstance(o.get("payload"), dict) else {}
    ptype = pl.get("type") if isinstance(pl, dict) else None
    return f"{o.get('type')}/{ptype}" if ptype else str(o.get("type"))


def trim_rows(rows: list[str], head: int, tail: int) -> tuple[list[str], str]:
    """Keep head/tail rows plus every row of a type that is rare inside this file."""
    if len(rows) <= head + tail:
        return rows, ""
    labels = [row_label(line) for line in rows]
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    keep = set(range(head)) | set(range(len(rows) - tail, len(rows)))
    for i, label in enumerate(labels):
        if counts[label] <= RARE_ROW_MAX:
            keep.add(i)
    out = [rows[i] for i in sorted(keep)]
    rule = (f"head{head}+tail{tail}+all-rows-of-types-occurring<={RARE_ROW_MAX}-times-in-file")
    return out, rule


def import_trim(rows: list[str], head: int, tail_min: int) -> tuple[list[str], str]:
    """First `head` rows, plus a tail that reaches back to the last token_count row."""
    if head >= len(rows):
        return rows, f"head{head}"
    if tail_min <= 0:
        return rows[:head], f"head{head}"
    tail = tail_min
    while tail < len(rows) - head:
        window = rows[len(rows) - tail:]
        if any('"token_count"' in line for line in window):
            break
        tail += 1
    out = rows[:head] + rows[len(rows) - tail:]
    return out, f"head{head}+tail{tail}(back-to-last-token_count)"


# --------------------------------------------------------------- state db


def open_ro(db: Path, tmp: Path) -> sqlite3.Connection:
    """Copy a possibly-live sqlite file (with side files) and open it read-only."""
    last: Exception | None = None
    for attempt in range(4):
        work = tmp / f"{db.name}.{attempt}"
        work.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db, work)
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                shutil.copy2(side, work.with_name(work.name + suffix))
        try:
            con = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
            con.execute("pragma quick_check(1)").fetchone()
            return con
        except sqlite3.DatabaseError as exc:  # torn copy of a live database
            last = exc
    raise RuntimeError(f"could not read a consistent copy of {db.name}: {last}")


def table_sql(con: sqlite3.Connection, names: list[str]) -> dict:
    out = {}
    for n in names:
        row = con.execute("select sql from sqlite_master where name=?", (n,)).fetchone()
        if row and row[0]:
            out[n] = row[0]
    return out


def thread_rows(con: sqlite3.Connection, where: str, params: tuple, limit: int) -> list[dict]:
    cols = [r[1] for r in con.execute("pragma table_info(threads)")]
    sql = f"select {', '.join(cols)} from threads where {where} order by id limit {limit}"
    return [dict(zip(cols, r)) for r in con.execute(sql, params)]


# --------------------------------------------------------------- leak guard


def leak_guard(text: str, forbidden: dict[str, str], where: str) -> None:
    """Refuse to write anything carrying a known literal or a forbidden shape.

    Never echoes the offending text: a leak report must not itself leak.
    """
    for label, needle in forbidden.items():
        if needle and needle in text:
            idx = text.index(needle)
            raise RuntimeError(
                f"leak guard tripped in {where}: {label} at offset {idx} "
                f"(context length {len(text)})"
            )
    for label, shape in FORBIDDEN_SHAPES.items():
        hit = shape.search(text)
        if hit:
            raise RuntimeError(
                f"leak guard tripped in {where}: {label} at offset {hit.start()} "
                f"(match length {len(hit.group(0))})"
            )


# --------------------------------------------------------------- main build


def build(args) -> int:
    codex_home = Path(args.codex_home).expanduser().resolve()
    evidence = Path(args.evidence).expanduser().resolve()
    os_db = Path(args.os_db).expanduser()
    out = Path(args.out).expanduser().resolve()

    probe142 = evidence / "codex-probe-proj"
    probe152 = evidence / "codex-probe-proj-0152"

    native_files = sorted(
        [p for sub in ("sessions", "archived_sessions") for p in (codex_home / sub).rglob("*.jsonl")],
        key=lambda p: str(p.relative_to(codex_home)),
    )
    probe142_rollouts = sorted((probe142 / "evidence/rollouts").rglob("*.jsonl"), key=lambda p: p.name)
    probe152_rollouts = sorted((probe152 / "evidence/rollouts/2026").rglob("*.jsonl"), key=lambda p: p.name)
    hook142 = sorted(probe142.glob("capture-run*.jsonl"), key=lambda p: p.name)
    hook152 = sorted(probe152.glob("capture-run*.jsonl"), key=lambda p: p.name)

    # -- pass 1: learn mappings (fixed traversal order) --------------------
    probe_homes: set[str] = set()
    for f in hook142 + hook152:
        for obj, _ in iter_json_lines(f):
            if isinstance(obj, dict) and isinstance(obj.get("env_codex_home"), str):
                probe_homes.add(obj["env_codex_home"])
    for f in probe142_rollouts + probe152_rollouts:
        for obj, _ in iter_json_lines(f):
            if isinstance(obj, dict) and isinstance(obj.get("payload"), dict):
                tp = obj["payload"].get("transcript_path")
                if isinstance(tp, str) and "/sessions/" in tp:
                    probe_homes.add(tp.split("/sessions/")[0])
            break
    for f in hook142 + hook152:
        for obj, _ in iter_json_lines(f):
            pl = obj.get("payload") if isinstance(obj, dict) else None
            if isinstance(pl, dict):
                for k in ("transcript_path", "agent_transcript_path"):
                    v = pl.get(k)
                    if isinstance(v, str) and "/sessions/" in v:
                        probe_homes.add(v.split("/sessions/")[0])

    homes = [str(codex_home)] + sorted(probe_homes)
    homes += [h[len("/private"):] for h in homes if h.startswith("/private/")]
    reg = Registry(homes)

    learn_sources = (native_files + probe142_rollouts + probe152_rollouts + hook142 + hook152
                     + sorted(probe142.glob("notify-capture.jsonl"))
                     + sorted(probe152.glob("notify-capture.jsonl"))
                     + sorted(probe142.glob("exec-json-stream.jsonl"))
                     + sorted(probe152.glob("exec-json-stream.jsonl")))
    for f in learn_sources:
        for obj, _ in iter_json_lines(f):
            if obj is not None:
                collect_learnables(obj, reg)

    tmp = Path(tempfile.mkdtemp(prefix="codex-fixture-"))
    try:
        state_dbs = [codex_home / "state_5.sqlite", probe152 / "evidence/state_5.sqlite"]
        for db in state_dbs:
            if not db.exists():
                continue
            con = open_ro(db, tmp)
            try:
                cols = [r[1] for r in con.execute("pragma table_info(threads)")]
                for row in con.execute(f"select {', '.join(cols)} from threads"):
                    collect_learnables(dict(zip(cols, row)), reg)
            finally:
                con.close()
        reg.renumber_workspaces()
        reg.renumber_agent_paths()

        emitted: list[dict] = []
        unknown_row_types: dict[str, int] = {}
        unknown_payload_keys: dict[str, int] = {}
        # Literals that must never reach a tracked file. Derived at runtime so
        # this script never stores a real home directory or user name itself.
        forbidden = {
            "home": str(Path.home()),
            "home_root": str(Path.home().parent) + "/",
            "volumes_root": "/Volumes/",
            "base_instructions": "base_instructions",
        }
        for region in ("Africa", "America", "Asia", "Australia", "Europe", "Pacific"):
            forbidden[f"timezone_{region.lower()}"] = region + "/"
        for slug in reg.models:
            forbidden[f"model_slug_{reg.models[slug]}"] = slug
        # Site-specific literals (employer, internal tool names, ...) are supplied at
        # run time so this script never stores one; labels stay value-free.
        for i, extra in enumerate(os.environ.get("CODEX_FIXTURE_FORBIDDEN", "").split(",")):
            if extra.strip():
                forbidden[f"site_literal_{i}"] = extra.strip()

        def write(rel: str, text: str, meta: dict) -> None:
            leak_guard(text, forbidden, rel)
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = text.encode("utf-8")
            dst.write_bytes(data)
            meta = dict(meta)
            meta.update({
                "path": rel,
                "sha256": sha256_bytes(data),
                "bytes": len(data),
                "lines": text.count("\n"),
            })
            ordered = {k: meta[k] for k in (
                "path", "sha256", "bytes", "lines", "source_class", "source_ref",
                "codexhome_root", "kept_row_types", "dropped_row_types", "trim_rule",
            ) if k in meta}
            emitted.append(ordered)

        if out.exists():
            for child in sorted(out.rglob("*"), reverse=True):
                if child.name in {"golden.json", "README.md"}:
                    continue
                if child.is_file():
                    child.unlink()
                elif child.is_dir() and not any(child.iterdir()):
                    child.rmdir()

        # -- rollouts --------------------------------------------------------
        golden_stems = (
            "019f8d58-610c", "019f8d92-93fd", "019f8b2f-1617", "019f8a5c-8a8b",
            "019f8d1a-c148", "019f8d7f-9314", "019f8a63-ef67", "019f8d95-d818",
            "019f8b21-a6ae", "019f8b4d-1b95",
        )
        import_trims = {"019f8d58-610c": (60, 6), "019f8d58-606e": (40, 0)}
        import_hits = 0
        for src in native_files:
            imported = is_imported(src)
            if imported:
                import_hits += 1
            stem = next((s for s in import_trims if s in src.name), None)
            if imported and stem is None:
                continue
            sanitizer = Sanitizer(reg, import_sample=imported)
            stats = {"kept": {}, "dropped": {}}
            rows = rollout_rows(src, sanitizer, stats)
            rel = f"rollouts/native/{src.relative_to(codex_home)}"
            trim_rule = ""
            if imported:
                head, tail = import_trims[stem]
                rows, trim_rule = import_trim(rows, head, tail)
            else:
                body = "\n".join(rows)
                is_golden = any(g in src.name for g in golden_stems)
                if not is_golden and len(body.encode()) > 100 * 1024:
                    rows, trim_rule = trim_rows(rows, 40, 40)
            note_unknown(stats, sanitizer, unknown_row_types, unknown_payload_keys)
            write(rel, "\n".join(rows) + ("\n" if rows else ""), {
                "source_class": "import-trimmed" if imported else "native",
                "source_ref": src.name,
                "codexhome_root": "rollouts/native",
                "kept_row_types": dict(sorted(stats["kept"].items())),
                "dropped_row_types": dict(sorted(stats["dropped"].items())),
                "trim_rule": trim_rule,
            })

        for label, files in (("probe-0142", probe142_rollouts), ("probe-0152", probe152_rollouts)):
            for src in files:
                sanitizer = Sanitizer(reg)
                stats = {"kept": {}, "dropped": {}}
                rows = rollout_rows(src, sanitizer, stats)
                note_unknown(stats, sanitizer, unknown_row_types, unknown_payload_keys)
                write(f"rollouts/{label}/sessions/2026/09/02/{src.name}",
                      "\n".join(rows) + ("\n" if rows else ""), {
                          "source_class": f"probe-rollout-{label}",
                          "source_ref": src.name,
                          "codexhome_root": f"rollouts/{label}",
                          "kept_row_types": dict(sorted(stats["kept"].items())),
                          "dropped_row_types": dict(sorted(stats["dropped"].items())),
                          "trim_rule": "",
                      })

        # -- hook payload captures -------------------------------------------
        run8_sha = None
        for label, base, files in (("probe-0142", probe142, hook142), ("probe-0152", probe152, hook152)):
            extra = [p for p in (base / "notify-capture.jsonl", base / "exec-json-stream.jsonl") if p.exists()]
            for src in files + extra:
                sanitizer = Sanitizer(reg)
                lines = []
                kinds: dict[str, int] = {}
                for obj, raw in iter_json_lines(src):
                    if obj is None:
                        lines.append(dumps({"_unparseable_line_len": len(raw)}))
                        kinds["<unparseable>"] = kinds.get("<unparseable>", 0) + 1
                        continue
                    kind = obj.get("argv_event") or obj.get("type") or ("notify" if "argv" in obj else "?")
                    kinds[str(kind)] = kinds.get(str(kind), 0) + 1
                    lines.append(dumps(sanitizer.walk(obj)))
                note_unknown({"kept": {}, "dropped": {}}, sanitizer,
                             unknown_row_types, unknown_payload_keys)
                text = "\n".join(lines) + ("\n" if lines else "")
                if src.name == "capture-run8-compact.jsonl":
                    run8_sha = sha256_bytes(src.read_bytes())
                write(f"hooks/{label}/{src.name}", text, {
                    "source_class": f"hook-capture-{label}",
                    "source_ref": src.name,
                    "codexhome_root": f"rollouts/{label}",
                    "kept_row_types": dict(sorted(kinds.items())),
                    "dropped_row_types": {},
                    "trim_rule": "",
                })

        # -- hooks.json / hooks.state -----------------------------------------
        hooks_src = codex_home / "hooks.json"
        kept_entries = 0
        dropped_entries = 0
        if hooks_src.exists():
            data = json.loads(hooks_src.read_text(encoding="utf-8", errors="replace"))
            out_hooks: dict = {"hooks": {}}
            for event, groups in data.get("hooks", {}).items():
                new_groups = []
                for group in groups:
                    entries = []
                    for entry in group.get("hooks", []):
                        cmd = entry.get("command", "")
                        if "/hooks/ai-team-os/" not in cmd:
                            dropped_entries += 1
                            continue
                        kept_entries += 1
                        entries.append({**entry, "command": reg_command(cmd, reg)})
                    if entries:
                        ng = {k: v for k, v in group.items() if k != "hooks"}
                        ng["hooks"] = entries
                        new_groups.append(ng)
                if new_groups:
                    out_hooks["hooks"][event] = new_groups
            write("hooks/hooks.sample.json",
                  json.dumps(out_hooks, ensure_ascii=False, indent=1) + "\n", {
                      "source_class": "hooks-config",
                      "source_ref": hooks_src.name,
                      "kept_row_types": {"entries": kept_entries},
                      "dropped_row_types": {"third_party_entries": dropped_entries},
                      "trim_rule": "",
                  })

        cfg = codex_home / "config.toml"
        if cfg.exists():
            entries = parse_hooks_state(cfg.read_text(encoding="utf-8", errors="replace"), reg)
            write("hooks/hooks-state.sample.json",
                  json.dumps({"hooks_state": entries}, ensure_ascii=False, indent=1) + "\n", {
                      "source_class": "hooks-trust-state",
                      "source_ref": cfg.name,
                      "kept_row_types": {"entries": len(entries)},
                      "dropped_row_types": {},
                      "trim_rule": "",
                  })

        # -- state samples -----------------------------------------------------
        user_db = codex_home / "state_5.sqlite"
        if user_db.exists():
            write("state/threads.sample.json",
                  json.dumps(build_user_state(user_db, tmp, reg), ensure_ascii=False, indent=1) + "\n", {
                      "source_class": "state-db-user",
                      "source_ref": user_db.name,
                      "kept_row_types": {"threads_sampled": 6},
                      "dropped_row_types": {},
                      "trim_rule": "sampled",
                  })
        probe_db = probe152 / "evidence/state_5.sqlite"
        if probe_db.exists():
            write("state/threads.sample-0152.json",
                  json.dumps(build_probe_state(probe_db, tmp, reg), ensure_ascii=False, indent=1) + "\n", {
                      "source_class": "state-db-probe-0152",
                      "source_ref": probe_db.name,
                      "kept_row_types": {"threads_sampled": 3},
                      "dropped_row_types": {},
                      "trim_rule": "sampled",
                  })

        # -- OS events ---------------------------------------------------------
        if os_db.exists():
            for prefix, types in (("019f8d5f", ("cc.session_start",)), ("019f99a6", ("cc.tool_use",))):
                rows = os_events(os_db, tmp, prefix, types, reg)
                write(f"os-events/{prefix}.jsonl", "\n".join(rows) + ("\n" if rows else ""), {
                    "source_class": "os-event-log",
                    "source_ref": "aiteam.db",
                    "kept_row_types": {t: len(rows) for t in types},
                    "dropped_row_types": {},
                    "trim_rule": "",
                })

        # -- manifest ----------------------------------------------------------
        emitted.sort(key=lambda m: m["path"])
        manifest = {
            "fixture_set": "codex-evidence",
            "collected_on": "2026-09-02",
            "generator": {
                "script": "scripts/redact_codex_fixture.py",
                "sha256": sha256_bytes(Path(__file__).resolve().read_bytes()),
            },
            "codexhome_roots": {
                "rollouts/native": "user Codex home",
                "rollouts/probe-0142": "probe Codex home (cli 0.142.0)",
                "rollouts/probe-0152": "probe Codex home (cli 0.152.1)",
            },
            "model_placeholders": sorted(reg.models.values()),
            "import_samples": len(import_trims),
            "import_detect_hits": import_hits,
            "external_import_records": external_import_records(codex_home),
            "import_selfcheck_ok": import_hits == external_import_records(codex_home),
            "duplicate_skipped": {
                "hook-capture.jsonl": {
                    "reason": "byte-identical to capture-run8-compact.jsonl",
                    "sha256_of_source": run8_sha,
                }
            },
            "row_type_counts_scope": "redaction stage, before trim_rule is applied",
            "unknown_row_types": dict(sorted(unknown_row_types.items())),
            "unknown_payload_keys": dict(sorted(unknown_payload_keys.items())),
            "files": emitted,
        }
        text = json.dumps(manifest, ensure_ascii=False, indent=1) + "\n"
        leak_guard(text, forbidden, "MANIFEST.json")
        (out / "MANIFEST.json").write_bytes(text.encode("utf-8"))
        total = sum(m["bytes"] for m in emitted) + len(text.encode("utf-8"))
        print(json.dumps({"files": len(emitted), "total_bytes": total,
                          "import_hits": import_hits,
                          "over_100kb": [m["path"] for m in emitted if m["bytes"] > 100 * 1024]},
                         ensure_ascii=False))
        for m in emitted:
            print(f"{m['sha256']}  {m['bytes']:>8}  {m['path']}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def note_unknown(stats: dict, sanitizer: Sanitizer, rows: dict, keys: dict) -> None:
    """Account for row labels and payload keys this fixture set has not seen before."""
    for bucket in ("kept", "dropped"):
        for label, count in stats.get(bucket, {}).items():
            if label not in KNOWN_ROW_TYPES:
                rows[label] = rows.get(label, 0) + count
    for k, v in sanitizer.unknown_keys.items():
        keys[k] = keys.get(k, 0) + v


def reg_command(cmd: str, reg: Registry) -> str:
    def sub(match: re.Match) -> str:
        return reg.map_path(match.group(0))
    return re.sub(r"(?:/[^\s\"']+)+", sub, cmd)


def parse_hooks_state(text: str, reg: Registry) -> list[dict]:
    entries: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r'^\[hooks\.state\."(.+)"\]$', s)
        if m:
            current = {"key": reg.map_path(m.group(1).split(":")[0]) + ":" + ":".join(m.group(1).split(":")[1:]),
                       "trusted_hash": None, "enabled": None}
            entries.append(current)
            continue
        if s.startswith("[") and not s.startswith('[hooks.state."'):
            current = None
            continue
        if current is None:
            continue
        m = re.match(r'^(trusted_hash|enabled)\s*=\s*(.+)$', s)
        if m:
            raw = m.group(2).strip()
            current[m.group(1)] = raw.strip('"') if raw.startswith('"') else (raw == "true")
    return entries


THREAD_CONTENT_COLS = {"cwd", "title", "first_user_message", "preview", "git_origin_url",
                       "git_branch", "git_sha", "rollout_path", "name", "agent_path"}


def sanitize_thread(row: dict, reg: Registry) -> dict:
    s = Sanitizer(reg)
    out = {}
    for k, v in row.items():
        if v is None or isinstance(v, (int, float)):
            out[k] = v
        elif k == "model":
            out[k] = reg.map_model(v)
        elif k == "rollout_path":
            out[k] = reg.map_path(v)
        elif k in THREAD_CONTENT_COLS:
            out[k] = reg.map_path(v) if _PATH_RE.match(v) else f"<str:{len(v)}>"
        elif k == "source" and v.startswith("{"):
            try:
                out[k] = dumps(s.walk(json.loads(v)))
            except Exception:
                out[k] = f"<str:{len(v)}>"
        else:
            out[k] = s.string(k, v)
    return out


def build_user_state(db: Path, tmp: Path, reg: Registry) -> dict:
    con = open_ro(db, tmp)
    try:
        picks = [
            ("native-user", "thread_source='user' and archived=0 and cli_version like '0.145%'"),
            ("guardian", "source like '%guardian%'"),
            ("thread-spawn", "source like '%thread_spawn%'"),
            ("archived", "archived=1"),
            ("imported", "id like '019f8d58-610c%'"),
            ("cli-0146", "cli_version like '0.146%'"),
        ]
        samples = []
        for label, where in picks:
            rows = thread_rows(con, where, (), 1)
            if rows:
                samples.append({"sample": label, "row": sanitize_thread(rows[0], reg)})
        dist = [{"cli_version": a, "thread_source": b, "archived": c, "count": d}
                for a, b, c, d in con.execute(
                    "select cli_version, thread_source, archived, count(*) from threads "
                    "group by 1,2,3 order by 1,2,3")]
        edges = [{"parent_thread_id": p, "child_thread_id": c, "status": st}
                 for p, c, st in con.execute(
                     "select parent_thread_id, child_thread_id, status from thread_spawn_edges "
                     "order by parent_thread_id")]
        return {
            "schema": table_sql(con, ["threads", "thread_spawn_edges"]),
            "threads_sample": samples,
            "thread_spawn_edges": edges,
            "threads_distribution": dist,
            "threads_total": con.execute("select count(*) from threads").fetchone()[0],
        }
    finally:
        con.close()


def build_probe_state(db: Path, tmp: Path, reg: Registry) -> dict:
    con = open_ro(db, tmp)
    try:
        tables = sorted(r[0] for r in con.execute(
            "select name from sqlite_master where type='table'"))
        cols = [r[1] for r in con.execute("pragma table_info(threads)")]
        rows = thread_rows(con, "1=1", (), 3)
        migration_tables = [t for t in tables if t.startswith("rollout_migration")]
        return {
            "tables": tables,
            "threads_columns": cols,
            "threads_sample": [sanitize_thread(r, reg) for r in rows],
            "rollout_migration_schema": table_sql(con, migration_tables),
        }
    finally:
        con.close()


def os_events(db: Path, tmp: Path, prefix: str, types: tuple, reg: Registry) -> list[str]:
    con = open_ro(db, tmp)
    try:
        marks = ",".join("?" for _ in types)
        sql = ("select type, timestamp, data from events "
               f"where type in ({marks}) "
               "and json_extract(data, '$.session_id') like ? "
               "order by timestamp, id")
        rows = []
        s = Sanitizer(reg)
        for etype, ts, data in con.execute(sql, (*types, prefix + "%")):
            try:
                payload = json.loads(data)
            except Exception:
                payload = {"_unparseable_len": len(data)}
            clean = s.walk(payload)
            rows.append(dumps({
                "type": etype,
                "timestamp": ts,
                "session_id": payload.get("session_id"),
                "data": clean,
            }))
        return rows
    finally:
        con.close()


def external_import_records(codex_home: Path) -> int:
    p = codex_home / "external_agent_session_imports.json"
    if not p.exists():
        return -1
    try:
        return len(json.loads(p.read_text(encoding="utf-8", errors="replace")).get("records", []))
    except Exception:
        return -1


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codex-home",
                    default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--os-db", default=str(Path.home() / ".claude/data/ai-team-os/aiteam.db"))
    ap.add_argument("--out", default=str(repo / "tests" / "fixtures" / "codex"))
    args = ap.parse_args()
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
