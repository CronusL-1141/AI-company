"""Prediction input from the usage journal: native logs -> recorder -> projection.

Every case writes real JSONL, records it through the production scanner and
storage, then projects one interval. The rules are the former log reader's
(its cases are ported here); what changed is that facts are consumed by
recording position, so late facts count once and nothing is re-read.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from aiteam.services import codex_usage_projection as projection
from aiteam.services.codex_local_usage import CodexLocalUsageError, local_provider_mapping_digest
from aiteam.services.codex_usage_journal import source_namespace
from aiteam.services.codex_usage_projection import JournalPosition, project_journal
from aiteam.storage.codex_usage_journal import CodexUsageJournalRepository, CodexUsageJournalUnavailableError
from aiteam.storage.engine_pool import engine_pool
from aiteam.types import CodexLaunchTierEvidence

from .._codex_journal import record_all

START = datetime(2026, 9, 14, 10, tzinfo=UTC)
END = START + timedelta(minutes=10)


def at(minutes: float) -> str:
    return (START + timedelta(minutes=minutes)).isoformat()


def usage(tokens: int, output: int = 0, cached: int = 0, reasoning: int = 0) -> dict:
    return {
        "input_tokens": tokens, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
        "output_tokens": output, "reasoning_output_tokens": reasoning, "total_tokens": tokens + output,
    }


def meta(session: str = "parent", provider: str | None = "openai", parent: str | None = None,
         minute: float = -20) -> dict:
    return {"timestamp": at(minute), "type": "session_meta", "payload": {
        "id": session, "model_provider": provider, "parent_thread_id": parent,
    }}


def ledger(response: str, minute: float, tokens: int = 100, **kwargs: int) -> dict:
    return {"timestamp": at(minute), "type": "token_usage_record", "payload": {
        "response_id": response, "usage": usage(tokens, **kwargs),
    }}


def legacy(minute: float, total: int | None, last: int | None = None) -> dict:
    return {"timestamp": at(minute), "type": "event_msg", "payload": {
        "type": "token_count", "info": {
            "total_token_usage": usage(total) if total is not None else None,
            "last_token_usage": usage(last) if last is not None else None,
        },
    }}


def settings(minute: float, provider: object = "openai", *, present: bool = True) -> dict:
    return {"timestamp": at(minute), "type": "event_msg", "payload": {
        "type": "thread_settings_applied", "thread_settings": {"model_provider_id": provider} if present else {},
    }}


def turn_context(model: str, turn_id: str | None = "turn-one", minute: float = -1) -> dict:
    return {"timestamp": at(minute), "type": "turn_context", "payload": {"model": model, "turn_id": turn_id}}


def modeled_ledger(response: str, minute: float, model: str | None = "gpt-6-astra", *,
                   turn_id: str | None = "turn-one", tokens: int = 100, **token_fields: int) -> dict:
    row = ledger(response, minute, tokens, **token_fields)
    row["payload"]["turn_id"] = turn_id
    if model is not None:
        row["payload"]["model"] = model
    return row


def log(home: Path, name: str, rows: list[dict], *, archived: bool = False, tail: bytes = b"") -> Path:
    folder = home / ("archived_sessions" if archived else "sessions")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name}.jsonl"
    target.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows) + tail)
    return target


def alias_config(home: Path, *, alias: str = "decitron", minute: float = -5, extra: str = "",
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


@pytest_asyncio.fixture
async def journal(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'journal.sqlite'}"
    repository = CodexUsageJournalRepository(url)
    await repository.init_db()
    yield repository
    await engine_pool.get_engine(url).dispose()


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "codex"
    root.mkdir()
    return root


async def project(journal, home, *, after: int = 0, since=START, until=END, binding=None, record=True, **options):
    """One interval as the capture asks for it; by default nothing was consumed before ``since``."""
    if record:
        await record_all(journal, home)
    return await project_journal(
        journal, home, binding=binding or since, previous=JournalPosition(after, since), until=until, **options,
    )


async def totals(journal, home, **options) -> tuple[int, dict[str, int]]:
    view = await project(journal, home, **options)
    return view.tokens, view.counts


# ---------------------------------------------------------------- pricing evidence


async def test_pricing_preserves_observed_fast_tier_and_cache_context(journal, home):
    row = modeled_ledger("r", 1, tokens=300000, cached=250000, output=1000, reasoning=800)
    row["payload"]["usage"]["cache_write_input_tokens"] = 10000
    row["payload"]["service_tier"] = "fast"
    log(home, "one", [meta(), row])
    view = await project(journal, home)
    assert view.entries[0].request.model_dump() == {
        "request_id": "r", "model": "gpt-6-astra", "service_tier": "fast",
        "input_tokens": 300000, "cached_input_tokens": 250000,
        "cache_write_input_tokens": 10000, "output_tokens": 1000,
    }
    assert view.counts["pricing_incomplete"] == 0 and view.counts["pricing_entries"] == 1
    assert view.tokens == 301000


async def test_pricing_without_tier_keeps_explicit_standard_fallback(journal, home):
    log(home, "one", [meta(), modeled_ledger("r", 1)])
    view = await project(journal, home)
    assert view.entries[0].request.service_tier == "standard"
    assert view.entries[0].service_tier_source == "standard_assumption"
    assert view.counts["pricing_service_tier_assumed"] == 1
    assert view.counts["pricing_missing_service_tier"] == view.counts["pricing_incomplete"] == 1


async def test_turn_tier_is_frozen_before_later_settings_and_null_never_inherits(journal, home):
    def tier_settings(minute, tier):
        return {"timestamp": at(minute), "type": "event_msg", "payload": {
            "type": "thread_settings_applied", "thread_settings": {"service_tier": tier},
        }}
    explicit_null = modeled_ledger("null", 4)
    explicit_null["payload"]["service_tier"] = None
    rows = [meta("child", parent="parent"), tier_settings(-2, "priority"),
            turn_context("gpt-6-astra", "turn-one"), tier_settings(1, "default"),
            modeled_ledger("first", 2, None), modeled_ledger("mismatch", 3, turn_id="other"), explicit_null,
            turn_context("gpt-5.6-sol", "turn-two", 5), modeled_ledger("second", 6, None, turn_id="turn-two")]
    for row in rows:
        if row["type"] == "token_usage_record":
            row["payload"]["thread_id"] = "child"  # each native child response names its own owner
    log(home, "child", rows)
    view = await project(journal, home)
    assert [(entry.request.service_tier, entry.service_tier_source) for entry in view.entries] == [
        ("priority", "thread_settings"), ("standard", "standard_assumption"),
        ("standard", "standard_assumption"), ("default", "thread_settings"),
    ]
    assert view.entries[0].model_source == view.entries[-1].model_source == "matching_turn_context"
    assert view.counts["pricing_missing_service_tier"] == 2


@pytest.mark.parametrize("explicit_null", [False, True], ids=["missing-tier", "null-tier"])
async def test_unidentified_turn_cannot_recover_tier_from_thread_settings(journal, home, explicit_null):
    metadata = meta()
    metadata["payload"]["service_tier"] = "priority"
    context = turn_context("gpt-6-astra", turn_id=None)
    if explicit_null:
        context["payload"]["service_tier"] = None
    log(home, "one", [metadata, context, modeled_ledger("unidentified", 1, turn_id=None)])
    view = await project(journal, home)
    assert view.entries[0].service_tier_source == "standard_assumption"
    assert view.counts.get("pricing_service_tier_observed", 0) == 0
    assert view.counts["pricing_missing_service_tier"] == view.counts["pricing_incomplete"] == 1


@pytest.mark.parametrize("context_turn,event_turn", [
    ("current", "other"), (None, "current"), ("current", None), (None, None),
])
async def test_pricing_never_guesses_model_from_an_unidentified_or_different_turn(
    journal, home, context_turn, event_turn,
):
    log(home, "one", [meta(), turn_context("gpt-6-astra", context_turn),
                      modeled_ledger("missing", 1, None, turn_id=event_turn)])
    view = await project(journal, home)
    assert view.entries == [] and view.tokens == 100
    assert view.counts["pricing_missing_model"] == view.counts["pricing_incomplete"] == 1


async def test_pricing_keeps_unknown_models_and_spark_for_catalog_and_bucket_rules(journal, home):
    log(home, "one", [meta(), modeled_ledger("spark", 2, "gpt-5.3-codex-spark"),
                      modeled_ledger("unknown", 1, "future-unlisted-model")])
    view = await project(journal, home)
    assert [entry.request.model for entry in view.entries] == ["future-unlisted-model", "gpt-5.3-codex-spark"]


async def test_pricing_reuses_production_combined_cache_validation(journal, home):
    row = modeled_ledger("bad-cache", 1, cached=80)
    row["payload"]["usage"]["cache_write_input_tokens"] = 30
    log(home, "one", [meta(), row])
    view = await project(journal, home)
    assert view.entries == []
    assert view.counts["pricing_invalid_requests"] == view.counts["pricing_incomplete"] == 1


# ---------------------------------------------------------------- response identity


async def test_ledger_uses_input_including_cache_and_output_once_and_ignores_its_mirror(journal, home):
    log(home, "one", [meta(), ledger("r", 1, 100, output=20, cached=70, reasoning=15), legacy(2, 1000)])
    tokens, counts = await totals(journal, home)
    assert tokens == 120
    assert counts["ledger_events_counted"] == 1 and counts["token_count_events_ignored"] == 1


async def test_response_identity_deduplicates_parent_child_and_archive(journal, home):
    common = ledger("parent-response", 1)
    log(home, "parent", [meta(), common])
    log(home, "child", [meta("child", parent="parent"), common, ledger("child-response", 3, 30)])
    log(home, "copy", [meta(), ledger("unrelated", -30), common], archived=True)
    tokens, counts = await totals(journal, home)
    assert tokens == 130
    assert counts["duplicate_response_records"] == 2


async def test_inherited_copy_never_counts_even_without_its_owner(journal, home):
    inherited = ledger("inherited", 1, 9000)
    inherited["payload"]["thread_id"] = "parent"
    own = ledger("own", 2, 20)
    own["payload"]["thread_id"] = "child"
    log(home, "child", [meta("child", parent="parent", minute=1), inherited, own])
    tokens, counts = await totals(journal, home)
    assert tokens == 20
    assert counts["inherited_response_records_skipped"] == 1


async def test_child_ledger_without_owner_or_parent_is_unidentified(journal, home):
    log(home, "child", [meta("child", parent="missing"), ledger("ambiguous", 1)])
    view = await project(journal, home)
    assert view.tokens == 0 and view.entries == []
    assert view.counts["unanchored_child_response_records_skipped"] == 1
    assert view.counts["pricing_unidentified_events"] == view.counts["pricing_incomplete"] == 1


@pytest.mark.parametrize("child_first", [False, True])
@pytest.mark.parametrize("rewrite", ["timestamp", "provider", "both"])
async def test_rewritten_inherited_ledger_is_ignored_before_any_conflict(journal, home, child_first, rewrite):
    original = ledger("shared-response", 1, 100, output=20)
    original["payload"]["thread_id"] = "parent"
    inherited = ledger("shared-response", 3 if rewrite in {"timestamp", "both"} else 1, 100, output=20)
    inherited["payload"]["thread_id"] = "parent"
    child_provider = "custom-gateway" if rewrite in {"provider", "both"} else "openai"
    log(home, "parent", [meta(), original], archived=child_first)
    log(home, "child", [meta("child", provider=child_provider, parent="parent"), inherited],
        archived=not child_first)
    tokens, counts = await totals(journal, home)
    assert tokens == 120
    assert counts["ledger_events_counted"] == counts["inherited_response_records_skipped"] == 1
    assert counts.get("conflicting_response_records", 0) == 0


@pytest.mark.parametrize("change", ["timestamp", "provider", "usage"])
async def test_conflicting_copies_count_the_first_and_mark_the_interval_incomplete(journal, home, change):
    original = ledger("conflict-response", 1, 100)
    conflict = ledger("conflict-response", 2 if change == "timestamp" else 1, 101 if change == "usage" else 100)
    log(home, "parent", [meta(), original])
    log(home, "copy", [meta(provider="custom-gateway" if change == "provider" else "openai"), conflict],
        archived=True)
    view = await project(journal, home)
    # The former reader rejected the whole interval; one bad copy no longer prices everything at zero.
    assert view.tokens == 100
    assert view.counts["conflicting_response_records"] == 1
    assert view.counts["pricing_incomplete"] == 1


async def test_same_response_different_model_counts_once(journal, home):
    log(home, "one", [meta(), modeled_ledger("same", 1), modeled_ledger("same", 1, "gpt-5.5")])
    view = await project(journal, home)
    assert view.tokens == 100 and [entry.request.model for entry in view.entries] == ["gpt-6-astra"]
    assert view.counts["pricing_conflicting_responses"] == 1


# ---------------------------------------------------------------- legacy token counts


async def test_ledger_elsewhere_suppresses_old_legacy_copy(journal, home):
    log(home, "old", [meta(), legacy(1, 100)], archived=True)
    log(home, "new", [meta(), legacy(1, 100), ledger("r", 1)])
    assert (await totals(journal, home))[0] == 100


async def test_legacy_last_preferred_only_when_cumulative_advances(journal, home):
    log(home, "one", [meta(), legacy(-1, 100), legacy(1, 200, 30), legacy(2, 200, 30), legacy(3, 240)])
    view = await project(journal, home)
    assert view.tokens == 70 and view.entries == []
    assert view.counts["duplicate_cumulative_snapshots"] == 1
    assert view.counts["pricing_legacy_events"] == 2 and view.counts["pricing_incomplete"] == 1


async def test_legacy_duplicate_session_active_archive_prefix(journal, home):
    first = [meta(), legacy(-1, 100), legacy(1, 120)]
    log(home, "active", first + [legacy(2, 150)])
    log(home, "archive", first, archived=True)
    assert (await totals(journal, home))[0] == 50


async def test_legacy_conflicting_session_copies_are_unidentified_not_fatal(journal, home):
    log(home, "active", [meta(), legacy(1, 120)])
    log(home, "archive", [meta(), legacy(1, 121)], archived=True)
    view = await project(journal, home)
    assert view.tokens == 0 and view.counts["pricing_unidentified_events"] >= 1


async def test_legacy_exact_parent_replay_excludes_inherited_usage(journal, home):
    log(home, "parent", [meta(), legacy(-2, 100), legacy(-1, 200), legacy(4, 300)])
    log(home, "child", [meta("child", parent="parent", minute=1),
                        legacy(1, 100), legacy(1.5, 200), legacy(3, 250)])
    tokens, counts = await totals(journal, home)
    assert tokens == 150
    assert counts["replayed_legacy_events_skipped"] == 2


@pytest.mark.parametrize("parent_kind", ["missing", "partial", "cycle"])
async def test_legacy_ambiguous_parent_is_skipped_without_time_heuristics(journal, home, parent_kind):
    if parent_kind == "partial":
        log(home, "parent", [meta(), legacy(-2, 100), legacy(-1, 200)])
    parent = "child" if parent_kind == "cycle" else "parent"
    log(home, "child", [meta("child", parent=parent, minute=1), legacy(1, 100), legacy(3, 150)])
    view = await project(journal, home)
    assert view.tokens == 0
    assert view.counts["ambiguous_legacy_records_skipped"] == 2
    assert view.counts["pricing_unidentified_events"] == 2


async def test_legacy_child_detects_old_nested_source(journal, home):
    child = meta("child")
    child["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "missing"}}}
    log(home, "child", [child, legacy(1, 100)])
    assert (await totals(journal, home))[1]["ambiguous_legacy_records_skipped"] == 1


@pytest.mark.parametrize("rows", [[legacy(1, 100), legacy(2, 50)], [legacy(2, 100), legacy(1, 150)]],
                         ids=["cumulative-rollback", "time-rollback"])
async def test_cumulative_rollback_is_unidentified_instead_of_counted(journal, home, rows):
    log(home, "one", [meta(), *rows])
    view = await project(journal, home)
    assert view.tokens == 0 and view.counts["pricing_unidentified_events"] == 2


async def test_last_without_cumulative_is_diagnosed(journal, home):
    log(home, "one", [meta(), legacy(1, None, 100), legacy(2, None, 100)])
    view = await project(journal, home)
    assert view.tokens == 0
    assert view.counts["unanchored_legacy_events_skipped"] == 2
    assert view.counts["pricing_unidentified_events"] == 2


async def test_ledger_wins_even_if_legacy_token_payload_is_invalid(journal, home):
    log(home, "one", [meta(), ledger("r", 1), legacy(2, -1)])
    assert (await totals(journal, home))[0] == 100


async def test_quota_only_event_is_not_usage_or_corruption(journal, home):
    row = legacy(1, None)
    row["payload"]["info"] = None
    log(home, "one", [meta(), row])
    view = await project(journal, home)
    assert view.tokens == 0 and view.counts["pricing_incomplete"] == 0


async def test_unidentified_legacy_session_is_a_gap(journal, home):
    unknown = meta()
    unknown["payload"].pop("id")
    log(home, "one", [unknown, legacy(1, 100)])
    view = await project(journal, home)
    assert view.tokens == 0 and view.counts["pricing_unidentified_events"] == 1


# ---------------------------------------------------------------- provider evidence


async def test_provider_is_metadata_and_explicit_persisted_settings(journal, home):
    log(home, "one", [meta(provider="custom-gateway"), ledger("gateway-request", 1), settings(2), ledger("o1", 3),
                      settings(4, present=False), ledger("o2", 5), settings(6, None), ledger("unknown", 7),
                      settings(8, "custom-gateway"), ledger("d2", 9)])
    tokens, counts = await totals(journal, home)
    assert tokens == 200
    assert counts["other_provider_events_skipped"] == 2
    assert counts["unknown_provider_events_skipped"] == 1


async def test_provider_switch_keeps_legacy_cumulative_baseline(journal, home):
    log(home, "one", [meta(provider="custom-gateway"), legacy(1, 100), settings(2), legacy(3, 130)])
    tokens, counts = await totals(journal, home)
    assert tokens == 30
    assert counts["other_provider_events_skipped"] == 1


@pytest.mark.parametrize("alias", ["decitron", "historical-account-alias"])
async def test_official_chatgpt_alias_requires_fresh_persisted_settings(journal, home, alias):
    alias_config(home, alias=alias)
    log(home, "one", [meta(provider=alias), settings(-1, alias), ledger("current", 1, 120)])
    tokens, counts = await totals(journal, home)
    assert tokens == 120
    assert counts["official_provider_aliases"] == counts["official_alias_records_recognized"] == 1


async def test_old_cached_provider_is_skipped_until_fresh_settings(journal, home):
    alias_config(home)
    log(home, "one", [meta(provider="decitron"), settings(-6, "decitron"), ledger("old", 1, 9000),
                      settings(2, "decitron"), ledger("fresh", 3, 120)])
    tokens, counts = await totals(journal, home)
    assert tokens == 120
    assert counts["stale_alias_evidence_records_skipped"] == 1
    assert counts["other_provider_events_skipped"] == 1


@pytest.mark.parametrize("format_kind", ["ledger", "legacy"])
async def test_child_alias_metadata_does_not_reclassify_older_inherited_events(journal, home, format_kind):
    alias_config(home, minute=-5)
    if format_kind == "ledger":
        old, parent_fresh, child_fresh = ledger("shared", -10, 100), ledger("parent-fresh", 1, 120), \
            ledger("child-fresh", 2, 50)
    else:
        old, parent_fresh, child_fresh = legacy(-10, 100), legacy(1, 220), legacy(2, 150)
    log(home, "parent", [meta(provider="decitron", minute=-20), old, settings(-1, "decitron"), parent_fresh])
    log(home, "child", [meta("child", provider="decitron", parent="parent", minute=-4), old, child_fresh])
    view = await project(journal, home, binding=START - timedelta(minutes=30))
    assert view.tokens == 170


@pytest.mark.parametrize("endpoint", [
    "https://decitron.org/v1", "https://api.openai.com/v1", "http://chatgpt.com/backend-api/codex",
    "https://chatgpt.com.example.org/backend-api/codex",
])
async def test_third_party_or_nonexact_endpoint_is_not_a_chatgpt_alias(journal, home, endpoint):
    alias_config(home, endpoint=endpoint)
    log(home, "one", [meta(provider="decitron", minute=-1), ledger("not-account", 1, 120)])
    tokens, counts = await totals(journal, home)
    assert tokens == 0 and counts["other_provider_events_skipped"] == 1


async def test_newer_config_cannot_claim_an_older_interval(journal, home):
    alias_config(home, minute=2)
    log(home, "one", [meta(provider="decitron", minute=3), ledger("new", 4, 120)])
    tokens, counts = await totals(journal, home)
    assert tokens == 0 and counts["alias_configuration_newer_than_window"] == 1


@pytest.mark.parametrize("rewrite", ["touch", "formatting", "atomic_mcp_edit"])
async def test_persisted_mapping_evidence_survives_unrelated_rewrites(journal, home, rewrite):
    config = alias_config(home)
    evidence = local_provider_mapping_digest(home), START - timedelta(minutes=5)
    log(home, "one", [meta(provider="decitron", minute=-4), modeled_ledger("current", 3, tokens=120)])
    if rewrite == "formatting":
        config.write_text("# Rewritten without route changes\n\n" + config.read_text())
    elif rewrite == "atomic_mcp_edit":
        replacement = home / "replacement.toml"
        replacement.write_text(config.read_text() + '\n[mcp_servers.example]\nurl = "http://localhost:8000/mcp"\n')
        replacement.replace(config)
    os.utime(config, (START.timestamp() + 120, START.timestamp() + 120))
    view = await project(journal, home, provider_mapping_evidence=evidence)
    assert view.tokens == 120 and view.entries[0].request.input_tokens == 120
    # A first-time caller has no authority to infer the earlier mapping time.
    assert (await project(journal, home)).tokens == 0


@pytest.mark.parametrize("override", [
    {"endpoint": "https://decitron.org/v1"}, {"login": "api"}, {"extra": 'env_key = "TEST_ACCOUNT_KEY"\n'},
    {"alias": "new-alias"},
])
async def test_changed_mapping_rejects_persisted_evidence(journal, home, override):
    alias_config(home)
    evidence = local_provider_mapping_digest(home), START - timedelta(minutes=5)
    alias_config(home, **override)
    log(home, "one", [meta(), modeled_ledger("native", 1)])
    with pytest.raises(CodexLocalUsageError) as caught:
        await project(journal, home, provider_mapping_evidence=evidence)
    assert caught.value.code == "unreadable"


@pytest.mark.parametrize("evidence", [
    ("wrong-mapping", START), ("placeholder", START + timedelta(seconds=1)),
    ("placeholder", START.replace(tzinfo=None)), ("placeholder", "invalid"),
], ids=["wrong-digest", "after-start", "naive", "not-a-time"])
async def test_invalid_mapping_evidence_fails_closed(journal, home, evidence):
    alias_config(home)
    log(home, "one", [meta(), modeled_ledger("native", 1)])
    with pytest.raises(CodexLocalUsageError) as caught:
        await project(journal, home, provider_mapping_evidence=evidence)
    assert caught.value.code == "unreadable"


async def test_mapping_change_during_the_projection_rejects_its_result(journal, home, monkeypatch):
    alias_config(home)
    log(home, "one", [meta(provider="decitron", minute=-1), ledger("current", 1, 120)])
    original = projection._project

    def changing(*args):
        result = original(*args)
        alias_config(home, endpoint="https://decitron.org/v1")
        return result

    monkeypatch.setattr(projection, "_project", changing)
    with pytest.raises(CodexLocalUsageError) as caught:
        await project(journal, home)
    assert caught.value.code == "unreadable"


async def test_missing_home_is_unreadable_without_its_path(journal, home):
    with pytest.raises(CodexLocalUsageError) as caught:
        await project(journal, home / "private-session-path", record=False)
    assert "private-session-path" not in str(caught.value) and caught.value.__cause__ is None


# ---------------------------------------------------------------- unreadable lines


@pytest.mark.parametrize("bad", [True, -1, "100", 1.5])
async def test_complete_bad_token_event_never_becomes_a_complete_zero(journal, home, bad):
    row = ledger("r", 1)
    row["payload"]["usage"]["input_tokens"] = bad
    log(home, "one", [meta(), row])
    view = await project(journal, home)
    assert view.tokens == 0 and view.counts["pricing_unreadable_records"] == view.counts["pricing_incomplete"] == 1


async def test_broken_complete_line_is_a_gap_without_its_text(journal, home):
    log(home, "one", [meta(), modeled_ledger("before", 1)], tail=b'{"private":"SECRET-TEXT","private":"x"}\n')
    view = await project(journal, home)
    assert view.tokens == 100 and view.counts["pricing_unreadable_records"] == 1
    assert "SECRET-TEXT" not in json.dumps(view.counts)


# ---------------------------------------------------------------- consumption by position


async def test_window_boundaries_and_copied_old_history(journal, home):
    log(home, "one", [meta(), ledger("old", -5), ledger("equal-start", 0), ledger("end", 10, 20),
                      ledger("future", 11)])
    log(home, "copied-history", [meta("old"), ledger("older", -15, 9000)])
    view = await project(journal, home)
    assert view.tokens == 20
    # "future" is not due yet: the next interval consumes it.
    later = await project(journal, home, after=view.position.seq, since=END, until=END + timedelta(minutes=5),
                          binding=START)
    assert later.tokens == 100


async def test_a_fact_recorded_late_is_consumed_once_by_the_next_interval(journal, home):
    path = log(home, "one", [meta(), ledger("on-time", 1, 10)])
    first = await project(journal, home)
    assert first.tokens == 10
    with path.open("a") as stream:  # written before END, recorded only after the first interval
        stream.write(json.dumps(ledger("late", 9, 7)) + "\n")
    second_end = END + timedelta(minutes=5)
    second = await project(journal, home, after=first.position.seq, since=END, until=second_end, binding=START)
    assert second.tokens == 7 and [entry.request.request_id for entry in second.entries] == []
    third = await project(journal, home, after=second.position.seq, since=second_end,
                          until=second_end + timedelta(minutes=5), binding=START)
    assert third.tokens == 0


async def test_a_fact_recorded_before_its_time_waits_for_its_interval(journal, home):
    log(home, "one", [meta(), ledger("ahead", 12, 9)])  # recorded now, occurs after END
    first = await project(journal, home)
    assert first.tokens == 0
    second = await project(journal, home, after=first.position.seq, since=END, until=END + timedelta(minutes=5),
                           binding=START, record=False)
    assert second.tokens == 9


async def test_binding_excludes_history_however_late_it_is_recorded(journal, home):
    log(home, "one", [meta(), ledger("before-binding", 1, 500), ledger("after-binding", 6, 5)])
    view = await project(journal, home, binding=START + timedelta(minutes=5), since=START + timedelta(minutes=5))
    assert view.tokens == 5


async def test_rewritten_file_and_resumed_copy_do_not_count_a_consumed_response_again(journal, home):
    path = log(home, "one", [meta(), ledger("counted", 1, 40)])
    first = await project(journal, home)
    assert first.tokens == 40
    # The file is rewritten before the response (a new generation re-records it
    # with new fact IDs), and a resumed log copies the same response.
    path.write_bytes(json.dumps(meta(minute=-21)).encode() + b"\n" + json.dumps(ledger("counted", 1, 40)).encode()
                     + b"\n" + json.dumps(ledger("new", 11, 3)).encode() + b"\n")
    log(home, "resumed", [meta(), turn_context("gpt-6-astra", minute=0), ledger("counted", 1, 40)], archived=True)
    second = await project(journal, home, after=first.position.seq, since=END,
                           until=END + timedelta(minutes=5), binding=START)
    assert second.tokens == 3
    assert second.counts["duplicate_response_records"] == 2


async def test_backlog_beyond_one_round_drains_in_later_rounds(journal, home):
    log(home, "one", [meta(), *(ledger(f"r{index}", 1, 1) for index in range(7))])
    await record_all(journal, home)
    seen, after, since = 0, 0, START
    for _ in range(4):
        until = END if since == START else since + timedelta(minutes=1)
        view = await project(journal, home, after=after, since=since, until=until, binding=START,
                             record=False, max_facts=3)
        assert view.counts.get("journal_backlog", 0) == max(0, 7 - after - 3)
        seen += view.tokens
        after, since = view.position.seq, until
    assert seen == 7 and after == 7


async def test_a_journal_behind_the_saved_position_is_a_rollback(journal, home):
    log(home, "one", [meta(), ledger("r", 1)])
    with pytest.raises(CodexLocalUsageError) as caught:
        await project(journal, home, after=99)
    assert caught.value.code == "rollback"


async def test_an_unreadable_journal_is_infrastructure_not_an_empty_interval(journal, home, tmp_path):
    log(home, "one", [meta(), ledger("r", 1)])
    missing = CodexUsageJournalRepository(f"sqlite+aiosqlite:///{tmp_path / 'no-journal.sqlite'}")
    try:
        with pytest.raises(CodexUsageJournalUnavailableError):
            await project(missing, home, record=False)
    finally:
        await engine_pool.get_engine(f"sqlite+aiosqlite:///{tmp_path / 'no-journal.sqlite'}").dispose()


@pytest.mark.parametrize("window", ["reversed", "naive", "before-binding"])
async def test_bad_window_is_rejected(journal, home, window):
    log(home, "one", [meta()])
    options = {
        "reversed": {"since": END, "until": START},
        "naive": {"since": START.replace(tzinfo=None)},
        "before-binding": {"binding": START + timedelta(minutes=1)},
    }[window]
    with pytest.raises(CodexLocalUsageError) as caught:
        await project(journal, home, **options)
    assert caught.value.code == "invalid_window"


# ---------------------------------------------------------------- launch tiers


def launch_evidence(home, **changes) -> CodexLaunchTierEvidence:
    return CodexLaunchTierEvidence.model_validate({
        "launch_id": "launch-one", "source_namespace": source_namespace(home), "thread_id": "parent",
        "requested_service_tier": "fast", "started_at": at(-1), "finished_at": at(9),
        "turn_ids": [], "binding_source": "cc_stdout_thread_started", **changes,
    })


def loader(*records, calls=None):
    async def load(since, until):
        if calls is not None:
            calls.append((since, until))
        return list(records)
    return load


def new_exec_rows():
    row = modeled_ledger("r", 2)
    row["payload"]["thread_id"] = "parent"
    metadata = meta(minute=0)
    metadata["payload"]["source"] = "exec"
    return [metadata, turn_context("gpt-6-astra", minute=1), row]


@pytest.mark.parametrize("tier", ["fast", "ultrafast"])
async def test_bound_new_launch_requests_its_tier_without_changing_tokens(journal, home, tier):
    log(home, "new", new_exec_rows())
    calls = []
    view = await project(journal, home, launch_evidence=loader(launch_evidence(home, requested_service_tier=tier),
                                                               calls=calls))
    assert calls == [(START, END)]
    assert view.tokens == 100
    entry = view.entries[0]
    assert (entry.request.service_tier, entry.service_tier_source, entry.launch_id) == (
        tier, "launch_requested", "launch-one",
    )
    assert view.counts["pricing_service_tier_requested"] == 1
    assert view.counts["pricing_missing_service_tier"] == view.counts["pricing_incomplete"] == 1
    without = await project(journal, home)
    assert without.entries[0].service_tier_source == "standard_assumption"


async def test_launch_evidence_is_not_loaded_without_a_candidate_response(journal, home):
    rows = new_exec_rows()
    rows[-1]["payload"]["service_tier"] = "priority"
    log(home, "new", rows)
    calls = []
    view = await project(journal, home, launch_evidence=loader(launch_evidence(home), calls=calls))
    assert calls == [] and view.entries[0].service_tier_source == "payload"


@pytest.mark.parametrize("native_source,native_tier", [
    (source, tier) for source in ("payload", "turn_context", "settings", "session_meta")
    for tier in (None, "default")
], ids=[f"{source}-{tier}" for source in ("payload", "turn_context", "settings", "session_meta")
        for tier in (None, "default")])
async def test_native_presence_including_null_always_blocks_launch(journal, home, native_source, native_tier):
    rows = new_exec_rows()
    if native_source == "settings":
        rows.insert(1, {"timestamp": at(0), "type": "event_msg", "payload": {
            "type": "thread_settings_applied", "thread_settings": {"service_tier": native_tier},
        }})
    else:
        rows[{"session_meta": 0, "turn_context": 1, "payload": 2}[native_source]]["payload"]["service_tier"] = (
            native_tier
        )
    log(home, "new", rows)
    view = await project(journal, home, launch_evidence=loader(launch_evidence(home)))
    assert view.entries[0].request.service_tier == (native_tier or "standard")
    assert view.entries[0].service_tier_source != "launch_requested"


@pytest.mark.parametrize("native_tier", ["absent", None, "priority"], ids=["missing", "null", "priority"])
@pytest.mark.parametrize("delayed", [False, True], ids=["matching-context", "lost-context"])
async def test_launch_tier_distinguishes_missing_and_lost_turn_context(journal, home, native_tier, delayed):
    rows = new_exec_rows()
    if native_tier != "absent":
        rows[1]["payload"]["service_tier"] = native_tier
    if delayed:
        rows[-1]["timestamp"] = at(4)
        rows.insert(2, turn_context("gpt-6-astra", "turn-two", 3))
    log(home, "new", rows)
    view = await project(journal, home, launch_evidence=loader(launch_evidence(home)))
    expected = ("standard", "standard_assumption") if delayed or native_tier is None else (
        ("fast", "launch_requested") if native_tier == "absent" else ("priority", "matching_turn_context")
    )
    assert view.tokens == 100
    assert [(entry.request.service_tier, entry.service_tier_source) for entry in view.entries] == [expected]


@pytest.mark.parametrize("boundary", [
    "wrong-namespace", "wrong-thread", "missing-owner", "missing-turn", "different-turn",
    "old-session", "fork-session", "after-finish", "conflicting-launches", "unidentified-first-turn",
], ids=lambda value: value)
async def test_launch_tier_never_crosses_execution_owner_or_turn_boundary(journal, home, boundary):
    rows = new_exec_rows()
    changes = {}
    if boundary == "wrong-namespace":
        changes["source_namespace"] = "f" * 64
    elif boundary == "wrong-thread":
        changes["thread_id"] = "other"
    elif boundary == "missing-owner":
        del rows[-1]["payload"]["thread_id"]
    elif boundary == "missing-turn":
        del rows[-1]["payload"]["turn_id"]
    elif boundary == "different-turn":
        rows[-1]["payload"]["turn_id"] = "restored-turn"
    elif boundary == "old-session":
        rows[0]["timestamp"] = at(-20)
    elif boundary == "fork-session":
        rows[0]["payload"]["parent_thread_id"] = "ancestor"
    elif boundary == "after-finish":
        changes["finished_at"] = at(1)
    elif boundary == "unidentified-first-turn":
        rows.insert(1, turn_context("gpt-6-astra", turn_id=None, minute=0))
    evidence = [launch_evidence(home, **changes)]
    if boundary == "conflicting-launches":
        evidence.append(launch_evidence(home, launch_id="another-launch"))
    log(home, "new", rows)
    view = await project(journal, home, binding=START - timedelta(minutes=30), launch_evidence=loader(*evidence))
    assert all(entry.service_tier_source == "standard_assumption" for entry in view.entries)
    assert view.counts.get("pricing_service_tier_requested", 0) == 0


async def test_launch_exact_explicit_turn_collection_excludes_other_turns(journal, home):
    rows = new_exec_rows()
    second = modeled_ledger("second", 4, turn_id="turn-two")
    second["payload"]["thread_id"] = "parent"
    rows += [turn_context("gpt-6-astra", "turn-two", 3), second]
    log(home, "new", rows)
    view = await project(journal, home, launch_evidence=loader(launch_evidence(home, turn_ids=["turn-two"])))
    assert [entry.service_tier_source for entry in view.entries] == ["standard_assumption", "launch_requested"]


@pytest.mark.parametrize("native_source", ["exec", "cli", None], ids=["native-exec", "interactive-cli", "absent"])
async def test_launch_requires_a_native_exec_source_but_keeps_native_tokens(journal, home, native_source):
    rows = new_exec_rows()
    if native_source is None:
        del rows[0]["payload"]["source"]
    else:
        rows[0]["payload"]["source"] = native_source
    log(home, "fresh-thread", rows)
    view = await project(journal, home, launch_evidence=loader(launch_evidence(home)))
    assert view.tokens == 100
    expected = "launch_requested" if native_source == "exec" else "standard_assumption"
    assert view.entries[0].service_tier_source == expected


async def test_a_launch_for_a_late_recorded_response_is_looked_up_from_its_own_time(journal, home):
    path = log(home, "new", new_exec_rows()[:2])
    first = await project(journal, home)
    with path.open("a") as stream:
        stream.write(json.dumps(new_exec_rows()[-1]) + "\n")
    calls = []
    second_end = END + timedelta(minutes=5)
    view = await project(journal, home, after=first.position.seq, since=END, until=second_end, binding=START,
                         launch_evidence=loader(launch_evidence(home), calls=calls))
    assert calls == [(START + timedelta(minutes=2), second_end)]
    assert view.entries[0].service_tier_source == "launch_requested"
