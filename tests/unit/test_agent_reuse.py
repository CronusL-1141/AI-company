"""Tests for agent reuse recommendation (P2 decision layer, batch 3B).

Covers the pure decision logic (domain match, availability inference, the
three-way action tree with its boundaries), the ranking/filtering in
build_recommendations, the repo-scoped gather, and the compact projection.
See docs/agent-reuse-design.md section 5.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from aiteam.api import agent_reuse as ar
from aiteam.mcp.tools.views import compact_reuse_candidate_row, resolve_view
from aiteam.types import Agent, AgentStatus

NOW = datetime(2026, 7, 14, 12, 0, 0)


# ============================================================
# tokenize / domain_match
# ============================================================


class TestDomainMatch:
    def test_tokenize_drops_stopwords_and_short(self):
        toks = ar.tokenize("Fix the API bug in auth")
        assert "api" in toks and "auth" in toks and "bug" in toks
        assert "the" not in toks  # stopword
        assert "in" not in toks  # stopword

    def test_tokenize_captures_cjk_chars(self):
        toks = ar.tokenize("修复搜索模块")
        assert "搜" in toks and "索" in toks

    def test_overlap_coefficient(self):
        q = {"api", "auth", "bug"}
        a = {"api", "auth"}
        # |{api,auth}| / min(3,2) = 2/2 = 1.0
        assert ar.domain_match(q, a) == 1.0

    def test_no_overlap_is_zero(self):
        assert ar.domain_match({"api"}, {"frontend"}) == 0.0

    def test_empty_is_zero(self):
        assert ar.domain_match(set(), {"api"}) == 0.0


# ============================================================
# infer_availability
# ============================================================


class TestInferAvailability:
    def test_fresh_busy_same_session_is_live(self):
        assert (
            ar.infer_availability(
                status="busy",
                session_id="s1",
                ctx_measured_at=NOW - timedelta(hours=1),
                last_active_at=NOW - timedelta(hours=1),
                now=NOW,
                caller_session_id="s1",
            )
            == ar.AVAIL_LIVE
        )

    def test_fresh_offline_same_session_is_resumable(self):
        assert (
            ar.infer_availability(
                status="offline",
                session_id="s1",
                ctx_measured_at=NOW - timedelta(days=2),
                last_active_at=NOW - timedelta(days=2),
                now=NOW,
                caller_session_id="s1",
            )
            == ar.AVAIL_RESUMABLE
        )

    def test_different_session_is_cross_session_even_if_busy(self):
        assert (
            ar.infer_availability(
                status="busy",
                session_id="other",
                ctx_measured_at=NOW - timedelta(hours=1),
                last_active_at=NOW - timedelta(hours=1),
                now=NOW,
                caller_session_id="s1",
            )
            == ar.AVAIL_CROSS_SESSION
        )

    def test_past_retention_window_is_expired(self):
        assert (
            ar.infer_availability(
                status="offline",
                session_id="s1",
                ctx_measured_at=NOW - timedelta(days=ar.CLEANUP_WINDOW_DAYS + 1),
                last_active_at=NOW - timedelta(days=ar.CLEANUP_WINDOW_DAYS + 1),
                now=NOW,
                caller_session_id="s1",
            )
            == ar.AVAIL_EXPIRED
        )

    def test_boundary_exactly_at_window_is_fresh(self):
        # Exactly CLEANUP_WINDOW_DAYS old is still within the window (<=).
        assert (
            ar.infer_availability(
                status="offline",
                session_id="s1",
                ctx_measured_at=NOW - timedelta(days=ar.CLEANUP_WINDOW_DAYS),
                last_active_at=None,
                now=NOW,
                caller_session_id="s1",
            )
            == ar.AVAIL_RESUMABLE
        )

    def test_no_caller_session_degrades_to_status(self):
        # Without caller session, offline+fresh -> resumable (cannot tell cross-session).
        assert (
            ar.infer_availability(
                status="offline",
                session_id="other",
                ctx_measured_at=NOW - timedelta(days=1),
                last_active_at=NOW - timedelta(days=1),
                now=NOW,
                caller_session_id=None,
            )
            == ar.AVAIL_RESUMABLE
        )


# ============================================================
# recommend_action — the three-way decision tree
# ============================================================


class TestRecommendAction:
    def test_cross_domain_spawns_new(self):
        action, _ = ar.recommend_action(
            dmatch=0.4, availability=ar.AVAIL_LIVE, ctx_pct=10.0, ctx_tokens=5000
        )
        assert action == ar.ACTION_NEW

    def test_expired_spawns_new(self):
        action, _ = ar.recommend_action(
            dmatch=0.9, availability=ar.AVAIL_EXPIRED, ctx_pct=10.0, ctx_tokens=5000
        )
        assert action == ar.ACTION_NEW

    def test_cross_session_spawns_new(self):
        action, hint = ar.recommend_action(
            dmatch=0.9, availability=ar.AVAIL_CROSS_SESSION, ctx_pct=10.0, ctx_tokens=5000
        )
        assert action == ar.ACTION_NEW
        assert "resume" in hint or "会话" in hint

    def test_low_watermark_reuses(self):
        action, _ = ar.recommend_action(
            dmatch=0.9, availability=ar.AVAIL_LIVE, ctx_pct=30.0, ctx_tokens=50_000
        )
        assert action == ar.ACTION_REUSE

    def test_mid_watermark_slims(self):
        action, _ = ar.recommend_action(
            dmatch=0.9, availability=ar.AVAIL_RESUMABLE, ctx_pct=70.0, ctx_tokens=130_000
        )
        assert action == ar.ACTION_SLIM

    def test_high_pct_spawns_new(self):
        action, _ = ar.recommend_action(
            dmatch=0.9, availability=ar.AVAIL_LIVE, ctx_pct=90.0, ctx_tokens=100_000
        )
        assert action == ar.ACTION_NEW

    def test_absolute_token_floor_trips_before_pct(self):
        # Low pct but huge absolute tokens (window mis-detection guard) -> new.
        action, _ = ar.recommend_action(
            dmatch=0.9, availability=ar.AVAIL_LIVE, ctx_pct=20.0, ctx_tokens=200_000
        )
        assert action == ar.ACTION_NEW

    def test_absolute_token_floor_mid_band_slims(self):
        # Low pct, tokens between reuse and slim floors -> slim.
        action, _ = ar.recommend_action(
            dmatch=0.9, availability=ar.AVAIL_LIVE, ctx_pct=20.0, ctx_tokens=130_000
        )
        assert action == ar.ACTION_SLIM

    def test_unknown_watermark_treated_low(self):
        action, rationale = ar.recommend_action(
            dmatch=0.9, availability=ar.AVAIL_LIVE, ctx_pct=None, ctx_tokens=None
        )
        assert action == ar.ACTION_REUSE
        assert "未测" in rationale


# ============================================================
# build_recommendations — ranking + filtering + default
# ============================================================


def _agent(**kw) -> Agent:
    base = {
        "team_id": "t1",
        "name": "a",
        "role": "researcher",
        "cc_tool_use_id": "acc-" + kw.get("name", "a"),
        "session_id": "s1",
        "status": AgentStatus.BUSY,
        "ctx_pct": 20.0,
        "ctx_tokens": 40_000,
        "ctx_measured_at": NOW - timedelta(hours=1),
        "last_active_at": NOW - timedelta(hours=1),
    }
    base.update(kw)
    return Agent(**base)


class TestBuildRecommendations:
    def test_excludes_leader_workflow_and_no_cc_id(self):
        agents = [
            _agent(name="leader1", role="leader"),
            _agent(name="wf1", role="workflow-subagent"),
            _agent(name="nocc", cc_tool_use_id=None),
            _agent(name="good", role="researcher", current_task="fix auth api bug"),
        ]
        out = ar.build_recommendations(
            agents=agents, query_text="auth api bug", now=NOW, caller_session_id="s1"
        )
        names = {c["name"] for c in out["candidates"]}
        assert names == {"good"}

    def test_ranks_same_domain_reusable_first_and_defaults_reuse(self):
        agents = [
            _agent(name="offtopic", role="frontend", current_task="css layout"),
            _agent(name="ontopic", role="researcher", current_task="auth api bug deep dive"),
        ]
        out = ar.build_recommendations(
            agents=agents, query_text="auth api bug", now=NOW, caller_session_id="s1"
        )
        assert out["candidates"][0]["name"] == "ontopic"
        assert out["candidates"][0]["recommended_action"] == ar.ACTION_REUSE
        assert out["default_recommendation"] == ar.ACTION_REUSE

    def test_addresses_by_cc_id_in_hint(self):
        agents = [_agent(name="ontopic", current_task="auth api bug", cc_tool_use_id="aXYZ")]
        out = ar.build_recommendations(
            agents=agents, query_text="auth api bug", now=NOW, caller_session_id="s1"
        )
        assert "aXYZ" in out["candidates"][0]["resume_hint"]

    def test_no_reachable_candidate_defaults_new(self):
        # Cross-domain only -> no reuse-worthy candidate -> default spawn_new.
        agents = [_agent(name="offtopic", role="frontend", current_task="css layout work")]
        out = ar.build_recommendations(
            agents=agents, query_text="database migration", now=NOW, caller_session_id="s1"
        )
        assert out["default_recommendation"] == ar.ACTION_NEW

    def test_thresholds_reported_for_tuning_transparency(self):
        out = ar.build_recommendations(agents=[], query_text="x", now=NOW)
        assert out["thresholds"]["domain_match_min"] == ar.DOMAIN_MATCH_MIN
        assert out["candidates"] == []


# ============================================================
# compact projection
# ============================================================


class TestCompactProjection:
    def test_keeps_signals_and_call_keys_drops_rationale(self):
        full = {
            "agent_id": "id1",
            "cc_tool_use_id": "acc1",
            "session_id": "s1",
            "name": "n",
            "role": "r",
            "domain_match": 0.8,
            "ctx_pct": 20.0,
            "ctx_tokens": 40000,
            "availability": "live",
            "recommended_action": "reuse",
            "resume_hint": "SendMessage(to='acc1')",
            "rationale": "some long explanatory rationale text",
            "ctx_window": 1_000_000,
        }
        row = compact_reuse_candidate_row(full)
        assert row["agent_id"] == "id1"
        assert row["cc_tool_use_id"] == "acc1"
        assert row["recommended_action"] == "reuse"
        assert row["resume_hint"] == "SendMessage(to='acc1')"  # call key kept whole
        assert "rationale" not in row  # verbose, dropped in compact
        assert "ctx_window" not in row

    def test_resolve_view(self):
        assert resolve_view("compact") == "compact"
        assert resolve_view("") == "compact"
        assert resolve_view("all") == "all"
        assert resolve_view("bogus") is None


# ============================================================
# repo-scoped gather + route wiring
# ============================================================


class TestRepoGatherAndRoute:
    @pytest.mark.asyncio
    async def test_gather_from_repo(self, db_repository):
        repo = db_repository
        team = await repo.create_team(name="reuse-team", mode="coordinate")
        agent = await repo.create_agent(
            team_id=team.id,
            name="researcher",
            role="researcher",
            source="hook",
            session_id="s1",
            cc_tool_use_id="acc-r1",
        )
        await repo.update_agent(
            agent.id,
            status="busy",
            current_task="auth api bug",
            ctx_tokens=40_000,
            ctx_pct=4.0,
            ctx_window=1_000_000,
            ctx_measured_at=datetime.now(),
            last_active_at=datetime.now(),
        )
        agents = await repo.list_agents(team.id)
        out = ar.build_recommendations(
            agents=agents,
            query_text="auth api bug",
            now=datetime.now(),
            caller_session_id="s1",
        )
        assert len(out["candidates"]) == 1
        assert out["candidates"][0]["recommended_action"] == ar.ACTION_REUSE

    def test_route_wiring_smoke(self):
        """End-to-end HTTP wiring: the endpoint is registered and returns the
        expected envelope (empty candidates on a fresh in-memory DB is fine)."""
        import asyncio
        from contextlib import asynccontextmanager

        from fastapi.testclient import TestClient

        from aiteam.api import deps
        from aiteam.api.app import create_app
        from aiteam.api.event_bus import EventBus
        from aiteam.memory.store import MemoryStore
        from aiteam.orchestrator.team_manager import TeamManager
        from aiteam.storage.connection import close_db
        from aiteam.storage.repository import StorageRepository

        repo = StorageRepository(db_url="sqlite+aiosqlite://")
        asyncio.get_event_loop().run_until_complete(repo.init_db())
        memory = MemoryStore(repository=repo)
        deps._repository = repo
        deps._memory_store = memory
        deps._event_bus = EventBus(repo=repo)
        deps._manager = TeamManager(repository=repo, memory=memory)

        app = create_app()

        @asynccontextmanager
        async def _noop_lifespan(app):
            yield

        app.router.lifespan_context = _noop_lifespan
        try:
            client = TestClient(app)
            resp = client.get("/api/agents/reuse-recommend?query=anything")
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert "candidates" in body
            assert "default_recommendation" in body
        finally:
            asyncio.get_event_loop().run_until_complete(close_db())
            deps._repository = None
            deps._memory_store = None
            deps._event_bus = None
            deps._manager = None
