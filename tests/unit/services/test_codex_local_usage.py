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


def alias_config(home: Path, *, alias: str = "decitron", minute: int = -5, extra: str = "",
                 endpoint: str = "https://chatgpt.com/backend-api/codex",
                 login: str = "chatgpt", requires_auth: str = "true", wire_api: str = "responses") -> Path:
    target = home / "config.toml"
    target.write_text(
        f'forced_login_method = "{login}"\n[model_providers.{alias}]\n'
        f'base_url = "{endpoint}"\nrequires_openai_auth = {requires_auth}\n'
        f'wire_api = "{wire_api}"\n{extra}', encoding="utf-8",
    )
    timestamp = START.timestamp() + minute * 60
    os.utime(target, (timestamp, timestamp))
    return target


async def read(home: Path) -> tuple[int, dict[str, int]]:
    return await reader.read_local_usage_delta(home, START, END)


async def read_prices(home: Path):
    return await reader.read_local_pricing_usage(home, START, END)


async def read_combined(home: Path):
    return await reader.read_local_usage_and_pricing(home, START, END)


def turn_context(model: str, turn_id: str | None = "turn-one", minute: int = -1) -> dict:
    return {"timestamp": at(minute), "type": "turn_context", "payload": {"model": model, "turn_id": turn_id}}


def modeled_ledger(response: str, minute: int, model: str | None = "gpt-6-astra", *,
                   turn_id: str | None = "turn-one", tokens: int = 100, **token_fields: int) -> dict:
    row = ledger(response, minute, tokens, **token_fields)
    row["payload"]["turn_id"] = turn_id
    if model is not None:
        row["payload"]["model"] = model
    return row


async def test_pricing_preserves_per_response_cache_context_and_standard_assumption(tmp_path):
    row = modeled_ledger("r", 1, tokens=300000, cached=250000, output=1000, reasoning=800)
    row["payload"]["usage"]["cache_write_input_tokens"] = 10000
    row["payload"]["service_tier"] = "fast"
    log(tmp_path, "one", [meta(), row])
    entries, counts = await read_prices(tmp_path)
    request = entries[0].request
    assert request.model_dump() == {
        "request_id": "r", "model": "gpt-6-astra", "service_tier": "standard",
        "input_tokens": 300000, "cached_input_tokens": 250000,
        "cache_write_input_tokens": 10000, "output_tokens": 1000,
    }
    assert counts["pricing_incomplete"] == 0
    assert counts["pricing_entries"] == 1


async def test_pricing_payload_model_wins_and_matching_turn_falls_back(tmp_path):
    log(tmp_path, "one", [meta(), turn_context("gpt-6-astra"),
                         modeled_ledger("explicit", 1, "gpt-5.5", turn_id="different-turn"),
                         modeled_ledger("fallback", 2, None)])
    entries, counts = await read_prices(tmp_path)
    assert [entry.request.model for entry in entries] == ["gpt-5.5", "gpt-6-astra"]
    assert counts["pricing_incomplete"] == 0


@pytest.mark.parametrize("context_turn,event_turn", [
    ("current", "other"), (None, "current"), ("current", None), (None, None),
])
async def test_pricing_never_guesses_model_from_an_unidentified_or_different_turn(tmp_path, context_turn, event_turn):
    log(tmp_path, "one", [meta(), turn_context("gpt-6-astra", context_turn),
                         modeled_ledger("missing", 1, None, turn_id=event_turn)])
    entries, counts = await read_prices(tmp_path)
    assert entries == []
    assert counts["pricing_missing_model"] == counts["pricing_incomplete"] == 1
    assert (await read(tmp_path))[0] == 100


async def test_pricing_does_not_drop_known_models_or_spark_for_catalog_or_bucket_guessing(tmp_path):
    log(tmp_path, "one", [meta(), modeled_ledger("spark", 2, "gpt-5.3-codex-spark"),
                         modeled_ledger("unknown", 1, "future-unlisted-model")])
    entries, counts = await read_prices(tmp_path)
    assert [entry.request.model for entry in entries] == ["future-unlisted-model", "gpt-5.3-codex-spark"]
    assert counts["pricing_incomplete"] == 0


