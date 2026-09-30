"""Seed a neutral demo AI Team OS database for screenshots and walkthroughs.

Builds a self-contained SQLite database at the ``--db`` path using the project's
own storage layer (``types`` Pydantic models + ``storage.models`` ORM +
``storage.connection`` async session). Everything written is generic, English and
technology-neutral — a fictional "Aurora Web Platform" product — so the dashboard
can be demoed without exposing any real project vocabulary.

What it creates (all DB-backed, portable):
  * 1 project              "Aurora Web Platform"
  * 2 delivery teams       Platform Core + Quality Guild (active, Claude Code)
  * 1 Codex session team   Codex Leader + members with Codex-style nicknames
  * 4 workflow teams        1 completed-rich + 1 running-inflight + 2 light completed
  * 1 session container     completed CC-session team (history)
  * 1 archived team         plain delivery squad (history)
  * 3 leader agents         two Claude sessions (context watermarks) + one Codex
  * 7 worker agents         varied status / trust score / model (some ctx bars)
  * 5 workflow members      members of the running workflow team
  * 21 tasks               every status and horizon; recent work, a stale running
                           task, a blocked and a failed one, dormant backlog
  * ~40 task memos         mixed authors and types, several in the last hours
  * 4 briefings            three decisions waiting on the user, one resolved
  * 8 user notices         four queued (one snoozed; terminal deliveries) + four
                           recent blocks, written through the notice ledger itself
  * 1 Codex plan account   three days of quota observations and priced local
                           requests (5-hour and weekly windows)
  * 2 reports              a design doc and an analysis
  * 1 meeting + 6 messages sprint planning, two discussion rounds
  * ~130 agent activities  spread across the last ~20h (fills swimlane / analytics)
  * 80+ events             spread across the last six weeks
  * 4 workflow runs        with per-agent telemetry (swimlanes, one failed lane)
  * token usage            measured on most dispatches, agent->task edges from
                           memo authorship (Token Attribution page)
  * 18 ecosystem profiles  real public open-source repos, neutral metadata

Live state decays: the API resets busy agents on startup and its reaper takes a
busy hook agent offline after five quiet minutes. Run with ``--refresh-live``
against an existing demo database (API running) to put the working agents,
the running workflow and the fixture transcripts back to "a minute ago".

Two of the project-detail cards are LIVE probes, NOT rows in the DB (探查结论):
  * Leaders (with context %) come from ``session_probe`` reading
    ``~/.claude/projects/<slug>/*.jsonl`` transcripts, keyed on project.root_path.
  * Worktrees come from ``worktree_probe`` running ``git worktree list`` on
    project.root_path.
Pass ``--fixture`` to also build a throwaway git repo (with two worktrees) and
two synthetic session transcripts under the project's root_path, so those two
cards render for a project-detail screenshot. Without ``--fixture`` the DB is a
clean, portable artifact and those two cards degrade gracefully to empty. The
fixture writes under ``~/.claude/projects``: run it with an isolated ``HOME``.

Usage:
    python scripts/demo_seed.py --db /tmp/aiteam-demo.db [--force] [--fixture]
    python scripts/demo_seed.py --db /tmp/aiteam-demo.db --refresh-live

Options:
    --db PATH        Target SQLite file to build (required).
    --force          Overwrite the target file if it already exists.
    --fixture        Also build the git + transcript fixture for the live-probe
                     project-detail cards (leaders / worktrees). Machine-local.
    --refresh-live   Do not build: refresh the live state of an existing demo DB.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

# src layout: make ``import aiteam`` resolve to THIS checkout, not any globally
# installed copy — the seed must exercise the local storage layer under test.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiteam.clock import utc_now
from aiteam.services.notices import ledger  # noqa: E402
from aiteam.services.plan_summary import pricing_sample_total  # noqa: E402
from aiteam.services.pricing import catalog_digest, load_catalog, quote_requests  # noqa: E402
from aiteam.storage.account_monitor import MonitorRepository  # noqa: E402
from aiteam.storage.account_usage import AccountUsageRepository  # noqa: E402
from aiteam.storage.connection import get_session, init_db  # noqa: E402
from aiteam.storage.models import (  # noqa: E402
    AgentActivityModel,
    AgentModel,
    EcosystemRepoProfileModel,
    EventModel,
    KnowledgeLinkModel,
    LeaderBriefingModel,
    MeetingMessageModel,
    MeetingModel,
    ProjectModel,
    ReportModel,
    TaskMemoModel,
    TaskModel,
    TeamModel,
    WorkflowAgentModel,
    WorkflowRunModel,
)
from aiteam.storage.repository import StorageRepository  # noqa: E402
from aiteam.types import (  # noqa: E402
    AGENT_TASK_LINK_FROM_KIND,
    AGENT_TASK_LINK_SOURCE,
    AGENT_TASK_LINK_TO_KIND,
    AGENT_TASK_LINK_TYPE,
    Agent,
    AgentActivity,
    AgentStatus,
    EcosystemRepoProfile,
    Event,
    EventType,
    HarnessId,
    LeaderBriefing,
    Meeting,
    MeetingMessage,
    MeetingStatus,
    NoticeDelivery,
    NoticeStatus,
    OrchestrationMode,
    PricingAccount,
    PricingPlanSample,
    PricingPlanSnapshot,
    PricingQuotaSnapshot,
    PricingQuoteRequest,
    PricingRequestLine,
    PricingUsageEntry,
    Project,
    Report,
    Task,
    TaskHorizon,
    TaskMemo,
    TaskPriority,
    TaskStatus,
    Team,
    TeamStatus,
    TokenSource,
    WorkflowAgent,
    WorkflowRun,
)

# Anchor every timestamp to a single "now" so relative offsets stay coherent.
NOW = utc_now()

# The fixed production database path — the seed must NEVER build over it.
_PROD_DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"

# Workflow run identifiers (neutral, Aurora-themed).
WF_SDKGEN = "wf_aurora_sdkgen_01"       # completed + rich telemetry (view ②)
WF_METRICS = "wf_aurora_metrics_07"     # running + in-flight agents
WF_FIXTURES = "wf_aurora_fixtures_04"   # light completed (history)
WF_APIDIFF = "wf_aurora_apidiff_05"     # light completed (history)

# Demo CC session ids for the two Claude leaders (fixture transcripts reuse these).
LEADER_SESSIONS = {
    "atlas-lead": "a1c0ffee-0001-4d00-9000-aurora000001",
    "nova-qa": "b2d0face0-0002-4d00-9000-aurora000002",
}
# Context token totals → watermark percentages (÷ 1,000,000 window).
LEADER_CTX_TOKENS = {
    "atlas-lead": 421_000,   # ~42.1%
    "nova-qa": 184_000,      # ~18.4%
}
# The Codex session: its root thread id names the session team and the leader.
CODEX_ROOT = "0199f3a2-6c1d-7b40-8e21-5a9d0c3e7f41"
# Codex members keep their native nicknames; role is a separate field.
CODEX_MEMBERS = (("Hypatia", "explorer"), ("Kepler", "worker"), ("Noether", "worker"))

# What "working right now" looks like: name -> (status, minutes since last activity,
# current task). Applied at build time and again by --refresh-live, because the API
# resets busy agents when it starts and the reaper offlines quiet busy hook agents.
LIVE_STATE: dict[str, tuple[AgentStatus, float, str | None]] = {
    "atlas-lead": (AgentStatus.BUSY, 1, "Coordinating the authentication milestone"),
    "nova-qa": (AgentStatus.BUSY, 3, "Reviewing the checkout test plan"),
    "Codex Leader": (AgentStatus.BUSY, 1, "Hardening public API error responses"),
    "orion-backend": (AgentStatus.BUSY, 2, "Adding rate limiting to the public API"),
    "corvus-backend": (AgentStatus.WAITING, 40, "Draining the notification queue backlog"),
    "vega-frontend": (AgentStatus.WAITING, 55, None),
    "sirius-reviewer": (AgentStatus.BUSY, 4, "Reviewing the notification service refactor"),
    "mira-qa-eng": (AgentStatus.BUSY, 3, "Drafting the API outage runbook"),
    "Hypatia": (AgentStatus.BUSY, 1, "Mapping error responses across public routes"),
    "Kepler": (AgentStatus.BUSY, 2, "Adding problem+json error bodies"),
    "Noether": (AgentStatus.WAITING, 12, None),
}

# Codex plan account: the capacities the simulated quota percentages are drawn
# from (USD at standard API rates). The page estimates them back from the saved
# observations, so the two numbers it shows land near these.
PLAN_CAPACITY_WEEK_USD = Decimal("900")
PLAN_CAPACITY_5H_USD = Decimal("140")
PLAN_WEEK_MS = 604_800_000
PLAN_5H_MS = 18_000_000


def ago(days: float = 0, hours: float = 0, minutes: float = 0) -> datetime:
    """Return a timestamp *days/hours/minutes* before :data:`NOW`."""
    return NOW - timedelta(days=days, hours=hours, minutes=minutes)


def _cc_id(run: str, index: int) -> str:
    """A stable hex agent id shaped like the ones Claude Code assigns."""
    return "a" + hashlib.sha1(f"{run}:{index}".encode()).hexdigest()[:16]


def _uuid(label: str) -> str:
    """A stable UUID for a demo row, so reruns produce the same ids."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"aurora-demo:{label}"))


def _delta_days(ts: datetime) -> float:
    """Fractional days between *ts* and NOW (for event `ago(...)` offsets)."""
    return (NOW - ts).total_seconds() / 86400.0


