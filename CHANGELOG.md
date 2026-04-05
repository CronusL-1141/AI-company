# Changelog

All notable changes to AI Team OS will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [1.1.0] — 2026-04-05

### Added
- **Agent 信任评分系统** — `trust_score` 字段 (0-1)，任务成功/失败自动调整，`auto_assign` 加权匹配
- **语义缓存层** — BM25 + Jaccard 相似度匹配，JSON 持久化，TTL 过期机制
- **工具分级定义** — CORE (15 个) vs ADVANCED (46 个) 分类

### Changed
- `TaskModel.status` 添加数据库索引
- `resolve_task_dependencies` 批量 IN 查询替换逐条查询（N+1 优化）
- `detect_dependency_cycle` 改为 BFS + 批量查询

## [1.0.0] — 2026-04-05

### Added
- **错误类型→恢复策略映射表** — `_api_call` 统一附加 `_recovery` 和 `_error_category`
- **文件锁/工作区隔离** — `acquire` / `release` / `check` / `list` + TTL=300s + hook 警告
- **Channel 通讯系统** — `team:` / `project:` / `global` 三种 channel + `@mention`
- **执行模式记忆** — 成功/失败模式记录 + BM25 检索 + subagent 上下文注入
- **Git 自动化工具** — `git_auto_commit` / `git_create_pr` / `git_status_check`
- **Guardrails L1** — 7 种危险模式检测 + PII 警告 + `InputGuardrailMiddleware`
- **Alembic 数据库迁移系统** — 初始 revision + 双路径 init
- **辩论模式** — 4 轮结构 (Advocate→Critic→Response→Judge) + `debate_start` / `debate_code_review`
- **3 个 Dashboard 可观测性页面** — Pipeline 可视化 / Failure Analysis / Prompt Registry
- **Agent 模板自动安装** — 安装到 `~/.claude/agents/`（默认 opus 模型）
- **MCP debug 日志系统** — `~/.claude/data/ai-team-os/mcp-debug.log`

### Changed
- 陷阱工具 (`team_create` / `agent_register`) description 第一行警告 + `_warning` 返回
- `task_id` 自动注入 subagent 上下文
- 增强任务分配 — `completion_rate` + `trust_score` 加权
- `task_list_project` 分页 — `limit` / `offset` / `include_completed` / `status` 参数
- `inject_subagent_context` 环境变量统一为 `AITEAM_API_URL`

### Fixed
- StateReaper 级联关闭活跃会议（检查近期消息后再关闭）
- `_read_pid_file` catches `SystemError` on Windows
- `context_monitor` 读取项目级 monitor 文件（非过时的全局文件）
- `trust.py` 错误响应改为 `HTTPException`
- `git_ops.py` 敏感文件过滤用 `basename`（避免误拦）
- `channels.py` 死代码删除
- 预存在的 `test_check_for_updates_no_git_repo_silent` 修复

### Tests
- 28 个跨功能集成测试
- 总测试数：769（从 389 增长）

## [0.9.0] — 2026-04-04

### Added
- **Prompt Registry** — Agent 模板版本追踪 + 效果统计（`prompt_version_list` / `prompt_effectiveness` MCP 工具）
- **BM25 搜索升级** — Chinese bigram + English word 分词替代简单关键词匹配，搜索质量提升 3-5x
- **事件日志增强** — EventModel 新增 `entity_id` / `entity_type` / `state_snapshot` 字段，支持状态变更追踪
- **CC Marketplace 提交** — 正式提交到 Anthropic 官方 Plugin Marketplace

### Changed
- **workflow_reminder 项目隔离** — 所有 API 调用添加 `X-Project-Id` header
- **install.py 重构** — 支持多 hook group/event、自动设置 `AGENT_TEAMS` 环境变量和 `effortLevel`
- **`_resolve_project_id` 缓存** — 5 分钟 TTL 文件缓存，减少高频 hook 的 HTTP 调用
- **inject_subagent_context 环境变量统一** — `AI_TEAM_OS_API` → `AITEAM_API_URL`
- **测试 import 路径迁移** — `plugin/hooks/` → `aiteam.hooks` 包导入

### Fixed
- workflow_reminder 项目级任务查询缺 `X-Project-Id` header（B1）
- TeamDelete PUT 请求缺 `X-Project-Id` header（B2）
- 测试文件 import 路径断裂（plugin/hooks 删除后）
- statusline.py 相关废弃测试清理

### Removed
- **plugin/hooks/ 死代码清理** — 删除 11 个过时 `.py` / `.ps1` 文件，只保留 `hooks.json` + `README`
- **重复 Agent 模板清理** — 删除 `meeting-facilitator.md` 和 `tech-lead.md` 旧版（25 → 23 个模板）
- **enforce_model hook 移除** — 保留用户模型选择灵活性
- **model 设置从 install.py 移除** — 不强制新用户模型配置

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