async def test_pricing_deduplicates_responses_across_parent_child_and_archive(tmp_path):
    parent = modeled_ledger("parent", 1)
    parent["payload"]["thread_id"] = "parent"
    inherited = modeled_ledger("parent", 2)
    inherited["payload"]["thread_id"] = "parent"
    child = modeled_ledger("child", 3, "gpt-5.5")
    child["payload"]["thread_id"] = "child"
    log(tmp_path, "parent", [meta(), parent])
    log(tmp_path, "parent-copy", [meta(), parent], archived=True)
    log(tmp_path, "child", [meta("child", parent="parent"), inherited, child])
    entries, counts = await read_prices(tmp_path)
    assert [entry.request.request_id for entry in entries] == ["parent", "child"]
    assert counts["duplicate_response_records"] == 1
    assert counts["inherited_response_records_skipped"] == 1
    assert counts["pricing_incomplete"] == 0


async def test_pricing_same_response_different_model_conflicts_but_token_api_remains_compatible(tmp_path):
    log(tmp_path, "one", [meta(), modeled_ledger("same", 1), modeled_ledger("same", 1, "gpt-5.5")])
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read_prices(tmp_path)
    assert caught.value.code == "conflict"
    assert (await read(tmp_path))[0] == 100


async def test_pricing_missing_model_or_legacy_makes_partial_entries_explicit(tmp_path):
    log(tmp_path, "ledger", [meta(), modeled_ledger("known", 1), modeled_ledger("missing", 2, None)])
    log(tmp_path, "legacy", [meta("legacy"), legacy(-1, 100), legacy(1, 150)])
    entries, counts = await read_prices(tmp_path)
    assert [entry.request.request_id for entry in entries] == ["known"]
    assert counts["pricing_missing_model"] == counts["pricing_legacy_events"] == 1
    assert counts["pricing_incomplete"] == 1


@pytest.mark.parametrize("case", ["unanchored-legacy", "unknown-session", "ambiguous-child", "ambiguous-ledger"])
async def test_pricing_ambiguous_usage_cannot_be_reported_as_complete_zero(tmp_path, case):
    if case == "unanchored-legacy":
        rows = [meta(), legacy(1, None, 100)]
    elif case == "unknown-session":
        unknown = meta()
        unknown["payload"].pop("id")
        rows = [unknown, legacy(1, 100)]
    elif case == "ambiguous-child":
        rows = [meta("child", parent="missing"), legacy(1, 100)]
    else:
        rows = [meta("child", parent="missing"), modeled_ledger("ambiguous", 1)]
    log(tmp_path, "one", rows)
    entries, counts = await read_prices(tmp_path)
    assert entries == []
    assert counts["pricing_incomplete"] == 1
    assert counts["pricing_unidentified_events"] >= 1


async def test_pricing_ignores_legacy_mirrors_and_out_of_window_gaps(tmp_path):
    log(tmp_path, "one", [meta(), modeled_ledger("old-missing-model", -1, None),
                         modeled_ledger("current", 1), legacy(2, 9000)])
    entries, counts = await read_prices(tmp_path)
    assert [entry.request.request_id for entry in entries] == ["current"]
    assert counts["pricing_incomplete"] == 0


async def test_pricing_reuses_production_combined_cache_validation(tmp_path):
    row = modeled_ledger("bad-cache", 1, cached=80)
    row["payload"]["usage"]["cache_write_input_tokens"] = 30
    log(tmp_path, "one", [meta(), row])
    entries, counts = await read_prices(tmp_path)
    assert entries == []
    assert counts["pricing_invalid_requests"] == counts["pricing_incomplete"] == 1


async def test_pricing_partial_tail_is_an_incomplete_interval(tmp_path):
    log(tmp_path, "one", [meta(), modeled_ledger("current", 1)], tail=b'{"type":')
    entries, counts = await read_prices(tmp_path)
    assert len(entries) == 1
    assert counts["partial_tail"] == counts["pricing_incomplete"] == 1


async def test_pricing_official_alias_is_reused_and_third_party_models_stay_excluded(tmp_path):
    alias_config(tmp_path)
    log(tmp_path, "alias", [meta(provider="decitron", minute=-1), modeled_ledger("official", 1)])
    log(tmp_path, "third-party", [meta("other", provider="actual-d1"), modeled_ledger("other", 2)])
    entries, counts = await read_prices(tmp_path)
    assert [entry.request.request_id for entry in entries] == ["official"]
    assert counts["other_provider_events_skipped"] == 1
    assert counts["pricing_incomplete"] == 0