def build_dataset(root_path: str) -> dict[str, list]:
    """Construct all demo rows as ORM instances, grouped by table.

    Returns a mapping ``table_label -> [orm rows]`` in FK-safe insertion order.
    All content is neutral English; no real project vocabulary appears anywhere.
    """
    project = Project(
        name="Aurora Web Platform",
        root_path=root_path,
        description=(
            "Customer-facing web platform: authentication, dashboard, public API "
            "and supporting infrastructure."
        ),
        config={"demo": True, "stack": ["python", "react", "postgres"]},
        created_at=ago(days=45),
        updated_at=ago(hours=2),
    )
    pid = project.id

    # -- Teams -----------------------------------------------------------------
    team_core = Team(
        name="Platform Core",
        mode=OrchestrationMode.COORDINATE,
        project_id=pid,
        status=TeamStatus.ACTIVE,
        summary="Owns auth, API and core services.",
        created_at=ago(days=44),
        updated_at=ago(hours=3),
    )
    team_quality = Team(
        name="Quality Guild",
        mode=OrchestrationMode.ROUTE,
        project_id=pid,
        status=TeamStatus.ACTIVE,
        summary="Testing, review and documentation.",
        created_at=ago(days=40),
        updated_at=ago(hours=6),
    )
    # A Codex session: the adapter names the team after the root thread.
    team_codex = Team(
        name=f"session-{CODEX_ROOT}",
        mode=OrchestrationMode.COORDINATE,
        project_id=pid,
        status=TeamStatus.ACTIVE,
        summary="Codex session: public API error handling.",
        config={"kind": "session", "owner_session_id": CODEX_ROOT},
        created_at=ago(hours=6),
        updated_at=ago(minutes=1),
    )
    # Running workflow team (auto-tracked). Active so it shows in "Active Teams".
    wf_metrics_team = Team(
        name=f"workflow-{WF_METRICS}",
        mode=OrchestrationMode.ROUTE,
        project_id=pid,
        status=TeamStatus.ACTIVE,
        summary="Auto-tracked workflow run: aggregate API usage metrics.",
        config={"kind": "workflow", "workflow_run_id": WF_METRICS},
        created_at=ago(minutes=13),
        updated_at=ago(minutes=1),
    )
    # Completed-rich workflow team (drives the workflow observability detail page).
    wf_sdkgen_team = Team(
        name=f"workflow-{WF_SDKGEN}",
        mode=OrchestrationMode.ROUTE,
        project_id=pid,
        status=TeamStatus.COMPLETED,
        summary="Auto-tracked workflow run: generate API client SDKs.",
        config={"kind": "workflow", "workflow_run_id": WF_SDKGEN},
        created_at=ago(days=3, hours=5),
        updated_at=ago(days=3, hours=4),
        completed_at=ago(days=3, hours=4, minutes=45),
    )
    # Two light completed workflow teams (history depth).
    wf_fixtures_team = Team(
        name=f"workflow-{WF_FIXTURES}",
        mode=OrchestrationMode.ROUTE,
        project_id=pid,
        status=TeamStatus.COMPLETED,
        summary="Auto-tracked workflow run: build deterministic test fixtures.",
        config={"kind": "workflow", "workflow_run_id": WF_FIXTURES},
        created_at=ago(days=6, hours=2),
        updated_at=ago(days=6, hours=1),
        completed_at=ago(days=6, hours=1, minutes=52),
    )
    wf_apidiff_team = Team(
        name=f"workflow-{WF_APIDIFF}",
        mode=OrchestrationMode.ROUTE,
        project_id=pid,
        status=TeamStatus.COMPLETED,
        summary="Auto-tracked workflow run: diff API versions and draft changelog.",
        config={"kind": "workflow", "workflow_run_id": WF_APIDIFF},
        created_at=ago(days=9, hours=3),
        updated_at=ago(days=9, hours=2),
        completed_at=ago(days=9, hours=2, minutes=51),
    )
    # A completed CC-session container team (history, renders as time range).
    session_sid = "cd8423a5"
    session_team = Team(
        name=f"session-{session_sid}",
        mode=OrchestrationMode.COORDINATE,
        project_id=pid,
        status=TeamStatus.COMPLETED,
        summary="Earlier working session: dashboard polish and bug triage.",
        config={"kind": "session", "owner_session_id": f"{session_sid}-0000-4d00-9000-aurora0000aa"},
        created_at=ago(days=4, hours=7),
        updated_at=ago(days=4, hours=3),
        completed_at=ago(days=4, hours=3),
    )
    # An archived plain delivery squad (history).
    archived_team = Team(
        name="Onboarding Revamp Squad",
        mode=OrchestrationMode.COORDINATE,
        project_id=pid,
        status=TeamStatus.ARCHIVED,
        summary="Delivered the new user onboarding flow; archived after launch.",
        created_at=ago(days=15, hours=4),
        updated_at=ago(days=11),
        completed_at=ago(days=11),
    )

    all_teams = [
        team_core, team_quality, team_codex, wf_metrics_team, wf_sdkgen_team,
        wf_fixtures_team, wf_apidiff_team, session_team, archived_team,
    ]

    # -- Agents ----------------------------------------------------------------
    # Claude leaders (role=leader): two parallel CC sessions with distinct watermarks.
    # (name, team, status, model, current_task, trust)
    leader_specs = [
        ("atlas-lead", team_core, AgentStatus.BUSY, "claude-opus-4-8[1m]",
         "Coordinating the authentication milestone", 0.90),
        ("nova-qa", team_quality, AgentStatus.WAITING, "claude-opus-4-8[1m]",
         "Reviewing the checkout test plan", 0.83),
    ]
    # Worker agents (non-leader). ctx_pct populated for the live-looking ones.
    # (name, team, role, status, trust, model, current_task, cdays, ahours, ctx_pct)
    worker_specs = [
        ("orion-backend", team_core, "backend-engineer", AgentStatus.BUSY, 0.72,
         "claude-sonnet-4-5", "Adding rate limiting to the public API", 19, 0.05, 34.2),
        ("corvus-backend", team_core, "backend-engineer", AgentStatus.WAITING, 0.69,
         "claude-sonnet-4-5", "Draining the notification queue backlog", 17, 1.2, 21.0),
        ("vega-frontend", team_core, "frontend-engineer", AgentStatus.WAITING, 0.66,
         "", None, 17, 5, 12.4),
        ("lyra-infra", team_core, "infra-engineer", AgentStatus.OFFLINE, 0.51,
         "claude-sonnet-4-5", None, 16, 30, None),
        ("sirius-reviewer", team_quality, "code-reviewer", AgentStatus.BUSY, 0.64,
         "claude-sonnet-4-5", "Reviewing the notification service refactor", 15, 0.15, 28.6),
        ("mira-qa-eng", team_quality, "qa-engineer", AgentStatus.WAITING, 0.58,
         "claude-sonnet-4-5", "Preparing the checkout e2e sandbox", 14, 3, 9.1),
        ("draco-docs", team_quality, "tech-writer", AgentStatus.OFFLINE, 0.47,
         "", None, 12, 48, None),
    ]

    agents: dict[str, Agent] = {}
    for name, team, status, model, cur, trust in leader_specs:
        ctx_tokens = LEADER_CTX_TOKENS[name]
        agents[name] = Agent(
            team_id=team.id,
            name=name,
            role="leader",
            status=status,
            trust_score=trust,
            model=model,
            current_task=cur,
            project_id=pid,
            source="hook",
            harness=HarnessId.CLAUDE_CODE,
            session_id=LEADER_SESSIONS[name],
            ctx_tokens=ctx_tokens,
            ctx_window=1_000_000,
            ctx_pct=round(ctx_tokens / 10_000, 1),
            ctx_measured_at=ago(minutes=2),
            created_at=ago(days=20),
            last_active_at=ago(minutes=2) if status is AgentStatus.BUSY else ago(hours=2),
        )
    for (name, team, role, status, trust, model, cur, cdays, ahours,
         ctx_pct) in worker_specs:
        agents[name] = Agent(
            team_id=team.id,
            name=name,
            role=role,
            status=status,
            trust_score=trust,
            model=model,
            current_task=cur,
            project_id=pid,
            source="hook",
            harness=HarnessId.CLAUDE_CODE,
            session_id=LEADER_SESSIONS["atlas-lead" if team is team_core else "nova-qa"],
            ctx_tokens=int(ctx_pct * 10_000) if ctx_pct is not None else None,
            ctx_window=1_000_000 if ctx_pct is not None else None,
            ctx_pct=ctx_pct,
            ctx_measured_at=ago(hours=ahours) if ctx_pct is not None else None,
            created_at=ago(days=cdays),
            last_active_at=ago(hours=ahours),
        )
    # The Codex session: the adapter names its root actor "Codex Leader" and keeps
    # each member's native nickname as the name, with the role beside it.
    agents["Codex Leader"] = Agent(
        team_id=team_codex.id,
        name="Codex Leader",
        role="leader",
        status=AgentStatus.BUSY,
        trust_score=0.8,
        model="gpt-5.5",
        current_task="Hardening public API error responses",
        project_id=pid,
        source="hook",
        harness=HarnessId.CODEX,
        session_id=CODEX_ROOT,
        cc_tool_use_id=CODEX_ROOT,
        created_at=ago(hours=6),
        last_active_at=ago(minutes=1),
    )
    for index, (name, role) in enumerate(CODEX_MEMBERS):
        agents[name] = Agent(
            team_id=team_codex.id,
            name=name,
            role=role,
            status=AgentStatus.WAITING,
            trust_score=0.6,
            model="gpt-5.5",
            project_id=pid,
            source="hook",
            harness=HarnessId.CODEX,
            session_id=CODEX_ROOT,
            cc_tool_use_id=_uuid(f"codex-member-{index}"),
            created_at=ago(hours=5, minutes=40 - index * 10),
            last_active_at=ago(minutes=30),
        )
    # Working state last: the same table --refresh-live applies later.
    for name, (status, minutes, cur) in LIVE_STATE.items():
        agents[name].status = status
        agents[name].last_active_at = ago(minutes=minutes)
        agents[name].current_task = cur
    # Wire each team's leader.
    team_core.leader_agent_id = agents["atlas-lead"].id
    team_quality.leader_agent_id = agents["nova-qa"].id
    team_codex.leader_agent_id = agents["Codex Leader"].id

    # -- Tasks + memos ---------------------------------------------------------
    # One row per top-level task. Day offsets are before NOW; "moved" is when the
    # task reached its current status (blocked / failed / completed). Memos carry
    # their own age in hours, so the task-wall digest has recent work, a running
    # task that went quiet 9 days ago (stale), a blocked and a failed one, and a
    # backlog with two items untouched for 30+ days (dormant).
    # (title, team, assignee, status, priority, horizon, created, started|None,
    #  moved|None, result, depends_on, [(author, memo_type, content, hours_ago)])
    task_specs = [
        ("Implement OAuth login flow", team_core, "orion-backend",
         TaskStatus.COMPLETED, TaskPriority.HIGH, TaskHorizon.SHORT, 14, 13, 10,
         "OAuth2 authorization-code flow shipped behind a feature flag.", [], [
             ("orion-backend", "progress", "Scaffolded provider config and callback route.", 13 * 24 - 6),
             ("atlas-lead", "decision", "Chose authorization-code flow over implicit for security.", 12 * 24),
             ("nova-qa", "summary", "Verified happy-path and token-refresh; approved for flag rollout.",
              10 * 24 + 2),
         ]),
        ("Design responsive dashboard layout", team_core, "vega-frontend",
         TaskStatus.COMPLETED, TaskPriority.MEDIUM, TaskHorizon.SHORT, 13, 12, 8,
         "Grid-based layout with mobile breakpoints merged.", [], [
             ("vega-frontend", "progress", "Built the 12-column grid and card primitives.", 11 * 24),
             ("vega-frontend", "issue", "Sidebar overlaps content below 360px; needs a breakpoint.", 9 * 24),
         ]),
        ("Set up CI pipeline for preview deploys", team_core, "lyra-infra",
         TaskStatus.COMPLETED, TaskPriority.HIGH, TaskHorizon.MID, 12, 11, 9,
         "Preview environments now spin up per pull request.", [], [
             ("lyra-infra", "progress", "Wired the build matrix and artifact cache.", 10 * 24 + 6),
             ("lyra-infra", "decision", "Ephemeral preview envs keyed by PR number.", 10 * 24),
             ("atlas-lead", "summary", "Preview deploys green across three sample PRs.", 9 * 24 + 1),
         ]),
        ("Harden password reset flow", team_core, "orion-backend",
         TaskStatus.COMPLETED, TaskPriority.HIGH, TaskHorizon.SHORT, 6, 5, 2,
         "Reset tokens are single-use, expire in 20 minutes and are rate limited.", [], [
             ("orion-backend", "progress", "Made reset tokens single-use and short-lived.", 3 * 24),
             ("nova-qa", "summary", "Replay and expiry cases pass; closing.", 2 * 24 + 1),
         ]),
        ("Add request tracing to worker jobs", team_core, "corvus-backend",
         TaskStatus.COMPLETED, TaskPriority.MEDIUM, TaskHorizon.MID, 8, 6, 4,
         "Every queued job now carries the trace id of the request that enqueued it.", [], [
             ("corvus-backend", "progress", "Propagated trace ids through the job envelope.", 5 * 24),
             ("corvus-backend", "summary", "Traces now span API request to worker completion.", 4 * 24 + 1),
         ]),
        ("Fix timezone bug in usage reports", team_quality, "vega-frontend",
         TaskStatus.COMPLETED, TaskPriority.MEDIUM, TaskHorizon.SHORT, 3, 2, 0.8,
         "Report buckets now use the account's timezone instead of the server's.", [], [
             ("vega-frontend", "issue", "Daily buckets split at server midnight, not the user's.", 40),
             ("vega-frontend", "summary", "Buckets follow the account timezone; regression test added.", 20),
         ]),
        ("Add rate limiting to the public API", team_core, "orion-backend",
         TaskStatus.RUNNING, TaskPriority.HIGH, TaskHorizon.SHORT, 6, 4, None, None, [], [
             ("orion-backend", "progress", "Token-bucket middleware drafted; tuning limits.", 3 * 24),
             ("orion-backend", "issue", "Bursty clients trip the limiter; trying a sliding window.", 26),
             ("orion-backend", "progress", "Sliding window in place; per-key limits load from config.", 0.8),
         ]),
        ("Harden public API error responses", team_codex, "Hypatia",
         TaskStatus.RUNNING, TaskPriority.HIGH, TaskHorizon.SHORT, 0.3, 0.25, None, None, [], [
             ("Codex Leader", "decision", "Use RFC 9457 problem+json for every 4xx and 5xx body.", 5),
             ("Hypatia", "progress", "Mapped 42 routes; 11 still return plain-text errors.", 0.4),
         ]),
        ("Draft incident runbook for API outages", team_quality, "mira-qa-eng",
         TaskStatus.RUNNING, TaskPriority.MEDIUM, TaskHorizon.SHORT, 2, 1, None, None, [], [
             ("mira-qa-eng", "progress", "Outlined detection, rollback and customer comms steps.", 3),
         ]),
        ("Migrate session store to Redis", team_core, "lyra-infra",
         TaskStatus.RUNNING, TaskPriority.MEDIUM, TaskHorizon.MID, 5, 3, None, None, [], [
             ("lyra-infra", "progress", "Provisioned Redis and dual-writing sessions.", 2 * 24),
             ("atlas-lead", "decision", "Cut over reads after a 24h shadow window.", 22),
         ]),
        ("Upgrade Postgres to 16 on staging", team_core, "lyra-infra",
         TaskStatus.RUNNING, TaskPriority.MEDIUM, TaskHorizon.MID, 16, 12, None, None, [], [
             ("lyra-infra", "progress", "Snapshot taken; replica upgrade rehearsed once.", 9 * 24),
         ]),
        ("Refactor notification service", team_core, "sirius-reviewer",
         TaskStatus.BLOCKED, TaskPriority.MEDIUM, TaskHorizon.MID, 9, 8, 7, None,
         ["Add rate limiting to the public API"], [
             ("sirius-reviewer", "issue", "Blocked on the rate-limit contract from the API task.", 7 * 24),
             ("atlas-lead", "decision", "Hold the refactor until the rate-limit contract lands.",
              6 * 24 + 20),
         ]),
        ("Fix flaky WebSocket reconnect", team_core, "orion-backend",
         TaskStatus.FAILED, TaskPriority.CRITICAL, TaskHorizon.SHORT, 9, 8, 6,
         "Reconnect storm reproduced but root cause not yet isolated.", [], [
             ("orion-backend", "issue", "Reconnect storm under network flaps; backoff insufficient.", 7 * 24),
             ("sirius-reviewer", "issue", "Repro is intermittent; needs a deterministic harness.", 6 * 24 + 8),
             ("atlas-lead", "summary", "Parked until a stable reproduction exists.", 6 * 24),
         ]),
        ("Write end-to-end tests for checkout", team_quality, "mira-qa-eng",
         TaskStatus.PENDING, TaskPriority.HIGH, TaskHorizon.SHORT, 4, None, None, None, [], [
             ("mira-qa-eng", "progress", "Drafted the cart-to-confirmation scenario matrix.", 3 * 24),
             ("mira-qa-eng", "issue", "Needs a seeded payment sandbox for deterministic runs.", 2 * 24),
         ]),
        ("Document REST API with OpenAPI", team_quality, "draco-docs",
         TaskStatus.PENDING, TaskPriority.MEDIUM, TaskHorizon.MID, 3, None, None, None, [], [
             ("draco-docs", "progress", "Imported existing routes into an OpenAPI skeleton.", 2 * 24 + 4),
             ("nova-qa", "decision", "Generate the client stubs from the spec, not by hand.", 2 * 24),
         ]),
        ("Consolidate feature flag configuration", team_core, "vega-frontend",
         TaskStatus.PENDING, TaskPriority.MEDIUM, TaskHorizon.MID, 10, None, None, None, [], []),
        ("Optimize image asset loading", team_quality, "vega-frontend",
         TaskStatus.PENDING, TaskPriority.LOW, TaskHorizon.LONG, 3, None, None, None, [], [
             ("vega-frontend", "progress", "Audited hero images; candidates for lazy loading.", 2 * 24 + 8),
         ]),
        ("Add audit log export for admins", team_core, "corvus-backend",
         TaskStatus.PENDING, TaskPriority.MEDIUM, TaskHorizon.LONG, 18, None, None, None, [], []),
        ("Evaluate edge caching for the public API", team_core, "lyra-infra",
         TaskStatus.PENDING, TaskPriority.LOW, TaskHorizon.LONG, 24, None, None, None, [], [
             ("lyra-infra", "progress", "Listed candidate routes by cache hit potential.", 20 * 24),
         ]),
        ("Remove legacy v2 REST endpoints", team_core, "orion-backend",
         TaskStatus.PENDING, TaskPriority.LOW, TaskHorizon.LONG, 41, None, None, None, [], []),
        ("Localize onboarding emails", team_quality, "draco-docs",
         TaskStatus.PENDING, TaskPriority.LOW, TaskHorizon.LONG, 35, None, None, None, [], []),
    ]
    descriptions = {
        "Implement OAuth login flow": "Third-party sign-in with PKCE; no stored passwords.",
        "Design responsive dashboard layout": "One layout from phone to wide desktop.",
        "Set up CI pipeline for preview deploys": "A preview environment for every pull request.",
        "Harden password reset flow": "Rate-limited, single-use reset tokens that expire in 20 minutes.",
        "Add request tracing to worker jobs": "Carry the request trace id into queued jobs.",
        "Fix timezone bug in usage reports": "Daily buckets must follow the account timezone.",
        "Add rate limiting to the public API": "Per-key limits with a sliding window; 429 with Retry-After.",
        "Harden public API error responses": "Consistent problem+json bodies for every error status.",
        "Draft incident runbook for API outages": "Detection, rollback and customer communication steps.",
        "Migrate session store to Redis": "Move sessions off Postgres; dual-write, then cut over reads.",
        "Upgrade Postgres to 16 on staging": "Rehearse the major upgrade on staging before production.",
        "Refactor notification service": "Rate-limit contract first, then split delivery from templating.",
        "Fix flaky WebSocket reconnect": "Clients reconnect in a storm after network flaps.",
        "Write end-to-end tests for checkout": "Cart to confirmation against a seeded payment sandbox.",
        "Document REST API with OpenAPI": "One spec that the client SDKs are generated from.",
        "Consolidate feature flag configuration": "One source for flags instead of three config files.",
        "Optimize image asset loading": "Responsive sizes and lazy loading below the fold.",
        "Add audit log export for admins": "CSV and JSON export of the admin audit trail.",
        "Evaluate edge caching for the public API": "Which read routes can be cached at the edge.",
        "Remove legacy v2 REST endpoints": "Retire v2 once the last clients move to v3.",
        "Localize onboarding emails": "Translate the welcome series into the top five locales.",
    }
    tasks: dict[str, Task] = {}
    title_to_id = {spec[0]: _uuid(f"task:{spec[0]}") for spec in task_specs}
    memos: list[TaskMemo] = []
    # (task, status it moved to, when): replayed as task.updated events below.
    status_changes: list[tuple[Task, TaskStatus, datetime]] = []
    for (title, team, assignee, status, prio, horizon, cdays, sdays, mdays,
         result, deps, memo_list) in task_specs:
        moved_at = ago(days=mdays) if mdays is not None else None
        task = Task(
            id=title_to_id[title],
            team_id=team.id,
            title=title,
            description=descriptions[title],
            status=status,
            assigned_to=assignee,
            result=result,
            project_id=pid,
            priority=prio,
            horizon=horizon,
            depends_on=[title_to_id[d] for d in deps],
            tags=["frontend"] if team is team_quality else ["backend"],
            created_at=ago(days=cdays),
            started_at=ago(days=sdays) if sdays is not None else None,
            completed_at=moved_at if status is TaskStatus.COMPLETED else None,
        )
        tasks[title] = task
        if task.started_at is not None:
            status_changes.append((task, TaskStatus.RUNNING, task.started_at))
        if moved_at is not None:
            status_changes.append((task, status, moved_at))
        for author, memo_type, content, hours in memo_list:
            memos.append(TaskMemo(
                task_id=task.id,
                project_id=pid,
                author=author,
                memo_type=memo_type,
                content=content,
                scope_path="/project/platform",
                created_at=ago(hours=hours),
            ))

    # -- Reports ---------------------------------------------------------------
    reports = [
        Report(
            project_id=pid,
            author="atlas-lead",
            topic="OAuth Integration Design",
            report_type="design",
            date=ago(days=13).strftime("%Y-%m-%d"),
            task_id=tasks["Implement OAuth login flow"].id,
            team_id=team_core.id,
            content=(
                "# OAuth Integration Design\n\n"
                "## Goal\n"
                "Add third-party sign-in without storing user passwords.\n\n"
                "## Approach\n"
                "Use the authorization-code flow with PKCE. Tokens are stored "
                "server-side; the browser only ever holds a short-lived session "
                "cookie.\n\n"
                "## Risks\n"
                "- Provider outages must degrade gracefully to email login.\n"
                "- Refresh tokens require rotation and revocation support.\n"
            ),
            created_at=ago(days=13),
        ),
        Report(
            project_id=pid,
            author="nova-qa",
            topic="Q3 Performance Audit",
            report_type="analysis",
            date=ago(days=5).strftime("%Y-%m-%d"),
            task_id=tasks["Optimize image asset loading"].id,
            team_id=team_quality.id,
            content=(
                "# Q3 Performance Audit\n\n"
                "## Summary\n"
                "Largest Contentful Paint is dominated by unoptimized hero images.\n\n"
                "## Findings\n"
                "1. Hero images ship at 3x the required resolution.\n"
                "2. No lazy loading below the fold.\n"
                "3. Session lookups add ~40ms; Redis migration should help.\n\n"
                "## Recommendation\n"
                "Adopt responsive image sizes and defer off-screen assets.\n"
            ),
            created_at=ago(days=5),
        ),
    ]

    # -- Meeting + messages ----------------------------------------------------
    meeting = Meeting(
        team_id=team_core.id,
        topic="Sprint Planning: Authentication Milestone",
        status=MeetingStatus.CONCLUDED,
        participants=["atlas-lead", "orion-backend", "vega-frontend", "nova-qa"],
        project_id=pid,
        meta_json={"template": "standup", "rounds": 2},
        created_at=ago(days=6, hours=2),
        concluded_at=ago(days=6, hours=1),
    )
    msg_specs = [
        ("atlas-lead", 1, "Focus this sprint: rate limiting and the Redis session cutover."),
        ("orion-backend", 1, "Rate limiter is drafted; bursty clients still trip it."),
        ("vega-frontend", 1, "Dashboard layout is merged; I can pick up image optimization next."),
        ("nova-qa", 1, "I'll block out the checkout end-to-end suite once the sandbox is ready."),
        ("atlas-lead", 2, "Agreed. Notification refactor stays blocked on the rate-limit contract."),
        ("orion-backend", 2, "I'll publish the limiter contract by Thursday so the refactor can start."),
    ]
    meeting_msgs = [
        MeetingMessage(
            meeting_id=meeting.id,
            agent_id=agents[name].id,
            agent_name=name,
            content=content,
            round_number=rnd,
            timestamp=ago(days=6, hours=2) + timedelta(minutes=5 * i),
        )
        for i, (name, rnd, content) in enumerate(msg_specs)
    ]

    # -- Agent activities (fills swimlanes / analytics; spans the last ~20h) ----
    # Per-agent (tool, input) rotation. All neutral Aurora-flavoured operations.
    activity_scripts: dict[str, list[tuple[str, str]]] = {
        "orion-backend": [
            ("Read", "src/api/routes/auth.py"),
            ("Grep", "rate_limit in src/api"),
            ("Edit", "src/api/middleware/rate_limit.py"),
            ("Bash", "pytest tests/api/test_rate_limit.py -q"),
            ("Read", "src/api/deps.py"),
            ("Edit", "src/api/routes/public.py"),
            ("Bash", "ruff check src/api"),
            ("Write", "docs/rate-limit-contract.md"),
        ],
        "corvus-backend": [
            ("Read", "src/workers/notifications.py"),
            ("Bash", "python -m aurora.worker drain --queue notifications"),
            ("Edit", "src/workers/notifications.py"),
            ("Grep", "TODO in src/workers"),
            ("Read", "src/queue/backend.py"),
            ("Bash", "pytest tests/workers -q"),
        ],
        "vega-frontend": [
            ("Read", "web/src/components/DashboardGrid.tsx"),
            ("Edit", "web/src/components/HeroImage.tsx"),
            ("Bash", "npm run build"),
            ("Grep", "srcSet in web/src"),
            ("Edit", "web/src/styles/layout.css"),
            ("Bash", "npm run test -- HeroImage"),
        ],
        "sirius-reviewer": [
            ("Read", "src/services/notification_service.py"),
            ("Grep", "publish( in src/services"),
            ("Read", "src/services/dispatcher.py"),
            ("Edit", "src/services/notification_service.py"),
            ("Bash", "pytest tests/services/test_notification.py -q"),
        ],
        "mira-qa-eng": [
            ("Read", "tests/e2e/checkout.spec.ts"),
            ("Write", "tests/e2e/fixtures/payment_sandbox.ts"),
            ("Bash", "npx playwright test checkout --project=chromium"),
            ("Grep", "await page in tests/e2e"),
            ("Edit", "tests/e2e/checkout.spec.ts"),
        ],
        "atlas-lead": [
            ("Read", "docs/architecture.md"),
            ("Bash", "git log --oneline -20"),
            ("Grep", "milestone in docs"),
            ("Read", "src/api/routes/auth.py"),
        ],
    }
    # Deterministic spread: for agent index a, its i-th activity lands
    # (base + i*step) hours ago, jittered by agent so lanes interleave.
    activities: list[AgentActivity] = []
    error_slots = {("orion-backend", 3), ("mira-qa-eng", 2), ("sirius-reviewer", 4)}
    for a_idx, (aname, script) in enumerate(activity_scripts.items()):
        agent = agents[aname]
        # ~22 activities each, cycling the script; newest ~4 min ago.
        for i in range(22):
            tool, inp = script[i % len(script)]
            minutes_ago = 4 + a_idx * 7 + i * 52  # spreads across ~19h
            status = "error" if (aname, i % len(script)) in error_slots and i < len(script) else "completed"
            activities.append(AgentActivity(
                agent_id=agent.id,
                session_id=f"sess-{aname}",
                tool_name=tool,
                input_summary=inp,
                output_summary="" if status == "error" else "ok",
                duration_ms=800 + (i * 137 % 5200) + a_idx * 60,
                status=status,
                error="Command exited with a non-zero status." if status == "error" else None,
                timestamp=ago(minutes=minutes_ago),
            ))

    # -- Workflow runs + per-agent telemetry -----------------------------------
    # Phase indexes are 1-based: 0 means the phase is not known yet, and the swim
    # lane files such agents under "Phase pending".
    # Agent states are the ones the workflow JSON writes (a failed agent is "error").
    # (A) Completed-rich run — 4 phases, 7 fan-out agents, one FAILED lane.
    sdk_phases = [
        {"index": 1, "title": "Analyze API surface"},
        {"index": 2, "title": "Generate service clients"},
        {"index": 3, "title": "Run integration checks"},
        {"index": 4, "title": "Assemble release bundle"},
    ]
    # (label, phase_index, phase_title, state, tokens, tool_calls, dur_ms, prompt, result)
    sdk_agent_specs = [
        ("scan:endpoints", 1, "Analyze API surface", "done", 48_200, 14, 171_000,
         "Enumerate REST endpoints from the OpenAPI schema.",
         "Catalogued 42 endpoints across 9 resource groups."),
        ("gen:typescript", 2, "Generate service clients", "done", 61_200, 19, 204_000,
         "Generate the TypeScript client from the schema.",
         "Emitted a typed client with 42 methods and models."),
        ("gen:python", 2, "Generate service clients", "done", 57_800, 18, 198_000,
         "Generate the Python client from the schema.",
         "Emitted an async Python client and pydantic models."),
        ("gen:go", 2, "Generate service clients", "done", 52_100, 16, 182_000,
         "Generate the Go client from the schema.",
         "Emitted a Go client with typed request builders."),
        ("test:contract", 3, "Run integration checks", "error", 39_400, 21, 142_000,
         "Validate the generated clients against the contract suite.",
         "Contract mismatch on pagination params; 3 checks failed."),
        ("test:smoke", 3, "Run integration checks", "done", 33_600, 12, 121_000,
         "Run smoke tests against a mock server.",
         "All smoke tests passed against the mock server."),
        ("reduce:bundle", 4, "Assemble release bundle", "done", 44_900, 15, 158_000,
         "Merge the clients, lint, and assemble the release bundle.",
         "Bundled three clients; lint clean; artifact produced."),
    ]
    sdk_start = ago(days=3, hours=5)
    wf_sdkgen = WorkflowRun(
        wf_id=WF_SDKGEN,
        project_id=pid,
        team_id=wf_sdkgen_team.id,
        name="Generate API client SDKs",
        status="completed",
        source="hook+file",
        phases=sdk_phases,
        planned_agent_count=7,
        agent_count=len(sdk_agent_specs),
        total_tokens=sum(s[4] for s in sdk_agent_specs),
        total_tool_calls=sum(s[5] for s in sdk_agent_specs),
        duration_ms=905_000,
        summary="Generated TypeScript, Python and Go clients; one contract check failed and was triaged.",
        script_path="/workspace/aurora-web/workflows/sdkgen.js",
        started_at=sdk_start,
        completed_at=ago(days=3, hours=4, minutes=45),
        created_at=sdk_start,
        updated_at=ago(days=3, hours=4),
    )
    wf_sdkgen_agents = [
        WorkflowAgent(
            run_id=WF_SDKGEN, wf_id=WF_SDKGEN, project_id=pid,
            cc_agent_id=_cc_id(WF_SDKGEN, i),
            label=label, phase_index=pidx, phase_title=ptitle,
            model="claude-opus-4-8[1m]", state=state,
            tokens=tokens, tool_calls=tool_calls, duration_ms=dur,
            last_tool_name="Bash" if "test" in label else "Write",
            last_tool_summary=result[:80],
            prompt_preview=prompt, result_preview=result,
            started_at=sdk_start + timedelta(seconds=30 * i),
            queued_at=sdk_start,
            last_activity_at=sdk_start + timedelta(milliseconds=dur),
            created_at=sdk_start,
            updated_at=ago(days=3, hours=4),
        )
        for i, (label, pidx, ptitle, state, tokens, tool_calls, dur, prompt, result)
        in enumerate(sdk_agent_specs)
    ]

    # (B) Running run — 3 phases, 5 fan-out agents, 4 still in-flight.
    metrics_phases = [
        {"index": 1, "title": "Collect usage metrics"},
        {"index": 2, "title": "Aggregate per-endpoint stats"},
        {"index": 3, "title": "Publish dashboard export"},
    ]
    # (label, phase_index, phase_title, state, tokens, tool_calls, dur_ms, prompt, result)
    metrics_agent_specs = [
        ("collect:logs", 1, "Collect usage metrics", "done", 28_700, 9, 96_000,
         "Ingest raw request logs from the metrics bucket.",
         "Ingested 6 log shards (~2.1M rows)."),
        ("aggregate:shard-a", 2, "Aggregate per-endpoint stats", "running", 14_300, 6, None,
         "Aggregate per-endpoint latency for shard A.", ""),
        ("aggregate:shard-b", 2, "Aggregate per-endpoint stats", "running", 12_900, 5, None,
         "Aggregate per-endpoint latency for shard B.", ""),
        ("aggregate:shard-c", 2, "Aggregate per-endpoint stats", "progress", 9_800, 4, None,
         "Aggregate per-endpoint latency for shard C.", ""),
        ("publish:export", 3, "Publish dashboard export", "queued", 0, 0, None,
         "Publish the aggregated export to the dashboard store.", ""),
    ]
    metrics_start = ago(minutes=12)
    wf_metrics = WorkflowRun(
        wf_id=WF_METRICS,
        project_id=pid,
        team_id=wf_metrics_team.id,
        name="Aggregate API usage metrics",
        status="running",
        source="hook",
        phases=metrics_phases,
        planned_agent_count=5,
        agent_count=len(metrics_agent_specs),
        total_tokens=0,
        total_tool_calls=sum(s[5] for s in metrics_agent_specs),
        live_tokens=sum(s[4] for s in metrics_agent_specs),
        duration_ms=None,
        summary="In progress: aggregating per-endpoint usage across three shards.",
        script_path="/workspace/aurora-web/workflows/metrics.js",
        started_at=metrics_start,
        last_activity_at=ago(minutes=1),
        created_at=metrics_start,
        updated_at=ago(minutes=1),
    )
    # Members of the running workflow team, as the hooks register them: one row per
    # fan-out agent, named after its agent id, linked to its telemetry row. The
    # collector already finished (offline); the export is still queued (waiting).
    metrics_member_agents: list[Agent] = []
    for i, (label, _pidx, _pt, state, *_rest) in enumerate(metrics_agent_specs):
        cc_id = _cc_id(WF_METRICS, i)
        metrics_member_agents.append(Agent(
            team_id=wf_metrics_team.id,
            name=f"wf-{cc_id[:10]}",
            role="workflow-subagent",
            status=(AgentStatus.BUSY if state in ("running", "progress")
                    else AgentStatus.OFFLINE if state == "done" else AgentStatus.WAITING),
            trust_score=0.6,
            model="",
            current_task=label if state != "done" else None,
            project_id=pid,
            source="hook",
            harness=HarnessId.CLAUDE_CODE,
            session_id=LEADER_SESSIONS["atlas-lead"],
            cc_tool_use_id=cc_id,
            created_at=metrics_start + timedelta(seconds=20 * i),
            last_active_at=ago(minutes=1) if state != "done" else ago(minutes=10),
        ))
    wf_metrics_agents = [
        WorkflowAgent(
            run_id=WF_METRICS, wf_id=WF_METRICS, project_id=pid,
            cc_agent_id=_cc_id(WF_METRICS, i),
            os_agent_id=metrics_member_agents[i].id,
            label=label, phase_index=pidx, phase_title=ptitle,
            model="claude-opus-4-8[1m]", state=state,
            tokens=tokens, tool_calls=tool_calls, duration_ms=dur,
            last_tool_name="Bash" if state in ("running", "progress") else ("Read" if state == "done" else ""),
            last_tool_summary="aggregating..." if state in ("running", "progress") else result[:80],
            prompt_preview=prompt, result_preview=result,
            started_at=metrics_start + timedelta(seconds=20 * i) if state != "queued" else None,
            queued_at=metrics_start,
            last_activity_at=ago(minutes=1) if state in ("running", "progress") else (
                metrics_start + timedelta(milliseconds=dur or 0) if state == "done" else None),
            created_at=metrics_start,
            updated_at=ago(minutes=1),
        )
        for i, (label, pidx, ptitle, state, tokens, tool_calls, dur, prompt, result)
        in enumerate(metrics_agent_specs)
    ]

    # (C) + (D) light completed runs for history depth.
    fix_start = ago(days=6, hours=2)
    wf_fixtures = WorkflowRun(
        wf_id=WF_FIXTURES, project_id=pid, team_id=wf_fixtures_team.id,
        name="Build deterministic test fixtures", status="completed", source="hook+file",
        phases=[{"index": 1, "title": "Extract schema"}, {"index": 2, "title": "Emit fixtures"}],
        planned_agent_count=2, agent_count=2, total_tokens=71_400, total_tool_calls=23,
        duration_ms=452_000, summary="Generated deterministic fixtures for the checkout suite.",
        script_path="/workspace/aurora-web/workflows/fixtures.js",
        started_at=fix_start, completed_at=ago(days=6, hours=1, minutes=52),
        created_at=fix_start, updated_at=ago(days=6, hours=1),
    )
    wf_fixtures_agents = [
        WorkflowAgent(
            run_id=WF_FIXTURES, wf_id=WF_FIXTURES, project_id=pid,
            cc_agent_id=_cc_id(WF_FIXTURES, i), label=label,
            phase_index=i + 1, phase_title=ptitle, model="claude-opus-4-8[1m]",
            state="done", tokens=tokens, tool_calls=tc, duration_ms=dur,
            last_tool_name="Write", prompt_preview=prompt, result_preview=result,
            started_at=fix_start + timedelta(seconds=15 * i), queued_at=fix_start,
            last_activity_at=fix_start + timedelta(milliseconds=dur),
            created_at=fix_start, updated_at=ago(days=6, hours=1),
        )
        for i, (label, ptitle, tokens, tc, dur, prompt, result) in enumerate([
            ("extract:models", "Extract schema", 34_100, 10, 214_000,
             "Extract entity models from the database schema.", "Extracted 18 entity models."),
            ("emit:fixtures", "Emit fixtures", 37_300, 13, 238_000,
             "Emit deterministic JSON fixtures for each model.", "Emitted 18 fixture files."),
        ])
    ]
    diff_start = ago(days=9, hours=3)
    wf_apidiff = WorkflowRun(
        wf_id=WF_APIDIFF, project_id=pid, team_id=wf_apidiff_team.id,
        name="Diff API versions and draft changelog", status="completed", source="hook+file",
        phases=[{"index": 1, "title": "Diff API versions"}, {"index": 2, "title": "Write changelog"}],
        planned_agent_count=3, agent_count=3, total_tokens=96_800, total_tool_calls=31,
        duration_ms=548_000, summary="Diffed v3 vs v4 and drafted a migration changelog.",
        script_path="/workspace/aurora-web/workflows/apidiff.js",
        started_at=diff_start, completed_at=ago(days=9, hours=2, minutes=51),
        created_at=diff_start, updated_at=ago(days=9, hours=2),
    )
    wf_apidiff_agents = [
        WorkflowAgent(
            run_id=WF_APIDIFF, wf_id=WF_APIDIFF, project_id=pid,
            cc_agent_id=_cc_id(WF_APIDIFF, i), label=label,
            phase_index=pidx, phase_title=ptitle, model="claude-opus-4-8[1m]",
            state="done", tokens=tokens, tool_calls=tc, duration_ms=dur,
            last_tool_name="Write", prompt_preview=prompt, result_preview=result,
            started_at=diff_start + timedelta(seconds=15 * i), queued_at=diff_start,
            last_activity_at=diff_start + timedelta(milliseconds=dur),
            created_at=diff_start, updated_at=ago(days=9, hours=2),
        )
        for i, (label, pidx, ptitle, tokens, tc, dur, prompt, result) in enumerate([
            ("diff:v3", 1, "Diff API versions", 31_200, 9, 168_000,
             "Diff the v3 and v4 OpenAPI schemas.", "Found 11 breaking and 24 additive changes."),
            ("diff:v4", 1, "Diff API versions", 28_600, 8, 151_000,
             "Classify the changes by severity.", "Classified 35 changes across 6 resources."),
            ("write:changelog", 2, "Write changelog", 37_000, 14, 229_000,
             "Draft a migration changelog from the diff.", "Drafted a changelog with a migration guide."),
        ])
    ]

    workflow_runs = [wf_sdkgen, wf_metrics, wf_fixtures, wf_apidiff]
    workflow_agents = (
        wf_sdkgen_agents + wf_metrics_agents + wf_fixtures_agents + wf_apidiff_agents
    )

    # -- Ecosystem profiles (18 real public repos, neutral metadata) -----------
    # (repo_full_name, name, owner, stars, language, topics, category, score,
    #  one_line, pushed_days_ago, homepage, status)
    eco_specs = [
        ("fastapi/fastapi", "fastapi", "fastapi", 78_400, "Python",
         ["web", "api", "asgi"], "web-framework", 9,
         "High-performance ASGI web framework with type-driven validation.",
         1, "https://fastapi.tiangolo.com", "active"),
        ("vitejs/vite", "vite", "vitejs", 68_900, "TypeScript",
         ["build-tool", "frontend", "esm"], "build-tooling", 9,
         "Next-generation frontend build tool with instant HMR.", 1, "https://vitejs.dev", "active"),
        ("microsoft/playwright", "playwright", "microsoft", 66_100, "TypeScript",
         ["testing", "e2e", "browser"], "testing", 8,
         "Cross-browser end-to-end testing and automation framework.", 2, "https://playwright.dev", "active"),
        ("pallets/flask", "flask", "pallets", 68_200, "Python",
         ["web", "wsgi", "microframework"], "web-framework", 7,
         "Lightweight WSGI web application framework.", 4, "https://flask.palletsprojects.com", "active"),
        ("encode/httpx", "httpx", "encode", 13_600, "Python",
         ["http-client", "async"], "http-client", 7,
         "A fully featured HTTP client with sync and async support.", 3, "https://www.python-httpx.org", "active"),
        ("pydantic/pydantic", "pydantic", "pydantic", 22_100, "Python",
         ["validation", "typing", "serialization"], "data-validation", 9,
         "Data validation using Python type hints.", 1, "https://docs.pydantic.dev", "active"),
        ("sqlalchemy/sqlalchemy", "sqlalchemy", "sqlalchemy", 9_800, "Python",
         ["orm", "sql", "database"], "orm", 8,
         "The database toolkit and ORM for Python.", 2, "https://www.sqlalchemy.org", "active"),
        ("redis/redis", "redis", "redis", 67_300, "C",
         ["cache", "database", "in-memory"], "infrastructure", 8,
         "In-memory data store used as a cache, database and message broker.", 1, "https://redis.io", "active"),
        ("vercel/next.js", "next.js", "vercel", 128_400, "JavaScript",
         ["react", "ssr", "framework"], "web-framework", 8,
         "The React framework for production-grade web apps.", 1, "https://nextjs.org", "active"),
        ("facebook/react", "react", "facebook", 232_900, "JavaScript",
         ["ui", "library", "frontend"], "ui-library", 9,
         "A JavaScript library for building user interfaces.", 1, "https://react.dev", "active"),
        ("tailwindlabs/tailwindcss", "tailwindcss", "tailwindlabs", 84_600, "TypeScript",
         ["css", "styling", "utility-first"], "styling", 8,
         "A utility-first CSS framework for rapid UI development.", 1, "https://tailwindcss.com", "active"),
        ("astral-sh/ruff", "ruff", "astral-sh", 33_700, "Rust",
         ["linter", "python", "formatter"], "linting", 9,
         "An extremely fast Python linter and formatter, written in Rust.", 1, "https://docs.astral.sh/ruff", "active"),
        ("pytest-dev/pytest", "pytest", "pytest-dev", 12_300, "Python",
         ["testing", "framework"], "testing", 8,
         "The mature, full-featured Python testing framework.", 2, "https://pytest.org", "active"),
        ("python/mypy", "mypy", "python", 18_900, "Python",
         ["typing", "static-analysis"], "type-checking", 7,
         "Optional static typing and type checking for Python.", 3, "https://mypy-lang.org", "active"),
        ("prettier/prettier", "prettier", "prettier", 49_800, "JavaScript",
         ["formatter", "code-style"], "formatting", 7,
         "An opinionated code formatter for many languages.", 5, "https://prettier.io", "active"),
        ("tokio-rs/tokio", "tokio", "tokio-rs", 27_400, "Rust",
         ["async", "runtime", "networking"], "runtime", 8,
         "An asynchronous runtime for building reliable network apps in Rust.", 2, "https://tokio.rs", "active"),
        ("denoland/deno", "deno", "denoland", 98_100, "Rust",
         ["runtime", "javascript", "typescript"], "runtime", 7,
         "A modern, secure runtime for JavaScript and TypeScript.", 1, "https://deno.com", "active"),
        ("honojs/hono", "hono", "honojs", 21_600, "TypeScript",
         ["web", "edge", "framework"], "web-framework", 7,
         "A small, fast web framework for the edge.", 1, "https://hono.dev", "archived"),
    ]
    eco_profiles = []
    for (full, name, owner, stars, lang, topics, category, score, one_line,
         pushed_days, homepage, status) in eco_specs:
        eco_profiles.append(EcosystemRepoProfile(
            project_id=pid,
            repo_full_name=full,
            name=name,
            owner=owner,
            description=one_line,
            stars=stars,
            language=lang,
            topics=topics,
            homepage=homepage,
            last_commit_at=ago(days=pushed_days),
            needs_deep_review=stars < 15_000,
            relevance_category=category,
            relevance_score=score,
            one_line_summary=one_line,
            description_excerpt=one_line,
            pushed_at=ago(days=pushed_days),
            is_archived=(status == "archived"),
            is_active=(status == "active"),
            last_active_status=status,
            source_kind="github",
            primary_source="github",
            canonical_id=f"github/{full}",
            popularity_percentile=round(min(1.0, stars / 250_000), 3),
            first_seen_at=ago(days=20),
            last_scanned_at=ago(hours=6),
        ))

    # -- Events (time-spread across the last ~3 weeks) -------------------------
    events: list[Event] = []

    def ev(days: float, etype: EventType, source: str, data: dict,
           entity_id: str | None = None, entity_type: str | None = None,
           snapshot: dict | None = None) -> None:
        events.append(Event(
            type=etype, source=source, data=data,
            entity_id=entity_id, entity_type=entity_type,
            state_snapshot=snapshot, timestamp=ago(days=days),
        ))

    # System + team + agent lifecycle
    ev(45, EventType.SYSTEM_STARTED, "system", {"version": "demo"})
    ev(44, EventType.TEAM_CREATED, "api", {"name": team_core.name},
       team_core.id, "team", {"name": team_core.name})
    ev(40, EventType.TEAM_CREATED, "api", {"name": team_quality.name},
       team_quality.id, "team", {"name": team_quality.name})
    ev(15.2, EventType.TEAM_CREATED, "api", {"name": archived_team.name},
       archived_team.id, "team", {"name": archived_team.name})
    ev(_delta_days(team_codex.created_at), EventType.TEAM_CREATED, "hook",
       {"name": team_codex.name, "kind": "session"}, team_codex.id, "team",
       {"name": team_codex.name})
    for name in ("Codex Leader", *(member for member, _ in CODEX_MEMBERS)):
        a = agents[name]
        ev(_delta_days(a.created_at), EventType.AGENT_CREATED, "hook",
           {"name": name, "role": a.role, "harness": "codex"}, a.id, "agent", {"name": name})
    for offset, name in enumerate(
        ["atlas-lead", "orion-backend", "corvus-backend", "vega-frontend", "lyra-infra"]
    ):
        a = agents[name]
        ev(20 - offset * 0.4, EventType.AGENT_CREATED, "hook",
           {"name": name, "role": a.role}, a.id, "agent", {"name": name})
    for offset, name in enumerate(["nova-qa", "sirius-reviewer", "mira-qa-eng", "draco-docs"]):
        a = agents[name]
        ev(18 - offset * 0.5, EventType.AGENT_CREATED, "hook",
           {"name": name, "role": a.role}, a.id, "agent", {"name": name})

    # Task lifecycle: creation, then every status change. The task wall digest
    # reads status changes from task.updated events (the repository's own event
    # for an update), so each change is written in that shape too.
    legacy_status_events = {
        TaskStatus.RUNNING: EventType.TASK_STARTED,
        TaskStatus.COMPLETED: EventType.TASK_COMPLETED,
        TaskStatus.BLOCKED: EventType.TASK_BLOCKED,
        TaskStatus.FAILED: EventType.TASK_FAILED,
    }
    for title, t in tasks.items():
        ev(_delta_days(t.created_at),
           EventType.TASK_CREATED, "api", {"title": title}, t.id, "task",
           {"title": title, "status": "pending"})
    for t, status, at in status_changes:
        ev(_delta_days(at), EventType.TASK_UPDATED, "repository",
           {"task_id": t.id, "changes": ["status"]}, t.id, "task",
           {"status": status.value, "assigned_to": t.assigned_to, "title": t.title})
        ev(_delta_days(at), legacy_status_events[status], "hook", {"title": t.title}, t.id,
           "task", {"title": t.title, "status": status.value})

    # Agent status changes
    ev(2, EventType.AGENT_STATUS_CHANGED, "hook", {"to": "offline"},
       agents["draco-docs"].id, "agent", {"status": "offline"})
    ev(1.3, EventType.AGENT_STATUS_CHANGED, "hook", {"to": "offline"},
       agents["lyra-infra"].id, "agent", {"status": "offline"})
    ev(0.3, EventType.AGENT_STATUS_CHANGED, "hook", {"to": "busy"},
       agents["orion-backend"].id, "agent", {"status": "busy"})

    # Meeting events
    ev(6.08, EventType.MEETING_STARTED, "api", {"topic": meeting.topic},
       meeting.id, "meeting", {"topic": meeting.topic})
    ev(6.04, EventType.MEETING_CONCLUDED, "api", {"topic": meeting.topic},
       meeting.id, "meeting", {"status": "concluded"})

    # Workflow observability events for each run.
    for run, team, planned in (
        (wf_sdkgen, wf_sdkgen_team, 7),
        (wf_fixtures, wf_fixtures_team, 2),
        (wf_apidiff, wf_apidiff_team, 3),
    ):
        base = _delta_days(run.started_at)
        ev(base, EventType.WORKFLOW_PLANNED, "hook",
           {"wf_id": run.wf_id, "planned_agents": planned}, team.id, "team", {"status": "planned"})
        ev(base - 0.002, EventType.WORKFLOW_STARTED, "hook",
           {"wf_id": run.wf_id}, team.id, "team", {"status": "running"})
        ev(_delta_days(run.completed_at), EventType.WORKFLOW_COMPLETED, "file",
           {"wf_id": run.wf_id, "agent_count": run.agent_count, "total_tokens": run.total_tokens},
           team.id, "team", {"status": "completed"})
    # Running workflow — planned + started only (still in flight).
    ev(_delta_days(wf_metrics.started_at), EventType.WORKFLOW_PLANNED, "hook",
       {"wf_id": WF_METRICS, "planned_agents": 5}, wf_metrics_team.id, "team", {"status": "planned"})
    ev(_delta_days(wf_metrics.started_at) - 0.001, EventType.WORKFLOW_STARTED, "hook",
       {"wf_id": WF_METRICS}, wf_metrics_team.id, "team", {"status": "running"})

    # A couple of decision + channel events for texture
    ev(10.5, EventType.DECISION_APPROACH_CHOSEN, "api",
       {"choice": "authorization-code flow"},
       tasks["Implement OAuth login flow"].id, "task", None)
    ev(0.9, EventType.CHANNEL_MESSAGE, "api",
       {"channel": "team:Platform Core", "sender": "atlas-lead"}, None, None, None)

    # -- Briefings: decisions waiting on the user ------------------------------
    # Three pending (the Pending page's Decisions tab and the notice that counts
    # them) and one answered. Newest first matters: the notice quotes the newest.
    briefings = [
        LeaderBriefing(
            id=_uuid("briefing:cdn"),
            # The notice quotes the newest title in 16 characters and ends the sentence
            # itself: keep it short, without its own punctuation.
            title="Adopt a CDN",
            description=(
                "The performance audit traced most of the slow first paint to static "
                "assets served by the app servers. A CDN also takes that traffic off "
                "the API hosts."
            ),
            options=(
                "A) Keep serving assets from the app servers. "
                "B) Put a managed CDN in front of /static. "
                "C) Run our own caching proxy."
            ),
            recommendation="B: about 40% faster first paint in the audit, around $60 a month at current traffic.",
            urgency="medium",
            project_id=pid,
            tags=["performance", "infra"],
            created_at=ago(hours=3),
        ),
        LeaderBriefing(
            id=_uuid("briefing:v2"),
            title="Approve removing the v2 REST endpoints",
            description=(
                "v2 traffic is under 1% and comes from three known API keys. Removing "
                "it is a breaking change for those clients."
            ),
            options=(
                "A) Remove v2 next release with a 30-day notice to the three clients. "
                "B) Keep v2 read-only for another quarter."
            ),
            recommendation="A: the three clients have a v3 migration guide already.",
            urgency="high",
            project_id=pid,
            tags=["api", "breaking-change"],
            created_at=ago(days=1, hours=2),
        ),
        LeaderBriefing(
            id=_uuid("briefing:cutover"),
            title="Pick the Redis session cutover window",
            description=(
                "Dual writes have run clean for two days. Moving reads to Redis logs "
                "out sessions that are older than the shadow window."
            ),
            options="A) Tuesday 02:00 UTC (lowest traffic). B) Friday 18:00 UTC (team on call).",
            recommendation="A: roughly 200 active sessions affected instead of 2,000.",
            urgency="low",
            project_id=pid,
            tags=["ops"],
            created_at=ago(days=2, hours=5),
        ),
        LeaderBriefing(
            id=_uuid("briefing:ratelimit"),
            title="Start rate limiting in report-only mode?",
            description="Enforcing limits right away could reject traffic from existing integrations.",
            options="A) Enforce immediately. B) Report-only for a week, then enforce.",
            recommendation="B: measure real per-key peaks before rejecting anything.",
            urgency="medium",
            status="resolved",
            resolution="B. One week report-only, then enforce with the measured limits.",
            project_id=pid,
            tags=["api"],
            created_at=ago(days=4),
            resolved_at=ago(days=3, hours=20),
        ),
    ]

    all_agents = list(agents.values()) + metrics_member_agents

    # -- Token usage (Token Attribution page) -----------------------------------
    # Measured dispatches carry four-layer usage from their transcript (Claude) or
    # rollout (Codex). Three are left unmeasured on purpose: a sub-agent that never
    # wrote a transcript, a Codex member still starting, and the queued export.
    # Memo authorship leaves the agent->task edges the attribution chain follows.
    usage_rng = random.Random(4217)
    slug = re.sub(r"[^a-zA-Z0-9]", "-", root_path)
    unmeasured = {"draco-docs", "Noether", f"wf-{_cc_id(WF_METRICS, 4)[:10]}"}
    for agent in all_agents:
        if agent.name in unmeasured:
            continue
        codex = agent.harness is HarnessId.CODEX
        scale = 6 if agent.role == "leader" else 1
        agent.input_tokens = usage_rng.randint(20_000, 160_000) * scale
        agent.output_tokens = usage_rng.randint(15_000, 120_000) * scale
        agent.cache_creation_tokens = 0 if codex else usage_rng.randint(60_000, 400_000) * scale
        agent.cache_read_tokens = usage_rng.randint(900_000, 9_000_000) * scale
        agent.tokens_source = TokenSource.CODEX_ROLLOUT if codex else TokenSource.TRANSCRIPT
        agent.tokens_measured_at = agent.last_active_at
        if not codex and agent.role != "leader":
            agent.transcript_path = str(
                Path.home() / ".claude" / "projects" / slug / (agent.session_id or "")
                / "subagents" / f"agent-{agent.cc_tool_use_id or agent.id[:17]}.jsonl"
            )
    by_name = {agent.name: agent for agent in all_agents}
    worked_on = sorted({
        (by_name[memo.author].id, memo.author, memo.task_id)
        for memo in memos if memo.author in by_name and by_name[memo.author].role != "leader"
    })
    links = [
        KnowledgeLinkModel(
            from_kind=AGENT_TASK_LINK_FROM_KIND, from_id=agent_id,
            to_kind=AGENT_TASK_LINK_TO_KIND, to_id=task_id,
            link_type=AGENT_TASK_LINK_TYPE, context=f"author={author} via=task_memo_add",
            link_source=AGENT_TASK_LINK_SOURCE, project_id=pid, created_at=ago(hours=1),
        )
        for agent_id, author, task_id in worked_on
    ]

    return {
        "projects": [ProjectModel.from_pydantic(project)],
        "teams": [TeamModel.from_pydantic(t) for t in all_teams],
        "agents": [AgentModel.from_pydantic(a) for a in all_agents],
        "tasks": [TaskModel.from_pydantic(t) for t in tasks.values()],
        "task_memos": [TaskMemoModel.from_pydantic(m) for m in memos],
        "reports": [ReportModel.from_pydantic(r) for r in reports],
        "leader_briefings": [LeaderBriefingModel.from_pydantic(b) for b in briefings],
        "meetings": [MeetingModel.from_pydantic(meeting)],
        "meeting_messages": [MeetingMessageModel.from_pydantic(m) for m in meeting_msgs],
        "agent_activities": [AgentActivityModel.from_pydantic(a) for a in activities],
        "events": [EventModel.from_pydantic(e) for e in events],
        "workflow_runs": [WorkflowRunModel.from_pydantic(r) for r in workflow_runs],
        "workflow_agents": [WorkflowAgentModel.from_pydantic(a) for a in workflow_agents],
        "knowledge_links": links,
        "ecosystem_repo_profiles": [EcosystemRepoProfileModel.from_pydantic(p) for p in eco_profiles],
    }


