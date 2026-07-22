"""Seed a neutral demo AI Team OS database for screenshots and walkthroughs.

Builds a self-contained SQLite database at the ``--db`` path using the project's
own storage layer (``types`` Pydantic models + ``storage.models`` ORM +
``storage.connection`` async session). Everything written is generic, English and
technology-neutral — a fictional "Aurora Web Platform" product — so the dashboard
can be demoed without exposing any real project vocabulary.

What it creates (all DB-backed, portable):
  * 1 project              "Aurora Web Platform"
  * 2 delivery teams       Platform Core + Quality Guild (active)
  * 4 workflow teams        1 completed-rich + 1 running-inflight + 2 light completed
  * 1 session container     completed CC-session team (history)
  * 1 archived team         plain delivery squad (history)
  * 3 leader agents         distinct context watermarks / sessions
  * 7 worker agents         varied status / trust score / model (some ctx bars)
  * 4 workflow members      live members of the running workflow team
  * 10 tasks               every status represented, English technical titles
  * ~23 task memos         2-3 per task, mixed authors and types
  * 2 reports              a design doc and an analysis
  * 1 meeting + 6 messages sprint planning, two discussion rounds
  * ~130 agent activities  spread across the last ~20h (fills swimlane / analytics)
  * 40+ events             spread across the last three weeks
  * 4 workflow runs        with per-agent telemetry (swimlanes, one failed lane)
  * 18 ecosystem profiles  real public open-source repos, neutral metadata

Two of the project-detail cards are LIVE probes, NOT rows in the DB (探查结论):
  * Leaders (with context %) come from ``session_probe`` reading
    ``~/.claude/projects/<slug>/*.jsonl`` transcripts, keyed on project.root_path.
  * Worktrees come from ``worktree_probe`` running ``git worktree list`` on
    project.root_path.
Pass ``--fixture`` to also build a throwaway git repo (with two worktrees) and
three synthetic session transcripts under the project's root_path, so those two
cards render for a project-detail screenshot. Without ``--fixture`` the DB is a
clean, portable artifact and those two cards degrade gracefully to empty.

Usage:
    python scripts/demo_seed.py --db /tmp/aiteam-demo.db [--force] [--fixture]

Options:
    --db PATH        Target SQLite file to build (required).
    --force          Overwrite the target file if it already exists.
    --fixture        Also build the git + transcript fixture for the live-probe
                     project-detail cards (leaders / worktrees). Machine-local.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# src layout: make ``import aiteam`` resolve to THIS checkout, not any globally
# installed copy — the seed must exercise the local storage layer under test.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiteam.storage.connection import get_session, init_db  # noqa: E402
from aiteam.storage.models import (  # noqa: E402
    AgentActivityModel,
    AgentModel,
    EcosystemRepoProfileModel,
    EventModel,
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
from aiteam.types import (  # noqa: E402
    Agent,
    AgentActivity,
    AgentStatus,
    EcosystemRepoProfile,
    Event,
    EventType,
    Meeting,
    MeetingMessage,
    MeetingStatus,
    OrchestrationMode,
    Project,
    Report,
    Task,
    TaskHorizon,
    TaskMemo,
    TaskPriority,
    TaskStatus,
    Team,
    TeamStatus,
    WorkflowAgent,
    WorkflowRun,
)

# Anchor every timestamp to a single "now" so relative offsets stay coherent.
NOW = datetime.now()

# The fixed production database path — the seed must NEVER build over it.
_PROD_DB = Path.home() / ".claude" / "data" / "ai-team-os" / "aiteam.db"

# Workflow run identifiers (neutral, Aurora-themed).
WF_SDKGEN = "wf_aurora_sdkgen_01"       # completed + rich telemetry (view ②)
WF_METRICS = "wf_aurora_metrics_07"     # running + in-flight agents
WF_FIXTURES = "wf_aurora_fixtures_04"   # light completed (history)
WF_APIDIFF = "wf_aurora_apidiff_05"     # light completed (history)

# Demo CC session ids for the three leaders (fixture transcripts reuse these).
LEADER_SESSIONS = {
    "atlas-lead": "a1c0ffee-0001-4d00-9000-aurora000001",
    "nova-qa": "b2d0face0-0002-4d00-9000-aurora000002",
    "cirrus-lead": "c3e0beef-0003-4d00-9000-aurora000003",
}
# Context token totals → watermark percentages (÷ 1,000,000 window).
LEADER_CTX_TOKENS = {
    "atlas-lead": 421_000,   # ~42.1%
    "nova-qa": 184_000,      # ~18.4%
    "cirrus-lead": 668_000,  # ~66.8%
}


def ago(days: float = 0, hours: float = 0, minutes: float = 0) -> datetime:
    """Return a timestamp *days/hours/minutes* before :data:`NOW`."""
    return NOW - timedelta(days=days, hours=hours, minutes=minutes)


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
        created_at=ago(days=21),
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
        created_at=ago(days=20),
        updated_at=ago(hours=3),
    )
    team_quality = Team(
        name="Quality Guild",
        mode=OrchestrationMode.ROUTE,
        project_id=pid,
        status=TeamStatus.ACTIVE,
        summary="Testing, review and documentation.",
        created_at=ago(days=18),
        updated_at=ago(hours=6),
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
        team_core, team_quality, wf_metrics_team, wf_sdkgen_team,
        wf_fixtures_team, wf_apidiff_team, session_team, archived_team,
    ]

    # -- Agents ----------------------------------------------------------------
    # Leaders (role=leader): three parallel CC sessions with distinct watermarks.
    # (name, team, session, status, ctx_tokens)
    leader_specs = [
        ("atlas-lead", team_core, AgentStatus.BUSY, "claude-opus-4-8[1m]",
         "Coordinating the authentication milestone", 0.90),
        ("nova-qa", team_quality, AgentStatus.WAITING, "claude-opus-4-8[1m]",
         "Reviewing the checkout test plan", 0.83),
        ("cirrus-lead", team_core, AgentStatus.BUSY, "claude-opus-4-8[1m]",
         "Driving the public API hardening track", 0.87),
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
            ctx_tokens=int(ctx_pct * 10_000) if ctx_pct is not None else None,
            ctx_window=1_000_000 if ctx_pct is not None else None,
            ctx_pct=ctx_pct,
            ctx_measured_at=ago(hours=ahours) if ctx_pct is not None else None,
            created_at=ago(days=cdays),
            last_active_at=ago(hours=ahours),
        )
    # Wire each team's leader.
    team_core.leader_agent_id = agents["atlas-lead"].id
    team_quality.leader_agent_id = agents["nova-qa"].id

    # -- Tasks -----------------------------------------------------------------
    # (title, team, assignee, status, priority, horizon, created_days_ago,
    #  started_days_ago|None, completed_days_ago|None, result, depends_on)
    task_specs = [
        ("Implement OAuth login flow", team_core, "orion-backend",
         TaskStatus.COMPLETED, TaskPriority.HIGH, TaskHorizon.SHORT,
         14, 13, 10, "OAuth2 authorization-code flow shipped behind a feature flag.", []),
        ("Design responsive dashboard layout", team_core, "vega-frontend",
         TaskStatus.COMPLETED, TaskPriority.MEDIUM, TaskHorizon.SHORT,
         13, 12, 8, "Grid-based layout with mobile breakpoints merged.", []),
        ("Set up CI pipeline for preview deploys", team_core, "lyra-infra",
         TaskStatus.COMPLETED, TaskPriority.HIGH, TaskHorizon.MID,
         12, 11, 7, "Preview environments now spin up per pull request.", []),
        ("Add rate limiting to the public API", team_core, "orion-backend",
         TaskStatus.RUNNING, TaskPriority.HIGH, TaskHorizon.SHORT,
         6, 4, None, None, []),
        ("Migrate session store to Redis", team_core, "lyra-infra",
         TaskStatus.RUNNING, TaskPriority.MEDIUM, TaskHorizon.MID,
         5, 3, None, None, []),
        ("Refactor notification service", team_core, "sirius-reviewer",
         TaskStatus.BLOCKED, TaskPriority.MEDIUM, TaskHorizon.MID,
         7, None, None, None, ["Add rate limiting to the public API"]),
        ("Write end-to-end tests for checkout", team_quality, "mira-qa-eng",
         TaskStatus.PENDING, TaskPriority.HIGH, TaskHorizon.SHORT,
         4, None, None, None, []),
        ("Optimize image asset loading", team_quality, "vega-frontend",
         TaskStatus.PENDING, TaskPriority.LOW, TaskHorizon.LONG,
         3, None, None, None, []),
        ("Document REST API with OpenAPI", team_quality, "draco-docs",
         TaskStatus.PENDING, TaskPriority.MEDIUM, TaskHorizon.MID,
         3, None, None, None, []),
        ("Fix flaky WebSocket reconnect", team_core, "orion-backend",
         TaskStatus.FAILED, TaskPriority.CRITICAL, TaskHorizon.SHORT,
         9, 8, None, "Reconnect storm reproduced but root cause not yet isolated.", []),
    ]
    tasks: dict[str, Task] = {}
    title_to_id: dict[str, str] = {}
    # First pass to allocate IDs so depends_on can reference by title.
    for spec in task_specs:
        title_to_id[spec[0]] = Task(team_id=None, title=spec[0]).id
    for (title, team, assignee, status, prio, horizon, cdays, sdays, xdays,
         result, deps) in task_specs:
        tasks[title] = Task(
            id=title_to_id[title],
            team_id=team.id,
            title=title,
            description=f"{title} for the Aurora Web Platform.",
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
            completed_at=ago(days=xdays) if xdays is not None else None,
        )

    # -- Task memos (2-3 each) -------------------------------------------------
    memo_specs = [
        ("Implement OAuth login flow", [
            ("orion-backend", "progress", "Scaffolded provider config and callback route."),
            ("atlas-lead", "decision", "Chose authorization-code flow over implicit for security."),
            ("nova-qa", "summary", "Verified happy-path and token-refresh; approved for flag rollout."),
        ]),
        ("Design responsive dashboard layout", [
            ("vega-frontend", "progress", "Built the 12-column grid and card primitives."),
            ("vega-frontend", "issue", "Sidebar overlaps content below 360px; needs a breakpoint."),
        ]),
        ("Set up CI pipeline for preview deploys", [
            ("lyra-infra", "progress", "Wired the build matrix and artifact cache."),
            ("lyra-infra", "decision", "Ephemeral preview envs keyed by PR number."),
            ("atlas-lead", "summary", "Preview deploys green across three sample PRs."),
        ]),
        ("Add rate limiting to the public API", [
            ("orion-backend", "progress", "Token-bucket middleware drafted; tuning limits."),
            ("orion-backend", "issue", "Bursty clients trip the limiter; considering a sliding window."),
        ]),
        ("Migrate session store to Redis", [
            ("lyra-infra", "progress", "Provisioned Redis and dual-writing sessions."),
            ("lyra-infra", "decision", "Cut over reads after a 24h shadow window."),
        ]),
        ("Refactor notification service", [
            ("sirius-reviewer", "issue", "Blocked on rate-limiting shape from the API task."),
            ("atlas-lead", "decision", "Hold refactor until rate-limit contract lands."),
        ]),
        ("Write end-to-end tests for checkout", [
            ("mira-qa-eng", "progress", "Drafted the cart-to-confirmation scenario matrix."),
            ("mira-qa-eng", "issue", "Need a seeded payment sandbox for deterministic runs."),
        ]),
        ("Optimize image asset loading", [
            ("vega-frontend", "progress", "Audited hero images; candidates for lazy loading."),
            ("draco-docs", "progress", "Documented the target size budget per breakpoint."),
        ]),
        ("Document REST API with OpenAPI", [
            ("draco-docs", "progress", "Imported existing routes into an OpenAPI skeleton."),
            ("nova-qa", "decision", "Generate the client stubs from the spec, not by hand."),
        ]),
        ("Fix flaky WebSocket reconnect", [
            ("orion-backend", "issue", "Reconnect storm under network flaps; backoff insufficient."),
            ("sirius-reviewer", "issue", "Repro is intermittent; needs a deterministic harness."),
            ("atlas-lead", "summary", "Escalated: park until a stable reproduction exists."),
        ]),
    ]
    memos: list[TaskMemo] = []
    for title, entries in memo_specs:
        base = tasks[title]
        for i, (author, mtype, content) in enumerate(entries):
            memos.append(TaskMemo(
                task_id=base.id,
                project_id=pid,
                author=author,
                memo_type=mtype,
                content=content,
                scope_path="/project/platform",
                created_at=(base.started_at or base.created_at) + timedelta(hours=6 + i * 8),
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
    # (A) Completed-rich run — 4 phases, 7 fan-out agents, one FAILED lane.
    sdk_phases = [
        {"index": 0, "title": "Analyze API surface"},
        {"index": 1, "title": "Generate service clients"},
        {"index": 2, "title": "Run integration checks"},
        {"index": 3, "title": "Assemble release bundle"},
    ]
    # (label, phase_index, phase_title, state, tokens, tool_calls, dur_ms, prompt, result)
    sdk_agent_specs = [
        ("scan:endpoints", 0, "Analyze API surface", "done", 48_200, 14, 171_000,
         "Enumerate REST endpoints from the OpenAPI schema.",
         "Catalogued 42 endpoints across 9 resource groups."),
        ("gen:typescript", 1, "Generate service clients", "done", 61_200, 19, 204_000,
         "Generate the TypeScript client from the schema.",
         "Emitted a typed client with 42 methods and models."),
        ("gen:python", 1, "Generate service clients", "done", 57_800, 18, 198_000,
         "Generate the Python client from the schema.",
         "Emitted an async Python client and pydantic models."),
        ("gen:go", 1, "Generate service clients", "done", 52_100, 16, 182_000,
         "Generate the Go client from the schema.",
         "Emitted a Go client with typed request builders."),
        ("test:contract", 2, "Run integration checks", "failed", 39_400, 21, 142_000,
         "Validate the generated clients against the contract suite.",
         "Contract mismatch on pagination params; 3 checks failed."),
        ("test:smoke", 2, "Run integration checks", "done", 33_600, 12, 121_000,
         "Run smoke tests against a mock server.",
         "All smoke tests passed against the mock server."),
        ("reduce:bundle", 3, "Assemble release bundle", "done", 44_900, 15, 158_000,
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
            cc_agent_id=f"{WF_SDKGEN}-agent-{i}",
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
        {"index": 0, "title": "Collect usage metrics"},
        {"index": 1, "title": "Aggregate per-endpoint stats"},
        {"index": 2, "title": "Publish dashboard export"},
    ]
    # (label, phase_index, phase_title, state, tokens, tool_calls, dur_ms, prompt, result)
    metrics_agent_specs = [
        ("collect:logs", 0, "Collect usage metrics", "done", 28_700, 9, 96_000,
         "Ingest raw request logs from the metrics bucket.",
         "Ingested 6 log shards (~2.1M rows)."),
        ("aggregate:shard-a", 1, "Aggregate per-endpoint stats", "running", 14_300, 6, None,
         "Aggregate per-endpoint latency for shard A.", ""),
        ("aggregate:shard-b", 1, "Aggregate per-endpoint stats", "running", 12_900, 5, None,
         "Aggregate per-endpoint latency for shard B.", ""),
        ("aggregate:shard-c", 1, "Aggregate per-endpoint stats", "progress", 9_800, 4, None,
         "Aggregate per-endpoint latency for shard C.", ""),
        ("publish:export", 2, "Publish dashboard export", "queued", 0, 0, None,
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
    wf_metrics_agents = [
        WorkflowAgent(
            run_id=WF_METRICS, wf_id=WF_METRICS, project_id=pid,
            cc_agent_id=f"{WF_METRICS}-agent-{i}",
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
    # Live members of the running workflow team (so the active team card renders
    # in-flight members with their phase labels). cc_tool_use_id ↔ cc_agent_id.
    metrics_member_agents: list[Agent] = []
    for i, (label, _pidx, _pt, state, *_rest) in enumerate(metrics_agent_specs):
        if i == 0:
            continue  # phase-0 collector already finished — skip as a member
        metrics_member_agents.append(Agent(
            team_id=wf_metrics_team.id,
            name=f"wf-{WF_METRICS[-6:]}-{i}",
            role="workflow-agent",
            status=AgentStatus.BUSY if state in ("running", "progress") else AgentStatus.WAITING,
            trust_score=0.6,
            model="claude-opus-4-8[1m]",
            current_task=label,
            project_id=pid,
            source="hook",
            cc_tool_use_id=f"{WF_METRICS}-agent-{i}",
            created_at=metrics_start,
            last_active_at=ago(minutes=1),
        ))

    # (C) + (D) light completed runs for history depth.
    fix_start = ago(days=6, hours=2)
    wf_fixtures = WorkflowRun(
        wf_id=WF_FIXTURES, project_id=pid, team_id=wf_fixtures_team.id,
        name="Build deterministic test fixtures", status="completed", source="hook+file",
        phases=[{"index": 0, "title": "Extract schema"}, {"index": 1, "title": "Emit fixtures"}],
        planned_agent_count=2, agent_count=2, total_tokens=71_400, total_tool_calls=23,
        duration_ms=452_000, summary="Generated deterministic fixtures for the checkout suite.",
        script_path="/workspace/aurora-web/workflows/fixtures.js",
        started_at=fix_start, completed_at=ago(days=6, hours=1, minutes=52),
        created_at=fix_start, updated_at=ago(days=6, hours=1),
    )
    wf_fixtures_agents = [
        WorkflowAgent(
            run_id=WF_FIXTURES, wf_id=WF_FIXTURES, project_id=pid,
            cc_agent_id=f"{WF_FIXTURES}-agent-{i}", label=label,
            phase_index=i, phase_title=ptitle, model="claude-opus-4-8[1m]",
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
        phases=[{"index": 0, "title": "Diff API versions"}, {"index": 1, "title": "Write changelog"}],
        planned_agent_count=3, agent_count=3, total_tokens=96_800, total_tool_calls=31,
        duration_ms=548_000, summary="Diffed v3 vs v4 and drafted a migration changelog.",
        script_path="/workspace/aurora-web/workflows/apidiff.js",
        started_at=diff_start, completed_at=ago(days=9, hours=2, minutes=51),
        created_at=diff_start, updated_at=ago(days=9, hours=2),
    )
    wf_apidiff_agents = [
        WorkflowAgent(
            run_id=WF_APIDIFF, wf_id=WF_APIDIFF, project_id=pid,
            cc_agent_id=f"{WF_APIDIFF}-agent-{i}", label=label,
            phase_index=pidx, phase_title=ptitle, model="claude-opus-4-8[1m]",
            state="done", tokens=tokens, tool_calls=tc, duration_ms=dur,
            last_tool_name="Write", prompt_preview=prompt, result_preview=result,
            started_at=diff_start + timedelta(seconds=15 * i), queued_at=diff_start,
            last_activity_at=diff_start + timedelta(milliseconds=dur),
            created_at=diff_start, updated_at=ago(days=9, hours=2),
        )
        for i, (label, pidx, ptitle, tokens, tc, dur, prompt, result) in enumerate([
            ("diff:v3", 0, "Diff API versions", 31_200, 9, 168_000,
             "Diff the v3 and v4 OpenAPI schemas.", "Found 11 breaking and 24 additive changes."),
            ("diff:v4", 0, "Diff API versions", 28_600, 8, 151_000,
             "Classify the changes by severity.", "Classified 35 changes across 6 resources."),
            ("write:changelog", 1, "Write changelog", 37_000, 14, 229_000,
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
    ev(21, EventType.SYSTEM_STARTED, "system", {"version": "demo"})
    ev(20, EventType.TEAM_CREATED, "api", {"name": team_core.name},
       team_core.id, "team", {"name": team_core.name})
    ev(18, EventType.TEAM_CREATED, "api", {"name": team_quality.name},
       team_quality.id, "team", {"name": team_quality.name})
    ev(15.2, EventType.TEAM_CREATED, "api", {"name": archived_team.name},
       archived_team.id, "team", {"name": archived_team.name})
    for offset, name in enumerate(
        ["atlas-lead", "orion-backend", "corvus-backend", "vega-frontend",
         "lyra-infra", "cirrus-lead"]
    ):
        a = agents[name]
        ev(20 - offset * 0.4, EventType.AGENT_CREATED, "hook",
           {"name": name, "role": a.role}, a.id, "agent", {"name": name})
    for offset, name in enumerate(["nova-qa", "sirius-reviewer", "mira-qa-eng", "draco-docs"]):
        a = agents[name]
        ev(18 - offset * 0.5, EventType.AGENT_CREATED, "hook",
           {"name": name, "role": a.role}, a.id, "agent", {"name": name})

    # Task lifecycle events derived from task timing
    for title, t in tasks.items():
        ev(_delta_days(t.created_at),
           EventType.TASK_CREATED, "api", {"title": title}, t.id, "task",
           {"title": title, "status": "pending"})
        if t.started_at is not None:
            ev(_delta_days(t.started_at),
               EventType.TASK_STARTED, "hook", {"title": title}, t.id, "task",
               {"title": title, "status": "running"})
        if t.status is TaskStatus.COMPLETED and t.completed_at is not None:
            ev(_delta_days(t.completed_at),
               EventType.TASK_COMPLETED, "hook", {"title": title}, t.id, "task",
               {"title": title, "status": "completed"})
        if t.status is TaskStatus.FAILED:
            ev(7.5, EventType.TASK_FAILED, "hook", {"title": title}, t.id, "task",
               {"title": title, "status": "failed"})
        if t.status is TaskStatus.BLOCKED:
            ev(6.8, EventType.TASK_BLOCKED, "api", {"title": title}, t.id, "task",
               {"title": title, "status": "blocked"})

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

    all_agents = list(agents.values()) + metrics_member_agents

    return {
        "projects": [ProjectModel.from_pydantic(project)],
        "teams": [TeamModel.from_pydantic(t) for t in all_teams],
        "agents": [AgentModel.from_pydantic(a) for a in all_agents],
        "tasks": [TaskModel.from_pydantic(t) for t in tasks.values()],
        "task_memos": [TaskMemoModel.from_pydantic(m) for m in memos],
        "reports": [ReportModel.from_pydantic(r) for r in reports],
        "meetings": [MeetingModel.from_pydantic(meeting)],
        "meeting_messages": [MeetingMessageModel.from_pydantic(m) for m in meeting_msgs],
        "agent_activities": [AgentActivityModel.from_pydantic(a) for a in activities],
        "events": [EventModel.from_pydantic(e) for e in events],
        "workflow_runs": [WorkflowRunModel.from_pydantic(r) for r in workflow_runs],
        "workflow_agents": [WorkflowAgentModel.from_pydantic(a) for a in workflow_agents],
        "ecosystem_repo_profiles": [EcosystemRepoProfileModel.from_pydantic(p) for p in eco_profiles],
    }


# Insertion order is FK-safe: parents before children.
_INSERT_ORDER = (
    "projects", "teams", "agents", "tasks", "task_memos", "reports",
    "meetings", "meeting_messages", "agent_activities", "events",
    "workflow_runs", "workflow_agents", "ecosystem_repo_profiles",
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
    return counts


# ---------------------------------------------------------------------------
# Optional filesystem fixture for the two LIVE-probe project-detail cards.
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> None:
    """Run a git command in *cwd*, raising on failure (fixture must be correct)."""
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def build_fixture(root_path: str) -> list[str]:
    """Build the throwaway git repo (+2 worktrees) and 3 session transcripts.

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

    # (c) three synthetic session transcripts so session_probe yields 3 leaders
    #     with distinct context watermarks. slug mirrors CC's naming.
    import re
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
    args = parser.parse_args()

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
