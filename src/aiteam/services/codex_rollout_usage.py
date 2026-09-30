"""Codex request usage for one owning thread, independent of account/provider.

Contract and lifecycle boundaries: docs/codex-usage-parser-design.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import aiteam
from aiteam.services import codex_local_usage as local
from aiteam.services.codex_usage_export import _unique_object

_FIELDS = local._FIELDS
_MAX_BYTES = 512 * 1024 * 1024
_MAX_FILES = 40000
_MAX_SECONDS = 20.0
_WORKERS = asyncio.Semaphore(2)
_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


class CodexRolloutUsageError(ValueError):
    """Only static reason codes; never return transcript text in diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str):
    raise CodexRolloutUsageError(code)


def _configured_codex_home(codex_home: str | Path | None = None) -> Path:
    selected = codex_home if codex_home is not None else os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    return Path(selected).expanduser()


def resolve_codex_home(codex_home: str | Path | None = None) -> Path:
    """Canonicalize the configured root only; internal paths remain NOFOLLOW."""
    return _configured_codex_home(codex_home).resolve()


def _source_path(path: str | Path | None, codex_home: str | Path | None) -> Path | None:
    if not path:
        return None
    source = Path(os.path.abspath(Path(path).expanduser()))
    home = resolve_codex_home(codex_home)
    roots = [codex_home, os.environ.get("CODEX_HOME"), Path.home() / ".codex"]
    for root in roots:
        if root is None:
            continue
        lexical_root = Path(os.path.abspath(Path(root).expanduser()))
        if lexical_root.resolve() != home:
            continue
        try:
            relative = source.relative_to(lexical_root)
        except ValueError:
            continue
        return home / relative
    return source


def _read_rows(path: Path) -> list[dict]:
    try:
        # Refuse symbolic links, including ancestors. Reuse the bounded reader's
        # filesystem boundary without reusing its account/provider restriction.
        descriptor = local._open_directory(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                _fail("unreadable")
            if before.st_size > _MAX_BYTES:
                _fail("limit")
            rows = []
            read_bytes = 0
            deadline = time.monotonic() + _MAX_SECONDS
            for raw in iter(lambda: stream.readline(local._MAX_LINE_BYTES + 1), b""):
                read_bytes += len(raw)
                if read_bytes > _MAX_BYTES or len(raw) > local._MAX_LINE_BYTES or time.monotonic() > deadline:
                    _fail("limit")
                if not raw.endswith(b"\n"):
                    break  # writer's partial tail is not a completed event
                if not raw.strip():
                    continue
                row = json.loads(raw, object_pairs_hook=_unique_object)
                if not isinstance(row, dict):
                    _fail("invalid_log")
                # Never retain conversation text in memory or IPC.
                kind, payload = row.get("type"), row.get("payload")
                if kind == "session_meta":
                    if not isinstance(payload, dict):
                        _fail("invalid_log")
                    rows.append(
                        {
                            "timestamp": row.get("timestamp"),
                            "type": kind,
                            "payload": {
                                key: payload.get(key)
                                for key in ("id", "session_id", "forked_from_id", "parent_thread_id", "source")
                            },
                        }
                    )
                elif kind == "token_usage_record" or (
                    kind == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count"
                ):
                    rows.append(row)
                elif kind == "turn_context" and isinstance(payload, dict):
                    rows.append({"type": kind, "payload": {"turn_id": payload.get("turn_id")}})
            after = os.fstat(stream.fileno())
            # An append during a live read is legitimate; replacement/truncation or
            # equal-sized rewriting is not a coherent observation.
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or after.st_size < before.st_size:
                _fail("source_changed")
            if after.st_size == before.st_size and local._stat_signature(before) != local._stat_signature(after):
                _fail("source_changed")
            return rows
    except CodexRolloutUsageError:
        raise
    except (OSError, local.CodexLocalUsageError):
        _fail("unreadable")
    except (ValueError, RecursionError):
        _fail("invalid_log")


def _meta(rows: list[dict]) -> dict:
    return next((r["payload"] for r in rows if r["type"] == "session_meta"), {})


def _session_id(rows: list[dict]) -> str | None:
    meta = _meta(rows)
    return local._text(meta.get("id")) or local._text(meta.get("session_id"))


def _candidate_paths(home: Path, session_id: str, supplied_name: str | None = None):
    deadline = time.monotonic() + _MAX_SECONDS
    count = 0
    for name in ("sessions", "archived_sessions"):
        root = home / name
        if not root.exists():
            continue
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(folder) / d).is_symlink()]
            count += len(dirs) + len(files)
            if count > _MAX_FILES or time.monotonic() > deadline:
                _fail("limit")
            for filename in files:
                if filename.endswith(".jsonl") and (
                    f"-{session_id}" in filename
                    or filename == f"{session_id}.jsonl"
                    or (supplied_name is not None and filename == supplied_name)
                ):
                    yield Path(folder) / filename


