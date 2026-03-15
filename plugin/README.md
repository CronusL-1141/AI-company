# AI Team OS

Turn Claude Code into a multi-agent team operating system with persistent coordination, task management, and autonomous loop execution.

## Installation

### Quick Install (recommended)

```bash
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company
python install.py
```

The install script will:
- Check Python 3.11+ and Node.js availability
- Install Python dependencies (`pip install -e .`)
- Build the Dashboard (if Node.js is available)
- Create data directory at `~/.claude/data/ai-team-os/`
- Generate `.mcp.json` for MCP tool discovery

### Manual Install

```bash
# 1. Install Python package
pip install -e .

# 2. (Optional) Build Dashboard
cd dashboard && npm install && npm run build && cd ..

# 3. Create data directory
mkdir -p ~/.claude/data/ai-team-os

# 4. Create .mcp.json in project root (if not exists)
# See .mcp.json.example for the format
```

### Plugin Marketplace

```
/plugin marketplace add CronusL-1141/AI-company
```

## System Requirements

- Python 3.11+
- SQLite (included with Python)
- Node.js 18+ (optional, for Dashboard)

## Quick Start

1. **Install** using one of the methods above
2. **Open** the project directory in Claude Code
3. **Create a team** — `/os-up`
4. **Start working** — `/os-task`

## Core Features

- **Team Management** — Create teams, register agents, assign roles, track status
- **Task Wall** — Decompose, assign, and monitor tasks across agents
- **Loop Engine** — Autonomous company loop: plan -> execute -> review -> iterate
- **Watchdog** — Health checks, issue reporting, system self-healing
- **Meeting System** — Create meetings, invite agents, structured discussion and conclusions
- **Memory Store** — Persistent memory search across conversations
- **Hooks & Events** — Session lifecycle, tool-use tracking, rule sync automation

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

## Agents

- **tech-lead** — Technical leadership and architecture decisions
- **team-member** — General development work
- **meeting-facilitator** — Structured meeting facilitation

## Skills

- **os-register** — Agent self-registration to OS
- **meeting-facilitate** — Lead and facilitate meetings
- **meeting-participate** — Participate in team meetings
- **continuous-mode** — Autonomous loop execution

## License

MIT
