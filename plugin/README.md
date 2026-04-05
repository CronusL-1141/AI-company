# AI Team OS

Turn Claude Code into a multi-agent team operating system with persistent coordination, task management, and autonomous loop execution.

## What is AI Team OS?

AI Team OS is a Claude Code plugin that adds full team orchestration capabilities to your AI workflow. It provides ~100 MCP tools, 25 agent templates, and 9 hook events to coordinate multiple AI agents on complex projects — with persistent state, meeting systems, task walls, and a company-style loop engine.

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

### Option 3: Manual (pip + script)

```bash
pip install ai-team-os
python scripts/install.py
```

The install script will:
- Check Python 3.11+ and Node.js availability
- Install Python dependencies (`pip install -e .`)
- Build the Dashboard (if Node.js is available)
- Create data directory at `~/.claude/data/ai-team-os/`
- Generate `.mcp.json` for MCP tool discovery

## Features

| Category | Details |
|----------|---------|
| MCP Tools | ~100 tools across team, task, loop, meeting, memory, channel, git, guardrail, and debate domains |
| Agent Templates | 25 pre-built agent roles (tech-lead, researcher, reviewer, debate roles, etc.) |
| Hook Events | 9 lifecycle events: SessionStart, SessionEnd, PreToolUse, PostToolUse, SubagentStart, SubagentStop, Stop, UserPromptSubmit, PreCompact |
| Team Management | Create teams, register agents, assign roles, track status |
| Task Wall | Decompose, assign, and monitor tasks across agents |
| Loop Engine | Autonomous company loop: plan → execute → review → iterate |
| Watchdog | Health checks, issue reporting, system self-healing |
| Meeting System | Structured meetings with conclusions and action items |
| Memory Store | Persistent cross-conversation memory search |

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
- Ensure Python 3.11+ is installed: `python --version`
- Run the install script: `python scripts/install.py`
- Check MCP server is running: `python -m aiteam.mcp.server --check`

**MCP tools not showing**
- Verify `.mcp.json` exists in project root
- Restart Claude Code after installation
- Run `/os-doctor` for automated diagnostics

**Hooks not firing**
- Check `hooks.json` is present in `plugin/hooks/`
- Use `--plugin-dir ./plugin` flag when starting Claude Code locally

## License

MIT — see [LICENSE](LICENSE)