async def test_pricing_result_limit_raises_instead_of_truncating(tmp_path, monkeypatch):
    log(tmp_path, "one", [meta(), modeled_ledger("first", 1), modeled_ledger("second", 2)])
    monkeypatch.setattr(reader, "_MAX_PRICING_ENTRIES", 1)
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read_prices(tmp_path)
    assert caught.value.code == "limit"


async def test_pricing_equal_time_baseline_never_scans_history(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("baseline must not read")

    monkeypatch.setattr(reader, "_read_pricing_sync", forbidden)
    entries, counts = await reader.read_local_pricing_usage(tmp_path, START, START)
    assert entries == []
    assert counts == {"baseline_only": 1, "pricing_entries": 0, "pricing_incomplete": 0}


async def test_combined_scan_freezes_tokens_and_prices_before_a_late_append(tmp_path, monkeypatch):
    log(tmp_path, "one", [meta()])
    original_read = reader._read_sync
    reads = 0

    def append_after_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        result = original_read(*args, **kwargs)
        late = modeled_ledger("late", 9, tokens=1000)
        late["timestamp"] = (END - timedelta(seconds=1)).isoformat()
        log(tmp_path, "one", [meta(), late])
        return result

    monkeypatch.setattr(reader, "_read_sync", append_after_read)
    total, entries, counts = await read_combined(tmp_path)
    assert reads == 1
    assert total == 0
    assert entries == []
    assert counts["pricing_incomplete"] == counts["pricing_entries"] == 0
    # A second scan would see the late row; it cannot alter the combined view.
    monkeypatch.setattr(reader, "_read_sync", original_read)
    later_entries, _ = await read_prices(tmp_path)
    assert len(later_entries) == 1
    assert later_entries[0].request.input_tokens == 1000


async def test_combined_scan_preserves_token_only_gaps_without_another_read(tmp_path):
    log(tmp_path, "ledger", [meta(), modeled_ledger("priced", 1, tokens=100, output=20)])
    log(tmp_path, "legacy", [meta("legacy"), legacy(-1, 100), legacy(1, 150)])
    total, entries, counts = await read_combined(tmp_path)
    assert total == 170
    assert len(entries) == 1
    assert entries[0].request.input_tokens + entries[0].request.output_tokens == 120
    assert counts["pricing_legacy_events"] == counts["pricing_incomplete"] == 1


async def test_combined_equal_time_baseline_does_not_scan(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("baseline must not read")

    monkeypatch.setattr(reader, "_read_combined_sync", forbidden)
    assert await reader.read_local_usage_and_pricing(tmp_path, START, START) == (
        0, [], {"baseline_only": 1, "pricing_incomplete": 0, "pricing_entries": 0},
    )


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


@pytest.mark.parametrize("alias", ["decitron", "historical-account-alias"])
async def test_official_chatgpt_alias_requires_fresh_persisted_settings(tmp_path, alias):
    alias_config(tmp_path, alias=alias)
    log(tmp_path, "one", [meta(provider=alias), settings(-1, alias), ledger("current", 1, 120)])
    total, counts = await read(tmp_path)
    assert total == 120
    assert counts["official_provider_aliases"] == 1
    assert counts["official_alias_records_recognized"] == 1


async def test_new_session_metadata_proves_current_alias_mapping(tmp_path):
    alias_config(tmp_path)
    log(tmp_path, "one", [meta(provider="decitron", minute=-4), ledger("new", 1, 120)])
    assert (await read(tmp_path))[0] == 120


async def test_old_cached_provider_is_skipped_until_fresh_settings(tmp_path):
    alias_config(tmp_path)
    log(tmp_path, "one", [meta(provider="decitron"), settings(-6, "decitron"), ledger("old", 1, 9000),
                         settings(2, "decitron"), ledger("fresh", 3, 120)])
    total, counts = await read(tmp_path)
    assert total == 120
    assert counts["stale_alias_evidence_records_skipped"] == 1
    assert counts["other_provider_events_skipped"] == 1


async def test_alias_still_uses_legacy_cumulative_baseline_without_backfilling(tmp_path):
    alias_config(tmp_path)
    log(tmp_path, "one", [meta(provider="decitron"), legacy(-10, 10000), settings(-1, "decitron"),
                         legacy(1, 10120)])
    assert (await read(tmp_path))[0] == 120


@pytest.mark.parametrize("format_kind", ["ledger", "legacy"])
async def test_child_alias_metadata_does_not_reclassify_older_inherited_events(tmp_path, format_kind):
    alias_config(tmp_path, minute=-5)
    if format_kind == "ledger":
        old = ledger("shared", -10, 100)
        parent_fresh = ledger("parent-fresh", 1, 120)
        child_fresh = ledger("child-fresh", 2, 50)
    else:
        old = legacy(-10, 100)
        parent_fresh = legacy(1, 220)
        child_fresh = legacy(2, 150)
    log(tmp_path, "parent", [meta(provider="decitron", minute=-20), old,
                            settings(-1, "decitron"), parent_fresh])
    log(tmp_path, "child", [meta("child", provider="decitron", parent="parent", minute=-4), old, child_fresh])
    total, counts = await read(tmp_path)
    assert total == 170
    assert counts["stale_alias_evidence_records_skipped"] == 2


@pytest.mark.parametrize("endpoint", [
    "https://decitron.org/v1", "https://api.openai.com/v1", "http://chatgpt.com/backend-api/codex",
    "https://chatgpt.com.example.org/backend-api/codex", "https://chatgpt.com/backend-api/codex?override=yes",
])
async def test_third_party_api_or_nonexact_endpoint_is_not_chatgpt_alias(tmp_path, endpoint):
    alias_config(tmp_path, endpoint=endpoint)
    log(tmp_path, "one", [meta(provider="decitron", minute=-1), ledger("not-account", 1, 120)])
    total, counts = await read(tmp_path)
    assert total == 0
    assert counts["other_provider_events_skipped"] == 1


@pytest.mark.parametrize("override", [
    {"login": "api"}, {"requires_auth": "false"}, {"requires_auth": '"true"'}, {"wire_api": "chat"},
])
async def test_alias_requires_explicit_chatgpt_auth_mode(tmp_path, override):
    alias_config(tmp_path, **override)
    log(tmp_path, "one", [meta(provider="decitron", minute=-1), ledger("not-account", 1, 120)])
    assert (await read(tmp_path))[0] == 0


@pytest.mark.parametrize("auth_key", [
    "env_key", "http_headers", "env_http_headers", "experimental_bearer_token", "bearer_token", "auth",
])
async def test_custom_alias_authentication_is_never_accepted(tmp_path, auth_key):
    alias_config(tmp_path, extra=f'{auth_key} = "synthetic-test-value"\n')
    log(tmp_path, "one", [meta(provider="decitron", minute=-1), ledger("custom-auth", 1, 120)])
    assert (await read(tmp_path))[0] == 0


async def test_newer_config_cannot_claim_older_sampling_interval(tmp_path):
    alias_config(tmp_path, minute=2)
    log(tmp_path, "one", [meta(provider="decitron", minute=3), ledger("new", 4, 120)])
    total, counts = await read(tmp_path)
    assert total == 0
    assert counts["alias_configuration_newer_than_window"] == 1


async def test_unrelated_config_edit_requires_refresh_but_does_not_permanently_exclude_alias(tmp_path):
    alias_config(tmp_path, minute=-3, extra='supports_websockets = true\n')
    log(tmp_path, "one", [meta(provider="decitron"), settings(-4, "decitron"), ledger("stale", 1, 9000),
                         settings(2, "decitron"), ledger("fresh", 3, 120)])
    assert (await read(tmp_path))[0] == 120


@pytest.mark.parametrize("api", [
    reader.read_local_usage_delta, reader.read_local_pricing_usage, reader.read_local_usage_and_pricing,
])
@pytest.mark.parametrize("rewrite", ["touch", "formatting", "atomic_mcp_edit"])
async def test_persisted_mapping_evidence_survives_unrelated_rewrites(tmp_path, api, rewrite):
    config = alias_config(tmp_path)
    evidence_path = tmp_path / "mapping-evidence.json"
    evidence_path.write_text(json.dumps({
        "digest": reader.local_provider_mapping_digest(tmp_path), "configured_at": at(-5),
    }))
    log(tmp_path, "one", [meta(provider="decitron", minute=-4), modeled_ledger("current", 3, tokens=120)])
    if rewrite == "formatting":
        config.write_text("# Rewritten without route changes\n\n" + config.read_text())
    elif rewrite == "atomic_mcp_edit":
        replacement = tmp_path / "replacement.toml"
        replacement.write_text(config.read_text() + '\n[mcp_servers.example]\nurl = "http://localhost:8000/mcp"\n')
        replacement.replace(config)
    os.utime(config, (START.timestamp() + 120, START.timestamp() + 120))
    saved = json.loads(evidence_path.read_text())
    evidence = saved["digest"], datetime.fromisoformat(saved["configured_at"])
    assert reader.local_provider_mapping_digest(tmp_path) == saved["digest"]

    # Recreating the call from persisted evidence needs no process-local cache.
    result = await api(tmp_path, START, END, provider_mapping_evidence=evidence)
    assert result[-1]["official_alias_records_recognized"] == 1
    if api is reader.read_local_usage_delta:
        assert result[0] == 120
    elif api is reader.read_local_pricing_usage:
        assert len(result[0]) == 1
        assert result[0][0].request.input_tokens == 120
    else:
        assert result[0] == 120
        assert len(result[1]) == 1
        assert result[1][0].request.input_tokens == 120
    # A first-time caller has no authority to infer the earlier mapping time.
    assert (await read(tmp_path))[0] == 0


async def test_persisted_mapping_evidence_does_not_reclassify_old_gateway_settings(tmp_path):
    alias_config(tmp_path)
    evidence = reader.local_provider_mapping_digest(tmp_path), START - timedelta(minutes=5)
    alias_config(tmp_path, minute=2, extra="supports_websockets = true\n")
    log(tmp_path, "one", [meta(provider="decitron", minute=-10), modeled_ledger("old-route", 1, tokens=9000),
                         settings(2, "decitron"), modeled_ledger("official", 3, tokens=120)])
    total, entries, counts = await reader.read_local_usage_and_pricing(
        tmp_path, START, END, provider_mapping_evidence=evidence,
    )
    assert total == 120
    assert [entry.request.request_id for entry in entries] == ["official"]
    assert counts["stale_alias_evidence_records_skipped"] == 1


@pytest.mark.parametrize("override", [
    {"endpoint": "https://decitron.org/v1"}, {"login": "api"}, {"requires_auth": "false"},
    {"wire_api": "chat"}, {"extra": 'env_key = "TEST_ACCOUNT_KEY"\n'}, {"alias": "new-alias"},
])
async def test_changed_mapping_rejects_persisted_evidence(tmp_path, override):
    alias_config(tmp_path)
    evidence = reader.local_provider_mapping_digest(tmp_path), START - timedelta(minutes=5)
    alias_config(tmp_path, **override)
    log(tmp_path, "one", [meta(), modeled_ledger("native", 1)])
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await reader.read_local_usage_and_pricing(tmp_path, START, END, provider_mapping_evidence=evidence)
    assert caught.value.code == "unreadable"


@pytest.mark.parametrize("evidence", [
    ("wrong-mapping", START), ("placeholder", START + timedelta(seconds=1)),
    ("placeholder", START.replace(tzinfo=None)), ("placeholder", "invalid"),
])
async def test_invalid_mapping_evidence_fails_closed(tmp_path, evidence):
    alias_config(tmp_path)
    log(tmp_path, "one", [meta(), modeled_ledger("native", 1)])
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await reader.read_local_usage_and_pricing(tmp_path, START, END, provider_mapping_evidence=evidence)
    assert caught.value.code == "unreadable"


@pytest.mark.parametrize("rewrite", ["touch", "same_mapping", "changed_mapping"])
async def test_persisted_mapping_keeps_scan_time_config_fence(tmp_path, monkeypatch, rewrite):
    config = alias_config(tmp_path)
    evidence = reader.local_provider_mapping_digest(tmp_path), START - timedelta(minutes=5)
    log(tmp_path, "one", [meta(provider="decitron", minute=-4), modeled_ledger("current", 1)])
    original_scan = reader._scan_directory

    def changing_scan(*args):
        original_scan(*args)
        if rewrite == "touch":
            os.utime(config, (START.timestamp(), START.timestamp()))
        elif rewrite == "same_mapping":
            alias_config(tmp_path, extra="supports_websockets = true\n")
        else:
            alias_config(tmp_path, endpoint="https://decitron.org/v1")

    monkeypatch.setattr(reader, "_scan_directory", changing_scan)
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await reader.read_local_usage_and_pricing(tmp_path, START, END, provider_mapping_evidence=evidence)
    assert caught.value.code == "unreadable"


def test_mapping_digest_ignores_unrelated_fields_and_provider_order(tmp_path):
    config = alias_config(tmp_path, extra='supports_websockets = true\nname = "Original name"\n')
    expected = reader.local_provider_mapping_digest(tmp_path)
    config.write_text(
        'model_provider = "openai"\nforced_login_method = "chatgpt"\n'
        '[model_providers.unused]\nbase_url = "https://example.org/v1"\n'
        '[model_providers.decitron]\nname = "Renamed"\nwire_api = "responses"\n'
        'requires_openai_auth = true\nsupports_websockets = false\n'
        'base_url = "https://chatgpt.com/backend-api/codex"\n'
        '[mcp_servers.example]\nurl = "http://localhost:8000/mcp"\n'
    )
    assert reader.local_provider_mapping_digest(tmp_path) == expected


@pytest.mark.parametrize("change", [
    'model_provider = "decitron"\n',
    'model_provider = "custom"\n',
    '[model_providers.openai]\nbase_url = "https://example.org/v1"\n',
    '[model_providers.openai]\nhttp_headers = { Authorization = "synthetic-fixture" }\n',
])
def test_mapping_digest_tracks_selected_provider_and_native_override(tmp_path, change):
    config = alias_config(tmp_path)
    expected = reader.local_provider_mapping_digest(tmp_path)
    current = config.read_text()
    config.write_text(current + change if change.startswith("[") else change + current)
    assert reader.local_provider_mapping_digest(tmp_path) != expected


def test_mapping_digest_tracks_login_even_without_aliases(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('forced_login_method = "chatgpt"\n')
    expected = reader.local_provider_mapping_digest(tmp_path)
    config.write_text('forced_login_method = "api"\n')
    assert reader.local_provider_mapping_digest(tmp_path) != expected


async def test_alias_configuration_change_during_scan_rejects_result(tmp_path, monkeypatch):
    alias_config(tmp_path)
    log(tmp_path, "one", [meta(provider="decitron", minute=-1), ledger("current", 1, 120)])
    original_scan = reader._scan_directory

    def changing_scan(*args):
        original_scan(*args)
        alias_config(tmp_path, endpoint="https://decitron.org/v1")

    monkeypatch.setattr(reader, "_scan_directory", changing_scan)
    with pytest.raises(reader.CodexLocalUsageError) as caught:
        await read(tmp_path)
    assert caught.value.code == "unreadable"


def test_local_binding_includes_mapping_digest_and_only_stats_auth(tmp_path, monkeypatch):
    from aiteam.services import local_plan_capture

    (tmp_path / "sessions").mkdir()
    (tmp_path / "auth.json").write_text("synthetic-auth-content-must-not-be-read")
    alias_config(tmp_path)
    original_open = os.open

    def guarded_open(path, *args, **kwargs):
        assert Path(path).name != "auth.json"
        return original_open(path, *args, **kwargs)

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(os, "open", guarded_open)
    real = local_plan_capture._local_source()
    monkeypatch.setattr(local_plan_capture, "local_provider_mapping_digest", lambda root: "different-mapping")
    changed = local_plan_capture._local_source()
    assert real[0] == changed[0] == tmp_path
    assert real[1] != changed[1]


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


@pytest.mark.parametrize("api,sync_name", [
    (read, "_read_sync"), (read_prices, "_read_pricing_sync"), (read_combined, "_read_combined_sync"),
])
async def test_cancellation_waits_for_reader_to_stop(tmp_path, monkeypatch, api, sync_name):
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

    monkeypatch.setattr(reader, sync_name, cooperative_read)
    task = asyncio.create_task(api(tmp_path))
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