# Insertion order is FK-safe: parents before children.
_INSERT_ORDER = (
    "projects", "teams", "agents", "tasks", "task_memos", "reports",
    "leader_briefings", "meetings", "meeting_messages", "agent_activities", "events",
    "workflow_runs", "workflow_agents", "knowledge_links", "ecosystem_repo_profiles",
)


async def seed(db_url: str, root_path: str) -> dict[str, int]:
    """Create the schema and insert the full demo dataset. Returns row counts."""
    await init_db(db_url)
    dataset = build_dataset(root_path)
    counts: dict[str, int] = {}
    async with get_session(db_url) as session:
        for label in _INSERT_ORDER:
            rows = dataset[label]
            session.add_all(rows)
            counts[label] = len(rows)
    counts["notices"] = await seed_notices(db_url)
    counts["plan_quota_snapshots"] = await seed_plan_account(db_url)
    return counts


async def seed_notices(db_url: str) -> int:
    """Queued notices and recent blocks, written through the notice ledger itself.

    The ledger validates every key, variant and parameter against the catalog, so
    a notice the product could never show is refused here too. The decisions
    notice is built the way its detector builds it, from the pending briefings.
    """
    from aiteam.services.notices.detectors.decisions import pending_decisions

    repo = StorageRepository(db_url)
    pid = (await repo.list_projects())[0].id
    session = LEADER_SESSIONS["atlas-lead"]

    waiting = await pending_decisions(repo, pid, NOW)
    ids = ",".join(sorted(item.id for item in waiting))
    decisions_key = f"decisions_pending:{hashlib.sha256(f'{pid}|{ids}'.encode()).hexdigest()[:8]}"
    # (key, catalog_id, params, project, host, source, first_seen, last_seen,
    #  terminal event it was last shown on, when) - None: never pushed to a terminal.
    # The two mentions cross hosts: a Codex member pings the Claude leader and a
    # Claude member pings the Codex leader, each shown to its own host's session.
    cc_mention, codex_mention = ago(minutes=9), ago(minutes=31)
    queued = [
        (decisions_key, "decisions_pending", {"n": len(waiting), "title": waiting[0].title},
         pid, "", "decisions", waiting[0].created_at, ago(minutes=6), "UserPromptSubmit", ago(minutes=6)),
        (f"channel_mention:leader-cc:{pid}:{cc_mention.isoformat()}", "channel_mention",
         {"sender": "Hypatia", "channel": "team:Platform Core", "n": 2,
          "details": "Hypatia in team:Platform Core: error bodies now follow problem+json; "
                     "the rate limiter should return the same shape for 429."},
         pid, "cc", "channels", cc_mention, cc_mention, "UserPromptSubmit", ago(minutes=8)),
        (f"channel_mention:leader-codex:{pid}:{codex_mention.isoformat()}", "channel_mention",
         {"sender": "orion-backend", "channel": "team:Platform Core", "n": 1,
          "details": "orion-backend in team:Platform Core: 429 responses carry a Retry-After "
                     "header; please keep it when you wrap the body."},
         pid, "codex", "channels", codex_mention, codex_mention, "UserPromptSubmit", ago(minutes=30)),
        (f"codex_untrusted:{_uuid('trust')[:8]}", "codex_untrusted", {},
         "", "codex", "codex_trust", ago(hours=5), ago(hours=1), None, None),
    ]
    for key, catalog_id, params, project_id, host, source, first, last, event, shown in queued:
        for at in (first, last):  # the second hit refreshes last_seen, as a detector does
            await ledger.register(
                repo, key=key, catalog_id=catalog_id, params=params, project_id=project_id,
                host=host, source=source, now=at,
            )
        if event is not None:
            await repo.insert_notice_delivery_if_absent(NoticeDelivery(
                id=_uuid(f"delivery:{key}"), key=key, host=host or "cc",
                session_id=CODEX_ROOT if host == "codex" else session,
                event=event, language="en", claimed_at=shown, emitted_at=shown, confirmed_at=shown,
            ))
    # The user put the Codex trust review off for a day (the Dashboard's snooze).
    snoozed_at = ago(minutes=50)
    await repo.set_notice_status(
        queued[-1][0], NoticeStatus.SNOOZED, snoozed_at, snoozed_until=snoozed_at + timedelta(hours=24),
    )

    # Blocks and branch switches are rendered by the hook on the spot and reach the
    # ledger as local records; import them the way the next exit hook would.
    checkout = hashlib.sha256(b"aurora-demo:checkout").hexdigest()[:8]
    blocks = [
        ("blocked_secret_add", f"blocked_secret_add:{session}:1", "", {"file": ".env.production"},
         "PreToolUse", ago(hours=5)),
        ("branch_switched", f"branch_switched:{checkout}:master:fix/session-cache", "",
         {"repo": "aurora-web", "ob": "master", "nb": "fix/session-cache"}, "UserPromptSubmit", ago(hours=2)),
        ("blocked_teardown", f"blocked_teardown:{session}:2", "unsaved",
         {"target": "aurora-worktrees/feature-auth-flow"}, "PreToolUse", ago(days=1, hours=1)),
        ("blocked_foreign_branch", f"blocked_foreign_branch:{session}:3", "",
         {"branch": "feature/auth-flow"}, "PreToolUse", ago(days=1, hours=4)),
    ]
    records = [
        {"uuid": _uuid(f"local:{key}"), "kind": "local_notice", "catalog_id": catalog_id, "key": key,
         "variant": variant, "params": params, "project_id": pid, "session_id": session,
         "source": "workflow_reminder.py", "event": event, "language": "en", "at": at.isoformat()}
        for catalog_id, key, variant, params, event, at in blocks
    ]
    await ledger.import_local_records(repo, "cc", records, NOW)
    written, _ = await repo.list_notices(statuses=None, limit=100)
    missing = {key for _, key, *_ in blocks} - {row.key for row in written}
    if missing:  # import skips invalid records silently; a demo must not
        raise ValueError(f"notice ledger refused demo records: {sorted(missing)}")
    return len(written)


