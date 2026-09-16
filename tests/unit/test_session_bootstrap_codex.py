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
