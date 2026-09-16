"""Tests for I17b — cross-version Codex hook trust drift.

The property under test is the one the host gives no feedback about: trust is
keyed on ``<event>:<group>:<handler>``, so removing or inserting a group in the
middle of an event slides every later group of that event into its predecessor's
slot, which the host still considers approved. Nothing is logged on either side.
I17 cannot see this because it only compares the lock with the manifest inside a
single commit; only a diff against the last published surface can.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_codex_trust_drift.py"
_spec = importlib.util.spec_from_file_location("check_codex_trust_drift", SCRIPT)
assert _spec and _spec.loader
drift = importlib.util.module_from_spec(_spec)
sys.modules["check_codex_trust_drift"] = drift
_spec.loader.exec_module(drift)


def _entry(key: str, command: str = "a", windows: str = "b") -> dict:
    return {
        "key": key,
        "command_sha256": f"sha256:{command}",
        "command_windows_sha256": f"sha256:{windows}",
    }


def _as_map(entries: list[dict]) -> dict[str, dict]:
    return {e["key"]: e for e in entries}


class TestClassify:
    def test_identical_surfaces_keep_every_approval(self):
        surface = _as_map([_entry("pre_tool_use:0:0"), _entry("stop:0:0")])
        verdict = drift.classify(surface, surface)
        assert verdict["unchanged"] == ["pre_tool_use:0:0", "stop:0:0"]
        assert not verdict["changed"] and not verdict["added"] and not verdict["removed"]

    def test_tail_append_only_asks_for_the_new_one(self):
        baseline = _as_map([_entry("session_start:0:0")])
        current = _as_map([_entry("session_start:0:0"), _entry("session_start:0:1")])
        verdict = drift.classify(baseline, current)
        assert verdict["added"] == ["session_start:0:1"]
        assert verdict["unchanged"] == ["session_start:0:0"]
        assert not verdict["changed"]

    def test_tail_removal_costs_nobody_their_trust(self):
        """What the 2026-09-15 Codex hook cleanup actually did."""
        baseline = _as_map([_entry("post_tool_use:0:0"), _entry("post_tool_use:1:0", "x")])
        current = _as_map([_entry("post_tool_use:0:0")])
        verdict = drift.classify(baseline, current)
        assert verdict["removed"] == ["post_tool_use:1:0"]
        assert verdict["unchanged"] == ["post_tool_use:0:0"]
        assert not verdict["changed"]

    def test_middle_removal_is_reported_as_slot_reuse(self):
        """Drop group 1 of three: group 2 slides into slot 1 and inherits its trust."""
        baseline = _as_map(
            [
                _entry("post_tool_use:0:0", "send"),
                _entry("post_tool_use:1:0", "review"),
                _entry("post_tool_use:2:0", "meeting"),
            ]
        )
        # After the removal the manifest renumbers: meeting is now group 1.
        current = _as_map(
            [_entry("post_tool_use:0:0", "send"), _entry("post_tool_use:1:0", "meeting")]
        )
        verdict = drift.classify(baseline, current)
        assert verdict["changed"] == ["post_tool_use:1:0"], (
            "a slot whose command changed must be reported, or the host silently "
            "runs 'meeting' under the approval the user gave 'review'"
        )
        assert verdict["removed"] == ["post_tool_use:2:0"]

    def test_middle_insertion_is_reported_as_slot_reuse(self):
        baseline = _as_map([_entry("pre_tool_use:0:0", "send"), _entry("pre_tool_use:1:0", "gate")])
        current = _as_map(
            [
                _entry("pre_tool_use:0:0", "send"),
                _entry("pre_tool_use:1:0", "new"),
                _entry("pre_tool_use:2:0", "gate"),
            ]
        )
        verdict = drift.classify(baseline, current)
        assert verdict["changed"] == ["pre_tool_use:1:0"]
        assert verdict["added"] == ["pre_tool_use:2:0"]

    def test_windows_only_change_still_counts(self):
        """Both platform commands are part of the declaration; either drifting matters."""
        baseline = _as_map([_entry("stop:0:0", "same", "old")])
        current = _as_map([_entry("stop:0:0", "same", "new")])
        assert drift.classify(baseline, current)["changed"] == ["stop:0:0"]


class TestMainExit:
    @staticmethod
    def _plant(tmp_path: Path, monkeypatch, baseline: list[dict], current: list[dict]) -> None:
        base = tmp_path / "hook-trust.baseline.lock"
        lock = tmp_path / "hook-trust.lock"
        base.write_text(
            json.dumps({"released_version": "9.9.9", "entries": baseline}), encoding="utf-8"
        )
        lock.write_text(json.dumps({"entries": current}), encoding="utf-8")
        monkeypatch.setattr(drift, "BASELINE_PATH", base)
        monkeypatch.setattr(drift, "LOCK_PATH", lock)
        monkeypatch.setattr(sys, "argv", ["check_codex_trust_drift.py"])

    def test_exit_1_on_slot_reuse(self, tmp_path: Path, monkeypatch, capsys):
        self._plant(
            tmp_path, monkeypatch, [_entry("stop:0:0", "old")], [_entry("stop:0:0", "new")]
        )
        assert drift.main() == 1
        out = capsys.readouterr().out
        assert "stop:0:0" in out and "v9.9.9" in out

    def test_exit_0_when_only_appended(self, tmp_path: Path, monkeypatch, capsys):
        self._plant(
            tmp_path,
            monkeypatch,
            [_entry("stop:0:0")],
            [_entry("stop:0:0"), _entry("stop:0:1", "new")],
        )
        assert drift.main() == 0
        assert "stop:0:1" in capsys.readouterr().out, "a new slot must reach the release notes"

    def test_missing_baseline_fails_loudly(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(drift, "BASELINE_PATH", tmp_path / "nope.lock")
        monkeypatch.setattr(drift, "LOCK_PATH", tmp_path / "also-nope.lock")
        monkeypatch.setattr(sys, "argv", ["check_codex_trust_drift.py"])
        with pytest.raises(SystemExit):
            drift.main()


class TestAdvance:
    def test_advance_adopts_the_current_surface(self, tmp_path: Path, monkeypatch, capsys):
        base = tmp_path / "hook-trust.baseline.lock"
        lock = tmp_path / "hook-trust.lock"
        base.write_text(
            json.dumps({"released_version": "1.0.0", "entries": [_entry("stop:0:0")]}),
            encoding="utf-8",
        )
        lock.write_text(
            json.dumps({"entries": [_entry("stop:0:0"), _entry("stop:0:1", "new")]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(drift, "BASELINE_PATH", base)
        monkeypatch.setattr(drift, "LOCK_PATH", lock)
        assert drift.advance() == 0
        adopted = json.loads(base.read_text(encoding="utf-8"))
        assert [e["key"] for e in adopted["entries"]] == ["stop:0:0", "stop:0:1"]
        # The version is not guessed: the release step has to set it deliberately.
        assert adopted["released_version"] == "1.0.0"
        assert "released_version" in capsys.readouterr().out


class TestRepoState:
    def test_shipped_baseline_and_lock_agree_on_schema(self):
        codex = Path(__file__).resolve().parents[2] / "plugin" / "harness" / "codex"
        baseline = json.loads((codex / "hook-trust.baseline.lock").read_text(encoding="utf-8"))
        lock = json.loads((codex / "hook-trust.lock").read_text(encoding="utf-8"))
        assert baseline["released_version"], "baseline must name the release it came from"
        keys = {e["key"] for e in baseline["entries"]}
        assert keys, "an empty baseline would make every slot look brand new"
        # Every surviving slot must still mean what the published release meant.
        assert not drift.classify(
            {e["key"]: e for e in baseline["entries"]},
            {e["key"]: e for e in lock["entries"]},
        )["changed"]
