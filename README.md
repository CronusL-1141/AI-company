[English](README.md) | [中文](README.zh-CN.md)

# AI Team OS

<!-- Logo placeholder -->
<!-- ![AI Team OS Logo](docs/assets/logo.png) -->

### Your AI coding tool stops when you stop prompting. Ours doesn't.

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Protocol-orange)](https://modelcontextprotocol.io)
[![Stars](https://img.shields.io/github/stars/CronusL-1141/AI-company?style=flat)](https://github.com/CronusL-1141/AI-company)

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

- Checks the task wall for next highest-priority item when a task completes
- When blocked on something requiring your approval, parks that thread and switches to parallel workstreams
- Batches all strategic questions and reports them when you return — no interruptions for tactical decisions
- Deadlock detection: if the loop stalls, it surfaces the blocker rather than spinning

### 2. Self-Improvement

The system doesn't just execute — it evolves:

- **R&D cycle**: Research agents scan competitors, new frameworks, and community tools. Findings go to brainstorming meetings where agents challenge each other. Conclusions become implementation plans on the task wall.
- **Failure Alchemy**: Every failed task triggers root cause extraction, classification, and three outputs:
  - *Antibody* — failure stored in team memory to prevent the same mistake
  - *Vaccine* — high-frequency failure patterns converted into pre-task warnings
  - *Catalyst* — analysis injected into Agent system prompts to improve future execution

### 3. Team Collaboration

Not a single Agent. A structured organization:

- **23 professional Agent templates** with recommendation engine — Engineering, Testing, Research, Management — ready out of the box
- **8 structured meeting templates** with keyword-based auto-select, built on Six Thinking Hats, DACI, and Design Sprint methodologies
- **Department grouping** — Engineering / QA / Research with cross-team coordination
- Every meeting produces actionable conclusions. "We discussed but didn't decide" is not an outcome.

### 4. Full Transparency

Nothing is a black box:

- **Decision Cockpit**: event stream + decision timeline + intent inspection — every decision has a traceable record
- **Activity Tracking**: real-time status of every Agent and what it's working on
- **What-If Analyzer**: compare multiple approaches before committing, with path simulation and recommendations

### 5. Workflow Pipeline Orchestration

Every task follows a structured, enforced workflow — no more ad-hoc execution:

- **7 pipeline templates**: `feature` (Research→Design→Implement→Review→Test→Deploy), `bugfix`, `research`, `refactor`, `quick-fix`, `spike`, `hotfix`
- **Auto-attach via `task_type`**: pass `task_type="feature"` to `task_create` and the pipeline mounts automatically
- **Progressive enforcement**: hook detects tasks without pipelines — soft reminder → strong reminder → hard block (`exit 2`) on third occurrence
- **Auto phase progression**: each stage recommends the right Agent template; `pipeline_advance` moves to next stage automatically
- **Lightest escape hatch**: `quick-fix` (Implement→Test only) for truly trivial changes

### 6. Safety & Behavioral Enforcement

Built-in guardrails so the system can run unsupervised without surprises:

- **Local agent blocking**: all non-readonly agents must declare `team_name`/`name` — prevents rogue background agents
- **S1 safety rules**: regex-based scan catches destructive commands (rm -rf, force push, hardcoded secrets) including uppercase flags and heredoc patterns
- **4-layer defense rule system**: 48+ rules covering workflow, delegation, session, and safety layers
- **`find_skill` 3-layer progressive discovery**: quick recommend → category browse → full detail, reducing tool-call overhead
- **`task_update` API**: programmatic partial update of tasks with auto timestamps, enabling fine-grained task state management

### 7. Zero Extra Cost

Runs entirely within your existing Claude Code subscription:

- No external API calls, no extra token spend
- MCP tools, hooks, and Agent templates are all local
- 100% utilization of your CC plan

---

## It Built Itself

AI Team OS managed its own development:

- Organized 5 innovation brainstorming meetings with multi-agent debate
- Conducted competitive analysis across CrewAI, AutoGen, LangGraph, and Devin
- Shipped 67 tasks across 5 major innovation features
- Generated 14 design documents totaling 10,000+ lines

The system that builds your projects... built itself.

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
| **Workflow Orchestration** | 7 pipeline templates + progressive enforcement | None | None | Manual | None |
| **Rule System** | 4-layer defense (48+ rules) + behavioral enforcement | Limited | Limited | None | Limited |
| **Agent Templates** | 23 ready-to-use + recommendation engine | Built-in roles | Built-in roles | None | None |
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
│      (22 roles)       Loop Engine  (7 templates)                 │
│            │            │            │                          │
│            └────────────┼────────────┘                          │
│                         ▼                                       │
│              ┌──────────────────────┐                           │
│              │   OS Enhancement Layer│                           │
│              │  ┌──────────────┐    │                           │
│              │  │  MCP Server  │    │                           │
│              │  │  (60+ tools) │    │                           │
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
│              │  + Memory System    │                            │
│              └─────────────────────┘                            │
└─────────────────────────────────────────────────────────────────┘
```

### Five-Layer Technical Architecture

```
Layer 5: Web Dashboard    — React 19 + TypeScript + Shadcn UI
Layer 4: CLI + REST API   — Typer + FastAPI
Layer 3: Team Orchestrator — LangGraph StateGraph
Layer 2: Memory Manager   — Mem0 / File fallback
Layer 1: Storage          — SQLite (development) / PostgreSQL (production)
```

### Hook System (The Bridge Between CC and OS)

```
SessionStart     → session_bootstrap.py          — Inject Leader briefing + rule set + team state
SubagentStart    → inject_subagent_context.py    — Inject sub-Agent OS rules (2-Action etc.)
PreToolUse       → workflow_reminder.py          — Workflow reminders + safety guardrails
PostToolUse      → send_event.py                 — Forward events to OS API
UserPromptSubmit → context_monitor.py            — Monitor context usage rate
```

---

## Quick Start

### Prerequisites

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (`pip install uv`)
- Claude Code (MCP support required)
- Node.js >= 20 (Dashboard frontend, optional)

### Option A: Plugin Install (Recommended)

```bash
# Install uv (Python package runner, required for MCP server)
pip install uv

# Add marketplace + install plugin
claude plugin marketplace add github:CronusL-1141/AI-company
claude plugin install ai-team-os

# Restart Claude Code — first launch takes ~30s to set up dependencies
# Subsequent launches are instant

# Update to latest version anytime
claude plugin update ai-team-os@ai-team-os
```

> **Note**: First launch after install takes ~30 seconds while dependencies are automatically configured. This only happens once — subsequent sessions start instantly with 63 MCP tools ready.

### Option B: Manual Install

```bash
# Step 1: Clone the repository
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company

# Step 2: Run the installer (auto-configures MCP + Hooks + Agent templates + API)
python install.py

# Step 3: Restart Claude Code — everything activates automatically
# API server starts automatically when MCP loads. No manual startup needed.
# Verify: run /mcp in CC and check that ai-team-os tools are mounted
```

### Verify Installation

```bash
# Check OS health (API must be running)
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

# Manual install:
python scripts/uninstall.py        # full cleanup
python scripts/uninstall.py --dry-run  # preview first
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

### Task Board — 68 Tasks Completed
![Task Board](docs/screenshots/task-board-en.png)

### Meeting Room
![Meeting Room](docs/screenshots/meeting-room.png)

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

## MCP Tools

<details>
<summary>Expand to see all 60+ MCP tools</summary>

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
| `memory_search` | Full-text search of the team memory store |
| `team_knowledge` | Get a team knowledge summary |

### Project Management

| Tool | Description |
|------|-------------|
| `project_create` | Create a project |
| `project_list` | List all projects |
| `phase_create` | Create a project phase |
| `phase_list` | List project phases |

### System Operations

| Tool | Description |
|------|-------------|
| `os_health_check` | OS health check |
| `event_list` | View the system event stream |
| `os_report_issue` | Report an issue |
| `os_resolve_issue` | Mark an issue as resolved |
| `agent_activity_query` | Query agent activity history and statistics |
| `find_skill` | 3-layer progressive skill discovery (quick recommend / category browse / full detail) |
| `team_close` | Close a team and cascade-close its active meetings |

</details>

---

## Agent Template Library

23 ready-to-use professional Agent templates with recommendation engine, covering a complete software engineering team:

### Engineering

| Template | Role | Use Case |
|----------|------|----------|
| `engineering-software-architect` | Software Architect | System design, architecture review |
| `engineering-backend-architect` | Backend Architect | API design, service architecture |
| `engineering-frontend-developer` | Frontend Developer | UI implementation, interaction development |
| `engineering-ai-engineer` | AI Engineer | Model integration, LLM applications |
| `engineering-mcp-builder` | MCP Builder | MCP tool development |
| `engineering-database-optimizer` | Database Optimizer | Query optimization, schema design |
| `engineering-devops-automator` | DevOps Automation Engineer | CI/CD, infrastructure |
| `engineering-sre` | Site Reliability Engineer | Observability, incident response |
| `engineering-security-engineer` | Security Engineer | Security review, vulnerability analysis |
| `engineering-rapid-prototyper` | Rapid Prototyper | MVP validation, fast iteration |
| `engineering-mobile-developer` | Mobile Developer | iOS/Android development |
| `engineering-git-workflow-master` | Git Workflow Master | Branch strategy, code collaboration |

### Testing

| Template | Role | Use Case |
|----------|------|----------|
| `testing-qa-engineer` | QA Engineer | Test strategy, quality assurance |
| `testing-api-tester` | API Test Specialist | Interface testing, contract testing |
| `testing-bug-fixer` | Bug Fix Specialist | Defect analysis, root cause investigation |
| `testing-performance-benchmarker` | Performance Benchmarker | Performance analysis, load testing |

### Research & Support

| Template | Role | Use Case |
|----------|------|----------|
| `specialized-workflow-architect` | Workflow Architect | Process design, automation orchestration |
| `support-technical-writer` | Technical Writer | API docs, user guides |
| `support-meeting-facilitator` | Meeting Facilitator | Structured discussion, decision facilitation |

### Management

| Template | Role | Use Case |
|----------|------|----------|
| `management-tech-lead` | Tech Lead | Technical decisions, team coordination |
| `management-project-manager` | Project Manager | Schedule management, risk tracking |

### Specialized Templates

| Template | Role | Use Case |
|----------|------|----------|
| `python-reviewer` | Python Code Reviewer | Python project code quality |
| `security-reviewer` | Security Reviewer | Code security scanning |
| `refactor-cleaner` | Refactor Cleaner | Technical debt cleanup |
| `tdd-guide` | TDD Guide | Test-driven development |

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
- [x] 26 professional Agent templates with recommendation engine
- [x] 4-layer defense rule system (48+ rules) + behavioral enforcement
- [x] Dashboard Command Center (React 19)
- [x] 60+ MCP tools
- [x] AWARE loop memory system
- [x] find_skill 3-layer progressive discovery
- [x] task_update API for programmatic task management
- [x] Workflow pipeline orchestration (7 templates + auto phase progression + progressive enforcement)
- [x] 467+ automated tests
- [x] Prompt Registry (version tracking + effectiveness metrics)
- [x] BM25 search upgrade (Chinese bigram + English word tokenization, 3-5x quality improvement)
- [x] Event log enhancement (entity_id / entity_type / state_snapshot fields)
- [x] CC Plugin Marketplace submission

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
│   ├── api/           — FastAPI REST endpoints
│   ├── mcp/           — MCP Server (60+ tools)
│   ├── loop/          — Loop Engine
│   ├── meeting/       — Meeting system
│   ├── memory/        — Team memory
│   ├── orchestrator/  — Team orchestrator
│   ├── storage/       — Storage layer (SQLite/PostgreSQL)
│   ├── templates/     — Agent template base classes
│   ├── hooks/         — CC Hook scripts
│   └── types.py       — Shared type definitions
├── dashboard/         — React 19 frontend
├── docs/              — Design documents (14 files)
├── tests/             — Test suite
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
# Set up development environment
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company/ai-team-os
pip install -e ".[dev]"
pytest tests/
```

Before submitting a PR, please ensure:
- `ruff check src/` passes
- `mypy src/` has no new errors
- Relevant tests pass

---

## License

MIT License — see [LICENSE](LICENSE)

---

<div align="center">

**AI Team OS** — The AI company that runs while you sleep.

*Built with Claude Code · Powered by MCP Protocol*

[Docs](docs/) · [Issues](https://github.com/CronusL-1141/AI-company/issues) · [Discussions](https://github.com/CronusL-1141/AI-company/discussions)

</div>
