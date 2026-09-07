#!/usr/bin/env python3
"""Compute the Claude Code hook zero-drift baseline (MANIFEST.json + golden.json).

The differential runs in two segments, because a single one would miss half the
surface that can drift:

**Segment 1 — hook-side processing.** Each corpus row whose ``stage`` is ``stdin``
is pushed through a real ``plugin/hooks/send_event.py`` subprocess whose API URL
points at a recording stub. What comes back is the exact POST body CC would have
sent. Rows the inert-tool guard drops produce no body at all, and that absence is
itself part of the baseline.

**Segment 2 — server-side ingestion.** The body sequence is replayed through
``HookTranslator`` against an in-memory database, then ``agents``,
``agent_activities`` and ``events`` are exported as a normalized snapshot.

Determinism, and how it is bought
---------------------------------
Three things in this pipeline are machine-dependent, and each is neutralized
rather than tolerated:

* the runner's working directory and home directory leak into the body through
  ``send_event``'s ``cwd`` injection — both literals are rewritten to fixed
  placeholders before anything is compared or stored;
* primary keys are random UUIDs — every UUID that is *not* already a corpus
  literal is replaced by an ordinal from one map shared by all three tables, so a
  row that ends up attached to the wrong agent still shows up as a difference;
* timestamps and durations are excluded by name, and rows are ordered by ``rowid``
  (insertion order) rather than by any time column.

The script refuses to write a baseline it cannot reproduce: it runs the whole
pipeline twice and compares before touching disk.

What "zero drift" means here
----------------------------
The normalized three-table snapshot equals the golden, and the columns added for
the Codex work are NULL. It does *not* mean no byte of behaviour may ever change:
a deliberate core fix re-computes the golden and the diff gets reviewed in the pull
request that makes it. What must never happen is a silent change.

Usage:
    python3 scripts/compute_cc_hook_golden.py --write    # refresh MANIFEST + golden
    python3 scripts/compute_cc_hook_golden.py --check    # recompute, compare, exit 1 on drift
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "cc-hooks"
HOOK = REPO_ROOT / "plugin" / "hooks" / "send_event.py"

CWD_PLACEHOLDER = "/workspace/cc-hook-cwd"
HOME_PLACEHOLDER = "/home/cc-hook-user"
FILLER_UNIT = "cc-hook-filler."
FILLER_DIRECTIVE = "$filler_bytes"

# The CC team the corpus expects to be resolvable, seeded into the temporary home
# so the cc_team_name injection branch fires deterministically.
SEEDED_TEAM_NAME = "cc-team-golden"
SEEDED_TEAM_DIR = "team-golden"

# Columns excluded from the comparison, with the reason each one cannot be compared.
EXCLUDED_COLUMNS: dict[str, dict[str, str]] = {
    "agents": {
        "id": "random UUID, replaced by an ordinal in the shared id map",
        "created_at": "wall clock",
        "last_active_at": "wall clock",
        "ctx_measured_at": "wall clock",
        "tokens_measured_at": "wall clock",
    },
    "agent_activities": {
        "id": "random UUID, replaced by an ordinal in the shared id map",
        "timestamp": "wall clock",
        "duration_ms": "measured elapsed time",
    },
    "events": {
        "id": "random UUID, replaced by an ordinal in the shared id map",
        "timestamp": "wall clock",
    },
}

# Columns the Codex work adds. Present or not, they must never carry a value on a
# pure-CC replay; the test asserts NULL when they exist and skips when they do not.
NEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "agents": ("harness", "harness_version", "dispatch_call_id", "reasoning_output_tokens"),
    "agent_activities": ("turn_id",),
}

TABLES = ("agents", "agent_activities", "events")

# Keys scrubbed from every JSON column: a timestamp nested inside `data` is just as
# unstable as one in a column of its own.
_TIME_KEY_RE = re.compile(r"(^|_)(at|ts|time|timestamp|duration_ms|elapsed|since)$")
# Unanchored on purpose: the `source` column stores "team:<uuid>" / "agent:<uuid>",
# so a full-string match would leave those primary keys raw and the baseline would
# differ on every run.
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# Branches of send_event.py the corpus is required to cover. A branch with no row
# is a hole in the baseline, so the build fails rather than quietly shrinking.
REQUIRED_BRANCHES = (
    "event_name_from_argv",
    "event_name_from_payload",
    "inert_dropped",
    "cwd_injected",
    "cc_team_injected",
    "cc_team_missing",
    "compact_summary_measured",
    "large_field_truncated",
    "large_field_dict_stringified",
    "tool_response_dict_truncated",
    "payload_stripped_32k",
    "all_strings_truncated_50k",
)

TRUNCATION_MARKER = "...(truncated)"
LARGE_FIELDS = ("last_assistant_message", "agent_transcript_path", "transcript_path")


# ------------------------------------------------------------------ helpers


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def filler(n_bytes: int) -> str:
    """ASCII filler of exactly `n_bytes` bytes. The one expansion rule, shared."""
    if n_bytes <= 0:
        return ""
    reps = n_bytes // len(FILLER_UNIT) + 1
    return (FILLER_UNIT * reps)[:n_bytes]


def expand(obj):
    """Expand `{"$filler_bytes": N}` directives so the corpus stays readable.

    Oversized payloads are what exercise the 32 KB and 50 KB gates, and a fixture
    holding 40 KB of literal filler is a fixture nobody reviews. The directive is
    expanded by this one function, which both the baseline builder and the tests
    call, so it is not a second source of truth.
    """
    if isinstance(obj, dict):
        if set(obj) == {FILLER_DIRECTIVE}:
            return filler(int(obj[FILLER_DIRECTIVE]))
        return {k: expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand(v) for v in obj]
    return obj


def corpus_files() -> list[Path]:
    """Synthetic rows first, then captures: adding a recording appends to the
    replay order instead of re-shuffling every row that came before it."""
    return sorted(FIXTURES.glob("synthetic-*.jsonl"), key=lambda p: p.name) + \
        sorted(FIXTURES.glob("capture-*.jsonl"), key=lambda p: p.name)


def load_corpus() -> list[dict]:
    """Every corpus row, in file-name then line order, with directives expanded."""
    rows: list[dict] = []
    for path in corpus_files():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            row["payload"] = expand(row["payload"])
            row["_file"] = path.name
            rows.append(row)
    return rows


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------- segment one


class _Recorder(BaseHTTPRequestHandler):
    bodies: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", "0"))
        _Recorder.bodies.append(self.rfile.read(length).decode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args) -> None:  # silence the default stderr access log
        return


def _seed_home(home: Path, session_id: str) -> None:
    """Seed one CC team so `_resolve_cc_team_name` has a deterministic hit."""
    team_dir = home / ".claude" / "teams" / SEEDED_TEAM_DIR
    team_dir.mkdir(parents=True, exist_ok=True)
    (team_dir / "config.json").write_text(
        json.dumps({"name": SEEDED_TEAM_NAME, "leadSessionId": session_id}), encoding="utf-8"
    )


def _normalize_body(body: str, runner_cwd: str, runner_home: str) -> str:
    """Rewrite the two machine-dependent literals send_event can inject."""
    return body.replace(runner_cwd, CWD_PLACEHOLDER).replace(runner_home, HOME_PLACEHOLDER)


def _observe_branches(payload: dict, body: dict | None) -> list[str]:
    """Which send_event branches this row actually took, read off the result."""
    seen: list[str] = []
    if body is None:
        return ["inert_dropped"]
    if "hook_event_name" not in payload:
        seen.append("event_name_from_argv")
    else:
        seen.append("event_name_from_payload")
    if "cwd" not in payload and "cwd" in body:
        seen.append("cwd_injected")
    # Only the two subagent events ever consult the CC teams directory; reporting
    # "missing" for a SessionStart would make the branch look covered everywhere.
    if body.get("hook_event_name") in ("SubagentStart", "SubagentStop") and "cc_team_name" not in payload:
        seen.append("cc_team_injected" if "cc_team_name" in body else "cc_team_missing")
    if "compact_summary" in payload and "compact_summary_chars" in body:
        seen.append("compact_summary_measured")
    if body.get("_stripped"):
        seen.append("payload_stripped_32k")
    for key in LARGE_FIELDS:
        value = body.get(key)
        if isinstance(value, str) and value.endswith(TRUNCATION_MARKER):
            if isinstance(payload.get(key), dict):
                seen.append("large_field_dict_stringified")
            else:
                seen.append("large_field_truncated")
    response = body.get("tool_response")
    if isinstance(response, dict) and any(
        isinstance(v, str) and v.endswith(TRUNCATION_MARKER) for v in response.values()
    ):
        seen.append("tool_response_dict_truncated")
    if any(
        isinstance(v, str) and v.endswith(TRUNCATION_MARKER) and len(v) == 200 + len(TRUNCATION_MARKER)
        for v in body.values()
    ):
        seen.append("all_strings_truncated_50k")
    return sorted(set(seen))


def segment_one(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Run every `stdin` row through the real hook and collect its POST body."""
    problems: list[str] = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    results: list[dict] = []
    try:
        with tempfile.TemporaryDirectory(prefix="cc-golden-home-") as home_dir, \
                tempfile.TemporaryDirectory(prefix="cc-golden-cwd-") as work_dir:
            home = Path(home_dir).resolve()
            work = Path(work_dir).resolve()
            seed_session = next(
                (r["payload"].get("session_id", "") for r in rows
                 if "cc_team_injected" in r.get("branches", [])),
                "",
            )
            _seed_home(home, seed_session)

            env = {**os.environ, "AITEAM_API_URL": f"http://127.0.0.1:{port}", "HOME": str(home)}
            env.pop("CLAUDE_PLUGIN_ROOT", None)  # never let the backup-chain yield fire
            env.pop("USERPROFILE", None)

            for row in rows:
                if row.get("stage") != "stdin":
                    results.append({**row, "body": None, "observed": [], "skipped": "not a stdin row"})
                    continue
                _Recorder.bodies.clear()
                proc = subprocess.run(
                    [sys.executable, str(HOOK), row["argv_event"]],
                    input=json.dumps(row["payload"], ensure_ascii=False),
                    capture_output=True, text=True, timeout=60, env=env, cwd=str(work),
                )
                if proc.returncode != 0:
                    problems.append(f"{row['case_id']}: hook exited {proc.returncode}: {proc.stderr.strip()}")
                if len(_Recorder.bodies) > 1:
                    problems.append(f"{row['case_id']}: hook posted {len(_Recorder.bodies)} bodies")
                raw = _Recorder.bodies[0] if _Recorder.bodies else None
                body_text = _normalize_body(raw, str(work), str(home)) if raw is not None else None
                body = json.loads(body_text) if body_text is not None else None
                observed = _observe_branches(row["payload"], body)
                declared = set(row.get("branches", []))
                missing = declared - set(observed)
                if missing:
                    problems.append(f"{row['case_id']}: declared branches never fired: {sorted(missing)}")
                results.append({**row, "body": body, "observed": observed})
    finally:
        server.shutdown()
        server.server_close()

    return results, problems


