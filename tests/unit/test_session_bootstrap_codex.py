"""Codex SessionStart preserves model context through the shared notice exit."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from aiteam.types import PendingResponse

HOOK = Path(__file__).parents[2] / "plugin/harness/codex/hooks/session_bootstrap_codex.py"
SCHEMA = json.loads((Path(__file__).parent / "hooks/codex_hook_output.schema.json").read_text())


def _module():
    spec = importlib.util.spec_from_file_location("session_bootstrap_codex", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _response(notice, **fields):
    value = PendingResponse.model_validate(fields, strict=True)
    return notice.Pending(value.language, value.user_text, value.model_text, value.delivery_ids)


@pytest.fixture
def setup(monkeypatch):
    module = _module()
    notice = sys.modules["user_notice"]
    monkeypatch.setattr(module, "_get", lambda *args, **kwargs: {"status": "ok"})
    monkeypatch.setattr(module, "_tool_index", lambda payload, language="": "MODEL_TOOL_INDEX")
    monkeypatch.setattr(notice, "resolve_language_local", lambda *args: "en")
    return module, notice


def _run(module, monkeypatch, capsys, **payload):
    monkeypatch.setattr(module.sys, "stdin", io.StringIO(json.dumps(payload)))
    module.main()
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    Draft7Validator(SCHEMA).validate(document)
    return document, captured.err


def test_session_start_keeps_both_channels_and_reports_only_after_output(setup, monkeypatch, capsys):
    module, notice = setup
    calls = []
    line = "[AI Team OS] v1.15.0 available"
    note = "Full update instructions from catalog"
    def pending(*args, **kwargs):
        calls.append((args, kwargs))
        return _response(notice, language="en", user_text=line, model_text=note, delivery_ids=["d-1"])
    monkeypatch.setattr(notice, "fetch_pending", pending)
    document, error = _run(module, monkeypatch, capsys, source="startup", session_id="s", cwd="/中文项目")
    assert document["systemMessage"] == line
    assert document["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    context = document["hookSpecificOutput"]["additionalContext"]
    assert "API 可达" in context and "MODEL_TOOL_INDEX" in context and note in context
    assert calls[0][0] == (
        "codex", "SessionStart", "startup", {"source": "startup", "session_id": "s", "cwd": "/中文项目"},
    )
    assert calls[0][1] == {"reader": "leader-codex", "timeout": 2.0}
    assert [r["delivery_ids"] for r in notice._tail_records("codex") if r["kind"] == "emitted"] == [["d-1"]]
    assert not error


@pytest.mark.parametrize("failure", ["unsupported", "error", "timeout"])
def test_unavailable_ledger_preserves_model_index_without_old_release_calls(setup, monkeypatch, capsys, failure):
    module, notice = setup
    monkeypatch.setattr(notice, "fetch_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr(notice, "last_failure", lambda: failure)
    calls = []
    monkeypatch.setattr(module, "_get", lambda path, **kwargs: calls.append(path) or {"status": "ok"})
    document, _ = _run(module, monkeypatch, capsys, source="resume")
    assert set(document) == {"hookSpecificOutput"}
    assert "MODEL_TOOL_INDEX" in document["hookSpecificOutput"]["additionalContext"]
    assert calls == ["/api/health"]


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_api_down_retries_and_uses_local_catalog_once(setup, monkeypatch, capsys, source):
    module, notice = setup
    calls = []
    monkeypatch.setattr(notice, "fetch_pending", lambda *args, **kwargs: calls.append(args) or None)
    monkeypatch.setattr(notice, "last_failure", lambda: "unreachable")
    monkeypatch.setattr(module.time, "sleep", lambda duration: None)
    monkeypatch.setattr(module, "_get", lambda *args, **kwargs: None)
    document, error = _run(module, monkeypatch, capsys, source=source, session_id="offline")
    assert len(calls) == 2
    expected, note = notice.render_local("api_down", {}, host="codex", language="en", reliable=source == "startup")
    assert document["systemMessage"] == expected
    assert note in document["hookSpecificOutput"]["additionalContext"]
    assert "api_unreachable" in error
    notice._WROTE_DOCUMENT = False
    repeated, _ = _run(module, monkeypatch, capsys, source=source, session_id="offline")
    assert "systemMessage" not in repeated


def test_child_keeps_model_index_without_claiming_leader_notices(setup, monkeypatch, capsys):
    module, notice = setup
    monkeypatch.setattr(notice, "fetch_pending", lambda *args, **kwargs: pytest.fail("child claimed notices"))
    document, _ = _run(module, monkeypatch, capsys, agent_id="child", agent_type="worker")
    assert "MODEL_TOOL_INDEX" in document["hookSpecificOutput"]["additionalContext"]
    assert "systemMessage" not in document


def test_health_probe_accepts_non_ascii_cwd_without_project_header(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "_api_url", lambda: "http://127.0.0.1:9")
    observed = []
    def open_request(request, **kwargs):
        observed.append(request)
        for value in request.headers.values():
            value.encode("latin-1")
        return io.BytesIO(b'{"status":"ok"}')
    monkeypatch.setattr(module.urllib.request, "urlopen", open_request)
    assert "API 可达" in module._context({"cwd": "/tmp/中文项目"})
    assert observed[0].headers == {}
