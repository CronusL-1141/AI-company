# AI Team OS

Turn Claude Code into a multi-agent team operating system with persistent coordination, task management, and autonomous loop execution.

## Installation

```
/plugin marketplace add CronusL-1141/AI-company
```

## Core Features

- **Team Management** — Create teams, register agents, assign roles, track status
- **Task Wall** — Decompose, assign, and monitor tasks across agents
- **Loop Engine** — Autonomous company loop: plan -> execute -> review -> iterate
- **Watchdog** — Health checks, issue reporting, system self-healing
- **Meeting System** — Create meetings, invite agents, structured discussion and conclusions
- **Memory Store** — Persistent memory search across conversations
- **Hooks & Events** — Session lifecycle, tool-use tracking, rule sync automation

## Quick Start

1. **Install the plugin**
   ```
   /plugin marketplace add CronusL-1141/AI-company
   ```

2. **Create a team**
   ```
   /os-up
   ```

3. **Start working**
   ```
   /os-task
   ```

## System Requirements

- Python 3.12+
- SQLite (included with Python)

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
