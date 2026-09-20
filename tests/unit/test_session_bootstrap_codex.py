"""Codex SessionStart adapter keeps host stdout JSON-only."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

HOOK = Path(__file__).parents[2] / "plugin/harness/codex/hooks/session_bootstrap_codex.py"


def _module():
    spec = importlib.util.spec_from_file_location("session_bootstrap_codex", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_session_start_output_is_valid_codex_json(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(module, "_get", lambda path, cwd="": None)
    monkeypatch.setattr(module.sys, "stdin", type("Input", (), {"read": lambda self: "{}"})())

    module.main()

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert list(document) == ["hookSpecificOutput"]
    output = document["hookSpecificOutput"]
    assert output["hookEventName"] == "SessionStart"
    assert isinstance(output["additionalContext"], str)
    assert "api_unreachable" in captured.err


def test_health_probe_accepts_non_ascii_cwd_without_project_header(monkeypatch):
    import io
    module = _module()
    monkeypatch.setattr(module, "_api_url", lambda: "http://127.0.0.1:1")
    observed = []
    def open_request(request, **kwargs):
        observed.append(request)
        # HTTP encodes header values using latin-1; cwd is unnecessary for health.
        for value in request.headers.values():
            value.encode("latin-1")
        return io.BytesIO(b'{"status":"ok"}')
    monkeypatch.setattr(module.urllib.request, "urlopen", open_request)
    assert "API 可达" in module._context({"cwd": "/tmp/中文项目"})
    assert observed[0].headers == {}