async def seed_plan_account(db_url: str) -> int:
    """Three days of Codex quota observations with priced local requests.

    Requests are simulated for a ten-hour working day that ends now and priced
    with the bundled catalog; the 5-hour and weekly percentages are drawn from
    PLAN_CAPACITY_*. Each capture then goes through the same steps the local
    capture takes (price the requests since the previous observation, carry the
    cumulative amount, preview the prediction value) and is saved with the
    repository, which maintains the incremental summaries the page reads.
    """
    import math

    repo = AccountUsageRepository(db_url)
    await repo.init_db()
    monitors = MonitorRepository(db_url)
    await monitors.init_db()
    catalog = load_catalog()
    digest = catalog_digest(catalog)
    account_key = hashlib.sha256(b"aurora-demo:codex-account").hexdigest()
    scope = hashlib.sha256(b"aurora-demo:local-usd-scope").hexdigest()
    rng = random.Random(20260930)

    week_start = NOW - timedelta(days=3, hours=2)
    week_resets = week_start + timedelta(milliseconds=PLAN_WEEK_MS)
    five_hours = timedelta(milliseconds=PLAN_5H_MS)
    current_5h = NOW - timedelta(hours=3)  # the current 5-hour window opened 3 hours ago
    account = PricingAccount(account_key=account_key, label="Aurora team", created_at=week_start)

    times = []
    t = week_start
    while t < NOW - timedelta(minutes=2):
        times.append(t)
        t += timedelta(minutes=30)
    times.append(NOW - timedelta(minutes=1))

    requests: list[tuple[datetime, PricingRequestLine]] = []
    for prev, cur in zip(times, times[1:]):
        span = (cur - prev).total_seconds()
        busy = ((NOW - cur).total_seconds() / 3600) % 24 < 10
        count = round((rng.randint(14, 22) if busy else rng.randint(0, 1)) * span / 1800)
        for _ in range(count):
            input_tokens = rng.randint(40_000, 180_000)
            requests.append((prev + timedelta(seconds=rng.uniform(1, span)), PricingRequestLine(
                request_id=_uuid(f"request:{len(requests)}"),
                model="gpt-5.5" if rng.random() < 0.7 else "gpt-5.3-codex",
                service_tier="default",
                input_tokens=input_tokens,
                cached_input_tokens=int(input_tokens * rng.uniform(0.75, 0.92)),
                output_tokens=rng.randint(900, 7_000),
            )))
    amounts: dict[str, Decimal] = {}
    for offset in range(0, len(requests), 1000):
        chunk = [request for _, request in requests[offset:offset + 1000]]
        for item in quote_requests(PricingQuoteRequest(requests=chunk), catalog).items:
            amounts[item.request_id] = Decimal(item.amount_usd)

    def window_start_5h(at: datetime) -> datetime:
        if at >= current_5h:
            return current_5h
        return current_5h - math.ceil((current_5h - at) / five_hours) * five_hours

    def used(start: datetime, at: datetime, capacity: Decimal) -> int:
        spent = sum((amounts[r.request_id] for when, r in requests if start < when <= at), Decimal(0))
        return min(100, int(spent * 100 / capacity))

    for index, at in enumerate(times):
        start_5h = window_start_5h(at)
        windows = [
            (PLAN_WEEK_MS, week_resets, used(week_start, at, PLAN_CAPACITY_WEEK_USD)),
            (PLAN_5H_MS, start_5h + five_hours, used(start_5h, at, PLAN_CAPACITY_5H_USD)),
        ]
        quotas = [
            PricingQuotaSnapshot(
                snapshot_id=_uuid(f"quota:{index}:{duration}"), account_key=account_key, limit_id="codex",
                used_percent=Decimal(percent), window_duration_ms=duration, resets_at=resets,
                observed_at=at, source="codex_app_server",
            )
            for duration, resets, percent in windows
        ]
        state = (await repo.summaries(account_key)).price.windows if index else {}
        prices = []
        for quota, (duration, resets, percent) in zip(quotas, windows):
            window = state.get(("codex", duration))
            previous = await repo.get_price_snapshot(window.last_id) if window is not None else None
            if previous is None:
                start = binding = at
                cumulative = Decimal(0)
            else:
                start, binding, cumulative = previous.observed_at, previous.activity_binding_at, previous.activity_usd
            entries = [
                PricingUsageEntry(occurred_at=when, request=request, model_source="payload",
                                  service_tier_source="payload")
                for when, request in requests if start < when <= at
            ]
            quotes = [
                quote_requests(PricingQuoteRequest(requests=[e.request for e in entries[o:o + 1000]]), catalog)
                for o in range(0, len(entries), 1000)
            ]
            sample = PricingPlanSample(
                pricing_mode="standard_equivalent", catalog_version=catalog.version, catalog_sha256=digest,
                interval_start=start, interval_end=at, entries=entries, quotes=quotes, complete=True,
            )
            candidate = PricingPlanSnapshot(
                snapshot_id=quota.snapshot_id, account_key=account_key, limit_id="codex",
                window_duration_ms=duration, resets_at=resets, observed_at=at, used_percent=percent,
                source="codex_local_logs", activity_scope=scope, activity_binding_at=binding,
                activity_usd=cumulative + pricing_sample_total(sample), pricing=sample,
            )
            prices.append(candidate.model_copy(update={
                "prediction_activity_usd": await repo.preview_prediction(account_key, window, candidate),
            }))
        if index < len(times) - 1:
            await repo.save_capture(account, quotas, pricing_plan_snapshots=prices)
            continue
        # The last capture goes through the source fence, like a manual capture:
        # it also turns monitoring on for the account with its default interval.
        claim = await monitors.claim_source("demo-seed", utc_now())
        if claim is None or await monitors.save_source_capture(
            claim, account, quotas, pricing_plan_snapshots=prices,
        ) is None:
            raise RuntimeError("could not save the final demo capture")
        await monitors.release_source(claim)
    return len(times) * 2


