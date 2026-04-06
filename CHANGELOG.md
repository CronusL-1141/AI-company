# Changelog

All notable changes to AI Team OS will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [1.2.0] — 2026-04-05

### Added
- **Agent Watchdog heartbeat system** — `agent_heartbeat` / `watchdog_check` MCP tools with 5-minute TTL timeout detection, automatic identification of stuck agents
- **SRE error budget model** — GREEN/YELLOW/ORANGE/RED four-level response, 20-task sliding window, `error_budget_status` / `error_budget_update` tools
- **Completion verification protocol** — `verify_completion` checks task status + memo existence, prevents hallucinated completion reports
- **Alembic incremental migration** — Full v1.1 schema migration file (trust_score / channel_messages / entity_id / state_snapshot, etc.)
- **Ecosystem integration recipes documentation** — GitHub / Slack / Linear / full-stack team, 4 preset recipes (`docs/ecosystem-recipes.md`)
- **`ecosystem_recipes()` MCP tool** — Integration recipe discovery and query
- **MCP debug log enhancement** — Startup lock mechanism logging, API startup process now traceable

### Changed
- **Session bootstrap context engineering** — Rules reduced from 23 to 5 core rules (context injection reduced by 60%)
- **Subagent context injection** — Added 60-line cap with priority-based auto-discard of low-priority content
- **`_ensure_api_running` atomic startup lock** — Prevents multi-session port race conditions (`O_CREAT|O_EXCL` file lock)

### Fixed
- Alembic integration caused `_run_migrations` to be skipped — changed to always execute (idempotent safe)
- Multiple CC sessions starting API simultaneously caused port conflicts — resolved with atomic file lock
- StateReaper cascade-closing active meetings incorrectly closed meetings with recent messages — added recent message check
- `_read_pid_file` threw `SystemError` on Windows — added catch

## [1.1.0] — 2026-04-05

### Added
- **Agent trust scoring system** — `trust_score` field (0-1), auto-adjusted on task success/failure, weighted matching in `auto_assign`, `agent_trust_scores` / `agent_trust_update` MCP tools
- **Semantic cache layer** — BM25 + Jaccard similarity matching, JSON persistence, TTL expiration, `cache_stats` / `cache_clear` MCP tools
- **Tool tiering definitions** — CORE (15 essential tools) vs ADVANCED (46 domain tools) classification, preparing for future context budget optimization

### Changed
- Added database index on `TaskModel.status` (query performance improvement)
- `resolve_task_dependencies` uses batch IN query replacing per-row queries (N+1 optimization)
- `detect_dependency_cycle` switched to BFS + batch query (large dependency graph performance optimization)
- `task_list_project` pagination — added `limit` / `offset` / `include_completed` / `status` parameters

### Fixed
- `trust.py` error responses changed to `HTTPException` (previously returned raw dict)
- `git_ops.py` sensitive file filter uses `basename` (avoids false positives when path contains keywords)
- `channels.py` dead code removed
- Pre-existing `test_check_for_updates_no_git_repo_silent` fix

## [1.0.0] — 2026-04-05

### Added
- **Error type to recovery strategy mapping** — `_api_call` uniformly attaches `_recovery` and `_error_category`, auto-recommends recovery actions
- **File lock / workspace isolation** — `file_lock_acquire` / `release` / `check` / `list` 4 MCP tools + TTL=300s + hook warning, prevents concurrent edit conflicts
- **Channel messaging system** — `team:` / `project:` / `global` three channel formats + `@mention` support, `channel_send` / `channel_read` / `channel_mentions` MCP tools
- **Execution pattern memory** — Success/failure pattern recording + BM25 retrieval + subagent context injection, `pattern_record` / `pattern_search` MCP tools
- **Git automation tools** — `git_auto_commit` / `git_create_pr` / `git_status_check` MCP tools with automatic sensitive file filtering
- **Guardrails L1** — 7 dangerous pattern detections + PII warning + `InputGuardrailMiddleware`, prevents destructive operations during unsupervised runs
- **Alembic database migration system** — Initial revision + dual-path init (fresh / existing database), migration history trackable
- **MCP debug logging system** — `~/.claude/data/ai-team-os/mcp-debug.log`, tool call chain observability

