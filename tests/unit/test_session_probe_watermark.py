"""Tests for session_probe.detect_live_sessions' context watermark fields.

Fleet layer P2 observation (docs/fleet-layer-design.md section 6.2): main-session
watermark reuses agent_context.read_ctx_tokens/compute_window_pct (batch 1B),
applied to the main-session transcript instead of a sub-agent transcript. See
tests/unit/test_agent_context_watermark.py for the underlying pure-function
coverage; this file only covers the session_probe wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiteam.api import session_probe


def _write_main_transcript(path: Path, *, inp: int, cache_c: int, cache_r: int, out: int) -> None:
    lines = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5",
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": cache_c,
                    "cache_read_input_tokens": cache_r,
                    "output_tokens": out,
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


class TestDetectLiveSessionsWatermark:
    def test_live_session_carries_ctx_watermark(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_probe, "_claude_projects_dir", lambda: tmp_path)
        root_path = "/Users/cronus/Desktop/some-project"
        slug = session_probe.project_slug(root_path)
        pdir = tmp_path / slug
        pdir.mkdir(parents=True)
        transcript = pdir / "11111111-1111-1111-1111-111111111111.jsonl"
        _write_main_transcript(transcript, inp=100_000, cache_c=50_000, cache_r=20_000, out=5_000)

        sessions = session_probe.detect_live_sessions(root_path)
        assert len(sessions) == 1
        s = sessions[0]
        assert s["ctx_tokens"] == 175_000
        assert s["ctx_window"] == 1_000_000
        assert s["ctx_pct"] == 17.5

    def test_unreadable_transcript_leaves_watermark_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_probe, "_claude_projects_dir", lambda: tmp_path)
        root_path = "/Users/cronus/Desktop/some-project"
        slug = session_probe.project_slug(root_path)
        pdir = tmp_path / slug
        pdir.mkdir(parents=True)
        transcript = pdir / "22222222-2222-2222-2222-222222222222.jsonl"
        transcript.write_text("", encoding="utf-8")  # no assistant usage line

        sessions = session_probe.detect_live_sessions(root_path)
        assert len(sessions) == 1
        s = sessions[0]
        assert s["ctx_tokens"] is None
        assert s["ctx_window"] is None
        assert s["ctx_pct"] is None

    def test_env_override_forces_smaller_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr(session_probe, "_claude_projects_dir", lambda: tmp_path)
        monkeypatch.setenv("CLAUDE_CONTEXT_SIZE", "200000")
        root_path = "/Users/cronus/Desktop/some-project"
        slug = session_probe.project_slug(root_path)
        pdir = tmp_path / slug
        pdir.mkdir(parents=True)
        transcript = pdir / "33333333-3333-3333-3333-333333333333.jsonl"
        _write_main_transcript(transcript, inp=80_000, cache_c=0, cache_r=0, out=20_000)

        sessions = session_probe.detect_live_sessions(root_path)
        s = sessions[0]
        assert s["ctx_tokens"] == 100_000
        assert s["ctx_window"] == 200_000
        assert s["ctx_pct"] == 50.0