def _load_rollouts(
    session_id: str, transcript_path: str | Path | None, home: Path
) -> tuple[Path, list[dict], int] | None:
    if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
        _fail("invalid_session_id")
    supplied = Path(transcript_path) if transcript_path else None
    candidates = list(_candidate_paths(home, session_id, supplied.name if supplied else None))
    if supplied and supplied.exists() and supplied not in candidates:
        candidates.insert(0, supplied)
    matches = []
    for path in candidates:
        rows = _read_rows(path)
        if _session_id(rows) == session_id:
            matches.append((path, rows))
    if not matches:
        return None
    own_meta = [next(r for r in rows if r["type"] == "session_meta") for _, rows in matches]
    first_meta = min(own_meta, key=lambda r: local._timestamp(r["timestamp"]))
    forks = {local._text(r["payload"].get("forked_from_id")) for r in own_meta}
    if len(forks) > 1:
        _fail("conflicting_rollouts")
    ledger_mode = any(r["type"] == "token_usage_record" for _, rows in matches for r in rows)
    winner = next((path for path, _ in matches if supplied and path.name == supplied.name), None)
    if winner is None:
        winner = max(matches, key=lambda item: len(item[1]))[0]
    if ledger_mode:
        bodies = [[r for r in rows if r["type"] != "session_meta"] for _, rows in matches]
        prefix_copies = all(
            left[: min(len(left), len(right))] == right[: min(len(left), len(right))]
            for left in bodies
            for right in bodies
        )
        if len(matches) > 1 and not prefix_copies:
            # Segments can only be joined by a provable owning cumulative chain.
            for _, rows in matches:
                for row in rows:
                    if row["type"] == "token_usage_record" and row["payload"].get("thread_id") in (None, session_id):
                        if row["payload"].get("thread_token_usage") is None:
                            _fail("conflicting_rollouts")
        body = []
        identical = set()
        for _, rows in matches:
            for row in rows:
                if row["type"] == "session_meta":
                    continue
                key = json.dumps(row, sort_keys=True, separators=(",", ":"))
                if key in identical:
                    continue
                identical.add(key)
                body.append(row)
        # Turn contexts only supply import detection; ordering them is immaterial.
        body.sort(
            key=lambda r: (
                local._timestamp(r["timestamp"]) if "timestamp" in r else local._timestamp(first_meta["timestamp"])
            )
        )
        rows = [first_meta, *body]
    else:
        rows = matches[0][1]
        for _, other in matches[1:]:
            left = [r for r in rows if r["type"] != "session_meta"]
            right = [r for r in other if r["type"] != "session_meta"]
            n = min(len(left), len(right))
            if left[:n] != right[:n]:
                _fail("conflicting_rollouts")
            if len(right) > len(left):
                rows = other
        rows = [first_meta, *[r for r in rows if r["type"] != "session_meta"]]
    return winner, rows, len(matches)


def resolve_codex_rollout_path(
    session_id: str,
    transcript_path: str | Path | None = None,
    codex_home: str | Path | None = None,
) -> Path | None:
    """Locate identity, including exact archived basenames and suffixed segments."""
    home = resolve_codex_home(codex_home)
    found = _load_rollouts(session_id, _source_path(transcript_path, codex_home), home)
    return found[0] if found else None


def _values(raw: object) -> tuple[int, ...]:
    if not isinstance(raw, dict) or "cached_input_tokens" not in raw:
        _fail("missing_fields")
    try:
        values = local._usage(raw).values
    except local.CodexLocalUsageError as exc:
        _fail(exc.code)
    if values[1] + values[2] > values[0]:
        _fail("invalid_log")
    return values


