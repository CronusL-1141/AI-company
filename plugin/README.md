# AI Team OS

Turn Claude Code into a multi-agent team operating system with persistent coordination, task management, and autonomous loop execution.

## What is AI Team OS?

AI Team OS is a Claude Code plugin that adds full team orchestration capabilities to your AI workflow. It provides 155 MCP tools, 25 agent templates, and 14 hook scripts across 12 lifecycle events to coordinate multiple AI agents on complex projects — with persistent state, meeting systems, task walls, and a company-style loop engine.

## Installation

### Option 1: Local Development (recommended for contributors)

```bash
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company
claude --plugin-dir ./plugin
```

### Option 2: GitHub Plugin Marketplace

In any Claude Code session:

```
/plugin install CronusL-1141/AI-company
```

### Option 3: From source (script install)

```bash
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company
python3 install.py
```

`python3 install.py` (repo root) is the primary installer; the older `python scripts/install.py` still works but the root version is preferred. The install script will:
- Check Python 3.11+ and Node.js availability
- Install Python dependencies editable against the working tree (`pip install -e .`; `greenlet` is a core dependency so Apple Silicon installs directly)
- Build the Dashboard (if Node.js is available)
- Create data directory at `~/.claude/data/ai-team-os/`
- Generate `.mcp.json` for MCP tool discovery

## Features

| Category | Details |
|----------|---------|
| MCP Tools | 155 tools across team, task, workflow, loop, meeting, memory, channel, git, guardrail, and debate domains |
| Agent Templates | 25 pre-built agent roles (tech-lead, researcher, reviewer, debate roles, etc.) |
| Hook Events | 14 hook scripts wired across 12 lifecycle events: SessionStart, SubagentStart, SubagentStop, PreToolUse, PostToolUse, TaskCreated, TaskCompleted, SessionEnd, Stop, UserPromptSubmit, PermissionDenied, PreCompact |
| Team Management | Create teams, register agents, assign roles, track status |
| Task Wall | Decompose, assign, and monitor tasks across agents |
| Loop Engine | Autonomous company loop: plan → execute → review → iterate |
| Watchdog | Health checks, issue reporting, system self-healing |
| Meeting System | Structured meetings with conclusions and action items |
| Memory Store | Persistent cross-conversation memory search |
| Auto Port Discovery | API server finds available port automatically, avoids multi-project conflicts |
| MCP HTTP Streamable | `/mcp/` endpoint mounted on FastAPI for HTTP-based MCP access |

## System Requirements

- Python 3.11+
- SQLite (included with Python)
- Claude Code (latest)
- Node.js 18+ (optional, for Dashboard UI)

## Quick Start

1. Install using one of the methods above
2. Open your project directory in Claude Code
3. Create a team: `/os-up`
4. Start working: `/os-task`

## Commands

| Command | Description |
|---------|-------------|
| `/os-up` | Start the OS and create a team |
| `/os-status` | View team and system status |
| `/os-task` | Manage tasks |
| `/os-meeting` | Start or join a meeting |
| `/os-doctor` | Run health diagnostics |
| `/os-hooks` | Manage hook configuration |
| `/os-help` | Show help information |
| `/os-init` | Initialize project setup |

## Troubleshooting

**Plugin not loading**
- Ensure Python 3.11+ is installed: `python3 --version`
- Run the install script: `python3 install.py`
- Check MCP server is running: `python3 -m aiteam.mcp.server --check`

**MCP tools not showing**
- Verify `.mcp.json` exists in project root
- Restart Claude Code after installation
- Run `/os-doctor` for automated diagnostics

**Hooks not firing**
- Check `hooks.json` is present in `plugin/hooks/`
- Use `--plugin-dir ./plugin` flag when starting Claude Code locally

## License

MIT — see [LICENSE](LICENSE)
