# Changelog

All notable changes to AI Team OS will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [0.7.1] — 2026-04-02

### Added
- **Leader Briefing system** — decision escalation for autonomous operation
  - DB table `leader_briefings` + Pydantic model + ORM
  - 3 MCP tools: `briefing_add`, `briefing_list`, `briefing_resolve`
  - API endpoints: GET/POST `/api/leader-briefings`, PUT `/{id}/resolve`, PUT `/{id}/dismiss`
  - Leader records pending decisions during autonomous work, user reviews on return
- **Auto-wake via CronCreate** — SessionStart bootstrap injects CronCreate instruction
  - Every 3 minutes, Leader auto-checks task wall and pushes work autonomously
  - Escalates decisions via `briefing_add`, reports pending items when user returns
- **install.py** — one-command setup for hooks, MCP, and verification
  - `python scripts/install.py` — full install (hooks + MCP + settings.json)
  - `python scripts/install.py --check` — verify 9 hooks, MCP, API, package
  - `python scripts/install.py --uninstall` — remove config, preserve data

## [0.7.0] — 2026-04-02

### Added
- **Wake Agent Scheduler** — auto-wake agents via `claude -p` subprocess
  - WakeAgentManager: subprocess lifecycle (communicate + 2-phase kill)
  - WakeSession data model + ORM + 7 repository CRUD methods
  - 7-layer security: array args, UUID validation, per-agent lock, 
    global semaphore (max=2), circuit breaker, prompt/data XML separation, env cleanup
  - Triage pre-check: skip wake if agent has no actionable tasks (~70% skip rate)
  - Kill switch API: `PUT /wake-pause-all`, `PUT /wake-resume-all`
  - StateReaper integration (fire-and-forget + graceful shutdown)
  - allowedTools presets: safe (no Bash) / with_bash (explicit opt-in)
- **CronCreate session wake** — verified CC built-in cron for waking current session
- 20 unit tests for wake_manager (all passing)
- Wake session outcome tracking (completed/timeout/error/fused/skipped_triage)

### Fixed
- `context_resolve()` auto-project selection: match by cwd→root_path instead of blindly picking first project
- Hook path encoding: moved hook scripts to ASCII path (`~/.claude/plugins/ai-team-os/hooks/`)
- Hook exempt list: added claude-code-guide, tdd-guide, refactor-cleaner to non-blocking agent types
- `valid_actions` in scheduler route: added "wake_agent" (was missing, blocked API creation)
- Semaphore private API access (`_value`) replaced with `locked()` 
- Circuit breaker: only count real failures (error/timeout), not skips
- `duration_seconds` now correctly calculated and recorded
- `shutdown()` dict iteration safety (snapshot values before cancel)
- Global MCP config: added `cwd` field for cross-directory availability
- Data migration: 19 tasks + 1 team moved from wrong project to correct one

### Changed
- `_clean_env()` switched from whitelist to blacklist strategy (inherit all, exclude secrets)
- Plugin manifest: added `hooks` field pointing to `hooks/hooks.json`
- Plugin `.mcp.json`: local dev mode uses `python -m aiteam.mcp.server` with `cwd`

## [0.6.0] — 2026-03-22

### Added
- Workflow orchestration pipeline (7 templates, auto phase progression)
- Pipeline enforcement: task_type parameter + progressive blocking
- Cross-project messaging system (v1, single machine)
- Auto-update mechanism (scripts/update.py)
- Team cleanup reminder (SessionStart + Rule 15)
- Self-contained install (hooks → ~/.claude/hooks/)
- CC Plugin package structure
- Uninstall script (scripts/uninstall.py)
- Dashboard: activity table + decision timeline enhancement

### Fixed
- Global MCP: ~/.claude.json (not settings.json)
- Install dependencies (fastapi, uvicorn, fastmcp now required)
- SessionStart API retry (3 attempts for timing issue)
- B0.9 noise reduction (remind once then every 10 calls)
- Windows UTF-8 encoding in all hook scripts

## [0.5.0] — 2026-03-22

### Added
- Cross-project messaging system (2 MCP tools + 4 API endpoints + global DB)
- Auto-update mechanism (scripts/update.py + install.py --update)
- SessionStart 24h-cooldown update checker
- Self-contained install: hooks copied to ~/.claude/hooks/ai-team-os/
- Global MCP registration in ~/.claude/settings.json

### Changed
- Install reduced to 3 steps (API auto-starts with MCP, no manual startup)

## [0.4.0] — 2026-03-21

### Added
- Per-project database isolation (Phase 1-4)
- EnginePool with LRU cache for multi-DB management
- ProjectContextMiddleware (X-Project-Dir header routing)
- Migration script: split global DB by project_id
- StateReaper + Watchdog multi-DB adaptation
- Dashboard project switcher
- install.py: full onboarding (hooks + agents + MCP + verification)
- GET /api/health endpoint

### Fixed
- Windows UTF-8 encoding in all hook scripts (gbk to utf-8)
- Team templates reference actual agent template names

## [0.3.0] — 2026-03-21

### Added
- Workflow enforcement: Rule 2 task wall check + template reminder
- Local agent blocking (B0.4): all non-readonly agents must have team_name
- Council meeting template (3-round multi-perspective expert review)
- Meeting auto-select: keyword matching across 8 templates
- Meeting cascade close on team shutdown
- find_skill MCP tool with 3-layer progressive loading
- task_update MCP tool + PUT /api/tasks/{id}
- 6 new MCP tools (total: 55)
- 467+ tests

### Fixed
- S1 safety regex catches uppercase -R flag
- S1 heredoc false positive
- Rule 7 taskwall timer initialization
- Meeting expiry 2h to 45min
- B0.9 infrastructure tools exempt from delegation counter

## [0.2.0] — 2026-03-20

### Added
- LoopEngine with AWARE cycle
- Task wall with score ranking + kanban
- Scheduler system (periodic tasks)
- React Dashboard (6 pages)
- Meeting system with 7 templates
- 26 agent templates across 7 categories
- Failure alchemy (antibody + vaccine + catalyst)
- What-if analysis
- i18n support (zh/en)
- R&D monitoring system (10 sources)

## [0.1.0] — 2026-03-12

### Added
- MCP server with FastAPI backend
- CC Hooks integration (7 lifecycle events)
- Team/agent/task/project management
- SQLite storage with async repository
- Session bootstrap with behavioral rule injection
- Event bus + decision logging
- Memory search