def _add(total: tuple[int, ...], values: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(a + b for a, b in zip(total, values, strict=True))


def _is_phantom(raw: object) -> bool:
    return (
        isinstance(raw, dict)
        and all(raw.get(k) == 0 for k in _FIELDS)
        and type(raw.get("total_tokens")) is int
        and raw["total_tokens"] > 0
    )


def _legacy_rows(rows: list[dict]) -> list[tuple]:
    events = []
    previous = None
    for row in rows:
        p = row["payload"]
        if row["type"] != "event_msg" or p.get("type") != "token_count":
            continue
        info = p.get("info")
        if info is None:
            continue
        if not isinstance(info, dict):
            _fail("invalid_log")
        total, last = info.get("total_token_usage"), info.get("last_token_usage")
        if _is_phantom(total):
            # Recognized context-window sentinel contains no usage measurement.
            if total["total_tokens"] != info.get("model_context_window"):
                _fail("import_ambiguous")
            continue
        if total is None:
            if last is not None:
                _fail("unanchored_legacy")
            _fail("missing_fields")
        current = _values(total)
        if current == previous:
            continue
        if _is_phantom(last):
            last = None  # only a cumulative delta may establish this request
        values = _values(last) if last is not None else None
        events.append((local._timestamp(row.get("timestamp")), current, values, total))
        previous = current
    return events


def _parse(
    path: Path,
    expected_session_id: str,
    home: Path,
    ancestors: set[str],
    loaded: tuple[Path, list[dict], int] | None = None,
) -> dict | None:
    if expected_session_id in ancestors:
        _fail("fork_cycle")
    if len(ancestors) >= 32:
        _fail("limit")
    loaded = loaded or _load_rollouts(expected_session_id, path, home)
    if loaded is None:
        return None
    path, rows, source_count = loaded
    session_id = _session_id(rows)
    if session_id != expected_session_id:
        _fail("identity_mismatch")
    meta = _meta(rows)
    meta_row = next(r for r in rows if r["type"] == "session_meta")
    started = local._timestamp(meta_row.get("timestamp"))
    fork = local._text(meta.get("forked_from_id"))
    if fork == session_id:
        _fail("fork_cycle")
    source = meta.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawned = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    child = (
        isinstance(spawned, dict)
        or bool(meta.get("parent_thread_id"))
        or (isinstance(source, dict) and "subagent" in source)
    )
    if any(
        r["type"] == "turn_context"
        and isinstance(r["payload"].get("turn_id"), str)
        and r["payload"]["turn_id"].startswith("external-import-turn-")
        for r in rows
    ):
        _fail("import_ambiguous")
    ledgers = []
    present_creation = present_reasoning = True
    seen: dict[str, tuple] = {}
    inherited = 0
    running = (0,) * 5
    baseline = (0,) * 5
    anchors = []
    parent_usage = None
    for row in rows:
        if row["type"] != "token_usage_record":
            continue
        p = row["payload"]
        if not isinstance(p, dict):
            _fail("invalid_log")
        owner = local._text(p.get("thread_id"))
        if owner is not None and owner != session_id:
            inherited += 1
            continue
        if owner is None and (fork or child):
            _fail("unidentified_owner")
        request_id = local._text(p.get("response_id"))
        if request_id is None:
            _fail("missing_fields")
        raw = p.get("usage")
        values = _values(raw)
        timestamp = local._timestamp(row.get("timestamp"))
        anchor = _values(p["thread_token_usage"]) if p.get("thread_token_usage") is not None else None
        snapshot = (timestamp, values, owner, anchor)
        if request_id in seen:
            if seen[request_id] != snapshot:
                _fail("conflict")
            continue
        seen[request_id] = snapshot
        if not ledgers and anchor is not None and anchor != values:
            try:
                inherited_baseline = local._Usage(anchor).subtract(local._Usage(values)).values
            except local.CodexLocalUsageError as exc:
                _fail(exc.code)
            if not fork:
                _fail("ledger_prefix_missing")
            parent_loaded = _load_rollouts(fork, None, home)
            if parent_loaded is None:
                _fail("fork_parent_missing")
            parent_usage = _parse(parent_loaded[0], fork, home, ancestors | {session_id}, loaded=parent_loaded)
            if parent_usage is None or not any(
                total == inherited_baseline and at <= started for at, total in parent_usage["_anchors"]
            ):
                _fail("fork_baseline_mismatch")
            baseline = inherited_baseline
        running = _add(running, values)
        expected_anchor = _add(baseline, running)
        if anchor is not None and anchor != expected_anchor:
            _fail("ledger_prefix_missing")
        if anchor is not None:
            anchors.append((timestamp, anchor))
        present_creation &= "cache_write_input_tokens" in raw
        present_reasoning &= "reasoning_output_tokens" in raw
        ledgers.append(snapshot)
    legacy = _legacy_rows(rows)
    diagnostics = Counter({"inherited_records_skipped": inherited, "rollout_sources": source_count})
    if baseline != (0,) * 5:
        diagnostics["fork_baseline_verified"] = 1
    if ledgers:
        # Each changing old snapshot needs one matching request BEFORE it. Matching
        # is chronological and consumes a request once: repeated equal-sized calls
        # remain distinct; duplicate cumulative events were removed above.
        cursor = 0
        for at, _total, last, _raw in legacy:
            if last is None:
                _fail("mixed_uncovered_legacy")
            while cursor < len(ledgers) and ledgers[cursor][0] <= at and ledgers[cursor][1] != last:
                cursor += 1
            if cursor >= len(ledgers) or ledgers[cursor][1] != last or ledgers[cursor][0] > at:
                # Copied old history before child creation belongs to its parent;
                # newer ownerless old rows must never be silently discarded.
                if fork and at < started:
                    diagnostics["inherited_legacy_skipped"] += 1
                    continue
                _fail("mixed_uncovered_legacy")
            cursor += 1
        values = running
        calls = len(ledgers)
    else:
        if not legacy:
            return None
        if fork:
            parent_path = resolve_codex_rollout_path(fork, codex_home=home)
            if parent_path is None:
                _fail("fork_parent_missing")
            if fork in ancestors | {session_id}:
                _fail("fork_cycle")
            lineage = ancestors | {session_id}
            ancestor_id, ancestor_path = fork, parent_path
            for _ in range(32):
                if ancestor_id in lineage:
                    _fail("fork_cycle")
                lineage.add(ancestor_id)
                ancestor_meta = _meta(_read_rows(ancestor_path))
                ancestor_id = local._text(ancestor_meta.get("forked_from_id"))
                if not ancestor_id:
                    break
                ancestor_path = resolve_codex_rollout_path(ancestor_id, codex_home=home)
                if ancestor_path is None:
                    _fail("fork_parent_missing")
            else:
                _fail("limit")
            parent_rows = _read_rows(parent_path)
            prefix = [event for event in _legacy_rows(parent_rows) if event[0] < started]
            if not prefix or legacy[: len(prefix)] != prefix:
                _fail("fork_prefix_mismatch")
            # Prefix is an inherited cumulative anchor; retain its last value as
            # baseline while feeding the shared cumulative validator.
            own = legacy[len(prefix) :]
            if not own:
                return None
            legacy = [prefix[-1]] + own
            skip = 1
            diagnostics["inherited_legacy_skipped"] += len(prefix)
        else:
            skip = 0
            if child and (legacy[0][0] < started or legacy[0][2] != legacy[0][1]):
                _fail("ambiguous_legacy_child")
            if legacy[0][2] is not None and legacy[0][2] != legacy[0][1]:
                _fail("legacy_prefix_missing")
        previous_total = None
        for _at, total, last, _raw in legacy:
            try:
                delta = (
                    local._Usage(total).subtract(local._Usage(previous_total))
                    if previous_total is not None
                    else local._Usage(total)
                )
            except local.CodexLocalUsageError as exc:
                _fail(exc.code)
            if last is not None and delta.values != last:
                # The last request is not the whole interval. Never use it to
                # mark an incomplete legacy stream as a complete measurement.
                # A copied fork anchor is not itself an owned interval.
                if previous_total is not None or not skip:
                    _fail("legacy_gap")
            previous_total = total
        session = local._Session(session_id=session_id)
        for at, total, last, _raw in legacy:
            session.legacy.append(
                local._Legacy(
                    at,
                    dict(zip(_FIELDS, total, strict=True)),
                    dict(zip(_FIELDS, last, strict=True)) if last is not None else None,
                    None,
                )
            )
        budget = local._Budget(threading.Event())
        try:
            increments = local._increments(session, budget)
        except local.CodexLocalUsageError as exc:
            _fail(exc.code)
        increments = increments[skip:]
        anchors = [(at, total) for at, total, _last, _raw in legacy]
        values = (0,) * 5
        for item in increments:
            values = _add(values, item.usage.values)
        calls = len(increments)
        # _increments intentionally skips genuine all-zero requests. A completed
        # zero cumulative anchor still proves a measured zero, not no data.
        if not calls and not skip and len(legacy) == 1 and legacy[0][1] == (0,) * 5:
            calls = 1
        present_creation = all("cache_write_input_tokens" in event[3] for event in legacy)
        present_reasoning = all("reasoning_output_tokens" in event[3] for event in legacy)
    return {
        "input_tokens": values[0] - values[1] - values[2],
        "output_tokens": values[3],
        "cache_read_tokens": values[1],
        "cache_creation_tokens": values[2] if present_creation else None,
        "reasoning_output_tokens": values[4] if present_reasoning else None,
        "api_calls": calls,
        "session_id": session_id,
        "transcript_path": str(path),
        "diagnostics": dict(diagnostics),
        "_anchors": anchors,
    }


def parse_codex_rollout_usage(
    path: str | Path | None,
    *,
    expected_session_id: str,
    codex_home: str | Path | None = None,
) -> dict | None:
    """Sync CLI/test reader. HTTP handlers must use the subprocess async wrapper."""
    home = resolve_codex_home(codex_home)
    try:
        loaded = _load_rollouts(expected_session_id, _source_path(path, codex_home), home)
        if loaded is None:
            return None
        result = _parse(loaded[0], expected_session_id, home, set(), loaded=loaded)
        if result is not None:
            result.pop("_anchors", None)
        return result
    except local.CodexLocalUsageError as exc:
        _fail(exc.code)


async def _read_subprocess(
    path: str | Path | None,
    *,
    expected_session_id: str,
    codex_home: str | Path | None = None,
    timeout: float = 30.0,
) -> dict | None:
    """Run disk IO and JSON parsing off the API process/GIL, with bounded lifetime."""
    package_root = Path(aiteam.__file__).absolute().parent.parent
    environment = dict(os.environ)
    pythonpath = environment.get("PYTHONPATH", "")
    # Python's own reading: an unset or empty PYTHONPATH adds nothing; only an empty
    # segment of a non-empty value means the current directory.
    inherited_paths = (
        [str(Path(entry or ".").absolute()) for entry in pythonpath.split(os.pathsep)] if pythonpath else []
    )
    environment["PYTHONPATH"] = os.pathsep.join([str(package_root), *inherited_paths])
    args = [
        sys.executable,
        "-P",  # Python 3.12: do not prepend a changed cwd before the pinned package.
        "-m",
        __name__,
        expected_session_id,
        # Only lexical normalization on the API process. Canonical root lookup
        # (including lstat) stays inside the worker's synchronous parser.
        str(Path(path).expanduser().absolute()) if path else "",
        str(_configured_codex_home(codex_home).absolute()),
    ]
    process = await asyncio.create_subprocess_exec(
        *args, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode or len(stdout) > 65536:
        _fail("worker_failed")
    result = json.loads(stdout)
    if "error" in result:
        _fail(result["error"])
    return result["usage"]


async def read_codex_rollout_usage(
    path: str | Path | None,
    *,
    expected_session_id: str,
    codex_home: str | Path | None = None,
    timeout: float = 30.0,
) -> dict | None:
    """Bound concurrent parsers so multiple native actors do not exhaust the host."""
    async with _WORKERS:
        return await _read_subprocess(
            path, expected_session_id=expected_session_id, codex_home=codex_home, timeout=timeout
        )


def _main() -> None:
    try:
        usage = parse_codex_rollout_usage(sys.argv[2] or None, expected_session_id=sys.argv[1], codex_home=sys.argv[3])
        print(json.dumps({"usage": usage}))
    except CodexRolloutUsageError as exc:
        print(json.dumps({"error": exc.code}))


if __name__ == "__main__":
    _main()
