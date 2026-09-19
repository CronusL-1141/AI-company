"""Synthetic native files exercise loss, attribution, and privacy boundaries."""

import json
import time

from aiteam.services.codex_usage_journal import CodexJournalDiscovery, scan_journal_file, source_namespace


def encode(*rows):
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


def ledger(response="response-1", **fields):
    return {"timestamp": "2026-09-15T03:00:00Z", "type": "token_usage_record", "payload": {
        "response_id": response, "usage": {"input_tokens": 123, "output_tokens": 7}, **fields,
    }}


def source(tmp_path, content):
    path = tmp_path / "sessions" / "one.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


def scan(path, previous=None, **kwargs):
    return scan_journal_file(path, source_namespace(path.parent.parent), previous, **kwargs)


def test_unknown_model_provider_and_legacy_retain_only_known_facts(tmp_path):
    sentinel = "PRIVATE MESSAGE AND SECRET"
    path = source(tmp_path, encode(
        {"type": "session_meta", "payload": {"id": "session-1", "instructions": sentinel}},
        ledger(usage={"input_tokens": 123, "output_tokens": -1, "total_tokens": 130, "password": sentinel},
               text=sentinel, tool_arguments={"secret": sentinel}),
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 123}, "last_token_usage": {"output_tokens": 7},
            "access_token": sentinel,
        }}},
        ledger("custom", model_provider="third-party", model="future-model"),
    ))
    batch = scan(path)
    first, legacy, custom = batch.observations
    assert first.model is None and first.provider is None
    assert first.usage.input_tokens == 123 and first.usage.total_tokens == 130
    assert first.usage.output_tokens is None
    assert "invalid_tokens" in first.diagnostics
    assert legacy.kind == "legacy" and legacy.response_id is None
    assert legacy.total_token_usage.input_tokens == 123 and legacy.last_token_usage.output_tokens == 7
    assert custom.provider == "third-party" and custom.model == "future-model"
    serialized = batch.model_dump_json()
    assert sentinel not in serialized and "password" not in serialized and "access_token" not in serialized
    assert "tool_arguments" not in serialized and "instructions" not in serialized


def test_matching_turn_context_is_the_only_model_fallback(tmp_path):
    path = source(tmp_path, encode(
        {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-6-astra"}},
        ledger("match", turn_id="turn-1"), ledger("different", turn_id="turn-2"),
        ledger("explicit", turn_id="turn-2", model="gpt-5.5"), ledger("absent"),
    ))
    facts = scan(path).observations
    assert [(row.model, row.model_source) for row in facts] == [
        ("gpt-6-astra", "matching_turn_context"), (None, None), ("gpt-5.5", "payload"), (None, None),
    ]


def test_partial_tail_completes_and_old_timestamp_append_is_saved(tmp_path):
    first = encode(ledger("first"))
    late = ledger("late")
    late["timestamp"] = "2020-01-01T00:00:00Z"
    tail = encode(late)
    path = source(tmp_path, first + tail[:70])
    batch = scan(path)
    assert batch.cursor.offset == len(first) and batch.cursor.status == "partial_tail"
    assert [row.response_id for row in batch.observations] == ["first"]
    path.write_bytes(first + tail)
    second = scan(path, batch.cursor)
    assert second.cursor.offset == path.stat().st_size
    assert [row.response_id for row in second.observations] == ["late"]
    assert second.observations[0].occurred_at.year == 2020
    assert scan(path, second.cursor).observations == []


def test_malformed_complete_line_is_a_digest_and_does_not_lose_later_usage(tmp_path):
    path = source(tmp_path, b'{"secret":"DO NOT SAVE",BROKEN}\n' + encode(ledger(response=None)))
    batch = scan(path)
    assert len(batch.observations) == 2
    assert batch.observations[0].diagnostics == ["invalid_json"]
    assert "missing_response_id" in batch.observations[1].diagnostics
    assert "DO NOT SAVE" not in batch.model_dump_json()
    assert batch.cursor.offset == path.stat().st_size


def test_copy_and_archive_keep_fact_ids_while_rewrites_preserve_generations(tmp_path):
    path = source(tmp_path, encode(ledger("original")))
    first = scan(path)
    archive = tmp_path / "archived_sessions"
    archive.mkdir()
    moved = archive / path.name
    path.rename(moved)
    assert scan(moved, first.cursor).observations == []
    copied = tmp_path / "sessions" / "copied.jsonl"
    copied.write_bytes(moved.read_bytes())
    copy = scan(copied)
    assert copy.cursor.source_id != first.cursor.source_id
    assert copy.observations[0].observation_id == first.observations[0].observation_id
    moved.write_bytes(encode(ledger("changed")))
    rewritten = scan(moved, first.cursor)
    assert rewritten.cursor.generation == first.cursor.generation + 1
    assert rewritten.observations[0].response_id == "changed"
    assert rewritten.observations[0].observation_id != first.observations[0].observation_id


def test_oversized_line_advances_in_bounded_chunks_without_its_body(tmp_path):
    path = source(tmp_path, b'{"text":"' + b"PRIVATE" * 3000 + b'"}\n' + encode(ledger("after-large")))
    cursor = None
    facts = []
    partial_seen = False
    for _ in range(50):
        batch = scan(path, cursor, max_line_bytes=1024, max_bytes=18000)
        assert batch.bytes_read <= 18000
        facts.extend(batch.observations)
        cursor = batch.cursor
        if cursor.pending_bytes:
            partial_seen = True
            assert cursor.offset == 0
        if cursor.offset == path.stat().st_size:
            break
    assert partial_seen
    assert [fact.kind for fact in facts] == ["diagnostic", "ledger"]
    assert facts[0].diagnostics == ["oversized_line"]
    assert facts[1].response_id == "after-large"
    assert "PRIVATE" not in cursor.model_dump_json()
    assert "PRIVATE" not in "".join(fact.model_dump_json() for fact in facts)


def test_discovery_resumes_across_small_entry_budgets_and_skips_symlinks(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for number in range(6):
        (sessions / f"{number}.jsonl").write_bytes(encode(ledger(str(number))))
    other = tmp_path / "auth.json"
    other.write_text("PRIVATE")
    (sessions / "secret.jsonl").symlink_to(other)
    (tmp_path / "archived_sessions").mkdir()
    (tmp_path / "archived_sessions" / "old.jsonl").write_bytes(encode(ledger("old")))
    discovery = CodexJournalDiscovery(tmp_path)
    names = set()
    try:
        for _ in range(40):
            names.update(path.name for path in discovery.next_paths(
                max_files=1, max_entries=1, deadline=time.monotonic() + 1,
            ))
    finally:
        discovery.close()
    assert names == {f"{number}.jsonl" for number in range(6)} | {"old.jsonl"}
