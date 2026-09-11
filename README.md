[English](README.md) | [中文](README.zh-CN.md)

# AI Team OS

<!-- Logo placeholder -->
<!-- ![AI Team OS Logo](docs/assets/logo.png) -->

### Shared context, accountable work, native agents.

AI Team OS is a shared operating layer for **Claude Code and Codex**. Keep tasks, project memory, reports and team messages in one place, and follow work across sessions in one Dashboard. Each host keeps its native agent tools; the OS provides the durable record that makes their work understandable and reusable.

> 🤝 **Codex is supported.** Use Codex or Claude Code on its own, or connect both to the same OS task wall, project memory, reports, channels and Dashboard. Codex uses its own MCP and hook configuration; native agent tools, host settings and hook trust remain separate. See the installation and capability sections below for the per-host setup and boundaries.

<!-- Keep the Codex compatibility note above across releases. For each release, replace the current-release announcement with that version's verified summary. Keep historical details in CHANGELOG.md. -->

> ⚡ **v1.12.4 — Shared-host observations and runtime reliability.** This batch improves Claude/Codex Leader attribution, native member names and parent teams, and current-work visibility; fixes project-scoped events and Analytics, cross-team totals and tool-completion pairing; and adds legacy session-team collision repair and runtime diagnostics. See the changelog for validation results, upgrade requirements and remaining limitations.
>
> Full version history: [CHANGELOG.md](CHANGELOG.md)

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Protocol-orange)](https://modelcontextprotocol.io)
[![Stars](https://img.shields.io/github/stars/CronusL-1141/AI-company?style=flat)](https://github.com/CronusL-1141/AI-company)

**116** MCP tools · **211** REST endpoints · **23** dashboard pages · **25** agent templates · **42** ecosystem research tools · **21** machine-checked invariants

---

**A session can end without taking the team's context with it.** Tasks, memos, decisions and reports remain available to the next authorized session, whether it runs in Claude Code or Codex.

---

## What Carries Across Sessions

Parallel agents are useful only when you can tell who owns the work, what actually happened and where to resume. AI Team OS keeps those answers outside any single chat:

- **Tasks and handoffs**: ownership, progress memos, blockers and completion records stay on the project task wall.
- **Project memory and reports**: retrieve earlier decisions and evidence instead of rebuilding context from scratch.
- **Team communication**: send and read project-scoped messages across hosts, with explicit reader identities and acknowledgements.
- **Operational visibility**: inspect Leaders, members, tool activity and project-level totals in one Dashboard.

The OS records and exposes the work. Your chosen host runs the agents, and you decide what they are authorized to do.

---

## How It Works

**You set the scope. Each root session has its own Leader.** A Claude Leader and a Codex Leader can contribute to the same project without pretending to be the same process or sharing host configuration.

1. Resolve the project and read its task wall, relevant memos and memory.
2. Let the session's Leader coordinate authorized work using its host's native agent tools. Members belong to their parent team; Codex's native nicknames stay distinct from roles and task names.
3. Record progress, decisions and reports through the shared MCP tools. Other sessions can pick them up through the same project records and channels.
4. Inspect the Dashboard to compare recorded work with current activity. The observation updates label the host, show only fresh working evidence in current views and fold waiting or historical records away without deleting them.

Claude Code's installed hooks can supply startup briefings and direction-layer context automatically. Codex can read the same records through MCP, with its own adapter handling supported observations. The OS does not replace either host's scheduler, permissions or agent lifecycle.

---

## Core Capabilities

### 1. Cross-Session Coordination

Shared project records and channels connect sessions while execution stays native to each host:

- **One Leader per root session**: the implementation combines registered sessions with Claude file observations and labels `Claude Leader` and `Codex Leader` explicitly. Native Codex children join the parent team instead of becoming extra Leaders.
- **Current work and history**: fresh `busy` evidence drives the current roster; waiting, closed and stale records remain available as history. Unknown source or model information stays unknown.
- **Project and worktree visibility**: inspect current tasks, observed context and uncommitted work before handing off or continuing a session.
- **Cross-host messages**: use `channel_send`, `channel_read` and `channel_wait` for explicit communication. A pending wait can return new messages; it does not restart an ended Codex turn.
- **Claude Code extensions**: the existing fleet path can resume a Claude session for one turn, and installed CC hooks support compaction checkpoints, session-registry observations and background-job visibility. These execution and injection paths are not Codex features.

### 2. Memory System v2 - Shared Direction and Task History

Keep team preferences and task evidence available across sessions, without relying on a single chat's remaining context.

- **Direction layer** (user preferences / corrections / design intent, 4 kinds): stored with per-bucket character quotas (global 1200 + 1500 per project + user 300 = 3000 chars, <=400 chars per entry), replacement via `supersedes` and auditable invalidation rather than deletion. Writes are scanned for invisible characters, instruction-override patterns and credential shapes. Claude Code's SessionStart and SubagentStart hooks inject this context; Codex retrieves the shared records through its configured tools.
- **Episodic layer** (`task_memos` ledger): task-level execution memos promoted to a dedicated table (row IDs / invalidation axis / quality score / scope_path), recalled on demand via pure-Python **BM25 Chinese retrieval**; 123 legacy memos backfilled with zero loss.
- **On-demand reconcile** (`memory_reconcile`): zero-LLM BM25 candidate clustering, then merge / invalidate / score / distill on agent confirmation — "the agent computes, the tool persists", with no background resident process introduced.

Surfaces: MCP `memory_add` / `memory_list` / `memory_invalidate` / `memory_search` / `memory_reconcile_candidates` / `memory_reconcile_apply`.

### 3. Progressive Tool-Loading Governance (new in v1.9.0)

Choose the MCP surface for each client instead of loading every capability into every session.

- **alwaysLoad dynamic rotation**: at session start a single SQL recomputes the hot-tool whitelist by **7-day real call frequency** (>=2-day span gate against bursty spikes + 20% hysteresis, hard cap <=5), and CC skips ToolSearch for them. Not additive, not hand-tuned; any stats failure silently degrades to all-defer, and every whitelist is logged for audit.
- **`AITEAM_TOOLSETS` group switch**: 16 capability-domain toolsets; a startup env var decides which modules register. `default` core profile = task/team/memory/infra/reports (29 tools, hard cap <=50), with incremental `default,ecosystem` — fits non-CC clients that cap tool counts.
- **`AITEAM_READONLY` read-only profile**: an orthogonal overlay that strips every write tool by explicit allowlist and keeps only read tools — ideal for audit / observer sessions.
- **5 Claude Code templates on least privilege**: meeting-facilitator / debate advocate & critic / technical-writer / project-manager carry `disallowedTools` structural denials. Codex uses its own native permission controls rather than interpreting CC template fields.

### 4. Claude Code Workflow / ultracode Observability (v1.7.0)

The OS does not intercept CC's built-in **ultracode/Workflow** — it becomes its persistent governance layer. Every Workflow run is automatically tracked into the OS, with no manual team setup:

- **Auto-tracking**: a hook turns each Workflow run into an OS "team" (`workflow-<wf_id>`) the moment it starts
- **Dashboard `/workflows`**: a live feed of run cards, a phase swimlane timeline, and per-agent telemetry — tokens / duration / status / tool-call counts, advancing live via incremental journal tailing while a run executes
- **Calibrated stall detection**: the stall threshold was calibrated on 3,378 real agent intervals (p99 = 77.6s, longest healthy silence 173.8s) and set at 5.2× the worst healthy case — it flags late rather than crying wolf
- **Project-detail integration**: workflow team rows carry an inline run summary (status / agent count / duration / finish time) plus a "view swimlane" deep link; members display semantic phase labels (e.g. `audit:sourceA`) instead of ids
- **Claude Leader file observations**: the backend can supplement registered Leaders with session, model and liveness observations from Claude's local records. Codex identity follows its separate native metadata path.
- **MCP tools**: `workflow_list` (browse runs), `workflow_get` (full archive + per-agent rows), `workflow_reconcile` (repair from on-disk snapshots after the OS was offline)
- **Self-healing ingestion**: hook receipt anchors + on-disk snapshot reconciliation + a reaper backstop close offline gaps automatically — finished runs on disk are ingested idempotently; cross-project attribution matches the on-disk path slug against registered projects

### 5. Ecosystem Research Platform — 42 tools

A project-isolated **knowledge base** that accumulates research findings over time. Each repo progresses through 4 stages (a progressive funnel, since v1.5.0), with token-efficient triggers and append-only history:

- **Stage 0 — Auto shallow-summary on archive**: newly-archived repos automatically get a 200-400 char `ai-engineer` summary (core function / positioning / advantages). 8-class failure handling with **self-learning hooks** (3+ same-class fails surface through `self_learning_pending`; the queue exposes recorder/searcher injection points you can wire to your own lesson store)
- **Stage 1 — On-demand architecture analysis**: user picks research direction ("memory_system") → batch-dispatch `backend-architect` agents to read architecture key files
- **Stage 2 — Multi-perspective debate**: triggers existing `debate_start` (NOT a built-in debate engine — **reuses meeting system**)
- **Stage 3 — Reference / Integrate marking**: `mark_as_reference` adds tag for future quick recall; `start_integration` triggers existing `task_create` for actual implementation
- **Active vs Full dual-view**: data is **append-only forever**. Stars-falling repos kept (just `is_active=False`); stars climbing back auto-promotes + re-queues Stage 0
- **Dashboard `/ecosystem`**: list with stage badges + research timeline + project filter dropdown + candidate-filter page (`/ecosystem/research`) + per-project settings tab — the single largest tool family in the OS

### 6. Knowledge Layer — Reference Graph + Unified Search (v1.8.0)

Everything the OS records — task memos, reports, tasks — becomes recallable knowledge:

- **Reference graph (P1a)**: a zero-LLM regex extractor mines OS-native ID references (wf_id / commit hash / task uuid / `[[memory]]`) out of memos and reports into an append-only `knowledge_links` table — the graph is a derived view, rebuildable from source text at any time
- **Unified search (P1b)**: `/api/search` fuses three arms via RRF — BM25 full-text (Chinese bigram native), knowledge-graph fanout (an ID query pulls in everything linked to it), and exact ID-prefix / title match
- **Global search box** in the Dashboard header, plus MCP tools `unified_search` / `link_query` / `link_trace` — recall past work by natural language ("how was the attribution fix done"), a `wf_` id, or a commit hash

> **Why zero-LLM retrieval?** ID extraction and search run locally without a model call, and the graph can be rebuilt from source text. Reading retrieved results into an agent's context still consumes that host's normal context budget.

### 7. Task Wall, Reports and Dashboard

Governance ledger and panoramic visualization — everything leaves a trace:

- **Task wall**: a live board of pending / in-progress / done, event-driven + intelligent Agent matching + deadlock detection
- **8 structured meeting templates** (keyword auto-select, built on Six Thinking Hats / DACI / Design Sprint) — every meeting must produce an actionable conclusion; "we discussed but didn't decide" is not an outcome
- **Shared React 19 Dashboard**: project task walls, reports, agent activity, events and Analytics sit alongside the Claude-specific Workflow and model-governance views.

### 8. Work That Can Be Resumed

The task wall gives a running Leader a durable plan:

- Find the next authorized item and record ownership before dispatching native agents.
- Keep blockers and approval requests visible through task memos and briefings.
- Hand off progress and evidence so another session can continue without guessing.
- Turn research findings and review decisions into explicit follow-up tasks.

Continued execution depends on the host session and the automation you enable. Persistent records do not imply an always-running model.

### 9. Evidence-Based Observations

The OS separates recorded facts from inferred or missing information:

- **Host-specific identity**: Claude file observations and Codex's exact native session metadata remain distinct. Codex's nickname and parent chain determine member identity; role text and ID appearance do not.
- **Freshness and ownership**: the implementation combines persisted project/session bindings with recent activity, keeping current work separate from historical rows. Event and Analytics project filters follow recorded ownership.
- **Reliable tool records**: stable Codex call IDs pair starts and completions across retries and API restarts. Later hooks can retry completion metadata within bounded limits; absent trustworthy start/end evidence, duration stays unknown.
- **Workflow telemetry**: the Claude Workflow view reconciles on-disk journals with persisted observations and exact project-path attribution.

### 10. Claude Code Model Governance (v1.8.1)

Inspect models observed in Claude Code transcripts and choose Claude Code's startup default. This setting does not control Codex's model selection.

- **Transcript-based discovery**: local CC records supply observed model names, including third-party gateway names, with a 60s cache. This is observation history, not a live account-availability test.
- **One-click global default startup model**: written to `~/.claude/settings.json` under triple write protection — touches only the `model` key, keeps a `.bak-aiteam` backup, writes atomically, refuses corrupted files
- **Zero coercion**: soft reminders only, never a block — and CC Workflow runs are fully exempt

Surfaces: REST `/api/models/{available,default}` · MCP `model_config_get` / `model_config_set` · the Model Governance card in Dashboard Settings.

### 11. Team Collaboration

Coordinate native agents and peer Leaders without flattening their identities:

- **25 professional role templates** (23 base + 2 debate roles) with a recommendation engine for engineering, testing, research and management. Claude Code installs them as native templates; Codex keeps its own native agent setup.
- **Department grouping** — Engineering / QA / Research with cross-team coordination
- **Channel communication**: `team:` / `project:` / `global` channels with `@mention` support
- **Cross-host messaging**: sending, reading and acknowledging use shared channels with distinct reader identities. Prompt-time unread hints have been measured on Claude Code and on Codex CLI/Desktop; the Codex hint uses structured `additionalContext`, not plain hook stdout.
- **Explicit waiting**: `channel_wait` holds a call open and identifies initial replay, event-triggered read or final timeout read in `delivery_source`. It is separate from acknowledgement and from Claude Code's optional session watcher; it does not wake an idle Codex session after a turn ends.
- **Debate mode**: 4-round structured debate (Advocate→Critic→Response→Judge) via `debate_start` / `debate_code_review`
- **Cross-agent lessons**: `failure_analysis` records root causes in project memory for later sessions to retrieve. Automatic injection follows the installed host integration, not a shared assumption about both runtimes.

### 12. Full Transparency

Trace the observations and records behind the Dashboard:

- **Decision Cockpit**: event stream + decision timeline + intent inspection — every decision has a traceable record
- **Activity Tracking**: observed agent status, current work and retained history, with explicit unknown values when evidence is missing
- **What-If Analyzer**: compare multiple approaches before committing, with path simulation and recommendations

### 13. Safety & Behavioral Enforcement

OS checks complement each host's native approvals and isolation controls. Install and review the applicable host hooks rather than assuming one host's rules protect the other:

- **Guardrails L1**: 7 dangerous pattern detections + PII warnings + `InputGuardrailMiddleware`
- **Claude Code dispatch checks**: CC-specific hook and template rules validate its agent-dispatch fields; they are not Codex's native agent schema
- **S1 safety rules**: regex-based scan catches destructive commands (rm -rf, force push, hardcoded secrets) including uppercase flags and heredoc patterns
- **4-layer defense rule system**: 48+ rules covering workflow, delegation, session, and safety layers
- **Concurrent-edit warnings**: hooks flag a file two agents touched in quick succession, read straight from recent edit events (the cooperative file-lock tools were retired in v1.10.3 — the lock file was empty in every real run)
- **Agent Watchdog**: on-demand `POST /api/teams/{id}/watchdog/check` plus the background patrol — flags BUSY-timeout agents, long-pending tasks and unblockable dependencies
- **Self-patrol**: watchdog lease patrol + reaper reconciliation backstop + identity verification before any kill — the OS keeps eyes on itself, not just on your agents
- **Completion verification**: `verify_completion` checks task status and memo existence; artifact review and relevant tests still establish whether the requested result is correct
- **Ecosystem integration recipes**: 4 preset recipes (GitHub / Slack / Linear / Full-stack team) under `find_skill(level=2, category="integration")`
- **`find_skill` 3-layer progressive discovery**: quick recommend → category browse → full detail, reducing tool-call overhead

### 14. Local-First Infrastructure

The OS does not require its own hosted model service:

- MCP tools, hooks, storage and the Dashboard run locally.
- Graph extraction, BM25 search and reconciliation candidate generation do not call a model.
- Agent reasoning, retrieved context and AI-assisted research use the configured host's normal subscription or API budget; external integrations may have their own costs.
- Full Codex token and cost attribution is not yet available. Unknown usage is not a measured zero.

### More Capabilities (legacy & secondary — still running, queryable on demand)

- **Failure Alchemy**: `failure_analysis` still runs as part of the loop subsystem — every failed task extracts root cause and produces *Antibody* (stored in team memory to prevent repeats) / *Vaccine* (high-frequency failures become pre-task warnings) / *Catalyst* (analysis injected into future Agent system prompts). No longer the headline, but defensive rules keep accruing.
- **AWARE loop memory · `find_skill` 3-layer discovery (skills + integration recipes) · Prompt Registry**: see the full tool table below. The scheduler and the loop state machine were retired in favour of CC-native `Cron*` and on-demand tools (CC-is-not-always-on principle); the `wake_agent` schedule kind survives for the fleet wake subsystem.

---

## Used to Build This Project

AI Team OS manages its own development — and since v1.7.0, it can prove it with its own telemetry:

Claude Code and Codex sessions use the same task records and channels to exchange implementation and review evidence. Their native execution histories stay distinct; the OS provides the common project record.

- Every feature line from v1.7.0 to v1.9.0 — the observability layer, the knowledge layer, model governance, Memory System v2, tool-loading governance — shipped through CC Workflow runs that the OS tracked itself. Open `/workflows` and replay how the system built its own features, swimlane by swimlane.
- Competitive research across CrewAI, AutoGen, LangGraph, and Devin feeds the roadmap through multi-agent brainstorming meetings — the minutes live in the OS's own report store.
- It learns from its own incidents, too: every machine-checked invariant in `scripts/check_invariants.sh` was distilled from a real accident in this repo's history.

The same task wall, reports and observations used in development are available for your projects.

---

## How It Compares

| Dimension | AI Team OS | CrewAI | AutoGen | LangGraph | Devin |
|-----------|-----------|--------|---------|-----------|-------|
| **Category** | Shared OS for native coding agents | Standalone Framework | Standalone Framework | Workflow Engine | Standalone AI Engineer |
| **Integration** | MCP + independent Claude Code/Codex adapters | Independent Python | Independent Python | Independent Python | SaaS Product |
| **Memory System** | Shared direction memory + task memos + BM25 retrieval | Short-term context | Short-term context | Checkpoint state | In-session |
| **Tool-Loading Governance** | alwaysLoad rotation + group switch + read-only profile + template least-privilege | None | None | None | None |
| **Autonomous Operation** | Durable task coordination; execution depends on the host | Task-by-task | Task-by-task | Workflow-driven | Limited |
| **Meeting System** | 8 structured templates with auto-select | None | Limited | None | None |
| **Failure Learning** | Failure Alchemy (Antibody/Vaccine/Catalyst) | None | None | None | Limited |
| **Decision Transparency** | Decision Cockpit + Timeline | None | Limited | Limited | Black box |
| **Workflow Observability** | Swimlane timeline + per-agent telemetry + offline reconcile over CC Workflow | None | None | Graph state only | None |
| **State Source** | Host-native metadata + persisted observations and journals | Agent self-report | Agent self-report | In-process state | Black box |
| **Rule System** | 4-layer defense (48+ rules) + behavioral enforcement | Limited | Limited | None | Limited |
| **Agent Templates** | 25 Claude Code templates + shared role recommendations | Built-in roles | Built-in roles | None | None |
| **Dashboard** | React 19 visualization | Commercial tier | None | None | Yes |
| **Open Source** | MIT | Apache 2.0 | MIT | MIT | No |
| **Native Coding Hosts** | Claude Code and Codex, with distinct integration paths | No | No | No | No |
| **Extra Cost** | Local OS; host and integration usage costs still apply | API costs | API costs | API costs | $500+/mo |

---

## Architecture

```text
Claude Code native agents -> CC MCP / hook adapter    \
                                                      > Shared OS API -> SQLite
Codex native agents       -> Codex MCP / hook adapter /       |
                                                             +-> Dashboard
```

The database holds project-scoped tasks, memory, reports, channels and observations. Each host owns its scripts, registration, trust and native agent controls; sharing the OS backend does not merge those settings.

### Five-Layer Technical Architecture

```
Layer 5: Web Dashboard    — React 19 + TypeScript + Shadcn UI (23 pages)
Layer 4: CLI + REST API   — Typer + FastAPI
Layer 3: Team Orchestrator — LangGraph StateGraph (optional extra — CLI graph execution only)
Layer 2: Memory Manager   — SQLite-backed store + pure-Python BM25 retrieval
Layer 1: Storage          — SQLite (WAL journaling) · PostgreSQL support on the roadmap
```

### Host Adapters

Claude Code's plugin and Codex's adapter feed the same OS through separate installation and trust surfaces. The Codex adapter lives in `plugin/harness/codex/`; its observation entry and matching helper modules must be installed together. The following event map describes the Claude Code adapter only.

### Hook System (13 scripts across 15 Lifecycle Events - Claude Code Adapter)

```
SessionStart     → auto_install.py, session_bootstrap.py, send_event.py
                   — Auto-install deps + inject Leader briefing / core rules / team state
SubagentStart    → inject_subagent_context.py, send_event.py   — Inject sub-Agent OS rules (2-Action etc.)
SubagentStop     → send_event.py                 — Record sub-Agent lifecycle event
PreToolUse       → workflow_reminder.py, send_event.py
                   — Workflow tracking reminders + event forwarding
PostToolUse      → workflow_reminder.py, deep_review_link.py,
                   meeting_ecosystem_writeback.py, send_event.py
TaskCompleted    → cc_task_bridge.py             — Mirror finished CC tasks onto the OS wall (owned or dependency-linked ones only)
TeammateIdle     → send_event.py                 — CC's own teammate-idle signal, recorded alongside the OS liveness track (observation only, changes no status)
UserPromptSubmit → context_tracker.py            — Track context usage
                 → channel_unread.py             — Unread channel badge
                 → turn_end_guard.py             — Standby reminder (user-prompt mode)
SessionEnd       → send_event.py                 — Record session end event
Stop             → send_event.py                 — Record stop event
PermissionDenied → permission_denied_recovery.py — Permission-denied self-recovery
PreCompact       → pre_compact_save.py           — Freeze the OS-side battle state (in-flight agents / open tasks / pending decisions) into a checkpoint
PostCompact      → send_event.py                 — Confirm the compaction actually happened (a triggered compaction can still be cancelled)
WorktreeRemove   → send_event.py                 — An isolated worktree is gone
```

---

## Choose an Installation Path

For Claude Code's AI-assisted installation, tell Claude Code:
> "Read https://github.com/CronusL-1141/AI-company/blob/master/INSTALL.md and follow the instructions to install AI Team OS"

Claude Code can read the install guide and walk through its plugin setup. Codex users should follow the separate manual path below; the Claude installer is not a Codex installer.

---

> **Important**: Install AI Team OS to your system Python, not inside a project virtual environment.
> If installed in a venv, AI Team OS will only work in that specific project.
> Run `deactivate` first if a venv is currently active, then install.

---

## Quick Start

### Prerequisites

- Python >= 3.11; Python 3.12 is recommended for development and validation
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (`pip install uv`)
- Claude Code or Codex with MCP support; hook setup is host-specific
- Node.js >= 20 (Dashboard frontend, optional)

### Option A: Claude Code Plugin Install

```bash
# Install uv (Python package runner, required for MCP server)
pip install uv

# Add marketplace + install plugin
claude plugin marketplace add CronusL-1141/AI-company
claude plugin install ai-team-os

# Restart Claude Code after installation; the first launch configures dependencies

# Update to latest version anytime
claude plugin update ai-team-os@ai-team-os
```

> **Note**: Claude Code's first launch configures dependencies; duration depends on the local environment. Verify the loaded MCP tools and installed hooks rather than relying on startup time.

### Option B: Claude Code Source Install

```bash
# Step 1: Clone the repository
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company

# Step 2: Run the Claude Code installer (MCP + CC hooks + CC templates + API)
python3 install.py

# Step 3: Restart Claude Code — everything activates automatically
# API server starts automatically when MCP loads. No manual startup needed.
# Verify: run /mcp in CC and check that ai-team-os tools are mounted
```

> **Dependencies**: `greenlet` (needed by SQLAlchemy async on Apple Silicon) is bundled by default. `LangGraph` is an optional extra — only the CLI graph-execution path needs it: `pip install 'ai-team-os[langgraph]'`.

### Option C: Codex Manual Integration

Codex uses the shared OS backend with an independently installed adapter:

1. Reuse your existing OS service, or install the Python package from a source checkout using `python3 -m pip install -e .` with your system interpreter. Follow that interpreter's package-management policy; do not run the Claude installer merely to configure Codex.
2. In Codex's MCP settings, connect to the existing API's `/mcp/` endpoint, or configure stdio with command `python3` and arguments `-m aiteam.mcp.server`. Use an interpreter that imports the intended source checkout and the same OS data target.
3. Copy the Codex hook entry scripts you intend to enable into a Codex-owned directory. The observation entry `send_event_codex.py` requires matching `codex_observation.py`, `codex_completion_delivery.py` and `hook_core.py` from `plugin/harness/codex/hooks/`; copy them as one set. The prompt-time unread entry is `channel_unread_codex.py`.
4. Register the selected commands in Codex and review them in its own hook trust controls. Keep existing safety guards, Claude settings and Claude hook files unchanged.
5. Verify a real tool call through the installed hook, API record and Dashboard. A file copy, loaded MCP tool list or approved trust entry alone is not an end-to-end check.

Final acceptance of the installed, automatically triggered observation chain remains pending; the code-level checks do not establish deployment. Claude Code startup briefings, template injection and its fleet/watcher execution are not installed by the Codex path.

### Verify Installation

```bash
# Use the actual running API port; 8000 is the usual default.
curl http://localhost:8000/api/health
# Expected: {"status": "ok"}
```

In either host, run `context_resolve` for the current project, read a task memo, and check the same project in the Dashboard. For observation changes, compare a real native tool call and its completion with the persisted activity record, then verify a native member's name, parent team and state. Check the running API, Dashboard assets and installed hook files separately.

### First Words to Your Session

After configuring the selected host, make the shared records part of the working protocol:

> "Resolve this project in AI Team OS, read its task wall and relevant memos, and record progress and decisions there. Use your own native agent tools for authorized work."

Claude Code's installed `/os-help` command can introduce its workflow. In Codex, use native tool discovery or your separately configured OS help skill; a Claude slash command is not automatically a Codex command.

### Tool Loading Configuration (optional)

The MCP server can expose the full tool inventory or a smaller set for each client. Two environment variables are read at server startup; configuration changes take effect on the next start, not in an already running server.

**`AITEAM_TOOLSETS`** - pick which capability-domain groups register:

- unset or `all` - the full registered inventory (backward compatible)
- `default` - core groups only (`task,team,memory,infra,reports` = 29 tools, hard-capped at <=50)
- a comma list of group names, mixable with `default` for incremental loading, e.g. `AITEAM_TOOLSETS=default,ecosystem`
- unknown names are warned on stderr and ignored (a config typo never blocks server start)

**`AITEAM_READONLY=1`** - orthogonal overlay that strips every write tool (create/update/delete/apply/send/... plus `os_restart_api`) after registration, keeping only read tools. Handy for audit/observer sessions.

The 16 groups (default groups marked *):

| Group | Tools | Group | Tools | Group | Tools |
|---|---|---|---|---|---|
| task * | 8 | project | 6 | links | 3 |
| team * | 5 | agent | 7 | channels | 3 |
| memory * | 6 | meeting | 10 | task_analysis | 2 |
| infra * | 7 | briefing | 4 | watchdog | 1 |
| reports * | 3 | analytics | 2 | workflows | 3 |
| ecosystem | 42 | | | | |

```bash
# Example: lean core + ecosystem, read-only
AITEAM_TOOLSETS=default,ecosystem AITEAM_READONLY=1 python3 -m aiteam.mcp.server
```

### Remove a Host Integration

```bash
# Claude Code plugin:
claude plugin uninstall ai-team-os

# Preview the Claude Code source uninstaller before deciding what to remove:
python scripts/uninstall.py --dry-run
```

For Codex, remove only its own MCP/hook registrations and independently copied adapter files. Before removing shared OS data or running a full source uninstall, inspect the plan, back up the records and confirm no other host still uses the backend. Removing one host's integration is not permission to delete the shared database.

### Start the Dashboard (optional)

```bash
cd dashboard
npm install
npm run dev
# Visit http://localhost:5173
```

---

## Dashboard Screenshots

These screenshots illustrate the interface and may predate the observation updates in this version. Current behavior is described in the captions and release notes; a screenshot is not a live-runtime verification.

### Command Center
![Command Center](docs/screenshots/dashboard-home-en.png)

### Team Working — Live Activity Tracking
![Team Working](docs/screenshots/team-working-en.png)

### Task Board
![Task Board](docs/screenshots/task-board-en.png)

### Workflows — CC ultracode Run Observability
Persistent governance layer for CC ultracode Workflow runs — every run is auto-tracked as a team, surfacing stage progress plus per-agent token and tool-call telemetry.
![Workflows](docs/screenshots/workflows-en.png)

### Workflow Detail — Phase Swim Lane & Per-Agent Telemetry
Drill into a single run: a phase swim lane aligns every stage against one timeline, and a per-agent telemetry table breaks down tokens, tool calls, duration and state per stage — with a failed contract check surfaced in red.
![Workflow Detail](docs/screenshots/workflow-detail-en.png)

### Project Detail — Decision Timeline
![Decision Timeline](docs/screenshots/decision-timeline-en.png)

### Project Detail — Leader Context & Worktrees
The Dashboard shows fresh working Leaders with explicit host labels and available context observations, alongside Git worktrees and uncommitted changes. Missing context is left unknown; historical Leaders do not fill the current roster.
![Project Detail](docs/screenshots/project-detail-en.png)

### Agent Board — Live Agent Lanes
The Dashboard groups fresh working Leaders and members by team, preserves Codex's native member names and folds waiting or historical records separately. Roles, tasks and available context observations remain distinct.
![Agent Board](docs/screenshots/agent-lanes-en.png)

### Meeting Room
![Meeting Room](docs/screenshots/meeting-room-en.png)

### Ecosystem Research Platform
The ecosystem archive's initial listing — the full set of tracked open-source repositories with stars, primary language and topic tags, ready to open into per-repo research and integration.
![Ecosystem](docs/screenshots/ecosystem-list-desktop-en.png)

### Activity Analytics
![Analytics](docs/screenshots/analytics-en.png)

### Event Log
![Events](docs/screenshots/events-en.png)

### Claude Code Session Watcher - Historical Demonstration
![Auto-Wake Demo](docs/screenshots/auto-wake-demo.png)

---

## Waiting, Notifications and Continued Work

These are different operations, not one universal background scheduler:

- **Prompt-time notification**: an installed unread hook can show a message when the host starts the next prompted turn.
- **Explicit waiting**: either host can call `channel_wait` during an active turn. The call returns messages or a timeout and does not automatically acknowledge them.
- **Claude Code session watcher**: the existing CC-specific watcher can drive a live Claude session when enabled with the appropriate reader identity and permissions.
- **Codex after a turn ends**: this version does not provide mail-triggered automatic wakeup. Persistent inbox records remain available to a later turn.

Keep authorization and execution separate from notification. A pending task or new message does not grant permission to start unrelated work.

---

## Ecosystem Integration Recipes

AI Team OS can coordinate records and handoffs around other MCP servers instead of reimplementing their capabilities. Recipes describe integrations that you configure and authorize in the host where the work runs:

| Recipe | Integrates With | What You Get |
|--------|----------------|--------------|
| **GitHub** | `@modelcontextprotocol/github` | Auto PR creation, issue tracking, code review coordination |
| **Slack** | `@anthropics/slack-mcp` | Team notifications, decision escalation, status broadcasts |
| **Linear** | `linear-mcp-server` | Task sync, sprint tracking, bug triage automation |
| **Full-Stack Team** | GitHub + Slack + Linear | Complete development workflow with cross-tool orchestration |

Use `find_skill(level=2, category="integration")` to discover recipes, or see the full guide: [docs/ecosystem-recipes.md](docs/ecosystem-recipes.md)

---

## Shared OS, Native Hosts

- **Shared project services**: the same MCP tools, API, database and Dashboard hold tasks, memory, reports, messages and observations.
- **Independent adapters**: Claude Code and Codex keep their own scripts, registration, trust and native dispatch controls.
- **Evidence before inference**: bind observed identities and tool calls using native metadata; preserve unknowns instead of guessing from names or timestamps.
- **Project-scoped views**: task, event and Analytics queries use recorded project ownership; multiple teams can contribute without overwriting one another's totals.
- **Host-specific context delivery**: Claude Code's bootstrap and template hooks are its own integration. Codex can retrieve shared context without inheriting Claude configuration.

---

## FAQ

### Do I need both Claude Code and Codex?

No. Either can use the shared OS services. Connecting both adds cross-host handoffs; it does not require merging their configuration or credentials.

### Does the OS run Codex after I finish a turn?

No. `channel_wait` is an explicit pending call, and a prompt-time unread hint needs a new turn. This version does not add an idle-session Codex wake mechanism.

### Why are a model, duration or usage value unknown?

The OS only displays evidence it can attribute. Missing native metadata stays unknown, tool duration needs trustworthy timing, and full Codex token/cost attribution is unfinished. Historical calls without reliable IDs are not marked complete by guesswork.

### Why can new files exist while the Dashboard still shows old behavior?

The running API, built Dashboard, installed adapter files and host hook trust are separate layers. Update compatible pieces together and verify an actual event through the chain; reloading MCP alone does not replace them all.

---

## MCP Tools

<details>
<summary>Expand to see the tool map (116 MCP tools across 16 modules)</summary>

> The tables below are a curated selection — the full inventory lives in `src/aiteam/mcp/tools/` and is machine-counted by `scripts/check_readme_numbers.sh`.

### Team Management

| Tool | Description |
|------|-------------|
| `team_status` | Get team details and member status |
| `team_list` | List all teams |
| `team_briefing` | Get a full team panorama in one call (members + events + meetings + todos) |

### Agent Management

| Tool | Description |
|------|-------------|
| `agent_update_status` | Update recorded Agent status |
| `agent_list` | List team members |
| `agent_template_list` | Get available Agent template list |
| `agent_template_recommend` | Recommend the best Agent template based on task description |

### Task Management

| Tool | Description |
|------|-------------|
| `task_run` | Execute a task with full execution recording |
| `task_status` | Query task execution status |
| `task_create` | Create a new task (`auto_start` supported; `task_type` is accepted but retired — a no-op kept for backward compatibility) |
| `task_update` | Partial update of task fields with auto timestamps |
| `task_memo_add` | Add an execution memo to a task |
| `task_memo_read` | Read task history memos |
| `task_list_project` | List all tasks under a project |

### Meeting System

| Tool | Description |
|------|-------------|
| `meeting_create` | Create a structured meeting (8 templates, keyword auto-select) |
| `meeting_send_message` | Send a meeting message |
| `meeting_read_messages` | Read meeting records |
| `meeting_conclude` | Summarize meeting conclusions |
| `meeting_template_list` | Get available meeting template list |
| `meeting_list` | List all meetings |
| `meeting_update` | Update meeting metadata |

### Channel Communication

| Tool | Description |
|------|-------------|
| `channel_send` | Send a message to a channel (team:/project:/global) with @mention support |
| `channel_read` | Read messages from a channel |
| `channel_wait` | Replay a scoped inbox, then wait for a peer message over WebSocket; read-only, no automatic ACK |
| `channel_mentions` | Get unread @mentions for an agent |

`channel_wait` keeps one MCP call pending: it subscribes before replaying the
project/reader/sender-scoped inbox, then returns persisted message bodies on an
event. It does not schedule model turns or start a daemon. A completed Desktop
turn cannot be restarted by this tool. The default wait is 45 seconds (maximum
300). `io_timeout_seconds` independently budgets connection, subscription ACK,
and each HTTP read (default 10 seconds, maximum 60). Set the client request
timeout above `timeout_seconds + 4 * io_timeout_seconds + 5`.
A client must send MCP cancellation or close the session to cancel
the server-side wait; a local timeout or coroutine cancellation does not notify
a server that the client has stopped waiting.
Disconnects return an error with a validated `resume_cursor`, not an empty inbox.
Use `since` for the initial
history boundary, then resume with the last processed `next_cursor`. This scoped
cursor follows SQLite insertion order, so late commits are not skipped because
of an older creation timestamp. A deleted or reused cursor anchor returns an
explicit error rather than silently skipping messages. Waiting never acknowledges
messages automatically; the legacy badge timestamp ACK is separate from this
delivery cursor. Retrying an unprocessed page can repeat messages; deduplicate by ID.

Successful calls include `delivery_source`: `replay` for the initial inbox read,
`event` for a read triggered by a candidate WebSocket event, or `timeout_read` for
the final read after the wait expires. The final read can still return messages;
an empty final read returns `status=timeout`. This field identifies the executed
branch, not whether every returned message had a corresponding push frame.
Error responses do not claim a delivery source.



### Debate System

| Tool | Description |
|------|-------------|
| `debate_start` | Start a structured 4-round debate (Advocate→Critic→Response→Judge) |
| `debate_code_review` | Start a code review debate session |



### Intelligence & Analysis

| Tool | Description |
|------|-------------|
| `failure_analysis` | Failure Alchemy — analyze root causes, generate antibody/vaccine/catalyst |
| `decision_log` | Log a decision to the cockpit timeline |
| `context_resolve` | Resolve current context and retrieve relevant background information |

### Memory System

| Tool | Description |
|------|-------------|
| `memory_search` | Search team memory — recency-window recall within scope + pure-Python BM25 rerank (Chinese bigram, no embeddings) |
| `memory_add` | Write a direction-layer memory (preference/correction/design intent, 4 kinds; bucket quotas 1200/1500/300 chars, <=400 chars per entry, supersedes swap) |
| `memory_invalidate` | Explicitly invalidate a direction-layer memory (by id or unique substring; invalidate, never delete — auditable) |
| `memory_list` | List shared direction-layer entries, optionally filtered by kind |
| `memory_reconcile_candidates` | On-demand reconcile coarse pass (zero-LLM): BM25-paired candidate groups + direction-layer inventory + promotion material + operation guide |
| `memory_reconcile_apply` | Apply agent-confirmed reconcile operations (merge / invalidate / score / promote); idempotent, size guardrails enforced on promote |

### Knowledge Layer (v1.8.0)

| Tool | Description |
|------|-------------|
| `unified_search` | Three-arm RRF search across memos / reports / tasks — BM25 full-text + knowledge-graph fanout + exact ID match |
| `link_query` | Query the cross-domain reference graph by node (what references this / what does this reference) |
| `link_trace` | Trace a reference chain from any OS ID (wf_id / commit / task uuid) with evidence snippets |

### Claude Code Model Governance (v1.8.1)

| Tool | Description |
|------|-------------|
| `model_config_get` | Read observed Claude Code model names and its startup default |
| `model_config_set` | Set Claude Code's startup default with protected writes to its settings; does not control Codex |

### Trust & Reliability

| Tool | Description |
|------|-------------|
| `verify_completion` | Verify task completion (status + memo check, anti-hallucination) |

### Analytics

| Tool | Description |
|------|-------------|
| `task_execution_trace` | Get unified execution timeline for a task |
| `diagnose_task_failure` | Auto-diagnose why a task failed |

### Briefing System

| Tool | Description |
|------|-------------|
| `briefing_add` | Add a decision item for user review |
| `briefing_list` | List pending briefing items |
| `briefing_resolve` | Resolve a briefing item with a decision |
| `briefing_dismiss` | Dismiss a briefing item |

### Reports (Database-backed)

| Tool | Description |
|------|-------------|
| `report_save` | Save a report to database with project isolation (research/design/analysis/meeting-minutes) |
| `report_list` | List reports with filtering by project, type, author, topic |
| `report_read` | Read a report by ID |

### Ecosystem Research (42 tools)

The single largest tool family — the full research funnel from scan to integration:

| Tool | Description |
|------|-------------|
| `ecosystem_scan` / `ecosystem_scan_periodic` | GitHub scan by project profile (stars / topics), one-off or periodic |
| `ecosystem_search` / `ecosystem_search_by_capability` | Search the archived research knowledge base |
| `ecosystem_deep_review_request` / `..._request_batch` | Dispatch architecture deep-review agents, single or batched |
| `ecosystem_tag_list` / `..._apply_batch` / `..._dispatch_llm` | Tag rule engine + LLM-assisted tagging |
| `ecosystem_summary_weekly` / `..._top_n` / `..._health` | Weekly digests, top-N and knowledge-base health reports |
| `ecosystem_diff_period` / `ecosystem_index_diff_latest` | Period-over-period diffs + index reconciliation |
| `ecosystem_mark_as_reference` / `ecosystem_start_integration` | Stage-3 marking: keep as reference, or kick off an integration task |
| … | Full family of 42 tools: see `src/aiteam/mcp/tools/ecosystem.py` |


### Prompt Registry

| Tool | Description |
|------|-------------|
| `prompt_effectiveness` | View template effectiveness metrics |

### Project Management

| Tool | Description |
|------|-------------|
| `project_create` | Create a project |
| `project_list` | List all projects |
| `project_update` | Update project settings |
| `project_delete` | Delete a project |
| `project_summary` | Get a quick project status summary |

### System Operations

| Tool | Description |
|------|-------------|
| `os_health_check` | Health check with on-demand reconciliation of the verified local API PID |
| `os_restart_api` | Restart safely; `dry_run=true` previews imports and `source_root` selects the checkout |
| `event_list` | View the system event stream |
| `agent_activity_query` | Query agent activity history and statistics |
| `find_skill` | 3-layer progressive skill discovery (quick recommend / category browse / full detail) |
| `team_close` | Close a team and cascade-close its active meetings |
| `team_delete` | Delete a team |

Development restarts can first use `os_restart_api(source_root="/absolute/repo", dry_run=true)`
to verify imports without stopping the service. An actual restart preserves the database target
when changing the working directory. Health checks reconcile only the managed port and a verified
process identity; they do not adopt arbitrary listeners. This is on-demand repair, not a daemon.
`psutil` is an explicit runtime dependency. If it is unavailable on POSIX, read-only process
checks can still recognize an existing API and a confirmed-dead lock owner; uncertain identities
do not authorize killing a process or launching a duplicate service. Health checks use the
current port file or explicit API URL, including non-default ports.

Event delivery isolates slow WebSocket clients with bounded concurrent sends. Dashboard events
coalesce query refreshes over 200 ms, preserving in-flight requests until a 30-second refresh
deadline. Only the captured request is cancelled at that deadline; a newer request on the same
query key is preserved. Later events can retry without a permanently stuck prefix. Ordinary API
traffic uses at most four of the five SQLite admission slots, leaving one available for hook
events; the total limit remains five.

</details>

---

## Agent Template Library

25 professional role templates are shipped in `plugin/agents/`, with a shared catalog and recommendation tools. Claude Code can install them as native agent definitions, including global copies in `~/.claude/agents/`. Codex can use the role guidance while keeping native dispatch, naming and permissions; CC template frontmatter is not a Codex installation format.

### Engineering (13 templates)

| Template | Role | Use Case |
|----------|------|----------|
| `engineering-software-architect` | Software Architect | System design, architecture review |
| `engineering-backend-architect` | Backend Architect | API design, service architecture |
| `engineering-frontend-developer` | Frontend Developer | UI implementation, interaction development |
| `engineering-ai-engineer` | AI Engineer | Model integration, LLM applications |
| `engineering-mcp-builder` | MCP Builder | MCP tool development |
| `engineering-code-reviewer` | Code Reviewer | Code quality review, PR review |
| `engineering-database-optimizer` | Database Optimizer | Query optimization, schema design |
| `engineering-devops-automator` | DevOps Automation Engineer | CI/CD, infrastructure |
| `engineering-sre` | Site Reliability Engineer | Observability, incident response |
| `engineering-security-engineer` | Security Engineer | Security review, vulnerability analysis |
| `engineering-rapid-prototyper` | Rapid Prototyper | MVP validation, fast iteration |
| `engineering-mobile-developer` | Mobile Developer | iOS/Android development |
| `engineering-git-workflow-master` | Git Workflow Master | Branch strategy, code collaboration |

### Testing (4 templates)

| Template | Role | Use Case |
|----------|------|----------|
| `testing-qa-engineer` | QA Engineer | Test strategy, quality assurance |
| `testing-api-tester` | API Test Specialist | Interface testing, contract testing |
| `testing-bug-fixer` | Bug Fix Specialist | Defect analysis, root cause investigation |
| `testing-performance-benchmarker` | Performance Benchmarker | Performance analysis, load testing |

### Research & Support (3 templates)

| Template | Role | Use Case |
|----------|------|----------|
| `specialized-workflow-architect` | Workflow Architect | Process design, automation orchestration |
| `support-technical-writer` | Technical Writer | API docs, user guides |
| `support-meeting-facilitator` | Meeting Facilitator | Structured discussion, decision facilitation |

### Management (2 templates)

| Template | Role | Use Case |
|----------|------|----------|
| `management-tech-lead` | Tech Lead | Technical decisions, team coordination |
| `management-project-manager` | Project Manager | Schedule management, risk tracking |

### Debate Roles (2 templates)

| Template | Role | Use Case |
|----------|------|----------|
| `debate-advocate` | Debate Advocate | Propose and defend solutions in structured debates |
| `debate-critic` | Debate Critic | Challenge proposals and find weaknesses |

### Utility (1 template)

| Template | Role | Use Case |
|----------|------|----------|
| `team-member` | Generic Team Member | Default role for general-purpose tasks |

---

## Roadmap

### Shipped and Historical Milestones

- [x] Core Task Wall + Watchdog + Review (the loop state machine was retired in v1.10.x; scoring and the wall live on in `loop/task_wall_engine.py`)
- [x] Failure Alchemy (Antibody + Vaccine + Catalyst)
- [x] Decision Cockpit (Event stream + Timeline + Intent inspection)
- [x] Event-driven Task Wall 2.0 (Real-time push + Intelligent matching)
- [x] Living Team Memory (Knowledge query + Experience sharing)
- [x] What-If Analyzer (Multi-option comparison)
- [x] 8 structured meeting templates with keyword auto-select
- [x] 25 professional Agent templates (23 base + 2 debate roles) with recommendation engine
- [x] 4-layer defense rule system (48+ rules) + behavioral enforcement
- [x] Dashboard Command Center (React 19) — 23 pages including the `/workflows` swimlane, Workflow detail, the Ecosystem suite, `/usage` token attribution, and Settings with model governance
- [x] 116 MCP tools across 16 modules
- [x] CC Workflow observability layer (auto-tracking + /workflows dashboard + workflow_list / workflow_get / workflow_reconcile)
- [x] Knowledge layer — zero-LLM reference graph + unified 3-arm RRF search (v1.8.0)
- [x] Claude Code model governance - transcript-based discovery and startup defaults (v1.8.1)
- [x] Machine-checked red-line invariants + one-command preflight (`scripts/preflight.sh`)
- [x] AWARE loop memory system
- [x] find_skill 3-layer progressive discovery
- [x] task_update API for programmatic task management
- [x] Workflow pipeline orchestration (7 templates + auto phase progression) — fully removed in v1.10.x, superseded by CC Workflow observability (`pipeline_stage_history` stays readable)
- [x] Automated unit and frontend regression suites maintained in CI
- [x] Prompt Registry (version tracking retired in v1.10.3 — nothing ever called `/track`, so every version column rendered "-"; effectiveness metrics live on, sourced from real agent activity)
- [x] BM25 as the main memory-retrieval chain (pure-Python Okapi BM25, Chinese bigram, recency-window recall + rerank)
- [x] Event log enhancement (entity_id / entity_type / state_snapshot fields)
- [x] CC Plugin Marketplace submission
- [x] File lock / workspace isolation (acquire/release/check/list + TTL=300s) — retired in v1.10.3; the lock file was empty in every real run, and hook-side edit-conflict warnings replaced it
- [x] Channel communication system (team:/project:/global + @mention)
- [x] Execution pattern memory (success/failure recording + BM25 retrieval) — retired in v1.10.3; the store never held a row, so the injected section was permanently blank
- [x] Guardrails L1 (7 dangerous patterns + PII warnings)
- [x] Alembic database migration system
- [x] Debate mode (4-round structured debate + code review)
- [x] Agent trust scoring system (auto-adjust on task success/failure) — scoring chain retired in v1.10.3 (no caller ever existed); the `trust_score` column stays and `auto_assign` still weights it
- [x] Tool tier draft (informational CORE/ADVANCED grouping — groundwork for context budgeting)
- [x] Agent Watchdog patrol (BUSY-timeout / stuck-task detection; the file-based heartbeat was retired in v1.10.x — CC subagents are one-shot and never polled)
- [x] SRE error budget model (GREEN/YELLOW/ORANGE/RED 4-level response) — retired in v1.10.3; its data directory sat empty for its entire lifetime
- [x] Completion verification protocol (anti-hallucination completion check)
- [x] Ecosystem integration recipes (GitHub/Slack/Linear/Full-stack presets, served by `find_skill`)
- [x] Session bootstrap rule compression (23 → 5 core rules, 60% context reduction)
- [x] Atomic API startup lock (multi-session port conflict prevention)
- [x] Auto port discovery (API finds available port, writes to `api_port.txt`)
- [x] MCP HTTP Streamable endpoint (`/mcp/` on FastAPI)
- [x] PyPI release - stopped at 1.3.4 (2026-04) and deprecated; the wheel ships without `plugin/` and config resources, so install via plugin or source instead
- [x] INSTALL.md CC-assisted installation guide

### In Progress / Planned

- [ ] Final installed-hook acceptance of the Codex/Dashboard observation chain
- [ ] Full Codex token and cost attribution

- [ ] Multi-tenant isolation
- [ ] Production validation and performance optimization
- [x] Claude Code Plugin Marketplace listing
- [ ] Full integration test suite
- [ ] Documentation site (Docusaurus)
- [ ] Video tutorial series

---

## Project Structure

```
ai-team-os/
├── src/aiteam/
│   ├── api/           — FastAPI REST endpoints (211 routes)
│   ├── mcp/
│   │   ├── server.py  — MCP server entry point
│   │   └── tools/     — 16 tool modules (116 MCP tools)
│   │       ├── agent.py, analytics.py, briefing.py, channels.py,
│   │       ├── ecosystem.py, infra.py, links.py, meeting.py,
│   │       ├── memory.py, project.py, reports.py, task.py,
│   │       ├── task_analysis.py, team.py, watchdog.py, workflows.py
│   │       └── __init__.py  — Toolset registration entry
│   ├── loop/          — Task wall engine + watchdog + failure alchemy
│   ├── meeting/       — Meeting system
│   ├── memory/        — Team memory
│   ├── orchestrator/  — Team orchestrator
│   ├── storage/       — Storage layer (SQLite, WAL journaling)
│   ├── templates/     — Agent template base classes
│   ├── hooks/         — CC Hook scripts (15 lifecycle events)
│   └── types.py       — Shared type definitions
├── plugin/
│   ├── agents/        - 25 Claude Code Agent templates (.md)
│   ├── harness/codex/ - Independent Codex adapter, hook manifest and helpers
│   └── .claude-plugin/ - Claude Code plugin manifest
├── dashboard/         — React 19 frontend (23 pages)
├── scripts/           — preflight + machine-checked invariants (incl. README number check)
├── docs/              — Design documents + ecosystem recipes
├── tests/             - Unit, integration and end-to-end checks
├── install.py         - Claude Code source installer
└── pyproject.toml
```

---

## Contributing

Contributions are welcome! We especially appreciate:

- **New Agent templates**: If you have prompt designs for specialized roles, PRs are welcome
- **Meeting template extensions**: New structured discussion patterns
- **Bug fixes**: Open an Issue or submit a PR directly
- **Documentation improvements**: Found a discrepancy between docs and code? Please correct it

```bash
# Set up source dependencies without changing either host's configuration
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company
python3 -m pip install -e ".[dev]"
npm --prefix dashboard ci

# Local preflight: lint, frontend regression tests, unit tests and invariants
bash scripts/preflight.sh

# CI also checks TypeScript; preflight does not run this command
(cd dashboard && npx --no-install tsc -b --noEmit)
```

Before submitting a PR, run the full preflight and the separate TypeScript check above. Preflight runs ruff, ESLint, frontend regression tests, the unit test suite and the invariants in `scripts/check_invariants.sh`; missing lint/frontend dependencies can cause skips, so a successful exit alone does not prove every check ran. Review the output, and do not use `--fast` for release acceptance.

Release preparation also needs targeted integration/end-to-end checks, Dashboard builds and a complete comparison of `dashboard/dist` with `plugin/dashboard-dist`. I3 compares JavaScript filenames only, not every asset's bytes. Keep both READMEs and both CHANGELOGs in sync, inspect distribution and privacy boundaries, and verify the installed hooks and running API/UI separately. Static manifest/trust checks do not prove the host loaded or executed a hook.

---

## License

MIT License — see [LICENSE](LICENSE)

---

<div align="center">

**AI Team OS** - Shared context and accountable work for native coding agents.

*Built with Claude Code and Codex · Connected through MCP*

[Docs](docs/) · [Issues](https://github.com/CronusL-1141/AI-company/issues) · [Discussions](https://github.com/CronusL-1141/AI-company/discussions)

</div>

<!-- README numbers are machine-checked against the code: scripts/check_readme_numbers.sh (invariant I6 in scripts/check_invariants.sh). Drift fails CI. -->