### Changed
- **Trap tool elimination** — `team_create` / `agent_register` description first line adds warning + `_warning` return value, prevents misuse
- **`task_id` auto-injection** — Subagent context automatically carries current task_id, no manual passing required
- **Enhanced task assignment** — `auto_assign` adds `completion_rate` + `trust_score` weighting, prioritizes reliable agents
- **`inject_subagent_context` environment variable unification** — Unified to `AITEAM_API_URL`

### Fixed
- `context_monitor` reads project-level monitor file (not outdated global file)
- Pre-existing `test_check_for_updates_no_git_repo_silent` fix

### Tests
- 28 cross-functional integration tests
- Total test count: 769 (up from 389)

## [0.9.0] — 2026-04-04

### Added
- **Prompt Registry** — Agent template version tracking + effectiveness statistics, 3 API endpoints + `prompt_version_list` / `prompt_effectiveness` MCP tools, linked with `failure_alchemy`
- **BM25 search upgrade** — Chinese bigram + English word tokenization replacing simple keyword matching, 3-5x search quality improvement, graceful degradation (`jieba` optional dependency)
- **Event log enhancement** — EventModel adds `entity_id` / `entity_type` / `state_snapshot` fields, automatic snapshot + entity filtering
- **Debate mode** — 4-round structured debate (Advocate -> Critic -> Response -> Judge) + `debate_start` / `debate_code_review` MCP tools + 2 debate role templates
- **3 Dashboard observability pages** — Pipeline visualization / Failure Analysis / Prompt Registry
- **Agent template auto-install** — `install.py` auto-installs to `~/.claude/agents/` (default opus model)
- **CC Marketplace submission** — Officially submitted to Anthropic Plugin Marketplace

### Changed
- **server.py modular split** — 3050-line monolith split into 57-line entry point + 14 tool modules + 2 base modules, significantly improved maintainability
- **Session startup optimization** — 15-25s reduced to 1-2s: parallelization + async git check + reduced retry count
- **workflow_reminder project isolation** — All API calls now include `X-Project-Id` header
- **install.py refactor** — Supports multiple hook groups/events, auto-sets `AGENT_TEAMS` environment variable and `effortLevel` recommended config
- **`_resolve_project_id` caching** — 5-minute TTL file cache, reduces HTTP calls from high-frequency hooks
- **inject_subagent_context environment variable unification** — `AI_TEAM_OS_API` renamed to `AITEAM_API_URL`
- **Test import path migration** — `plugin/hooks/` migrated to `aiteam.hooks` package imports

### Fixed
- workflow_reminder project-level task query missing `X-Project-Id` header (B1)
- TeamDelete PUT request missing `X-Project-Id` header (B2)
- Test file import paths broken (after plugin/hooks deletion)
- `context_monitor` path fix — reads project-level file instead of outdated global file
- statusline.py related deprecated tests cleaned up

### Removed
- **plugin/hooks/ dead code cleanup** — Deleted 11 obsolete `.py` / `.ps1` files, kept only `hooks.json` + `README`
- **Duplicate agent template cleanup** — Deleted old `meeting-facilitator.md` and `tech-lead.md` (25 reduced to 23 templates)
- **enforce_model hook removed** — Preserves user model selection flexibility
- **Model setting removed from install.py** — No longer forces model configuration on new users

## [0.8.0] — 2026-04-04