def refresh_live(db_path: Path) -> list[str]:
    """Put the working agents, the running workflow and the transcripts back to "just now".

    Writes only the demo database at ``db_path`` (plain sqlite, the API may be
    running) and touches the fixture transcripts under ``~/.claude/projects``.
    """
    now = utc_now()

    def stamp(minutes: float) -> str:
        # Storage keeps naive UTC text (UtcDateTime).
        return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S.%f")

    notes = []
    con = sqlite3.connect(db_path)
    try:
        with con:
            row = con.execute(
                "SELECT id, root_path FROM projects WHERE name = ?", ("Aurora Web Platform",)
            ).fetchone()
            if row is None:
                raise SystemExit(f"{db_path} is not a demo database (no Aurora Web Platform project)")
            pid, root_path = row
            for name, (status, minutes, cur) in LIVE_STATE.items():
                con.execute(
                    "UPDATE agents SET status = ?, last_active_at = ?, current_task = ? "
                    "WHERE name = ? AND project_id = ?",
                    (status.value, stamp(minutes), cur, name, pid),
                )
            members = con.execute(
                "SELECT cc_agent_id, label, state FROM workflow_agents WHERE run_id = ?", (WF_METRICS,),
            ).fetchall()
            for cc_id, label, state in members:
                live = state in ("running", "progress")
                status = "busy" if live else "offline" if state == "done" else "waiting"
                con.execute(
                    "UPDATE agents SET status = ?, last_active_at = ?, current_task = ? WHERE cc_tool_use_id = ?",
                    (status, stamp(1 if state != "done" else 10), None if state == "done" else label, cc_id),
                )
                if live:
                    con.execute(
                        "UPDATE workflow_agents SET last_activity_at = ?, updated_at = ? "
                        "WHERE run_id = ? AND cc_agent_id = ?",
                        (stamp(1), stamp(1), WF_METRICS, cc_id),
                    )
            con.execute(
                "UPDATE workflow_runs SET last_activity_at = ?, updated_at = ? WHERE wf_id = ?",
                (stamp(1), stamp(1), WF_METRICS),
            )
        notes.append(f"{len(LIVE_STATE) + len(members)} agents and run {WF_METRICS} set to now")
    finally:
        con.close()
    slug = re.sub(r"[^a-zA-Z0-9]", "-", root_path)
    for sid in LEADER_SESSIONS.values():
        transcript = Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl"
        if transcript.exists():
            os.utime(transcript)
            notes.append(f"touched {transcript}")
    return notes


