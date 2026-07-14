"""Tests for sub-agent context watermark capture (P1 ledger, batch 1B).

Covers agent_context pure functions, the agents-table migration, the update_agent
round-trip of the new columns, and the end-to-end SubagentStop capture path.
See docs/agent-reuse-design.md section 4.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aiteam.api import agent_context
from aiteam.api.event_bus import EventBus
from aiteam.api.hook_translator import HookTranslator
from aiteam.storage.connection import COLUMNS_TO_ENSURE, _sqlite_migrate


def _write_transcript(path: Path, *, inp: int, cache_c: int, cache_r: int, out: int) -> None:
    """Write a minimal sub-agent transcript whose last assistant line has usage."""
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


# ============================================================
# Pure functions
# ============================================================


class TestComputeWindowPct:
    def test_default_window_is_1m(self):
        window, pct = agent_context.compute_window_pct(250_000)
        assert window == 1_000_000
        assert pct == 25.0

    def test_env_override_forces_small_window(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONTEXT_SIZE", "200000")
        window, pct = agent_context.compute_window_pct(100_000)
        assert window == 200_000
        assert pct == 50.0

    def test_env_ignored_when_not_positive_int(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONTEXT_SIZE", "not-a-number")
        window, _ = agent_context.compute_window_pct(1)
        assert window == 1_000_000


class TestMeasure:
    def test_measure_sums_four_usage_fields(self, tmp_path: Path):
        t = tmp_path / "agent-atest.jsonl"
        _write_transcript(t, inp=2, cache_c=1000, cache_r=40000, out=3)
        m = agent_context.measure(t)
        assert m is not None
        assert m["ctx_tokens"] == 41005  # 2 + 1000 + 40000 + 3
        assert m["ctx_window"] == 1_000_000
        assert m["ctx_pct"] == 4.1
        assert m["ctx_measured_at"] is not None

    def test_measure_returns_none_on_missing_file(self, tmp_path: Path):
        assert agent_context.measure(tmp_path / "nope.jsonl") is None

    def test_measure_returns_none_without_usage(self, tmp_path: Path):
        t = tmp_path / "agent-nousage.jsonl"
        t.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}), encoding="utf-8")
        assert agent_context.measure(t) is None


class TestLocateTranscript:
    def test_prefers_existing_stored_path(self, tmp_path: Path):
        t = tmp_path / "agent-astored.jsonl"
        t.write_text("{}", encoding="utf-8")
        found = agent_context.locate_transcript(
            stored_path=str(t), cc_tool_use_id="astored", session_id=None
        )
        assert found == t

    def test_reconstructs_by_cc_id(self, tmp_path: Path, monkeypatch):
        # Build a fake ~/.claude/projects/<slug>/<session>/subagents/agent-<ccid>.jsonl
        slug = "-fake-project"
        session = "sess-xyz"
        sub = tmp_path / slug / session / "subagents"
        sub.mkdir(parents=True)
        t = sub / "agent-arebuilt.jsonl"
        t.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(agent_context, "_projects_dir", lambda: tmp_path)
        found = agent_context.locate_transcript(
            stored_path=None, cc_tool_use_id="arebuilt", session_id=session
        )
        assert found == t

    def test_returns_none_without_cc_id(self):
        assert agent_context.locate_transcript(
            stored_path=None, cc_tool_use_id=None, session_id="s"
        ) is None


# ============================================================
# Migration
# ============================================================


class TestAgentsMigration:
    def _legacy_agents_db(self, path: str) -> None:
        con = sqlite3.connect(path)
        con.execute(
            """CREATE TABLE agents (
                id TEXT PRIMARY KEY,
                team_id TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT
            )"""
        )
        con.commit()
        con.close()

    def test_columns_registered_in_ensure_list(self):
        pairs = {(t, c) for t, c, _ in COLUMNS_TO_ENSURE}
        for col in ("ctx_tokens", "ctx_window", "ctx_pct", "transcript_path", "ctx_measured_at", "reuse_domain"):
            assert ("agents", col) in pairs

    def test_migration_adds_ctx_columns(self, tmp_path: Path):
        db = str(tmp_path / "legacy.db")
        self._legacy_agents_db(db)
        _sqlite_migrate(db)
        con = sqlite3.connect(db)
        cols = {row[1] for row in con.execute("PRAGMA table_info(agents)")}
        con.close()
        assert {"ctx_tokens", "ctx_window", "ctx_pct", "transcript_path", "ctx_measured_at", "reuse_domain"} <= cols

    def test_migration_idempotent(self, tmp_path: Path):
        db = str(tmp_path / "legacy.db")
        self._legacy_agents_db(db)
        _sqlite_migrate(db)
        _sqlite_migrate(db)  # must not raise


# ============================================================
# DB round-trip + end-to-end SubagentStop capture
# ============================================================


class TestUpdateAgentRoundtrip:
    @pytest.mark.asyncio
    async def test_ctx_fields_persist(self, db_repository):
        repo = db_repository
        agent = await repo.create_agent(team_id="t1", name="researcher", role="researcher")
        await repo.update_agent(
            agent.id,
            ctx_tokens=71683,
            ctx_window=1_000_000,
            ctx_pct=7.2,
            transcript_path="/some/agent-a1.jsonl",
        )
        fetched = await repo.get_agent(agent.id)
        assert fetched.ctx_tokens == 71683
        assert fetched.ctx_pct == 7.2
        assert fetched.transcript_path == "/some/agent-a1.jsonl"
        assert fetched.reuse_domain is None  # provisioned, not written in P1


class TestSubagentStopCapture:
    @pytest.mark.asyncio
    async def test_stop_records_watermark(self, db_repository, tmp_path: Path):
        repo = db_repository
        event_bus = EventBus(repo=repo)
        translator = HookTranslator(repo=repo, event_bus=event_bus)

        cc_id = "asubstop01"
        agent = await repo.create_agent(
            team_id="team-x",
            name="researcher",
            role="researcher",
            source="hook",
            session_id="sess1",
            cc_tool_use_id=cc_id,
        )
        await repo.update_agent(agent.id, status="busy")

        transcript = tmp_path / f"agent-{cc_id}.jsonl"
        _write_transcript(transcript, inp=5, cache_c=2000, cache_r=90000, out=10)

        result = await translator.handle_event(
            {
                "hook_event_name": "SubagentStop",
                "agent_id": cc_id,
                "agent_type": "researcher",
                "session_id": "sess1",
                "agent_transcript_path": str(transcript),
            }
        )
        assert result["status"] == "updated"

        fetched = await repo.get_agent(agent.id)
        assert fetched.ctx_tokens == 92015  # 5 + 2000 + 90000 + 10
        assert fetched.ctx_window == 1_000_000
        assert fetched.ctx_pct == pytest.approx(9.2, abs=0.1)
        assert fetched.transcript_path == str(transcript)
        # status must be untouched by the stop path (reaper owns transitions)
        assert str(fetched.status).endswith("busy")
