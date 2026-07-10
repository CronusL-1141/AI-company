[English](README.md) | [中文](README.zh-CN.md)

# AI Team OS

<!-- Logo placeholder -->
<!-- ![AI Team OS Logo](docs/assets/logo.png) -->

### Your AI coding tool stops when you stop prompting. Ours doesn't.

> ⚡ **v1.8.1** — Model Governance + unified knowledge search + Workflow observability swimlanes. The public repo now ships the complete edition.

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Protocol-orange)](https://modelcontextprotocol.io)
[![Stars](https://img.shields.io/github/stars/CronusL-1141/AI-company?style=flat)](https://github.com/CronusL-1141/AI-company)

**160** MCP tools · **217** REST endpoints · **22** dashboard pages · **1,670+** tests · **25** agent templates · **46** ecosystem research tools · **5** machine-checked invariants

---

AI Team OS turns Claude Code into a **self-driving AI company**.
You're the Chairman. AI is the CEO. Set the vision — the system executes, learns, and evolves autonomously.

---

## The Problem With Every Other AI Tool

Every AI coding assistant works the same way: you prompt, it responds, it stops. The moment you step away, work stops. You come back to a blank prompt.

AI Team OS works differently.

You walk away at night. The next morning you open your laptop and find:
- The CEO checked the task wall, picked up the next highest-priority item, and shipped it
- When it hit a blocker that needed your approval, it parked that thread and switched to a parallel workstream
- R&D agents scanned three competitor frameworks and found a technique worth adopting
- A brainstorming meeting was organized, 5 agents debated 4 proposals, and the best one was put on the task wall

You didn't prompt any of that. The system just ran.

---

## How It Works

**You're the Chairman. The AI Leader is the CEO.**

The CEO doesn't wait for instructions. It checks the task wall, picks the highest-priority item, assigns the right specialist Agent, and drives execution. When blocked, it switches workstreams. When all planned work is done, R&D agents activate — scanning for new technologies, organizing brainstorming meetings, and feeding improvements back into the system.

Every failure makes the system smarter. "Failure Alchemy" extracts defensive rules, generates training cases for future Agents, and submits improvement proposals — the system develops antibodies against its own mistakes.

---

## Core Capabilities

### 1. Autonomous Operation

The CEO never idles. It continuously advances work based on task wall priorities:

- Checks the task wall for the next highest-priority item when a task completes
- When blocked on something requiring your approval, parks that thread and switches to parallel workstreams
- Batches all strategic questions and reports them when you return — no interruptions for tactical decisions
- Deadlock detection: if the loop stalls, it surfaces the blocker rather than spinning

And it doesn't just execute — it evolves:

- **R&D cycle**: research agents scan competitors, new frameworks, and community tools; findings go to brainstorming meetings where agents challenge each other; conclusions become implementation plans on the task wall
- **Failure Alchemy**: every failed task triggers root-cause extraction and three outputs — *Antibody* (failure stored in team memory to prevent repeats), *Vaccine* (high-frequency failure patterns become pre-task warnings), *Catalyst* (analysis injected into future Agent system prompts)

### 2. CC Workflow Observability (v1.7.0)

The OS does not intercept CC's built-in **ultracode/Workflow** — it becomes its persistent governance layer. Every Workflow run is automatically tracked into the OS, with no manual `team_create`:

- **Auto-tracking**: a hook turns each Workflow run into an OS "team" (`workflow-<wf_id>`) the moment it starts
- **Dashboard `/workflows`**: a live feed of run cards, a phase swimlane timeline, and per-agent telemetry — tokens / duration / status / tool-call counts, advancing live via incremental journal tailing while a run executes
- **Calibrated stall detection**: the stall threshold was calibrated on 3,378 real agent intervals (p99 = 77.6s, longest healthy silence 173.8s) and set at 5.2× the worst healthy case — it flags late rather than crying wolf
- **Project-detail integration**: workflow team rows carry an inline run summary (status / agent count / duration / finish time) plus a "view swimlane" deep link; members display semantic phase labels (e.g. `audit:sourceA`) instead of ids
- **Leader auto-detection**: a project's Leader session / model / liveness is probed directly from the `~/.claude/projects/` file truth by the backend — zero registration dependency, `/model` switches surface in real time
- **MCP tools**: `workflow_list` (browse runs), `workflow_get` (full archive + per-agent rows), `workflow_reconcile` (repair from on-disk snapshots after the OS was offline)
- **Self-healing ingestion**: hook receipt anchors + on-disk snapshot reconciliation + a reaper backstop close offline gaps automatically — finished runs on disk are ingested idempotently; cross-project attribution matches the on-disk path slug against registered projects

> **Legacy**: the OS's own pipeline orchestration (7 templates) was retired in v1.7.0 — superseded by CC Workflow + this observability layer; existing pipeline data stays readable.
> Retirement phases are documented in the [CHANGELOG](CHANGELOG.md).

### 3. Knowledge Layer — Reference Graph + Unified Search (v1.8.0)

Everything the OS records — task memos, reports, tasks — becomes recallable knowledge:

- **Reference graph (P1a)**: a zero-LLM regex extractor mines OS-native ID references (wf_id / commit hash / task uuid / `[[memory]]`) out of memos and reports into an append-only `knowledge_links` table — the graph is a derived view, rebuildable from source text at any time
- **Unified search (P1b)**: `/api/search` fuses three arms via RRF — BM25 full-text (Chinese bigram native), knowledge-graph fanout (an ID query pulls in everything linked to it), and exact ID-prefix / title match
- **Global search box** in the Dashboard header, plus MCP tools `unified_search` / `link_query` / `link_trace` — recall past work by natural language ("how was the attribution fix done"), a `wf_` id, or a commit hash

> **Why zero-LLM?** The graph is a derived view: plain regexes extract the IDs, the whole graph can be rebuilt from source text at any time, and both extraction and retrieval cost zero tokens. Your recall pipeline never touches your model budget.

### 4. File Truth as Source of Truth

Most multi-agent stacks trust agents to register themselves and self-report their status. AI Team OS treats self-reports as claims and files as facts — three subsystems already run on this philosophy:

- **Leader probing**: a project's Leader session, model, and liveness are read straight from `~/.claude/projects/` — transcript mtime is liveness, the model name in the transcript tail is the model. We don't ask an agent which model it runs — what's read out of the transcript is what's true.
- **Model discovery**: "available models" = every model that has actually appeared in your CC transcripts. Zero API dependency, zero hardcoded list — a hardcoded list will never contain your third-party gateway model; a transcript scan can't miss it.
- **Workflow telemetry**: on-disk run files are the full telemetry truth; the OS's projection tables are rebuildable caches of immutable files. Attribution iron law: a run belongs to a project only when its on-disk path slug exactly matches the registered project root — never guessed.

### 5. Model Governance (v1.8.1)

Know which models you can actually launch — and control what your sessions start on:

- **Auto-discovery of genuinely available models**: scans every CC transcript on your machine in about a second (60s cache) — including third-party gateway models that no hardcoded list would ever ship
- **One-click global default startup model**: written to `~/.claude/settings.json` under triple write protection — touches only the `model` key, keeps a `.bak-aiteam` backup, writes atomically, refuses corrupted files
- **Zero coercion**: soft reminders only, never a block — and CC Workflow runs are fully exempt

Surfaces: REST `/api/models/{available,default}` · MCP `model_config_get` / `model_config_set` · the Model Governance card in Dashboard Settings.

### 6. Team Collaboration

Not a single Agent. A structured organization:

- **25 professional Agent templates** (23 base + 2 debate roles) with recommendation engine — Engineering, Testing, Research, Management — ready out of the box
- **8 structured meeting templates** with keyword-based auto-select, built on Six Thinking Hats, DACI, and Design Sprint methodologies
- **Department grouping** — Engineering / QA / Research with cross-team coordination
- **Channel communication**: `team:` / `project:` / `global` channels with `@mention` support
- **Debate mode**: 4-round structured debate (Advocate→Critic→Response→Judge) via `debate_start` / `debate_code_review`
- **Git automation**: `git_auto_commit` / `git_create_pr` / `git_status_check` for streamlined version control
- **Execution pattern memory**: success/failure pattern recording + BM25 retrieval + subagent context injection
- Every meeting produces actionable conclusions. "We discussed but didn't decide" is not an outcome.

### 7. Full Transparency

Nothing is a black box:

- **Decision Cockpit**: event stream + decision timeline + intent inspection — every decision has a traceable record
- **Activity Tracking**: real-time status of every Agent and what it's working on
- **What-If Analyzer**: compare multiple approaches before committing, with path simulation and recommendations

### 8. Safety & Behavioral Enforcement

Built-in guardrails so the system can run unsupervised without surprises:

- **Guardrails L1**: 7 dangerous pattern detections + PII warnings + `InputGuardrailMiddleware`
- **Local agent blocking**: all non-readonly agents must declare `team_name`/`name` — prevents rogue background agents
- **S1 safety rules**: regex-based scan catches destructive commands (rm -rf, force push, hardcoded secrets) including uppercase flags and heredoc patterns
- **4-layer defense rule system**: 48+ rules covering workflow, delegation, session, and safety layers
- **File lock / workspace isolation**: acquire/release/check/list + TTL=300s + hook warnings to prevent concurrent edits
- **Agent trust scoring**: trust_score (0-1) auto-adjusts on task success/failure, weighted into auto_assign
- **Agent Watchdog heartbeat**: `agent_heartbeat` / `watchdog_check` with 5-min TTL — detects stalled or crashed agents automatically
- **Self-patrol**: watchdog lease patrol + reaper reconciliation backstop + identity verification before any kill — the OS keeps eyes on itself, not just on your agents
- **SRE error budget model**: GREEN/YELLOW/ORANGE/RED 4-level response with sliding window (20 tasks), `error_budget_status` / `error_budget_update` tools
- **Completion verification**: `verify_completion` checks task status + memo existence — prevents hallucinated "done" reports
- **Ecosystem integration recipes**: 4 preset recipes (GitHub / Slack / Linear / Full-stack team) via `ecosystem_recipes()` tool
- **`find_skill` 3-layer progressive discovery**: quick recommend → category browse → full detail, reducing tool-call overhead

### 9. Zero Extra Cost

Runs entirely within your existing Claude Code subscription:

- No external API calls, no extra token spend
- MCP tools, hooks, and Agent templates are all local
- The knowledge layer is zero-LLM by design — graph extraction and search cost zero tokens (see "Why zero-LLM" above)
- 100% utilization of your CC plan

### 10. Ecosystem Research Platform — 46 tools

A project-isolated **knowledge base** that accumulates research findings over time. Each repo progresses through 4 stages (a progressive funnel, since v1.5.0), with token-efficient triggers and append-only history:

- **Stage 0 — Auto shallow-summary on archive**: newly-archived repos automatically get a 200-400 char `ai-engineer` summary (core function / positioning / advantages). 8-class failure handling with **self-learning** (3+ same-class fails → `pattern_record`, future agents read lessons via `pattern_search`). Worker auto-revives deleted/private repos when GitHub returns 200 again.
- **Stage 1 — On-demand architecture analysis**: user picks research direction ("memory_system") → batch-dispatch `backend-architect` agents to read architecture key files
- **Stage 2 — Multi-perspective debate**: triggers existing `debate_start` (NOT a built-in debate engine — **reuses meeting system**). Meeting → ecosystem reverse-writeback hook reminds Leader to record verdicts back to deep_review
- **Stage 3 — Reference / Integrate marking**: `mark_as_reference` adds tag for future quick recall (avoid re-deep-scanning); `start_integration` triggers existing `task_create` for actual implementation
- **Project-customizable thresholds**: each project sets `min_stars` / `top_n` / `refresh_interval_days` / `focus_topics`. AI Team OS default: stars ≥ 5K, top 200, focus on claude-code / mcp / agent-framework
- **Active vs Full dual-view**: data is **append-only forever**. Stars-falling repos kept (just `is_active=False`); stars climbing back auto-promotes + re-queues Stage 0
- **Dashboard `/ecosystem`**: list with stage badges + research timeline + project filter dropdown (view a project's ecosystem) + candidate-filter page (`/ecosystem/research`) + per-project settings tab
- **Ecosystem-scale tooling**: 46 MCP tools + 67 REST endpoints + SQLite append-only history snapshots — the single largest tool family in the OS

---

## It Built Itself

AI Team OS manages its own development — and since v1.7.0, it can prove it with its own telemetry:

- Every feature line from v1.7.0 to v1.8.1 — the observability layer, the knowledge layer, model governance — shipped through CC Workflow runs that the OS tracked itself. Open `/workflows` and replay how the system built its own features, swimlane by swimlane.
- Competitive research across CrewAI, AutoGen, LangGraph, and Devin feeds the roadmap through multi-agent brainstorming meetings — the minutes live in the OS's own report store.
- It learns from its own incidents, too: every machine-checked invariant in `scripts/check_invariants.sh` was distilled from a real accident in this repo's history.

The system that builds your projects... built itself. With receipts.

---

## How It Compares

| Dimension | AI Team OS | CrewAI | AutoGen | LangGraph | Devin |
|-----------|-----------|--------|---------|-----------|-------|
| **Category** | CC Enhancement OS | Standalone Framework | Standalone Framework | Workflow Engine | Standalone AI Engineer |
| **Integration** | MCP Protocol into CC | Independent Python | Independent Python | Independent Python | SaaS Product |
| **Autonomous Operation** | Continuous loop, never idles | Task-by-task | Task-by-task | Workflow-driven | Limited |
| **Meeting System** | 8 structured templates with auto-select | None | Limited | None | None |
| **Failure Learning** | Failure Alchemy (Antibody/Vaccine/Catalyst) | None | None | None | Limited |
| **Decision Transparency** | Decision Cockpit + Timeline | None | Limited | Limited | Black box |
| **Workflow Observability** | Swimlane timeline + per-agent telemetry + offline reconcile over CC Workflow | None | None | Graph state only | None |
| **State Source** | File truth — transcripts / journals read directly | Agent self-report | Agent self-report | In-process state | Black box |
| **Rule System** | 4-layer defense (48+ rules) + behavioral enforcement | Limited | Limited | None | Limited |
| **Agent Templates** | 25 ready-to-use + recommendation engine | Built-in roles | Built-in roles | None | None |
| **Dashboard** | React 19 visualization | Commercial tier | None | None | Yes |
| **Open Source** | MIT | Apache 2.0 | MIT | MIT | No |
| **Claude Code Native** | Yes, deep integration | No | No | No | No |
| **Extra Cost** | $0 (CC subscription only) | API costs | API costs | API costs | $500+/mo |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     User (Chairman)                              │
│                         │                                       │
│                         ▼                                       │
│                   Leader (CEO)                                   │
│            ┌────────────┼────────────┐                          │
│            ▼            ▼            ▼                          │
│       Agent Templates  Task Wall  Meeting System                 │
│      (25 roles)       Loop Engine  (8 templates)                 │
│            │            │            │                          │
│            └────────────┼────────────┘                          │
│                         ▼                                       │
│              ┌──────────────────────┐                           │
│              │   OS Enhancement Layer│                           │
│              │  ┌──────────────┐    │                           │
│              │  │  MCP Server  │    │                           │
│              │  │ (160 tools)  │    │                           │
│              │  └──────┬───────┘    │                           │
│              │         │            │                           │
│              │  ┌──────▼───────┐    │                           │
│              │  │  FastAPI     │    │                           │
│              │  │  REST API    │    │                           │
│              │  └──────┬───────┘    │                           │
│              │         │            │                           │
│              │  ┌──────▼───────┐    │                           │
│              │  │  Dashboard   │    │                           │
│              │  │ (React 19)   │    │                           │
│              │  └──────────────┘    │                           │
│              └──────────────────────┘                           │
│                         │                                       │
│              ┌──────────▼──────────┐                            │
│              │  Storage (SQLite)   │                            │
│              │  + WAL journaling   │                            │
│              │  + Memory System    │                            │
│              └─────────────────────┘                            │
└─────────────────────────────────────────────────────────────────┘
```

### Five-Layer Technical Architecture

```
Layer 5: Web Dashboard    — React 19 + TypeScript + Shadcn UI (22 pages)
Layer 4: CLI + REST API   — Typer + FastAPI
Layer 3: Team Orchestrator — LangGraph StateGraph (optional extra — CLI graph execution only)
Layer 2: Memory Manager   — SQLite-backed store + pure-Python BM25 retrieval
Layer 1: Storage          — SQLite (WAL journaling) · PostgreSQL support on the roadmap
```

### Hook System (14 scripts across 12 Lifecycle Events — The Bridge Between CC and OS)

```
SessionStart     → auto_install.py, session_bootstrap.py, send_event.py
                   — Auto-install deps + inject Leader briefing / core rules / team state
SubagentStart    → inject_subagent_context.py, send_event.py   — Inject sub-Agent OS rules (2-Action etc.)
SubagentStop     → send_event.py                 — Record sub-Agent lifecycle event
PreToolUse       → workflow_reminder.py, pipeline_gate.py, send_event.py
                   — Workflow tracking reminders + pipeline gate + event forwarding
PostToolUse      → workflow_reminder.py, pipeline_gate.py, deep_review_link.py,
                   meeting_ecosystem_writeback.py, send_event.py
TaskCreated      → cc_task_bridge.py             — Bridge CC-native tasks onto the OS task wall
TaskCompleted    → task_completed_gate.py        — Completion gate verification
UserPromptSubmit → context_tracker.py, autopilot_auto_stop.py  — Track context usage + autopilot auto-stop
SessionEnd       → send_event.py                 — Record session end event
Stop             → send_event.py                 — Record stop event
PermissionDenied → permission_denied_recovery.py — Permission-denied self-recovery
PreCompact       → pre_compact_save.py           — Auto-save progress before context compression
```

---

## Quick Install (AI-Assisted)

Tell Claude Code:
> "Read https://github.com/CronusL-1141/AI-company/blob/master/INSTALL.md and follow the instructions to install AI Team OS"

Claude Code will read the install guide and walk you through the setup automatically.

---

> **Important**: Install AI Team OS to your system Python, not inside a project virtual environment.
> If installed in a venv, AI Team OS will only work in that specific project.
> Run `deactivate` first if a venv is currently active, then install.

---

## Quick Start

### Prerequisites

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (`pip install uv`)
- Claude Code (MCP support required)
- Node.js >= 20 (Dashboard frontend, optional)

### Option A: Plugin Install (Recommended — for most users)

```bash
# Install uv (Python package runner, required for MCP server)
pip install uv

# Add marketplace + install plugin
claude plugin marketplace add CronusL-1141/AI-company
claude plugin install ai-team-os

# Restart Claude Code — first launch takes ~30s to set up dependencies
# Subsequent launches are instant

# Update to latest version anytime
claude plugin update ai-team-os@ai-team-os
```

> **Note**: First launch after install takes ~30 seconds while dependencies are automatically configured. This only happens once — subsequent sessions start instantly with 160 MCP tools ready.

### Option B: Source Install (for developers — editable, tracks latest source)

```bash
# Step 1: Clone the repository
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company

# Step 2: Run the installer (auto-configures MCP + Hooks + Agent templates + API)
python3 install.py

# Step 3: Restart Claude Code — everything activates automatically
# API server starts automatically when MCP loads. No manual startup needed.
# Verify: run /mcp in CC and check that ai-team-os tools are mounted
```

> **Dependencies**: `greenlet` (needed by SQLAlchemy async on Apple Silicon) is bundled by default. `LangGraph` is an optional extra — only the CLI graph-execution path needs it: `pip install 'ai-team-os[langgraph]'`.

### Verify Installation

```bash
# Check OS health (API must be running — port may vary, check api_port.txt)
curl http://localhost:8000/api/health
# Expected: {"status": "ok"}

# Create your first team via CC
# Type in Claude Code:
# "Create a web development team with a frontend dev, backend dev, and QA engineer"
```

### Uninstall

```bash
# Plugin install:
claude plugin uninstall ai-team-os
# Then manually remove residual data:
# Windows: rmdir /s %USERPROFILE%\.claude\plugins\data\ai-team-os-ai-team-os
# Unix:    rm -rf ~/.claude/plugins/data/ai-team-os-*
# Restart Claude Code to stop active hooks.

# Source install — full cleanup:
python scripts/uninstall.py
# Preview first:
python scripts/uninstall.py --dry-run
```

### Start the Dashboard (optional)

```bash
cd dashboard
npm install
npm run dev
# Visit http://localhost:5173
```

---

## Dashboard Screenshots

### Command Center
![Command Center](docs/screenshots/dashboard-home.png)

### Team Working — Live Activity Tracking
![Team Working](docs/screenshots/team-working-en.png)

### Task Board
![Task Board](docs/screenshots/task-board-en.png)

### Project Detail — Decision Timeline
![Decision Timeline](docs/screenshots/decision-timeline.png)

### Meeting Room
![Meeting Room](docs/screenshots/meeting-room.png)

### Ecosystem Research Platform
![Ecosystem](docs/screenshots/ecosystem-list-desktop.png)

### Activity Analytics
![Analytics](docs/screenshots/analytics.png)

### Event Log
![Events](docs/screenshots/events.png)

### Auto-Wake System — Autonomous Task Advancement
![Auto-Wake Demo](docs/screenshots/auto-wake-demo.png)

---

## Auto-Wake System

The Leader supports scheduled auto-wake to autonomously advance tasks without supervision:

- Automatically checks context usage and pending tasks every 10 minutes
- When tasks are available, autonomously creates teams and assigns work
- When user decisions are needed, records them asynchronously via the Briefing system
- When context exceeds 80%, auto-saves progress and prompts to open a new session

---

## Ecosystem Integration Recipes

AI Team OS is designed as a **meta-plugin** — it orchestrates other MCP servers rather than reimplementing their capabilities. Pre-built recipes let you integrate popular tools in minutes:

| Recipe | Integrates With | What You Get |
|--------|----------------|--------------|
| **GitHub** | `@modelcontextprotocol/github` | Auto PR creation, issue tracking, code review coordination |
| **Slack** | `@anthropics/slack-mcp` | Team notifications, decision escalation, status broadcasts |
| **Linear** | `linear-mcp-server` | Task sync, sprint tracking, bug triage automation |
| **Full-Stack Team** | GitHub + Slack + Linear | Complete development workflow with cross-tool orchestration |

Use the `ecosystem_recipes` MCP tool to discover recipes, or see the full guide: [docs/ecosystem-recipes.md](docs/ecosystem-recipes.md)

---

## CC-First Design Principles

AI Team OS is built specifically for Claude Code, not as a standalone framework:

- **MCP Protocol native**: all 160 MCP tools are registered natively — no custom client, no API wrapper
- **Hook-driven lifecycle**: 12 CC lifecycle events (SessionStart → PreCompact) provide deep integration without modifying CC internals
- **Agent templates as `.md` files**: Installed to `~/.claude/agents/` (global) or `.claude/agents/` (project-level) — CC's native agent system, not a custom abstraction
- **Zero external dependencies at runtime**: No external API calls, no cloud services — runs entirely within your CC subscription
- **Context-aware**: Session bootstrap injects only 5 core rules (down from 23) to minimize context budget impact, with subagent context capped at 60 lines

---

## MCP Tools

<details>
<summary>Expand to see the tool map (160 MCP tools across 24 modules)</summary>

> The tables below are a curated selection — the full inventory lives in `src/aiteam/mcp/tools/` and is machine-counted by `scripts/check_readme_numbers.sh`.

### Team Management

| Tool | Description |
|------|-------------|
| `team_create` | Create an AI Agent team; supports coordinate/broadcast modes |
| `team_status` | Get team details and member status |
| `team_list` | List all teams |
| `team_briefing` | Get a full team panorama in one call (members + events + meetings + todos) |
| `team_setup_guide` | Recommend team role configuration based on project type |

### Agent Management

| Tool | Description |
|------|-------------|
| `agent_register` | Register a new Agent to a team |
| `agent_update_status` | Update Agent status (idle/busy/error) |
| `agent_list` | List team members |
| `agent_template_list` | Get available Agent template list |
| `agent_template_recommend` | Recommend the best Agent template based on task description |

### Task Management

| Tool | Description |
|------|-------------|
| `task_run` | Execute a task with full execution recording |
| `task_decompose` | Break a complex task into subtasks |
| `task_status` | Query task execution status |
| `taskwall_view` | View the task wall (all pending + in-progress + completed) |
| `task_create` | Create a new task (supports `auto_start` and `task_type` pipeline parameters) |
| `task_update` | Partial update of task fields with auto timestamps |
| `task_auto_match` | Intelligently match the best Agent based on task characteristics |
| `task_memo_add` | Add an execution memo to a task |
| `task_memo_read` | Read task history memos |
| `task_list_project` | List all tasks under a project |

### Pipeline Orchestration

| Tool | Description |
|------|-------------|
| `pipeline_create` | Attach a workflow pipeline to a task (7 templates: feature/bugfix/research/refactor/quick-fix/spike/hotfix) |
| `pipeline_advance` | Advance pipeline to next stage; returns next-stage Agent template recommendation |

### Loop Engine

| Tool | Description |
|------|-------------|
| `loop_start` | Start the auto-advance loop |
| `loop_status` | View loop status |
| `loop_next_task` | Get the next pending task |
| `loop_advance` | Advance the loop to the next stage |
| `loop_pause` | Pause the loop |
| `loop_resume` | Resume the loop |
| `loop_review` | Generate a loop review report (with failure analysis) |

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
| `channel_mentions` | Get unread @mentions for an agent |

### File Lock & Workspace Isolation

| Tool | Description |
|------|-------------|
| `file_lock_acquire` | Acquire a file lock (TTL=300s) to prevent concurrent edits |
| `file_lock_release` | Release a file lock |
| `file_lock_check` | Check if a file is locked and by whom |
| `file_lock_list` | List all active file locks |

### Git Automation

| Tool | Description |
|------|-------------|
| `git_auto_commit` | Auto-commit staged changes with generated message |
| `git_create_pr` | Create a pull request from current branch |
| `git_status_check` | Check git repository status |

### Debate System

| Tool | Description |
|------|-------------|
| `debate_start` | Start a structured 4-round debate (Advocate→Critic→Response→Judge) |
| `debate_code_review` | Start a code review debate session |

### Guardrails

| Tool | Description |
|------|-------------|
| `guardrail_check` | Run guardrail checks on a command string |
| `guardrail_check_payload` | Run guardrail checks on a structured payload |

### Execution Patterns

| Tool | Description |
|------|-------------|
| `pattern_record` | Record a success/failure execution pattern |
| `pattern_search` | Search execution patterns via BM25 for context injection |

### Intelligence & Analysis

| Tool | Description |
|------|-------------|
| `failure_analysis` | Failure Alchemy — analyze root causes, generate antibody/vaccine/catalyst |
| `what_if_analysis` | What-If Analyzer — multi-option comparison and recommendation |
| `decision_log` | Log a decision to the cockpit timeline |
| `context_resolve` | Resolve current context and retrieve relevant background information |

### Memory System

| Tool | Description |
|------|-------------|
| `memory_search` | Search team memory — recency-window recall within scope + pure-Python BM25 rerank (Chinese bigram, no embeddings) |
| `team_knowledge` | Get a team knowledge summary |

### Knowledge Layer (v1.8.0)

| Tool | Description |
|------|-------------|
| `unified_search` | Three-arm RRF search across memos / reports / tasks — BM25 full-text + knowledge-graph fanout + exact ID match |
| `link_query` | Query the cross-domain reference graph by node (what references this / what does this reference) |
| `link_trace` | Trace a reference chain from any OS ID (wf_id / commit / task uuid) with evidence snippets |

### Model Governance (v1.8.1)

| Tool | Description |
|------|-------------|
| `model_config_get` | Read discovered available models (transcript-scanned) + the current default startup model |
| `model_config_set` | Set the global default startup model (triple write protection on `~/.claude/settings.json`) |

### Trust & Reliability

| Tool | Description |
|------|-------------|
| `agent_trust_scores` | View trust scores for all agents |
| `agent_trust_update` | Manually adjust an agent's trust score |
| `agent_heartbeat` | Send a heartbeat signal from a running agent |
| `watchdog_check` | Check for stalled agents (5-min TTL timeout) |
| `error_budget_status` | View SRE error budget (GREEN/YELLOW/ORANGE/RED) |
| `error_budget_update` | Record task outcome against the error budget |
| `verify_completion` | Verify task completion (status + memo check, anti-hallucination) |

### Analytics

| Tool | Description |
|------|-------------|
| `task_execution_trace` | Get unified execution timeline for a task |
| `task_replay` | Replay task execution history |
| `task_compare` | Compare two task executions side-by-side |
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

### Scheduler

| Tool | Description |
|------|-------------|
| `scheduler_create` | Create a scheduled periodic task |
| `scheduler_list` | List scheduled tasks |
| `scheduler_delete` | Delete a scheduled task |
| `scheduler_pause` | Pause a scheduled task |

### Ecosystem Research (46 tools)

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
| … | Full family of 46 tools: see `src/aiteam/mcp/tools/ecosystem.py` |

### Integrations & Cross-Project

| Tool | Description |
|------|-------------|
| `ecosystem_recipes` | Discover integration recipes (GitHub/Slack/Linear/Full-stack) |
| `send_notification` | Send notifications via Slack/webhook |
| `cross_project_send` | Send cross-project messages |
| `cross_project_inbox` | Read cross-project inbox |

### Prompt Registry

| Tool | Description |
|------|-------------|
| `prompt_version_list` | List agent template versions |
| `prompt_effectiveness` | View template effectiveness metrics |

### Project Management

| Tool | Description |
|------|-------------|
| `project_create` | Create a project |
| `project_list` | List all projects |
| `project_update` | Update project settings |
| `project_delete` | Delete a project |
| `project_summary` | Get a quick project status summary |
| `phase_create` | Create a project phase |
| `phase_list` | List project phases |

### System Operations

| Tool | Description |
|------|-------------|
| `os_health_check` | OS health check |
| `os_restart_api` | Restart the OS API server (with safety checks) |
| `event_list` | View the system event stream |
| `os_report_issue` | Report an issue |
| `os_resolve_issue` | Mark an issue as resolved |
| `agent_activity_query` | Query agent activity history and statistics |
| `find_skill` | 3-layer progressive skill discovery (quick recommend / category browse / full detail) |
| `team_close` | Close a team and cascade-close its active meetings |
| `team_delete` | Delete a team |

</details>

---

## Agent Template Library

25 ready-to-use professional Agent templates with recommendation engine, covering a complete software engineering team. Templates are installed to `plugin/agents/` (project-level) and `~/.claude/agents/` (global, available across all projects).

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

### Completed

- [x] Core Loop Engine (LoopEngine + Task Wall + Watchdog + Review)
- [x] Failure Alchemy (Antibody + Vaccine + Catalyst)
- [x] Decision Cockpit (Event stream + Timeline + Intent inspection)
- [x] Event-driven Task Wall 2.0 (Real-time push + Intelligent matching)
- [x] Living Team Memory (Knowledge query + Experience sharing)
- [x] What-If Analyzer (Multi-option comparison)
- [x] 8 structured meeting templates with keyword auto-select
- [x] 25 professional Agent templates (23 base + 2 debate roles) with recommendation engine
- [x] 4-layer defense rule system (48+ rules) + behavioral enforcement
- [x] Dashboard Command Center (React 19) — 22 pages including the `/workflows` swimlane, Workflow detail, the Ecosystem suite, and Settings with model governance
- [x] 160 MCP tools across 24 modules
- [x] CC Workflow observability layer (auto-tracking + /workflows dashboard + workflow_list / workflow_get / workflow_reconcile)
- [x] Knowledge layer — zero-LLM reference graph + unified 3-arm RRF search (v1.8.0)
- [x] Model governance — transcript-based model discovery + global default startup model (v1.8.1)
- [x] Machine-checked red-line invariants + one-command preflight (`scripts/preflight.sh`)
- [x] AWARE loop memory system
- [x] find_skill 3-layer progressive discovery
- [x] task_update API for programmatic task management
- [x] Workflow pipeline orchestration (7 templates + auto phase progression) — retired in v1.7.0, superseded by CC Workflow observability
- [x] 1,670+ automated tests, CI green
- [x] Prompt Registry (version tracking + effectiveness metrics)
- [x] BM25 as the main memory-retrieval chain (pure-Python Okapi BM25, Chinese bigram, recency-window recall + rerank)
- [x] Event log enhancement (entity_id / entity_type / state_snapshot fields)
- [x] CC Plugin Marketplace submission
- [x] File lock / workspace isolation (acquire/release/check/list + TTL=300s)
- [x] Channel communication system (team:/project:/global + @mention)
- [x] Execution pattern memory (success/failure recording + BM25 retrieval)
- [x] Git automation tools (git_auto_commit / git_create_pr / git_status_check)
- [x] Guardrails L1 (7 dangerous patterns + PII warnings)
- [x] Alembic database migration system
- [x] Debate mode (4-round structured debate + code review)
- [x] Agent trust scoring system (auto-adjust on task success/failure)
- [x] Tool tier draft (informational CORE/ADVANCED grouping — groundwork for context budgeting)
- [x] Agent Watchdog heartbeat system (5-min TTL timeout detection)
- [x] SRE error budget model (GREEN/YELLOW/ORANGE/RED 4-level response)
- [x] Completion verification protocol (anti-hallucination completion check)
- [x] Ecosystem integration recipes (GitHub/Slack/Linear/Full-stack presets)
- [x] Session bootstrap rule compression (23 → 5 core rules, 60% context reduction)
- [x] Atomic API startup lock (multi-session port conflict prevention)
- [x] Auto port discovery (API finds available port, writes to `api_port.txt`)
- [x] MCP HTTP Streamable endpoint (`/mcp/` on FastAPI)
- [x] PyPI release — frozen at 1.2.0 and deprecated (install via plugin or source instead)
- [x] INSTALL.md CC-assisted installation guide

### In Progress / Planned

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
│   ├── api/           — FastAPI REST endpoints (217 routes)
│   ├── mcp/
│   │   ├── server.py  — MCP server entry point
│   │   └── tools/     — 24 tool modules (160 MCP tools)
│   │       ├── agent.py, analytics.py, briefing.py, channels.py,
│   │       ├── ecosystem.py, error_budget_tool.py, file_lock.py,
│   │       ├── git_ops.py, guardrails.py, infra.py, links.py, loop.py,
│   │       ├── meeting.py, memory.py, pipeline.py, project.py,
│   │       ├── reports.py, scheduler.py, task.py, task_analysis.py,
│   │       ├── team.py, trust.py, watchdog.py, workflows.py
│   │       └── __init__.py  — Tool tier draft (informational)
│   ├── loop/          — Loop Engine
│   ├── meeting/       — Meeting system
│   ├── memory/        — Team memory
│   ├── orchestrator/  — Team orchestrator
│   ├── storage/       — Storage layer (SQLite, WAL journaling)
│   ├── templates/     — Agent template base classes
│   ├── hooks/         — CC Hook scripts (12 lifecycle events)
│   └── types.py       — Shared type definitions
├── plugin/
│   ├── agents/        — 25 Agent templates (.md)
│   └── .claude-plugin/ — Plugin manifest
├── dashboard/         — React 19 frontend (22 pages)
├── scripts/           — preflight + machine-checked invariants (incl. README number check)
├── docs/              — Design documents + ecosystem recipes
├── tests/             — Test suite (1,670+ tests)
├── install.py         — One-click install script
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
# Set up the development environment
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company
python3 install.py

# One command = every gate CI runs (ruff + eslint + unit tests + machine-checked invariants)
bash scripts/preflight.sh
```

Before submitting a PR, make sure `bash scripts/preflight.sh` passes — it runs the exact gates CI enforces: ruff, eslint, the unit test suite, and the red-line invariant checks in `scripts/check_invariants.sh`. Every one of those invariants (hook copy sync, version lockstep, dist consistency, venv ban, README number drift) was distilled from a real incident in this repo's history — please keep them green.

---

## License

MIT License — see [LICENSE](LICENSE)

---

<div align="center">

**AI Team OS** — The AI company that runs while you sleep.

*Built with Claude Code · Powered by MCP Protocol*

[Docs](docs/) · [Issues](https://github.com/CronusL-1141/AI-company/issues) · [Discussions](https://github.com/CronusL-1141/AI-company/discussions)

</div>

<!-- README numbers are machine-checked against the code: scripts/check_readme_numbers.sh (invariant I6 in scripts/check_invariants.sh). Drift fails CI. -->
