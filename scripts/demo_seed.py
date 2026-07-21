"""Seed a neutral demo AI Team OS database for screenshots and walkthroughs.

Builds a self-contained SQLite database at the ``--db`` path using the project's
own storage layer (``types`` Pydantic models + ``storage.models`` ORM +
``storage.connection`` async session). Everything written is generic, English and
technology-neutral — a fictional "Aurora Web Platform" product — so the dashboard
can be demoed without exposing any real project vocabulary.

What it creates:
  * 1 project              "Aurora Web Platform"
  * 3 teams                2 delivery teams + 1 simulated workflow team
  * 7 agents               varied status / trust score / model
  * 10 tasks               every status represented, English technical titles
  * ~25 task memos         2-3 per task, mixed authors and types
  * 2 reports              a design doc and an analysis
  * 1 meeting + 6 messages sprint planning, two discussion rounds
  * 35 events              spread across the last two weeks
  * 1 workflow run + 3 workflow agents (the workflow-<id> team simulation)

Usage:
    python scripts/demo_seed.py --db /tmp/aiteam-demo.db [--force]

Options:
    --db PATH    Target SQLite file to build (required).
    --force      Overwrite the target file if it already exists.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

# src layout: make ``import aiteam`` resolve to THIS checkout, not any globally
# installed copy — the seed must exercise the local storage layer under test.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aiteam.storage.connection import get_session, init_db  # noqa: E402
from aiteam.storage.models import (  # noqa: E402
    AgentModel,
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
    AgentStatus,
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


def ago(days: float = 0, hours: float = 0, minutes: float = 0) -> datetime:
    """Return a timestamp *days/hours/minutes* before :data:`NOW`."""
    return NOW - timedelta(days=days, hours=hours, minutes=minutes)


def build_dataset() -> dict[str, list]:
    """Construct all demo rows as ORM instances, grouped by table.

    Returns a mapping ``table_label -> [orm rows]`` in FK-safe insertion order.
    All content is neutral English; no real project vocabulary appears anywhere.
    """
    project = Project(
        name="Aurora Web Platform",
        root_path="/workspace/aurora-web",
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
    wf_id = "wf_aurora_sdkgen_01"
    wf_team = Team(
        name=f"workflow-{wf_id}",
        mode=OrchestrationMode.ROUTE,
        project_id=pid,
        status=TeamStatus.COMPLETED,
        summary="Auto-tracked workflow run: generate API client SDKs.",
        created_at=ago(days=2, hours=4),
        updated_at=ago(days=2, hours=3),
        completed_at=ago(days=2, hours=3),
    )

    # -- Agents ----------------------------------------------------------------
    # (name, team, role, status, trust, model, current_task, created_days_ago, active_hours_ago)
    agent_specs = [
        ("atlas-lead", team_core, "leader", AgentStatus.BUSY, 0.90,
         "claude-opus-4-8[1m]", "Coordinating the authentication milestone", 20, 0.5),
        ("orion-backend", team_core, "backend-engineer", AgentStatus.BUSY, 0.72,
         "claude-sonnet-4-5", "Adding rate limiting to the public API", 19, 0.3),
        ("vega-frontend", team_core, "frontend-engineer", AgentStatus.WAITING, 0.66,
         "", None, 17, 5),
        ("lyra-infra", team_core, "infra-engineer", AgentStatus.OFFLINE, 0.51,
         "claude-sonnet-4-5", None, 16, 30),
        ("nova-qa", team_quality, "leader", AgentStatus.WAITING, 0.83,
         "claude-opus-4-8[1m]", "Reviewing the checkout test plan", 18, 2),
        ("sirius-reviewer", team_quality, "code-reviewer", AgentStatus.BUSY, 0.64,
         "claude-sonnet-4-5", "Reviewing the notification service refactor", 15, 0.8),
        ("draco-docs", team_quality, "tech-writer", AgentStatus.OFFLINE, 0.47,
         "", None, 12, 48),
    ]
    agents: dict[str, Agent] = {}
    for name, team, role, status, trust, model, cur, cdays, ahours in agent_specs:
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
        ("Write end-to-end tests for checkout", team_quality, "nova-qa",
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
            ("nova-qa", "progress", "Drafted the cart-to-confirmation scenario matrix."),
            ("nova-qa", "issue", "Need a seeded payment sandbox for deterministic runs."),
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

    # -- Workflow run + agents -------------------------------------------------
    wf_run = WorkflowRun(
        wf_id=wf_id,
        project_id=pid,
        team_id=wf_team.id,
        name="Generate API client SDKs",
        status="completed",
        source="hook+file",
        phases=[
            {"index": 0, "title": "Parse OpenAPI spec"},
            {"index": 1, "title": "Generate per-language clients"},
            {"index": 2, "title": "Merge and lint"},
        ],
        planned_agent_count=3,
        agent_count=3,
        total_tokens=184_320,
        total_tool_calls=57,
        duration_ms=612_000,
        summary="Generated TypeScript, Python and Go clients from the OpenAPI spec.",
        script_path="/workspace/aurora-web/workflows/sdkgen.js",
        started_at=ago(days=2, hours=4),
        completed_at=ago(days=2, hours=3, minutes=50),
        created_at=ago(days=2, hours=4),
        updated_at=ago(days=2, hours=3),
    )
    wf_agent_specs = [
        ("map:typescript", 1, "Generate per-language clients", "done", 61_200, 18,
         "Generate the TypeScript client from the OpenAPI schema."),
        ("map:python", 1, "Generate per-language clients", "done", 58_900, 17,
         "Generate the Python client from the OpenAPI schema."),
        ("reduce:merge", 2, "Merge and lint", "done", 64_220, 22,
         "Merge generated clients, run linters and assemble the release bundle."),
    ]
    wf_agents = [
        WorkflowAgent(
            run_id=wf_id,
            wf_id=wf_id,
            project_id=pid,
            cc_agent_id=f"{wf_id}-agent-{i}",
            label=label,
            phase_index=phase_idx,
            phase_title=phase_title,
            model="claude-opus-4-8[1m]",
            state=state,
            tokens=tokens,
            tool_calls=tool_calls,
            duration_ms=180_000 + i * 12_000,
            last_tool_name="Write",
            prompt_preview=prompt,
            result_preview="Completed successfully.",
            started_at=ago(days=2, hours=4) + timedelta(minutes=i),
            queued_at=ago(days=2, hours=4),
            last_activity_at=ago(days=2, hours=3, minutes=52),
            created_at=ago(days=2, hours=4),
            updated_at=ago(days=2, hours=3),
        )
        for i, (label, phase_idx, phase_title, state, tokens, tool_calls, prompt)
        in enumerate(wf_agent_specs)
    ]

    # -- Events (time-spread across the last ~2 weeks) -------------------------
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
    for name in ("atlas-lead", "orion-backend", "vega-frontend", "lyra-infra"):
        a = agents[name]
        ev(20 - list(agents).index(name), EventType.AGENT_CREATED, "hook",
           {"name": name, "role": a.role}, a.id, "agent", {"name": name})
    for name in ("nova-qa", "sirius-reviewer", "draco-docs"):
        a = agents[name]
        ev(18 - list(agents).index(name) + 4, EventType.AGENT_CREATED, "hook",
           {"name": name, "role": a.role}, a.id, "agent", {"name": name})

    # Task lifecycle events derived from task timing
    for title, t in tasks.items():
        ev((NOW - t.created_at).days + (NOW - t.created_at).seconds / 86400,
           EventType.TASK_CREATED, "api", {"title": title}, t.id, "task",
           {"title": title, "status": "pending"})
        if t.started_at is not None:
            ev((NOW - t.started_at).days + (NOW - t.started_at).seconds / 86400,
               EventType.TASK_STARTED, "hook", {"title": title}, t.id, "task",
               {"title": title, "status": "running"})
        if t.status is TaskStatus.COMPLETED and t.completed_at is not None:
            ev((NOW - t.completed_at).days + (NOW - t.completed_at).seconds / 86400,
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

    # Workflow observability events
    ev(2.17, EventType.WORKFLOW_PLANNED, "hook",
       {"wf_id": wf_id, "planned_agents": 3}, wf_team.id, "team", {"status": "planned"})
    ev(2.16, EventType.WORKFLOW_STARTED, "hook",
       {"wf_id": wf_id}, wf_team.id, "team", {"status": "running"})
    ev(2.12, EventType.WORKFLOW_COMPLETED, "file",
       {"wf_id": wf_id, "agent_count": 3, "total_tokens": wf_run.total_tokens},
       wf_team.id, "team", {"status": "completed"})

    # A couple of decision + channel events for texture
    ev(10.5, EventType.DECISION_APPROACH_CHOSEN, "api",
       {"choice": "authorization-code flow"},
       tasks["Implement OAuth login flow"].id, "task", None)
    ev(0.9, EventType.CHANNEL_MESSAGE, "api",
       {"channel": "team:Platform Core", "sender": "atlas-lead"}, None, None, None)

    return {
        "projects": [ProjectModel.from_pydantic(project)],
        "teams": [TeamModel.from_pydantic(t) for t in (team_core, team_quality, wf_team)],
        "agents": [AgentModel.from_pydantic(a) for a in agents.values()],
        "tasks": [TaskModel.from_pydantic(t) for t in tasks.values()],
        "task_memos": [TaskMemoModel.from_pydantic(m) for m in memos],
        "reports": [ReportModel.from_pydantic(r) for r in reports],
        "meetings": [MeetingModel.from_pydantic(meeting)],
        "meeting_messages": [MeetingMessageModel.from_pydantic(m) for m in meeting_msgs],
        "events": [EventModel.from_pydantic(e) for e in events],
        "workflow_runs": [WorkflowRunModel.from_pydantic(wf_run)],
        "workflow_agents": [WorkflowAgentModel.from_pydantic(a) for a in wf_agents],
    }


async def seed(db_url: str) -> dict[str, int]:
    """Create the schema and insert the full demo dataset. Returns row counts."""
    await init_db(db_url)
    dataset = build_dataset()
    counts: dict[str, int] = {}
    async with get_session(db_url) as session:
        # Insertion order is FK-safe: parents before children.
        for label in (
            "projects", "teams", "agents", "tasks", "task_memos", "reports",
            "meetings", "meeting_messages", "events", "workflow_runs",
            "workflow_agents",
        ):
            rows = dataset[label]
            session.add_all(rows)
            counts[label] = len(rows)
    return counts


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
    args = parser.parse_args()

    target = _resolve_target(args.db, args.force)
    db_url = f"sqlite+aiosqlite:///{target}"
    counts = asyncio.run(seed(db_url))

    print(f"Seeded demo database at {target}")
    for label, n in counts.items():
        print(f"  {label:18s} {n}")
    print(f"  {'TOTAL rows':18s} {sum(counts.values())}")


if __name__ == "__main__":
    main()
