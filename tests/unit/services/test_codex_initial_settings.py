"""Only the observed native adjacent bootstrap sequence can fill a turn tier."""

import json
from copy import deepcopy

import pytest

from aiteam.services.codex_usage_journal import scan_journal_file, source_namespace


def rows(tier="ultrafast"):
    return [
        {"type": "session_meta", "timestamp": "2026-10-22T01:00:00Z",
         "payload": {"id": "thread", "model_provider": "openai"}},
        {"type": "turn_context", "timestamp": "2026-10-22T01:00:01Z",
         "payload": {"turn_id": "first", "model": "gpt-6.1-sol"}},
        {"type": "event_msg", "timestamp": "2026-10-22T01:00:01.001Z", "payload": {
            "type": "thread_settings_applied", "thread_id": "thread",
            "thread_settings": {"model": "gpt-6.1-sol", "service_tier": tier}}},
        {"type": "token_usage_record", "timestamp": "2026-10-22T01:00:05Z", "payload": {
            "response_id": "response", "thread_id": "thread", "turn_id": "first",
            "usage": {"input_tokens": 100, "output_tokens": 10,
                      "cached_input_tokens": 0, "cache_write_input_tokens": 0}}},
    ]


def encoded(records):
    return b"".join(json.dumps(row).encode() + b"\n" for row in records)


def parse(tmp_path, records):
    """The recorded fact of the last response, as the prediction later reads it."""
    path = tmp_path / "sessions" / "one.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(encoded(records))
    return scan_journal_file(path, source_namespace(tmp_path), None).observations[-1]


@pytest.mark.parametrize("tier", ["ultrafast", "default", "future-tier"])
def test_adjacent_first_settings_bind_to_the_matching_turn(tmp_path, tier):
    journal = parse(tmp_path, rows(tier))
    assert journal.service_tier == tier and journal.service_tier_source == "thread_settings"
    assert journal.service_tier_present is True
    assert (journal.usage.input_tokens, journal.usage.output_tokens) == (100, 10)


@pytest.mark.parametrize("boundary", [
    "intervening-row", "foreign-thread", "different-model", "missing-thread", "null-turn",
    "null-meta", "null-settings", "late", "backwards", "prior-usage", "second-turn", "missing-turn",
])
def test_initial_binding_rejects_ambiguous_or_explicit_boundaries(tmp_path, boundary):
    data = deepcopy(rows())
    if boundary == "intervening-row":
        data.insert(2, {"type": "response_item", "payload": {}})
    elif boundary == "foreign-thread":
        data[2]["payload"]["thread_id"] = "foreign"
    elif boundary == "different-model":
        data[2]["payload"]["thread_settings"]["model"] = "other"
    elif boundary == "missing-thread":
        del data[2]["payload"]["thread_id"]
    elif boundary == "null-turn":
        data[1]["payload"]["service_tier"] = None
    elif boundary == "null-meta":
        data[0]["payload"]["service_tier"] = None
    elif boundary == "null-settings":
        data[2]["payload"]["thread_settings"]["service_tier"] = None
    elif boundary == "late":
        data[2]["timestamp"] = "2026-10-22T01:00:01.002Z"
    elif boundary == "backwards":
        data[2]["timestamp"] = "2026-10-22T01:00:00.999Z"
    elif boundary == "prior-usage":
        prior = deepcopy(data[-1])
        prior["timestamp"] = "2026-10-22T01:00:00.500Z"
        prior["payload"]["response_id"] = "prior"
        data.insert(1, prior)
    elif boundary == "second-turn":
        prior = deepcopy(data[1])
        prior["payload"]["turn_id"] = "prior"
        data.insert(1, prior)
    elif boundary == "missing-turn":
        data[1]["payload"].pop("turn_id")
    assert parse(tmp_path, data).service_tier is None


def test_settings_after_initial_pair_does_not_reprice_active_turn(tmp_path):
    data = rows()
    later = deepcopy(data[2])
    later["timestamp"] = "2026-10-22T01:00:02Z"
    later["payload"]["thread_settings"]["service_tier"] = "default"
    data.insert(3, later)
    assert parse(tmp_path, data).service_tier == "ultrafast"


def test_old_cursor_cannot_invent_a_first_turn_candidate(tmp_path):
    from aiteam.services.codex_usage_journal import _decode
    from aiteam.types import CodexUsageContext

    # Old cursors had no usage_seen field, even when earlier usage was consumed.
    context = CodexUsageContext(session_id="thread", turn_context_seen=False)
    context, _ = _decode(encoded([rows()[1]]), context)
    context, _ = _decode(encoded([rows()[2]]), context)
    _, fact = _decode(encoded([rows()[3]]), context)
    assert fact["service_tier"] is None


def test_blank_line_also_breaks_adjacent_pair(tmp_path):
    data = rows()
    path = tmp_path / "one.jsonl"
    path.write_bytes(encoded(data[:2]) + b"\n" + encoded(data[2:]))
    assert scan_journal_file(path, source_namespace(tmp_path), None).observations[-1].service_tier is None
