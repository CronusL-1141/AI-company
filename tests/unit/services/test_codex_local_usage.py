"""Local-only fixtures for bounded Codex usage readers."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aiteam.services import codex_local_usage as reader

START = datetime(2026, 9, 14, 10, tzinfo=UTC)
END = START + timedelta(minutes=10)


def at(minutes: int) -> str:
    return (START + timedelta(minutes=minutes)).isoformat()


def usage(tokens: int, output: int = 0, cached: int = 0, reasoning: int = 0) -> dict:
    return {
        "input_tokens": tokens, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
        "output_tokens": output, "reasoning_output_tokens": reasoning, "total_tokens": tokens + output,
    }


def meta(session: str = "parent", provider: str | None = "openai", parent: str | None = None,
         minute: int = -20) -> dict:
    return {"timestamp": at(minute), "type": "session_meta", "payload": {
        "id": session, "model_provider": provider, "parent_thread_id": parent,
    }}


def ledger(response: str, minute: int, tokens: int = 100, **kwargs: int) -> dict:
    return {"timestamp": at(minute), "type": "token_usage_record", "payload": {
        "response_id": response, "usage": usage(tokens, **kwargs),
    }}


def legacy(minute: int, total: int | None, last: int | None = None) -> dict:
    return {"timestamp": at(minute), "type": "event_msg", "payload": {
        "type": "token_count", "info": {
            "total_token_usage": usage(total) if total is not None else None,
            "last_token_usage": usage(last) if last is not None else None,
        },
    }}


def settings(minute: int, provider: object = "openai", *, present: bool = True) -> dict:
    return {"timestamp": at(minute), "type": "event_msg", "payload": {
        "type": "thread_settings_applied", "thread_settings": {"model_provider_id": provider} if present else {},
    }}


def log(home: Path, name: str, rows: list[dict], *, archived: bool = False, tail: bytes = b"") -> Path:
    folder = home / ("archived_sessions" if archived else "sessions")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.jsonl"
    target.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows) + tail)
    os.utime(target, (END.timestamp() + 1, END.timestamp() + 1))
    return target


async def read(home: Path) -> tuple[int, dict[str, int]]:
    return await reader.read_local_usage_delta(home, START, END)


async def test_ledger_uses_input_including_cache_and_output_once(tmp_path):
    log(tmp_path, "one", [meta(), ledger("r", 1, 100, output=20, cached=70, reasoning=15), legacy(2, 1000)])
    total, counts = await read(tmp_path)
    assert total == 120
    assert counts["ledger_events_counted"] == 1
    assert counts["token_count_events_ignored"] == 1


async def test_response_identity_deduplicates_parent_child_and_archive(tmp_path):
    common = ledger("parent-response", 1)
    log(tmp_path, "parent", [meta(), common])
    log(tmp_path, "child", [meta("child", parent="parent"), common, ledger("child-response", 3, 30)])
    log(tmp_path, "copy", [meta(), common], archived=True)
    total, counts = await read(tmp_path)
    assert total == 130
    assert counts["duplicate_response_records"] == 2


async def test_child_ledger_owner_excludes_history_when_parent_not_scanned(tmp_path):
    inherited = ledger("inherited", 1, 9000)
    inherited["payload"]["thread_id"] = "parent"
    own = ledger("own", 2, 20)
    own["payload"]["thread_id"] = "child"
    path = log(tmp_path, "parent", [meta(), ledger("inherited", -10, 9000)])
    os.utime(path, (START.timestamp() - 1, START.timestamp() - 1))
    log(tmp_path, "child", [meta("child", parent="parent", minute=1), inherited, own])
    total, counts = await read(tmp_path)
    assert total == 20
    assert counts["inherited_response_records_skipped"] == 1


async def test_child_ledger_without_owner_or_parent_is_ambiguous(tmp_path):
    log(tmp_path, "child", [meta("child", parent="missing"), ledger("ambiguous", 1)])
    total, counts = await read(tmp_path)
    assert total == 0
    assert counts["unanchored_child_response_records_skipped"] == 1


async def test_inherited_ledger_seen_first_does_not_hide_parent_original(tmp_path):
    inherited = ledger("r", 1)
    inherited["payload"]["thread_id"] = "parent"
    log(tmp_path, "child", [meta("child", parent="parent"), inherited])
    log(tmp_path, "parent", [meta(), inherited], archived=True)
    assert (await read(tmp_path))[0] == 100


@pytest.mark.parametrize("child_first", [False, True])
@pytest.mark.parametrize("rewrite", ["timestamp", "provider", "both"])
async def test_rewritten_inherited_ledger_is_ignored_before_global_conflicts(tmp_path, child_first, rewrite):
    original = ledger("shared-response", 1, 100, output=20)
    original["payload"]["thread_id"] = "parent"
    inherited = ledger("shared-response", 3 if rewrite in {"timestamp", "both"} else 1, 100, output=20)
    inherited["payload"]["thread_id"] = "parent"
    child_provider = "custom-gateway" if rewrite in {"provider", "both"} else "openai"
    # The reader scans sessions before archived_sessions. Both files are recent,
    # so reversing these locations exercises both global registration orders.
    log(tmp_path, "parent", [meta(), original], archived=child_first)
    log(tmp_path, "child", [meta("child", provider=child_provider, parent="parent"), inherited],
        archived=not child_first)
    total, counts = await read(tmp_path)
    assert total == 120
    assert counts["files_read"] == 2
    assert counts["ledger_events_counted"] == 1
    assert counts["inherited_response_records_skipped"] == 1


@pytest.mark.parametrize("owner_known", [False, True])
@pytest.mark.parametrize("change", ["timestamp", "provider", "usage"])
async def test_original_or_unknown_owner_conflicts_remain_rejected(tmp_path, owner_known, change):
    original = ledger("conflict-response", 1, 100)
    conflict = ledger("conflict-response", 2 if change == "timestamp" else 1, 101 if change == "usage" else 100)
    if owner_known:
        original["payload"]["thread_id"] = conflict["payload"]["thread_id"] = "parent"
    log(tmp_path, "parent", [meta(), original])
    log(tmp_path, "copy", [meta(provider="custom-gateway" if change == "provider" else "openai"), conflict],
        archived=True)
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read(tmp_path)
    assert caught.value.code == "conflict"


@pytest.mark.parametrize("field,value", [("input_tokens", 101), ("cached_input_tokens", 1)])
async def test_conflicting_response_rejects_entire_read(tmp_path, field, value):
    conflict = ledger("same", 1)
    conflict["payload"]["usage"][field] = value
    if field == "input_tokens":
        conflict["payload"]["usage"]["total_tokens"] = value
    log(tmp_path, "one", [meta(), ledger("same", 1)])
    log(tmp_path, "two", [meta("child", parent="parent"), conflict])
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read(tmp_path)
    assert caught.value.code == "conflict"
    assert str(tmp_path) not in str(caught.value)
    assert "same" not in str(caught.value)


async def test_response_conflict_outside_window_still_rejected(tmp_path):
    log(tmp_path, "one", [meta(), ledger("same", -1), ledger("same", 1)])
    with pytest.raises(reader.CodexLocalUsageError, match="冲突"):
        await read(tmp_path)


async def test_ledger_elsewhere_suppresses_old_legacy_copy(tmp_path):
    log(tmp_path, "old", [meta(), legacy(1, 100)], archived=True)
    log(tmp_path, "new", [meta(), legacy(1, 100), ledger("r", 1)])
    assert (await read(tmp_path))[0] == 100


async def test_legacy_last_preferred_only_when_cumulative_advances(tmp_path):
    log(tmp_path, "one", [meta(), legacy(-1, 100), legacy(1, 200, 30), legacy(2, 200, 30), legacy(3, 240)])
    total, counts = await read(tmp_path)
    assert total == 70
    assert counts["duplicate_cumulative_snapshots"] == 1


async def test_legacy_duplicate_session_active_archive_prefix(tmp_path):
    first = [meta(), legacy(-1, 100), legacy(1, 120)]
    log(tmp_path, "active", first + [legacy(2, 150)])
    log(tmp_path, "archive", first, archived=True)
    total, counts = await read(tmp_path)
    assert total == 50
    assert counts["duplicate_session_files"] == 1


async def test_legacy_conflicting_session_copies_fail(tmp_path):
    log(tmp_path, "active", [meta(), legacy(1, 120)])
    log(tmp_path, "archive", [meta(), legacy(1, 121)], archived=True)
    with pytest.raises(reader.CodexLocalUsageError, match="冲突"):
        await read(tmp_path)


async def test_legacy_exact_parent_replay_excludes_inherited_usage(tmp_path):
    log(tmp_path, "parent", [meta(), legacy(-2, 100), legacy(-1, 200), legacy(4, 300)])
    log(tmp_path, "child", [meta("child", parent="parent", minute=1),
                           legacy(1, 100), legacy(1, 200), legacy(3, 250)])
    total, counts = await read(tmp_path)
    assert total == 150
    assert counts["replayed_legacy_events_skipped"] == 2


@pytest.mark.parametrize("parent_kind", ["missing", "partial", "cycle", "old"])
async def test_legacy_ambiguous_parent_is_skipped_without_time_heuristics(tmp_path, parent_kind):
    if parent_kind in {"partial", "old"}:
        path = log(tmp_path, "parent", [meta(), legacy(-2, 100), legacy(-1, 200)])
        if parent_kind == "old":
            os.utime(path, (START.timestamp() - 1, START.timestamp() - 1))
    parent = "child" if parent_kind == "cycle" else "parent"
    log(tmp_path, "child", [meta("child", parent=parent, minute=1), legacy(1, 100), legacy(3, 150)])
    total, counts = await read(tmp_path)
    assert total == 0
    assert counts["ambiguous_legacy_children_skipped"] == 1


async def test_legacy_child_detects_old_nested_source(tmp_path):
    child = meta("child")
    child["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "missing"}}}
    log(tmp_path, "child", [child, legacy(1, 100)])
    assert (await read(tmp_path))[1]["ambiguous_legacy_children_skipped"] == 1


async def test_provider_is_metadata_and_explicit_persisted_settings(tmp_path):
    log(tmp_path, "one", [meta(provider="custom-gateway"), ledger("gateway-request", 1), settings(2), ledger("o1", 3),
                         settings(4, present=False), ledger("o2", 5), settings(6, None), ledger("unknown", 7),
                         settings(8, "custom-gateway"), ledger("d2", 9)])
    total, counts = await read(tmp_path)
    assert total == 200
    assert counts["other_provider_events_skipped"] == 2
    assert counts["unknown_provider_events_skipped"] == 1


async def test_unknown_provider_is_not_inferred_from_model(tmp_path):
    log(tmp_path, "one", [meta(provider=None), {"type": "turn_context", "payload": {"model": "codex-model-x"}},
                         ledger("r", 1)])
    total, counts = await read(tmp_path)
    assert total == 0
    assert counts["unknown_provider_events_skipped"] == 1


async def test_provider_switch_keeps_legacy_cumulative_baseline(tmp_path):
    log(tmp_path, "one", [meta(provider="custom-gateway"), legacy(1, 100), settings(2), legacy(3, 130)])
    total, counts = await read(tmp_path)
    assert total == 30
    assert counts["other_provider_events_skipped"] == 1


async def test_usage_before_latest_provider_setting_is_rejected(tmp_path):
    log(tmp_path, "one", [meta(provider="custom-gateway"), settings(5), ledger("earlier", 4)])
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read(tmp_path)
    assert caught.value.code == "rollback"


async def test_time_boundaries_and_newly_copied_history(tmp_path):
    log(tmp_path, "one", [meta(), ledger("old", -5), ledger("equal-start", 0), ledger("end", 10, 20),
                         ledger("future", 11)])
    log(tmp_path, "copied-history", [meta("old"), ledger("older", -15, 9000)])
    total, counts = await read(tmp_path)
    assert total == 20
    assert counts["out_of_window_events"] == 4


async def test_old_mtime_is_filtered_before_reading_bad_contents(tmp_path):
    path = log(tmp_path, "old", [], tail=b"broken complete line\n")
    os.utime(path, (START.timestamp(), START.timestamp()))
    total, counts = await read(tmp_path)
    assert total == 0
    assert counts["old_files_skipped"] == 1
    assert counts.get("files_read", 0) == 0


async def test_partial_tail_diagnostic_and_completed_bad_line_fails(tmp_path):
    log(tmp_path, "one", [meta(), ledger("r", 1)], tail=b'{"type":"token_usage_record"')
    total, counts = await read(tmp_path)
    assert total == 100
    assert counts["partial_tail"] == 1
    log(tmp_path, "one", [meta(), ledger("r", 1)], tail=b'{"type":"token_usage_record"\n')
    with pytest.raises(reader.CodexLocalUsageError, match="损坏"):
        await read(tmp_path)


@pytest.mark.parametrize("bad", [True, -1, "100", 1.5])
async def test_complete_bad_token_event_never_becomes_success_zero(tmp_path, bad):
    row = ledger("r", 1)
    row["payload"]["usage"]["input_tokens"] = bad
    log(tmp_path, "one", [meta(), row])
    with pytest.raises(reader.CodexLocalUsageError):
        await read(tmp_path)


async def test_duplicate_json_key_fails_without_source_text(tmp_path):
    secret = b'{"private":"SECRET-TEXT","private":"other"}\n'
    log(tmp_path, "one", [meta()], tail=secret)
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read(tmp_path)
    assert "SECRET-TEXT" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("rows", [[legacy(1, 100), legacy(2, 50)], [legacy(2, 100), legacy(1, 150)]])
async def test_cumulative_or_time_rollback_fails(tmp_path, rows):
    log(tmp_path, "one", [meta(), *rows])
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read(tmp_path)
    assert caught.value.code == "rollback"


async def test_last_without_cumulative_is_diagnosed(tmp_path):
    log(tmp_path, "one", [meta(), legacy(1, None, 100), legacy(2, None, 100)])
    total, counts = await read(tmp_path)
    assert total == 0
    assert counts["unanchored_legacy_events_skipped"] == 2


async def test_ledger_wins_even_if_legacy_token_payload_is_invalid(tmp_path):
    log(tmp_path, "one", [meta(), ledger("r", 1), legacy(2, -1)])
    assert (await read(tmp_path))[0] == 100


async def test_quota_only_event_is_not_corruption(tmp_path):
    row = legacy(1, None)
    row["payload"]["info"] = None
    log(tmp_path, "one", [meta(), row])
    assert (await read(tmp_path))[1]["quota_only_events"] == 1


async def test_symlink_files_and_directories_never_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    bad = outside / "bad.jsonl"
    bad.write_text("must not read\n")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "bad.jsonl").symlink_to(bad)
    (sessions / "linked-dir").symlink_to(outside, target_is_directory=True)
    total, counts = await read(tmp_path)
    assert total == 0
    assert counts["symlinks_skipped"] == 2


async def test_symlink_home_is_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read(alias)
    assert caught.value.code == "unreadable"


@pytest.mark.parametrize("limit,value", [("_MAX_FILES", 0), ("_MAX_BYTES", 5), ("_MAX_LINE_BYTES", 5),
                                         ("_MAX_SCAN_SECONDS", -1), ("_MAX_USAGE_RECORDS", 0),
                                         ("_MAX_DIRECTORY_ENTRIES", 0)])
async def test_safety_budget_never_returns_partial_success(tmp_path, monkeypatch, limit, value):
    log(tmp_path, "one", [meta(), ledger("r", 1)])
    monkeypatch.setattr(reader, limit, value)
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read(tmp_path)
    assert caught.value.code == "limit"


async def test_equal_time_is_zero_baseline_without_filesystem_access(tmp_path, monkeypatch):
    def no_read(*args):
        raise AssertionError("baseline must not read")
    monkeypatch.setattr(reader, "_read_sync", no_read)
    assert await reader.read_local_usage_delta(tmp_path / "missing", START, START) == (0, {"baseline_only": 1})


@pytest.mark.parametrize("start,end", [(END, START), (START.replace(tzinfo=None), END), (START, "bad")])
async def test_bad_window_is_safe(tmp_path, start, end):
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await reader.read_local_usage_delta(tmp_path, start, end)
    assert caught.value.code == "invalid_window"


async def test_cancellation_waits_for_reader_to_stop(tmp_path, monkeypatch):
    entered = threading.Event()
    exited = threading.Event()

    def cooperative_read(home, start, end, stop):
        entered.set()
        try:
            while not stop.wait(0.001):
                pass
        finally:
            exited.set()
        raise reader.CodexLocalUsageError("cancelled")

    monkeypatch.setattr(reader, "_read_sync", cooperative_read)
    task = asyncio.create_task(read(tmp_path))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert exited.is_set()


async def test_missing_home_safe_error_hides_path(tmp_path):
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read(tmp_path / "private-session-path")
    assert "private-session-path" not in str(caught.value)
    assert caught.value.__cause__ is None