# ---------------------------------------------------------------------------
# Optional filesystem fixture for the two LIVE-probe project-detail cards.
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> None:
    """Run a git command in *cwd*, raising on failure (fixture must be correct)."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def build_fixture(root_path: str) -> list[str]:
    """Build the throwaway git repo (+2 worktrees) and 2 session transcripts.

    Returns a list of human-readable notes about what was created. Everything is
    machine-local and disposable; the portable DB does not depend on it.
    """
    notes: list[str] = []
    root = Path(root_path)
    root.mkdir(parents=True, exist_ok=True)

    # (a) git repo at root_path with an initial commit on master.
    _git(["init", "-q", "-b", "master"], root)
    _git(["config", "user.email", "demo@aurora.local"], root)
    _git(["config", "user.name", "Aurora Demo"], root)
    (root / "README.md").write_text("# Aurora Web Platform (demo fixture)\n")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "chore: initial demo scaffold"], root)

    # (b) two subordinate worktrees with neutral branch names, next to root.
    wt_base = root.parent / "aurora-worktrees"
    wt_base.mkdir(parents=True, exist_ok=True)
    wt_specs = [
        ("feature/auth-flow", wt_base / "feature-auth-flow", True),   # dirty
        ("fix/session-cache", wt_base / "fix-session-cache", False),  # clean
    ]
    for branch, wt_path, make_dirty in wt_specs:
        if wt_path.exists():
            _git(["worktree", "remove", "--force", str(wt_path)], root)
        _git(["worktree", "add", "-q", "-b", branch, str(wt_path), "master"], root)
        if make_dirty:
            (wt_path / "WORKING.md").write_text("uncommitted work in progress\n")
        notes.append(f"worktree {branch} -> {wt_path}")

    # (c) two synthetic session transcripts so session_probe yields the two Claude
    #     leaders with distinct context watermarks. slug mirrors CC's naming.
    slug = re.sub(r"[^a-zA-Z0-9]", "-", root_path)
    proj_dir = Path.home() / ".claude" / "projects" / slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    for name, sid in LEADER_SESSIONS.items():
        ctx = LEADER_CTX_TOKENS[name]
        # Split the total across the four usage fields the D1 reader sums.
        usage = {
            "input_tokens": 12_000,
            "cache_creation_input_tokens": 18_000,
            "cache_read_input_tokens": ctx - 12_000 - 18_000 - 6_000,
            "output_tokens": 6_000,
        }
        lines = [
            {"type": "user", "message": {"role": "user", "content": "demo session bootstrap"}},
            {"type": "assistant", "message": {
                "role": "assistant",
                "model": "claude-opus-4-8[1m]",
                "usage": usage,
            }},
        ]
        fpath = proj_dir / f"{sid}.jsonl"
        fpath.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        notes.append(f"transcript {name} ({round(ctx/10_000, 1)}% ctx) -> {fpath}")

    return notes


def _resolve_target(raw: str, force: bool) -> Path:
    """Validate the --db target and return an absolute path to build."""
    target = Path(raw).expanduser().resolve()
    if target == _PROD_DB.resolve():
        sys.exit(
            "Refusing to seed over the production database "
            f"({_PROD_DB}). Pick a throwaway path such as /tmp/aiteam-demo.db."
        )
    if target.exists():
        if not force:
            sys.exit(
                f"Target already exists: {target}\n"
                "Pass --force to overwrite, or choose a different --db path."
            )
        target.unlink()
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a neutral demo AI Team OS database.")
    parser.add_argument("--db", required=True, help="Target SQLite file to build.")
    parser.add_argument(
        "--force", action="store_true", help="Overwrite the target file if it exists."
    )
    parser.add_argument(
        "--fixture", action="store_true",
        help="Also build the git + transcript fixture for the live-probe project-detail cards.",
    )
    parser.add_argument(
        "--root-path", default="",
        help="Project root_path (defaults to <db-parent>/aurora-web-src).",
    )
    parser.add_argument(
        "--refresh-live", action="store_true",
        help="Do not build: set the working agents, running workflow and transcripts to now.",
    )
    args = parser.parse_args()

    if args.refresh_live:
        target = Path(args.db).expanduser().resolve()
        if target == _PROD_DB.resolve() or not target.exists():
            sys.exit(f"--refresh-live needs an existing demo database, not {target}")
        for note in refresh_live(target):
            print(note)
        return

    target = _resolve_target(args.db, args.force)
    root_path = args.root_path or str(target.parent / "aurora-web-src")

    db_url = f"sqlite+aiosqlite:///{target}"
    counts = asyncio.run(seed(db_url, root_path))

    print(f"Seeded demo database at {target}")
    print(f"  project root_path: {root_path}")
    for label, n in counts.items():
        print(f"  {label:24s} {n}")
    print(f"  {'TOTAL rows':24s} {sum(counts.values())}")

    if args.fixture:
        print("\nBuilding live-probe fixture (git worktrees + session transcripts)...")
        try:
            for note in build_fixture(root_path):
                print(f"  {note}")
            print("Fixture ready — project-detail Leader/Worktree cards will render "
                  "while the transcripts are fresh (<15min).")
        except Exception as exc:  # noqa: BLE001 — fixture is best-effort, DB already built
            print(f"  fixture build failed (DB is still valid): {exc}")
    else:
        print("\n(No --fixture: Leader/Worktree project-detail cards are live probes "
              "and will be empty. Re-run with --fixture to populate them locally.)")


if __name__ == "__main__":
    main()
