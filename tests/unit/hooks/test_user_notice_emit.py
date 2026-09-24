"""user_notice.emit: the one stdout writer for hooks (design §5.7).

Pins the host field whitelists, "nothing to say prints nothing", dropping of
malformed user lines, one document per hook run, and when delivery ids count
as written.
"""

from __future__ import annotations

import json
import sys

import pytest

LINE = "[AI Team OS] 服务未启动"
ESC = "\x1b"


@pytest.fixture()
def un(tmp_path, monkeypatch):
    module = sys.modules["user_notice"]
    monkeypatch.setenv("HOME", str(tmp_path))
    return module


def _doc(capsys) -> dict:
    out = capsys.readouterr().out
    return json.loads(out) if out else {}


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_cc_prompt_and_start_carry_both_channels(un, capsys, event):
    un.emit("cc", event, user_text=LINE, model_text="note")
    assert _doc(capsys) == {
        "systemMessage": LINE,
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": "note"},
    }


def test_cc_pre_tool_use_user_line_alone(un, capsys):
    un.emit("cc", "PreToolUse", user_text=LINE)
    assert _doc(capsys) == {"systemMessage": LINE}


def test_cc_pre_tool_use_keeps_reminders_in_the_same_document(un, capsys):
    un.emit("cc", "PreToolUse", user_text=LINE, model_text="[安全] reminder")
    doc = _doc(capsys)
    assert doc["systemMessage"] == LINE
    assert doc["hookSpecificOutput"] == {"hookEventName": "PreToolUse", "additionalContext": "[安全] reminder"}
    assert "permissionDecision" not in json.dumps(doc)


def test_cc_stop_carries_decision_and_reason_only(un, capsys):
    un.emit("cc", "Stop", user_text=LINE, model_text="dropped",
            extra={"decision": "block", "reason": "why", "continue": False})
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"systemMessage": LINE, "decision": "block", "reason": "why"}
    assert "continue" in captured.err and "model context" in captured.err


def test_other_cc_events_show_nothing(un, capsys):
    un.emit("cc", "SubagentStart", user_text=LINE, model_text="x")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SubagentStart" in captured.err


def test_nothing_to_say_writes_zero_bytes(un, capsys):
    un.emit("cc", "SessionStart")
    un.emit("cc", "UserPromptSubmit", user_text="", model_text="")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("服务未启动", id="no-prefix"),
        pytest.param("[AI Team OS] " + "宽" * 80, id="161-columns"),
        pytest.param("[AI Team OS] see https://example.invalid", id="url"),
        pytest.param("[AI Team OS] run `ls`", id="backtick"),
        pytest.param("[AI Team OS] **bold**", id="markdown-bold"),
        pytest.param("[AI Team OS] [x](y)", id="markdown-link"),
        pytest.param("[AI Team OS] bell\x07", id="control-char"),
        pytest.param(f"[AI Team OS] {ESC}]0;title{ESC}\\", id="non-sgr-escape"),
    ],
)
def test_malformed_user_lines_are_dropped_not_raised(un, capsys, bad):
    un.emit("cc", "UserPromptSubmit", user_text=bad + "\n" + LINE, model_text="note")
    captured = capsys.readouterr()
    assert json.loads(captured.out)["systemMessage"] == LINE
    assert "dropped a user line" in captured.err


def test_width_counts_columns_not_ansi(un, capsys):
    body = "宽" * 73  # 13 + 146 = 159 columns, plus colour sequences
    line = f"[AI Team OS] {ESC}[33m{body}{ESC}[39m"
    assert un.display_width(line) == 159
    un.emit("cc", "SessionStart", user_text=line)
    assert json.loads(capsys.readouterr().out)["systemMessage"] == line


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "Stop"])
def test_codex_keeps_only_accepted_fields(un, capsys, event):
    un.emit("codex", event, user_text=LINE, model_text="note",
            extra={"decision": "block", "permissionDecision": "allow", "continue": True, "stopReason": "x"})
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert set(doc) <= {"continue", "stopReason", "suppressOutput", "systemMessage", "hookSpecificOutput"}
    assert doc["hookSpecificOutput"] == {"hookEventName": event, "additionalContext": "note"}
    assert doc["continue"] is True and doc["stopReason"] == "x"
    assert "decision" in captured.err and "permissionDecision" in captured.err


def test_one_document_per_hook_run(un, capsys):
    un.emit("cc", "SessionStart", user_text=LINE)
    un.emit("cc", "SessionStart", user_text=LINE, model_text="second")
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"systemMessage": LINE}  # exactly one parseable document
    assert "second output document" in captured.err


def test_delivery_ids_recorded_only_when_every_line_was_written(un, capsys):
    un.emit("cc", "SessionStart", user_text=LINE, delivery_ids=["d-1", "d-2"])
    records = [r for r in un._tail_records("cc") if r["kind"] == "emitted"]
    assert [r["delivery_ids"] for r in records] == [["d-1", "d-2"]]
    un._WROTE_DOCUMENT = False
    un.emit("cc", "SessionStart", user_text="no prefix\n" + LINE, delivery_ids=["d-3"])
    capsys.readouterr()
    records = [r for r in un._tail_records("cc") if r["kind"] == "emitted"]
    assert [r["delivery_ids"] for r in records] == [["d-1", "d-2"]], "a dropped line is not a delivery"


def test_emit_never_raises(un, capsys, monkeypatch):
    class Broken:
        def write(self, _):
            raise OSError("closed")

        def flush(self):
            pass

    monkeypatch.setattr(un.sys, "stdout", Broken())
    un.emit("cc", "SessionStart", user_text=LINE)  # no exception
    assert "emit failed" in capsys.readouterr().err
