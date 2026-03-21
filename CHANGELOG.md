# Changelog

All notable changes to AI Team OS will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- 6 new MCP tools: `project_list`, `meeting_list`, `team_close`, `task_list_project`, `agent_activity_query`, `meeting_update` (total: 55)
- Council meeting template (3-round multi-perspective expert review)
- Meeting auto-select: keyword matching across 8 templates
- `find_skill` MCP tool with 3-layer progressive loading (quick recommend / category browse / full detail)
- `task_update` MCP tool + `PUT /api/tasks/{id}` (partial update with auto timestamps)
- `GET /api/health` endpoint
- `auto_start` parameter for `task_create`
- 109 workflow_reminder unit tests + 34 find_skill tests + 27 API E2E tests + 31 dashboard E2E tests (total: 467+)
- Local agent blocking: all non-readonly agents must declare `team_name`/`name`

### Fixed

- BUG-001: S1 safety regex now catches uppercase `-R` flag (e.g. `rm -RF`)
- S1 heredoc false positive: strip heredoc blocks before scanning for destructive patterns
- B0.4: detect `name` param for implicit CC team context
- Rule 7: initialize taskwall timer on first call to prevent false stall detection
- Meeting cascade close when team is closed via `team_close`
- Meeting expiry reduced from 2h to 45min to prevent stale meeting accumulation
- B0.9: infrastructure MCP tools excluded from delegation counter

### Changed

- Session bootstrap Rule 4: updated template usage guidance
- Workflow reminder Rule 2: task wall check + template reminder + memo requirement
- Default team config: 4 roles (qa-engineer, bug-fixer, tech-writer, code-reviewer)
- Ecosystem guide restructured with Layer markers
- Agent template library expanded from 22 to 26 templates with recommendation engine
- Meeting template library expanded from 7 to 8 templates
