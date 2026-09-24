"""Pin the native Codex output subset, actual output accounting and API validation."""

import io
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, ValidationError

SCHEMA = json.loads((Path(__file__).parent / "hooks/codex_hook_output.schema.json").read_text())
VALIDATOR = Draft7Validator(SCHEMA)
LINE = "[AI Team OS] Notice"


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_accepted_fields_keep_model_channel_and_count_actual_stdout(capsys, event):
    notice = sys.modules["user_notice"]
    count = notice.emit("codex", event, user_text=LINE, model_text="MODEL_CONTEXT", delivery_ids=["d-1"],
                        extra={"continue": True, "stopReason": "reason", "suppressOutput": False,
                               "decision": "block", "reason": "discarded", "permissionDecision": "deny",
                               "hookSpecificOutput": {"additionalContext": "must not replace"}})
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    VALIDATOR.validate(document)
    assert document["hookSpecificOutput"] == {"hookEventName": event, "additionalContext": "MODEL_CONTEXT"}
    assert document["continue"] is True and document["suppressOutput"] is False
    assert document["stopReason"] == "reason"
    assert count == len(captured.out)
    assert "permissionDecision" in captured.err and "hookSpecificOutput" in captured.err
    assert [r["delivery_ids"] for r in notice._tail_records("codex")] == [["d-1"]]


@pytest.mark.parametrize("bad", [
    {"decision": "block"}, {"continue": 1}, {"stopReason": None}, {"suppressOutput": "false"},
    {"hookSpecificOutput": {"hookEventName": "SessionStart", "permissionDecision": "allow"}},
    {"hookSpecificOutput": {"hookEventName": "Other", "additionalContext": "x"}},
    {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": []}},
])
def test_strict_schema_rejects_extra_fields_and_wrong_types(bad):
    with pytest.raises(ValidationError):
        VALIDATOR.validate(bad)


def test_emit_discards_bad_field_types(capsys):
    notice = sys.modules["user_notice"]
    notice.emit("codex", "SessionStart", model_text="preserved", extra={
        "continue": 1, "suppressOutput": "false", "stopReason": None,
    })
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    VALIDATOR.validate(document)
    assert set(document) == {"hookSpecificOutput"}
    assert captured.err.count("invalid type") == 3


@pytest.mark.parametrize("text,ids", [
    (LINE + "\n[AI Team OS] Second", ["d-1", "d-2"]),
    (LINE, ["d-1", "d-2"]),
    ("", ["d-1"]),
    ("bad prefix", ["d-1"]),
])
def test_unshown_or_ambiguous_deliveries_are_not_marked_emitted(capsys, text, ids):
    notice = sys.modules["user_notice"]
    notice.emit("codex", "SessionStart", user_text=text, model_text="context", delivery_ids=ids)
    document = json.loads(capsys.readouterr().out)
    VALIDATOR.validate(document)
    assert "\n" not in document.get("systemMessage", "")
    assert not notice._tail_records("codex")


def test_ansi_removed_without_losing_delivery(capsys):
    notice = sys.modules["user_notice"]
    notice.emit("codex", "UserPromptSubmit", user_text="\x1b[33m" + LINE + "\x1b[39m",
                model_text="note", delivery_ids=["d-1"])
    document = json.loads(capsys.readouterr().out)
    assert document["systemMessage"] == LINE
    assert notice._tail_records("codex")[0]["delivery_ids"] == ["d-1"]


@pytest.mark.parametrize("document", [
    {}, {"success": True, "data": {"total": 0, "channels": []}},
    {"language": "en", "user_text": "", "model_text": "", "delivery_ids": [False]},
    {"language": "en", "user_text": "", "model_text": [], "delivery_ids": []},
])
def test_invalid_pending_response_preserves_import_offset_and_fallback(monkeypatch, document):
    notice = sys.modules["user_notice"]
    notice.record_local("codex", "emitted", delivery_ids=["previous"])
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(json.dumps(document).encode()))
    assert notice.fetch_pending("codex", "UserPromptSubmit", "", {"session_id": "s"}, timeout=1) is None
    assert notice.last_failure() == "error"
    assert not notice._offset_path("codex").exists()


def test_three_cores_are_byte_identical():
    root = Path(__file__).parents[2]
    shared = (root / "plugin/hooks/user_notice.py").read_bytes()
    assert (root / "src/aiteam/hooks/user_notice.py").read_bytes() == shared
    assert (root / "plugin/harness/codex/hooks/user_notice.py").read_bytes() == shared
