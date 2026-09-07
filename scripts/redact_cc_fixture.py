#!/usr/bin/env python3
"""Deterministic redactor turning raw Claude Code hook captures into fixture rows.

Reads (never writes) one or more raw capture files and emits a `capture-<label>.jsonl`
into `tests/fixtures/cc-hooks/`, in the same row shape the synthetic corpus uses, so
`scripts/compute_cc_hook_golden.py` can consume both without knowing which is which.

Two raw sources exist, and they are NOT interchangeable:

* `--stage stdin` — recorded on the hook side, before `send_event.py` runs. Only these
  rows can drive segment 1 of the differential (hook-side processing equivalence).
* `--stage post_body` — recorded on the server side by the `AITEAM_HOOK_RAW_DUMP`
  switch in `api/routes/hooks.py`. These are already-processed POST bodies, so they
  can only drive segment 2 (server-side three-table replay). Feeding them through
  `send_event.py` again would double-apply the trimming and prove nothing.

What redaction does
-------------------
* Home directories, transcript paths and working directories are folded to stable
  placeholders, keeping the structural components the translator actually reads
  (`.claude/projects/...`, `subagents/workflows/wf_<id>/agent-<id>.jsonl`).
* Identifiers (session/agent/tool-use ids, team names) become stable placeholders
  assigned in first-seen order, keeping their original shape.
* Free-text values (`tool_input`, `tool_response`, `last_assistant_message`, prompts)
  are replaced by ASCII filler of the same **UTF-8 byte length**. Byte length is what
  decides the 32 KB / 50 KB payload gates, so those branches replay exactly; the
  500-character field truncation can only fire more often than in the original, never
  less. That asymmetry is recorded in the MANIFEST as a known distortion.
* Output is pure ASCII, asserted, and screened by `leak_guard` for e-mail addresses,
  URLs and API-key shapes before a single byte is written.

Determinism is not assumed: the transform runs twice over fresh registries and the
two results must be byte-identical before anything reaches disk.

Usage:
    python3 scripts/redact_cc_fixture.py --raw <raw.jsonl> --label session-a \\
        --stage stdin [--home ~] [--out tests/fixtures/cc-hooks]

Re-run `scripts/compute_cc_hook_golden.py` afterwards: MANIFEST and golden are its
outputs, not this script's, so that each file has exactly one writer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# --------------------------------------------------------------- constants

FILLER_UNIT = "cc-hook-filler."
SEPARATORS = (",", ":")

# Payload keys whose value is structural and survives verbatim.
KEEP_KEYS = {
    "hook_event_name", "tool_name", "status", "reason", "trigger", "source",
    "permission_mode", "model", "agent_type", "stop_hook_active", "matcher",
    "compact_summary_chars", "_stripped", "_original_size", "hook_stage",
    "captured_at", "argv_event", "pid", "case_id", "stage", "decision",
    "subtype", "task_status", "notification_type", "harness", "turn_id",
}

# Keys carrying an identifier that must stay shaped but never real.
SESSION_KEYS = {"session_id", "leader_session_id", "parent_session_id"}
AGENT_KEYS = {"agent_id", "cc_agent_id", "subagent_id"}
TOOLUSE_KEYS = {"tool_use_id", "cc_tool_use_id"}
TEAM_KEYS = {"cc_team_name", "team_name"}
PATH_KEYS = {
    "transcript_path", "agent_transcript_path", "cwd", "cwd_of_hook",
    "file_path", "path", "project_path", "worktree_path", "notebook_path",
}

# Keys whose value is free text; replaced by equal-byte-length ASCII filler.
CONTENT_KEYS = {
    "tool_input", "tool_response", "last_assistant_message", "prompt",
    "message", "command", "description", "content", "output", "input",
    "summary", "compact_summary", "error", "stdout", "stderr", "title",
    "additionalContext", "systemMessage", "permissionDecisionReason",
    "task_title", "task_description", "current_task", "system_prompt",
}

# Shapes that must never reach a tracked fixture file. The literal list is derived
# at runtime (machine-specific), these are the machine-independent complement.
FORBIDDEN_SHAPES = {
    "non_ascii": re.compile(r"[^\x00-\x7F]"),
    "email": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    "http_url": re.compile(r"https?://(?!localhost|127\.0\.0\.1)"),
    "api_key": re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    "bearer": re.compile(r"(?i)bearer[\s\"':=]+[A-Za-z0-9._\-]{16,}"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# Path components that are structure, not content, and are kept verbatim.
STRUCTURAL_PARTS = {
    ".claude", "projects", "subagents", "workflows", "teams", "data",
    "ai-team-os", "hooks", "tmp", "var", "private",
}

_WF_RE = re.compile(r"^wf_[A-Za-z0-9_-]+$")
_AGENT_FILE_RE = re.compile(r"^agent-(.+)\.jsonl$")
_UUID_FILE_RE = re.compile(r"^[0-9a-fA-F-]{8,}\.jsonl$")
_SAFE_KEY_RE = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")


def dumps(obj) -> str:
    """Compact, stable JSON encoding used for every emitted line."""
    return json.dumps(obj, ensure_ascii=False, separators=SEPARATORS, sort_keys=False)


def filler(n_bytes: int) -> str:
    """ASCII filler of exactly `n_bytes` bytes (ASCII, so bytes == characters)."""
    if n_bytes <= 0:
        return ""
    reps = n_bytes // len(FILLER_UNIT) + 1
    return (FILLER_UNIT * reps)[:n_bytes]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------- registry


class Registry:
    """First-seen-order placeholder assignment. Same input order, same output."""

    def __init__(self, home: str):
        self.home = home.rstrip("/") or "/"
        self.sessions: dict[str, str] = {}
        self.agents: dict[str, str] = {}
        self.tooluses: dict[str, str] = {}
        self.teams: dict[str, str] = {}
        self.dirs: dict[str, str] = {}
        self.workflows: dict[str, str] = {}

    @staticmethod
    def _uuid(n: int) -> str:
        return f"00000000-0000-4000-8000-{n:012d}"

    def session(self, raw: str) -> str:
        if not raw:
            return raw
        return self.sessions.setdefault(raw, self._uuid(len(self.sessions) + 1))

    def agent(self, raw: str) -> str:
        if not raw:
            return raw
        return self.agents.setdefault(raw, self._uuid(900000 + len(self.agents) + 1))

    def tooluse(self, raw: str) -> str:
        if not raw:
            return raw
        return self.tooluses.setdefault(raw, f"toolu_{len(self.tooluses) + 1:016d}")

    def team(self, raw: str) -> str:
        if not raw:
            return raw
        return self.teams.setdefault(raw, f"cc-team-{len(self.teams) + 1}")

    def directory(self, raw: str) -> str:
        return self.dirs.setdefault(raw, f"dir-{len(self.dirs) + 1}")

    def workflow(self, raw: str) -> str:
        return self.workflows.setdefault(raw, f"wf_{len(self.workflows) + 1:08d}")

    def path(self, raw: str) -> str:
        """Fold a filesystem path, keeping only components the OS reads."""
        if not raw or not isinstance(raw, str):
            return raw
        rest = raw
        prefix = ""
        if rest == self.home or rest.startswith(self.home + "/"):
            prefix = "/home/user"
            rest = rest[len(self.home):]
        elif rest.startswith("~"):
            prefix = "/home/user"
            rest = rest[1:]
        elif rest.startswith("/"):
            prefix = ""
        else:
            # relative path: fold wholesale, it carries no structure the OS reads
            return self.directory(raw)
        parts = [p for p in rest.split("/") if p]
        out: list[str] = []
        for part in parts:
            if part in STRUCTURAL_PARTS:
                out.append(part)
            elif _WF_RE.match(part):
                out.append(self.workflow(part))
            elif (m := _AGENT_FILE_RE.match(part)) is not None:
                out.append(f"agent-{self.agent(m.group(1))}.jsonl")
            elif _UUID_FILE_RE.match(part):
                out.append(f"{self.session(part[:-len('.jsonl')])}.jsonl")
            else:
                out.append(self.directory(part))
        folded = prefix + ("/" + "/".join(out) if out else "")
        return folded or "/"


# --------------------------------------------------------------- sanitizer


class Sanitizer:
    def __init__(self, reg: Registry):
        self.reg = reg
        self.unknown_keys: dict[str, int] = {}

    def key(self, k: str) -> str:
        return k if _SAFE_KEY_RE.fullmatch(k) else f"<key:{len(k)}>"

    def string(self, key: str | None, s: str) -> str:
        if key in SESSION_KEYS:
            return self.reg.session(s)
        if key in AGENT_KEYS:
            return self.reg.agent(s)
        if key in TOOLUSE_KEYS:
            return self.reg.tooluse(s)
        if key in TEAM_KEYS:
            return self.reg.team(s)
        if key in PATH_KEYS:
            return self.reg.path(s)
        if key in CONTENT_KEYS:
            return filler(len(s.encode("utf-8")))
        if key in KEEP_KEYS:
            return s if s.isascii() and len(s) <= 200 else filler(len(s.encode("utf-8")))
        if key is not None:
            self.unknown_keys[key] = self.unknown_keys.get(key, 0) + 1
        # Unknown key: treat as content. Under-redacting an unknown key is the one
        # mistake this script must never make.
        return filler(len(s.encode("utf-8")))

    def walk(self, obj, key: str | None = None):
        if isinstance(obj, dict):
            # A nested dict under a content key keeps its schema keys (tool argument
            # names are structural) while every leaf string is still filled.
            return {self.key(k): self.walk(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            return self.string(key, obj)
        return obj


# --------------------------------------------------------------- leak guard


def leak_guard(text: str, forbidden: dict[str, str], where: str) -> None:
    """Refuse to write anything carrying a known literal or a forbidden shape.

    Never echoes the offending text: a leak report must not itself leak.
    """
    for label, needle in forbidden.items():
        if needle and needle in text:
            raise RuntimeError(
                f"leak guard tripped in {where}: {label} at offset {text.index(needle)} "
                f"(context length {len(text)})"
            )
    for label, shape in FORBIDDEN_SHAPES.items():
        hit = shape.search(text)
        if hit:
            raise RuntimeError(
                f"leak guard tripped in {where}: {label} at offset {hit.start()} "
                f"(match length {len(hit.group(0))})"
            )


# --------------------------------------------------------------- transform


def redact_rows(raw_lines: list[str], home: str, stage: str, label: str) -> tuple[list[str], dict]:
    """Turn raw capture lines into fixture rows. Pure function of its arguments."""
    reg = Registry(home)
    san = Sanitizer(reg)
    rows: list[str] = []
    events: dict[str, int] = {}

    for index, line in enumerate(raw_lines):
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        # A hook-side recorder wraps the payload; a server-side dump *is* the payload.
        payload = record.get("payload", record)
        argv_event = record.get("argv_event") or payload.get("hook_event_name") or "unknown"
        clean = san.walk(payload)
        cwd_of_hook = record.get("cwd_of_hook")
        row = {
            "case_id": f"{label}-{index + 1:04d}",
            "origin": "capture",
            "stage": stage,
            "argv_event": argv_event,
            "captured_at": record.get("captured_at"),
            "pid": record.get("pid"),
            "cwd_of_hook": reg.path(cwd_of_hook) if isinstance(cwd_of_hook, str) else None,
            "branches": [],
            "payload": clean,
        }
        rows.append(dumps(row))
        events[argv_event] = events.get(argv_event, 0) + 1

    meta = {
        "rows": len(rows),
        "events": dict(sorted(events.items())),
        "unknown_keys": dict(sorted(san.unknown_keys.items())),
        "placeholders": {
            "sessions": len(reg.sessions), "agents": len(reg.agents),
            "tool_uses": len(reg.tooluses), "teams": len(reg.teams),
            "directories": len(reg.dirs), "workflows": len(reg.workflows),
        },
    }
    return rows, meta


def build(args: argparse.Namespace) -> int:
    home = str(Path(args.home).expanduser().resolve())
    raw_lines: list[str] = []
    for raw_path in sorted(Path(p) for p in args.raw):
        raw_lines.extend(raw_path.read_text(encoding="utf-8", errors="replace").splitlines())

    rows, meta = redact_rows(raw_lines, home, args.stage, args.label)
    rows_again, _ = redact_rows(raw_lines, home, args.stage, args.label)
    if rows != rows_again:
        print("determinism check failed: two runs over the same input differ", file=sys.stderr)
        return 2

    text = "".join(line + "\n" for line in rows)
    forbidden = {"home": home, "user": Path(home).name}
    leak_guard(text, forbidden, f"capture-{args.label}.jsonl")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"capture-{args.label}.jsonl"
    out_file.write_text(text, encoding="utf-8")

    print(json.dumps({
        "written": str(out_file),
        "sha256": sha256_text(text),
        "bytes": len(text.encode("utf-8")),
        **meta,
    }, ensure_ascii=False, indent=1))
    print("\nNext: python3 scripts/compute_cc_hook_golden.py --write "
          "(MANIFEST and golden are its outputs, and both must be refreshed)", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", nargs="+", required=True, help="raw capture jsonl file(s)")
    ap.add_argument("--label", required=True, help="fixture label, e.g. session-a")
    ap.add_argument("--stage", choices=("stdin", "post_body"), required=True,
                    help="stdin = hook-side recording; post_body = server-side dump")
    ap.add_argument("--home", default="~", help="home directory literal to fold away")
    ap.add_argument("--out", default="tests/fixtures/cc-hooks", help="fixture directory")
    return build(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