# ------------------------------------------------------------- segment two


def _scrub_json(value, id_map: dict[str, str], literals: set[str]):
    """Drop time-ish keys and map non-corpus UUIDs, recursively."""
    if isinstance(value, dict):
        return {
            k: _scrub_json(v, id_map, literals)
            for k, v in sorted(value.items())
            if not _TIME_KEY_RE.search(k)
        }
    if isinstance(value, list):
        return [_scrub_json(v, id_map, literals) for v in value]
    if isinstance(value, str):
        return _map_id(value, id_map, literals)
    return value


def _map_id(value: str, id_map: dict[str, str], literals: set[str]) -> str:
    """Replace every generated UUID with an ordinal, in or out of a longer string.

    UUIDs that came in with the corpus are left alone: they are stable input, and
    keeping them literal is what makes a golden diff readable.
    """
    def swap(match: re.Match) -> str:
        found = match.group(0)
        if found in literals:
            return found
        if found not in id_map:
            id_map[found] = f"id-{len(id_map) + 1:04d}"
        return id_map[found]

    return _UUID_RE.sub(swap, value)


async def _table_columns(session, table: str) -> list[str]:
    from sqlalchemy import text
    rows = (await session.execute(text(f"PRAGMA table_info({table})"))).fetchall()
    return [r[1] for r in rows]