### Added
- **Cost tracking**: `tokens_input`/`tokens_output`/`cost_usd` on AgentActivity, `GET /api/analytics/token-costs`, `token_costs` MCP tool
- **Execution trace**: `GET /api/tasks/{id}/execution-trace` unified timeline (events + memos), `task_execution_trace` MCP tool
- **Agent live board**: `AgentLivePage` dashboard with status badges (busy/waiting/offline), 30s auto-refresh
- **Failure auto-diagnosis**: `FailureAlchemist.diagnose_failure()`, `POST /api/tasks/{id}/diagnose`, `diagnose_task_failure` MCP tool
- **Slack/webhook notifications**: `NotificationService`, EventBus auto-trigger, `GET/PUT/DELETE /api/settings/webhook`, `send_notification` MCP tool
- **Pipeline parallel execution**: `parallel_with` field, completion gate, 4 new parallel tests (28 total)
- **Execution replay engine**: `ReplayEngine` (get_replay + compare_executions), `task_replay`/`task_compare` MCP tools
- **Cost budget & alerts**: weekly budget limit ($50 default), 80% alert threshold, `GET /api/analytics/budget`, `budget_status` MCP tool
- **Leader Briefing page**: dual-layer tabs (project + status), project name badge, resolve/dismiss UI
- **79 MCP tools** (was 72)

### Fixed
- **P0 API process management**: PID file replaces file lock, `_is_api_healthy()` replaces `_is_port_open()`, stuck process 15s auto-kill
- **Universal project isolation**: `Repository._apply_project_filter()`, `X-Project-Id` header auto-injection from MCP
- **Session bootstrap**: uses cwd-matched project (not `projects[0]`)
- **Briefing list isolation**: uses scoped repository
- **context-monitor**: per-project file isolation (no more cross-session overwrite)

### Changed
- **Hook scripts**: `python -m aiteam.hooks.*` module invocation (no file paths)
- **Plugin hooks.json + .mcp.json**: unified python -m commands
- **install.py**: module-based hooks, `~/.mcp.json` for cross-project MCP

## [0.7.2] — 2026-04-02

### Added
- **MCP tools**: `project_update`, `project_delete`, `project_summary`, `task_subtasks`, `team_delete`, `briefing_dismiss` (72 total)
- **Dashboard project revamp**: status badge (active/inactive), expandable detail rows, wake settings tab
- **Project summary API**: `GET /api/projects/{id}/summary` — quick status + top tasks

### Changed
- **Project isolation redesigned**: removed per-project DB (dead code, -180 lines), unified `context_resolve()` with process-level cache
- **SQLite WAL mode**: enabled via engine event listener for multi-session concurrency
- **Disabled auto project registration**: SessionStart no longer creates projects automatically, prompts user to register via `project_create`
- **context_resolve()**: removed dangerous `projects[0]` fallback, returns None when no match

### Fixed
- Multi-session DB lock: SQLite `journal_mode=WAL` + `busy_timeout=10s` prevents concurrent write failures
- Data backfill: 272 orphan agents, 57 tasks, 72 meetings assigned to correct projects
- Garbage project cleanup: removed 6 auto-created projects, deduplicated quant project
- Dashboard `ProjectSwitcher` dropdown removed (was navigating to blank page)
- Wake agent `--output-format stream-json` error removed (incompatible with `-p` flag)
- Wake circuit breaker: only counts real failures (error/timeout), not skips

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
  - 7-layer security: array args, UUID validation, per-agent lock, global semaphore (max=2), circuit breaker, prompt/data XML separation, env cleanup
  - Triage pre-check: skip wake if agent has no actionable tasks (~70% skip rate)
  - Kill switch API: `PUT /wake-pause-all`, `PUT /wake-resume-all`
  - StateReaper integration (fire-and-forget + graceful shutdown)
  - allowedTools presets: safe (no Bash) / with_bash (explicit opt-in)
- **CronCreate session wake** — verified CC built-in cron for waking current session
- 20 unit tests for wake_manager (all passing)
- Wake session outcome tracking (completed/timeout/error/fused/skipped_triage)

### Fixed
- `context_resolve()` auto-project selection: match by cwd to root_path instead of blindly picking first project
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
- Self-contained install (hooks copied to ~/.claude/hooks/)
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
- Rule 7 task wall timer initialization
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