async def segment_two(bodies: list[dict | None], literals: set[str]) -> tuple[dict, list[dict], dict]:
    """Replay the body sequence and export a normalized three-table snapshot.

    The third return value reports the Codex columns: how many non-NULL values
    each one holds, or None when the column does not exist on this branch. It is
    kept out of the snapshot on purpose, because it changes the moment those
    columns land and the baseline must not move for that reason.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from sqlalchemy import text

    from aiteam.api.event_bus import EventBus
    from aiteam.api.hook_translator import HookTranslator
    from aiteam.storage.connection import close_db, get_session
    from aiteam.storage.repository import StorageRepository

    repo = StorageRepository(db_url="sqlite+aiosqlite://")
    await repo.init_db()
    translator = HookTranslator(repo=repo, event_bus=EventBus(repo=repo))

    outcomes: list[dict] = []
    for body in bodies:
        if body is None:
            outcomes.append({"status": "not_posted"})
            continue
        result = await translator.handle_event(dict(body))
        outcomes.append({"status": str(result.get("status", ""))})

    snapshot: dict = {}
    id_map: dict[str, str] = {}
    async with get_session(repo._db_url) as session:
        for table in TABLES:
            schema = await _table_columns(session, table)
            compared = [c for c in schema
                        if c not in EXCLUDED_COLUMNS[table] and c not in NEW_COLUMNS.get(table, ())]
            select = ", ".join(f'"{c}"' for c in compared)
            raw_rows = (await session.execute(text(f"select {select} from {table} order by rowid"))).fetchall()
            rows = []
            for raw in raw_rows:
                row = {}
                for name, value in zip(compared, raw, strict=True):
                    if isinstance(value, str) and value.startswith(("{", "[")):
                        try:
                            value = _scrub_json(json.loads(value), id_map, literals)
                        except json.JSONDecodeError:
                            value = _map_id(value, id_map, literals)
                    elif isinstance(value, (dict, list)):
                        value = _scrub_json(value, id_map, literals)
                    elif isinstance(value, str):
                        value = _map_id(value, id_map, literals)
                    row[name] = value
                rows.append(row)
            # The live schema is deliberately NOT recorded: it grows the moment the
            # Codex columns land, which would make the baseline differ between
            # branches for a reason that has nothing to do with behaviour. What was
            # compared (`columns`) and what was skipped (`excluded`, NEW_COLUMNS) is
            # already stated, and the live schema is one PRAGMA away when debugging.
            snapshot[table] = {
                "columns": compared,
                "excluded": EXCLUDED_COLUMNS[table],
                "row_count": len(rows),
                "rows": rows,
                "sha256": sha256_text(canonical(rows)),
            }
        new_columns: dict[str, int | None] = {}
        for table, columns in NEW_COLUMNS.items():
            schema = await _table_columns(session, table)
            for column in columns:
                if column not in schema:
                    new_columns[f"{table}.{column}"] = None
                    continue
                count = (await session.execute(
                    text(f'select count(*) from {table} where "{column}" is not null')
                )).scalar_one()
                new_columns[f"{table}.{column}"] = int(count)
    await close_db()
    return snapshot, outcomes, new_columns


# ----------------------------------------------------------------- baseline


def build_baseline() -> tuple[dict, dict, list[str], dict]:
    rows = load_corpus()
    if not rows:
        return {}, {}, ["corpus is empty: no synthetic-*.jsonl found"], {}

    processed, problems = segment_one(rows)
    # Every UUID the corpus itself supplies, so replay does not renumber its input.
    literals: set[str] = set()
    for row in rows:
        for value in row["payload"].values():
            if isinstance(value, str):
                literals.update(_UUID_RE.findall(value))
    snapshot, outcomes, new_columns = asyncio.run(
        segment_two([p["body"] for p in processed], literals)
    )
    # A pure-CC replay must never write a Codex column. Present-and-populated is a
    # defect; present-and-empty and absent are both fine.
    for name, count in sorted(new_columns.items()):
        if count:
            problems.append(f"{name} holds {count} non-NULL values on a Claude Code replay")

    coverage: dict[str, dict] = {}
    for item, outcome in zip(processed, outcomes, strict=True):
        entry = coverage.setdefault(item["argv_event"], {"rows": 0, "branches": [], "handler_status": []})
        entry["rows"] += 1
        entry["branches"] = sorted(set(entry["branches"]) | set(item["observed"]))
        if outcome["status"] not in entry["handler_status"]:
            entry["handler_status"] = sorted(entry["handler_status"] + [outcome["status"]])

    covered = {b for e in coverage.values() for b in e["branches"]}
    for branch in REQUIRED_BRANCHES:
        if branch not in covered:
            problems.append(f"required send_event branch never covered by the corpus: {branch}")

    capture_files = [p.name for p in FIXTURES.glob("capture-*.jsonl")]
    manifest = {
        "fixture_set": "cc-hooks",
        "purpose": "CC zero-drift differential baseline for the Codex harness work",
        "generator": {
            "script": "scripts/compute_cc_hook_golden.py",
            "sha256": sha256_text(Path(__file__).read_text(encoding="utf-8")),
        },
        "redactor": {
            "script": "scripts/redact_cc_fixture.py",
            "sha256": sha256_text((REPO_ROOT / "scripts" / "redact_cc_fixture.py").read_text(encoding="utf-8")),
        },
        "hook_entry": {
            "path": "plugin/hooks/send_event.py",
            "sha256": sha256_text(HOOK.read_text(encoding="utf-8")),
            "frozen_by": "scripts/hook_entry_freeze.json",
        },
        "capture": {
            "status": "pending" if not capture_files else "present",
            "files": sorted(capture_files),
            "reason": (
                "Real recordings need the AITEAM_HOOK_RAW_DUMP switch and a restart of the "
                "running OS API (server side), or a hook-side wrapper that touches the CC "
                "registration surface. Neither was authorised for this batch. Tests skip the "
                "capture arm and print why rather than reporting a pass they did not earn."
            ),
            "stages": {
                "stdin": "hook-side recording; drives segment 1 and segment 2",
                "post_body": "server-side dump; already processed, so it drives segment 2 only",
            },
        },
        "normalization": {
            "cwd_placeholder": CWD_PLACEHOLDER,
            "home_placeholder": HOME_PLACEHOLDER,
            "filler_directive": FILLER_DIRECTIVE,
            "filler_unit": FILLER_UNIT,
            "id_map": "one map shared by all three tables, ordinals in row order",
            "excluded_columns": EXCLUDED_COLUMNS,
            "new_columns_asserted_null": NEW_COLUMNS,
        },
        "redaction_distortions": [
            "Free text is replaced by ASCII filler of equal UTF-8 byte length, so the 32 KB "
            "and 50 KB payload gates replay exactly. Byte length is at least character "
            "length, so the 500-character field truncation can fire on a redacted row that "
            "did not trip it originally, never the other way round.",
        ],
        "coverage": dict(sorted(coverage.items())),
        "required_branches": list(REQUIRED_BRANCHES),
        "files": {
            path.name: {
                "sha256": sha256_text(path.read_text(encoding="utf-8")),
                "bytes": len(path.read_bytes()),
                "lines": len([x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]),
            }
            for path in corpus_files()
        },
    }

    golden = {
        "generator": manifest["generator"],
        "manifest_sha256": sha256_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n"),
        "corpus_rows": len(rows),
        "bodies": [
            {"case_id": p["case_id"], "argv_event": p["argv_event"], "branches": p["observed"],
             "body": p["body"]}
            for p in processed
        ],
        "handler_status": [
            {"case_id": p["case_id"], "status": o["status"]}
            for p, o in zip(processed, outcomes, strict=True)
        ],
        "tables": snapshot,
    }
    return manifest, golden, problems, {"new_columns": new_columns}


def render(manifest: dict, golden: dict) -> tuple[str, str]:
    return (
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
        json.dumps(golden, ensure_ascii=False, indent=1) + "\n",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="recompute and write MANIFEST + golden")
    group.add_argument("--check", action="store_true", help="recompute and compare, never write")
    ap.add_argument("--once", action="store_true",
                    help="skip the second run (diagnosis only; a baseline is never written this way)")
    args = ap.parse_args()

    manifest, golden, problems, extras = build_baseline()
    if problems:
        for line in problems:
            print(f"error: {line}", file=sys.stderr)
        return 1

    if not args.once:
        manifest2, golden2, _, _extras2 = build_baseline()
        if render(manifest, golden) != render(manifest2, golden2):
            print("error: two runs produced different baselines; find the unstable field first",
                  file=sys.stderr)
            return 2

    manifest_text, golden_text = render(manifest, golden)
    if args.check:
        drift = False
        for name, text in (("MANIFEST.json", manifest_text), ("golden.json", golden_text)):
            path = FIXTURES / name
            if not path.exists():
                print(f"error: {name} missing", file=sys.stderr)
                drift = True
            elif path.read_text(encoding="utf-8") != text:
                print(f"error: {name} differs from the recomputed baseline", file=sys.stderr)
                drift = True
        print("ok: baseline reproduces" if not drift else "drift detected")
        return 1 if drift else 0

    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "MANIFEST.json").write_text(manifest_text, encoding="utf-8")
    (FIXTURES / "golden.json").write_text(golden_text, encoding="utf-8")
    print(json.dumps({
        "corpus_rows": golden["corpus_rows"],
        "events": sorted(manifest["coverage"]),
        "branches": sorted({b for e in manifest["coverage"].values() for b in e["branches"]}),
        "tables": {t: golden["tables"][t]["row_count"] for t in TABLES},
        "manifest_sha256": golden["manifest_sha256"],
        "codex_columns": extras["new_columns"],
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
