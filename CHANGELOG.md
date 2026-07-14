# Changelog

All notable changes to AI Team OS will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [1.10.2] - 2026-07-14

### Fixed

- **Server-side event-write governance** (`4f508bd`) - "first transition to completed/terminal" detection moved inside the upsert transaction (atomic NamedTuple return) plus a per-wf_id in-process lock, making workflow completion events exactly-once regardless of connection topology; partial unique index on `agents.cc_tool_use_id` with a leader-preserving dedup migration closes the concurrent duplicate-member-row window; SessionEnd no longer offlines still-running workflow sub-agents (exemption now symmetric with the team-level one).
- **Workflow member terminal-state badges** (`a74e6ae`) - completed workflow members render as "done" (green) driven by the workflow_agents projection state instead of a grey offline state; session-container teams keep their true offline history untouched.

### Changed

- **Model-tier orchestration charter** (`ef99f73`) - os-workflow skill gains §3 tier discipline: Fable orchestrates, Opus executes (workflow `agent()` defaults to explicit `model:'opus'`, top-difficulty verdict stages only inherit Fable); CLAUDE.md model-default clause clarified (DB observation field vs template frontmatter); usage aggregation added to the observability read path.

## [1.10.1] - 2026-07-14

### Fixed

- **Dashboard write-op governance** (`c1770f2`, `77028e3`) - removed three hallucination-source controls that wrote DB state with no real effect: add-agent buttons that faked a permanently-busy agent, the wake-config tab whose file had zero consumers, and manual team creation that produced permanent orphans (plus two dead meeting hooks). Remaining queue-style actions now surface the backend's honest "waiting for an agent to pick this up" hints, and "Execute" became "Add to task wall".
- **Session-container team display** (`eb4365b`) - kind=session teams get their own card semantics instead of borrowing the workflow template: "N active / M past" member counts, CEO name mapping when resolvable, completion timestamps without bogus swimlane links; legacy teams with empty `config.kind` are detected by name pattern.
- **Hook auto-write governance** (`d65cd9f`) - PostToolUse SendMessage keyword matching no longer auto-completes wall tasks (advisory only; a "report back when done" instruction used to mark the task completed), TeamDelete closes only the deleted team instead of every active team, retired-pipeline auto running/completed/advance writes downgraded to advisories, dead `_auto_advance` removed.
- **S4 teardown guard patch-id upgrade** (`d65cd9f`) - merge-base ancestry check now falls back to `git cherry` patch-equivalence: squash/rebase-landed worktrees are released automatically, mixed cases stay blocked with the non-equivalent commits named.

### Changed

- README (EN/zh) documents the v1.10.0 cross-session orchestration capabilities with usage guidance; ecosystem tool count corrected to 47 (`568b01f`).

## [1.10.0] - 2026-07-14

### Added - Cross-session orchestration (new capability)

- **Fleet downlink primitive** (`f0b859c`) - headless `claude -p --resume <session_id>` drives a target sibling session for one operational turn, reusing the existing wake machinery (semaphore, fuse, allowlist, per-session dedupe, full audit trail). A session can now observe and drive its siblings instead of only spawning fresh ones.
- **Wake system v2** (`626e248`) - `/api/wake/actionable` single-source predicate endpoint (watcher and guard consult the same criteria), SessionStart template moves from fixed 30-min cron to dynamic `/loop` intervals, turn-end guard (Stop hook, `decision:block`, user-stop keywords always pass), session-scoped event watcher with 1h hard timeout. No resident daemons.
- **agent_reuse_recommend MCP tool** (`e3ed728`) - three-way reuse decision (reuse / slim-then-reuse / spawn-new) scored by domain match, reachability (live / resumable / cross-session / expired) and context watermark. Tools 166 -> 167.
- **Sub-agent context watermark ledger** (`aae63ff`) - agents table columns + SubagentStop event capture + reaper backfill; exact token usage read from transcript tail, cheap-checks-first.
- **Observability cards** (`88bbf06`, `f3f8c01`) - fleet card (per-session CEO / model / in-flight tasks / context watermark), worktree card (branch ownership + unlanded-work status via on-demand `worktree_probe`), three-color context watermark bars on agent views.

### Added - Worktree isolation governance

- **S4 teardown guard** (`fa230da`) - blocks `git worktree remove` / `branch -D` / `rm -rf .claude/worktrees` when unlanded work exists (uncommitted or local-only commits); push-pending-but-merged work passes.
- Code-writing agent templates default to `isolation: worktree` (`7c9f3c2`); tool list responses gain compact projection with an escape hatch (`289a183`).

### Fixed

- **Fleet identity P1** (`5985f9e`) - `session_id` becomes a first-class identity key: one leader row per session bound at birth, cross-session leader drift eliminated, SessionEnd closes only teams owned by the ending session (closes two long-standing backlog items).
- **Injection-layer audit P0-P3** (`ae54ebb`) - dead pipeline block reconnected (revives task-memo and pattern injection that had never fired), two unthrottled nag reminders gain cooldowns, report-format boilerplate deduped.
- S4 landed-check compares against the local main branch instead of a stale `origin/*` ref (`ae43c86`); wf_id regex bounded so worktree instance suffixes are no longer swallowed into ghost team ids (`d95dfc6`).

## [1.9.0] — 2026-07-13

### Added — Memory System v2 (two-layer ledger + on-demand reconcile)

- **Episodic layer promoted to a real table** (`573c0b8`) — task memos moved from tasks.config JSON arrays to a dedicated `task_memos` table (row IDs / invalidation axis / quality score / scope_path); 123 legacy memos backfilled with zero loss, original JSON frozen as archive; `task_memo_add` unchanged, new optional `supersedes`.
- **Direction layer activated** (`c2ffd31`) — memories table now carries user preferences/corrections/design intent (4 kinds), MCP `memory_add`/`memory_invalidate`/`memory_list`; size guardrails (<=40 entries x 400 chars); resident injection via SessionStart + SubagentStart hooks (<=2000 chars, silent when API is down) — every spawned agent inherits team preferences at birth.
- **On-demand reconcile** (`0666cdf`) — `memory_reconcile_candidates` (zero-LLM BM25 clustering) + `memory_reconcile_apply` (merge/invalidate/score/promote, idempotent); "agent computes, tool persists" architecture; volume-threshold hints. Tools 161→166.
- Design doc `docs/memory-v2-design.md` checked in: three-track industry research (adversarially verified) + Kun Chen case-study content standards.

### Added — Progressive tool loading (P1 alwaysLoad rotation + P2 toolset gating/read-only + P3 template least-privilege)

- **P1 - session-startup alwaysLoad rotation** (`dc5d652`) - a handful of recently-hot tools get `_meta {"anthropic/alwaysLoad": true}` so CC skips ToolSearch for them. The whitelist is recomputed once per session start from `agent_activities` (7-day frequency + >=2-day span gate + 20% hysteresis, hard cap <=5). Purely additive: any stats-query failure silently degrades to all-defer. Backed by `GET /api/tools/always-load`.
- **P2 - `AITEAM_TOOLSETS` startup group switch** - 24 capability-domain toolsets gate which modules register at startup. `default` = task/team/memory/infra/reports (44 tools, hard cap <=50); incremental via `default,ecosystem`; unknown group names warn on stderr and are ignored (never blocks server start). No env / `all` keeps the full 166 for backward compatibility.
- **P2 - `AITEAM_READONLY=1` read-only profile** - an orthogonal overlay that strips every write tool (explicit `WRITE_TOOLS` allowlist, not name-pattern guessing) after registration while keeping all read tools. `os_restart_api` is hand-added despite its GET verb; analytical POSTs like `diagnose_task_failure` stay on the read side.
- **P3 - agent-template tool trimming** - first, conservative batch of CC subagent `disallowedTools` (structural denylist): meeting-facilitator / debate-advocate / debate-critic drop git-write + project/team delete + os_restart_api + the ecosystem write family (34 each); technical-writer adds `task_run`; project-manager drops only git-write + os_restart_api. All read tools and meeting/memo bookkeeping tools kept; engineering/testing templates untouched. Ecosystem write set sourced from `toolsets.py::WRITE_TOOLS`.
- Design doc `docs/tool-loading-design.md` checked in (three-track research + open-source cross-project convergence). Total tool count stays 166 - gating is a runtime env switch, not a registry change.

### Fixed

- All 4 major review findings fixed (`ca50607`): search-path invalidation filter / supersedes guardrail bypass / backfill dual-instance race (uuid5 + INSERT OR IGNORE) / hook-injection single-line sanitization (cross-agent prompt-injection surface closed).
- delete_project now cascades task_memos; stale Rule13/relevance_score tests zeroed — full suite 1710 passed / 0 failed (`40935fd`).
- Integration-test real-file pollution isolated: team-defaults.json redirected to tmp (conftest).

### Changed — Scheduler retirement

- Periodic cron replaced by on-demand `ecosystem_refresh` (`6bbdc58`; CC is not always-on): startup auto-registration removed, 4 dead crons purged; `POST /api/ecosystem/refresh` verified live (81 repos, 107s). install.py now registers the previously-missing deep_review_link/meeting_ecosystem_writeback hooks.

## [1.8.1] — 2026-07-10

### Added — Model governance (user-scoped, zero coercion)

- **Available models auto-discovered from file truth** (`899217c`) — `model_discovery.py` scans every local CC transcript tail and aggregates the models you actually used (109 files ≈1s + 60s cache; `<synthetic>` filtered; tier aliases tagged). Live scan surfaced `deepreasoning-coding-max-4.7` — a third-party model no hardcoded list would ever contain.
- **Default startup model writes `~/.claude/settings.json`** — the `model` key, effective for new CC sessions. Three-layer write guard: touch only that key / `.bak-aiteam` backup / atomic tmp+rename; corrupt settings are refused untouched.
- **REST `/api/models/*` + MCP `model_config_get`/`model_config_set`** (tool count 158→160); Settings page gains a "Model Governance" card (default-model picker + discovered-model table).
- **Soft hint only, never a block** — registering an agent with a model outside the discovered list attaches a hint to the response and proceeds. ultracode/Workflow agents are entirely exempt: model choice belongs to CC's orchestrator (user ruling, 2026-07-10).
- **Pre-push preflight script** (`a00973e`) — catches red CI locally before commit leaves the machine.

### Fixed

- **ModelSelect degraded to a bare input** when the current value (e.g. the `fable` alias) wasn't in the discovered list — now always renders the dropdown with the current value pinned on top (`91180ab`).
- **Legacy "Save (Demo)" buttons** in General/Ports settings were never wired — now disabled with an explanatory tooltip.
- **Last hardcoded model defaults removed**: `DefaultsConfig.model = "claude-opus-4-7"` (the 4-7 ghost's final nest) and permanent-member default `claude-sonnet-4-6` — empty string now means "inherit the default startup model", zero maintenance across model generations.
- **CI three-red to green** (`25f85f9`) — ruff fully clean, 108 failing unit tests to zero, eslint 0 errors.
- Task-wall reminder now recommends `task_list_project` when no active team exists (`taskwall_view` requires one).

### Changed

- **Template flexibility made explicit** (`b27421e`) — `team_setup_guide` tips and the SessionStart briefing now state the three freedom paths: `general-purpose` + custom prompt (zero-template teams), rosters trimmed per task, and `plugin/agents/*.md` files editable at will. Templates are starting points, never requirements.

## [1.8.0] — 2026-07-10

> First full-featured public release since v1.6.2 — the entire v1.7.0 line (previously private-only) plus everything below now ships to the public repository.

### Added — Knowledge layer P1: reference graph + unified search

- **Cross-domain reference graph (P1a)** (`c0f49e8`) — a zero-LLM regex extractor mines OS-native ID references (`wf_` id / commit hash / task uuid / `[[memory]]`) out of task memos and reports into an append-only `knowledge_links` table (UNIQUE 5-tuple dedup). The graph is a derived view — rebuildable from source text at any time; a backfill script covers historical data.
- **Unified search (P1b)** (`6ba4a5f`) — `/api/search` fuses three arms via RRF (k=60): BM25 full-text (Chinese bigram native), knowledge-graph fanout (an ID query pulls in everything linked to it), and exact ID-prefix / title match. Global search box in the Dashboard header; MCP tools `unified_search` / `link_query` / `link_trace`.

### Added — Governance

- **Red-line invariant checker** (`fe0d843`) — `scripts/check_invariants.sh` machine-checks five incident-derived invariants (hook dual-copy sync, 5-place version lockstep, dual-dist consistency, dist freshness, venv ban) with a CI fast-fail job.

### Security

- **InputGuardrail large-payload bypass closed** (`2a5fd46`, public issue #1) — request bodies over 16 KB used to skip the L1 guardrail entirely, so padding a malicious payload past 16 KB bypassed all checks. Bodies are now fully inspected up to a 2 MB hard cap (413 beyond it); dangerous-pattern rules scan the full input (the 10 KB per-string truncation window is gone); and the XSS rule was de-ReDoS'd (`<script[^>]*>` → `<script\b` — the former backtracked O(n²), ~113 s on a 2 MB flood). 15 regression tests added.

### Fixed

- **Attribution iron law** (`bb78aa0`) — a session's startup directory is the sole attribution authority: subdirectories no longer spawn phantom projects, auto-registration is removed, receipt migration gains negative-exclusion + orphan reaping, and SessionEnd no longer kills workflow teams mid-run.
- **Per-run team creation moved to receipt time** (`3a31d8b`) — runs killed mid-flight (or with very long turns) no longer go invisible on the project page.
- **`project_delete` cascade 500** (`16dd004`) — the cascade referenced a nonexistent `EventModel.team_id`, so every delete returned 500.
- **Project-page run summaries restored** (`f97be00`) — request limit 500→200; a silently swallowed 422 had blanked the entire inline-summary feature.

### Internal

- Daily traffic-archiving workflow (`7dd9d95`) — runs in the private repository only (repository-guarded); public-release sanitation pass (`7b195c7`).

## [1.7.0] — 2026-07-07

> Version 1.6.2 was an internal transition number (5-place version lockstep, never tagged or released); its content ships here as part of 1.7.0.

### Added — Workflow observability Phase 2: live tracking + phase swimlane

- **Journal incremental tail** (`32becb5`) — byte-offset tail of `journal.jsonl` (consumes only up to the last complete line), `transcript_dir` direct addressing persisted from the Workflow receipt, run-time `live_tokens` / `last_activity_at` approximations (terminal file values overwrite on completion), conservative 900s `interrupted` detection, `mtime_ns:size` fingerprint short-circuit for cheap reconcile.
- **Phase swimlane UI** on the `/workflows` detail page — per-agent bars grouped by phase, live polling while running, sortable per-agent telemetry table.
- **Receipt / adoption hardening** (`10abd20`) — early-arriving workflow agents are adopted by a per-session fallback team (`workflow-session-<sid>`) and migrated to the per-run team once `wf_id` becomes visible; subagent model no longer falls back to a wrong hardcoded default.
- **Nested-workflow layout support** — runs launched from inside a subagent land under `<session>/subagents/workflows/<wf_id>/`; live tail and terminal reconcile both resolve this layout (agent labels backfill automatically at terminal state; the running-window label gap is a CC on-disk timing limit, mitigation tracked on the task wall).

### Added — Leader identity from file truth (zero registration dependency)

- **`session_probe` module** (`d75b3de`) — a project's Leader *is* the newest CC main-session under `~/.claude/projects/<slug>/`: file mtime = liveness (15-min window), transcript tail = current real model (`/model` switches surface within one refresh; compact's synthetic rows with `model:"<synthetic>"` are skipped, `05f4092`). `project_summary` returns a disk-probed `leader` block; the Dashboard LeaderCard no longer walks the team chain (which broke whenever the Leader row parasitized a workflow team that migrated projects).
- **Leader liveness** (`44c7f91`, `1ab5c37`) — in-stream tool events revive an offline Leader to busy (60s-throttled touch); the state reaper exempts `role=leader` and `workflow-subagent` from config-probe liveness (it previously exempted only the literal name "team-lead" and reaped every revived "Leader" row each tick).
- **Per-turn model refresh** (`9327038`) — the Stop hook tail-reads the transcript every turn and updates the Leader's model; the silent `import json` omission that swallowed this for two commit cycles is fixed with an offline-repro regression test (`44c7f91`).

### Added — Dashboard ultracode revamp

- **Workflow-name-first display** (`f01c590`) — run name is the primary title everywhere, the `wf_` id demoted to a mono faded tag; run list sorted by `COALESCE(started_at, created_at)` (historical backfill had buried the newest runs); the `/pipelines` display layer retired with a redirect to `/workflows`.
- **Project detail, inline run summaries (Plan A)** (`87aecd3`, `daa2df0`) — workflow team rows in both the active and history sections carry an inline run summary (status badge in swimlane colors, agent count, duration, finish time) plus a "view swimlane →" deep link; active workflow teams show `run.summary` as a subtitle and members display observability phase labels (`audit:…`) with the `wf-<ccid>` id demoted to small mono text.
- **Leader card & session counts** (`3403e0f`, `20fff24`) — model name displayed verbatim (no alias mapping; future-proof for non-Claude models), per-project session count from file truth, explanatory empty-state card instead of hiding, stale legacy project registration cleaned after the disk migration.

### Changed

- **D5 dual-axis convergence** (`618e176`) — `stage_status` is the single authority, `status` becomes a derived read-only projection, claim windows closed via atomic DB claims, idempotent backfill.
- **Pipeline retirement Phases 1–3** (`8fc3e2d`, `f01c590`) — new-entry hard blocks removed, auto-advance stopped, display layer superseded by `/workflows`; OS is now the persistence / observability layer for CC ultracode.
- **Governance lease** (`00c861b`) — reaper + watchdog share a single-row `governance_lease` (fail-open) so only one process governs at a time; kill paths verify process identity before acting.

### Fixed

- **Cross-project workflow attribution** (`86c6900`, `44c7f91`) — attribution now follows file truth: a run's on-disk slug is matched against registered projects' `_project_slug(root_path)` (char-for-char identical to CC's mapping); teams follow their runs; orphan-team cwd adoption excludes workflow teams. 59 mis-filed runs + 5 teams migrated back to their real project; unmatched runs stay unattributed instead of guessing.
- **The `claude-opus-4-7` ghost model** (`391a866`, `364060d`) — baked-in defaults removed at all four layers (types default / ORM column default / `to_pydantic` read-injection / MCP tool param). The read-injection was the real culprit — it re-materialized the ghost after every data cleanup while the DB was already clean.
- **Defunct-zombie false alarm on restart** (`d6d6ed5`) — graceful shutdown followed by a zombie PID no longer reports `shutdown_timeout`; liveness recognizes `ZOMBIE` via psutil status + `ps` state prefix.
- **fastmcp 3.4.3 startup crash behind SOCKS proxies** (`56733f5`) — the PyPI update check is disabled (`check_for_updates="off"`); a SOCKS proxy without `socksio` used to kill stdio with `-32000` on reconnect. Update detection now uses `git merge-base --is-ancestor` (strictly-behind), ending false "new version" reports when local is ahead.
- **`os_restart_api` child stderr now persists** (`9d8f020`) — restart-spawned API processes wrote stderr to a tmpdir file (lost on reboot, invisible to the self-check loop); unified into the persistent `api-stderr.log`.
- **Bootstrap no longer suggests starting a second uvicorn** (`f8da12b`); the autostart skip-test no longer hardcodes version `1.3.4` (`677c557`).

### Added — Workflow governance (follow CC ultracode)

OS repositions itself as **CC's persistence-and-governance layer** and stops competing with CC's built-in Workflow for in-session orchestration.

- **Workflow auto-tracked as a team + recognized as delegation** (`abec404`) — `hook_translator._on_subagent_start` gains a `workflow-subagent` branch: strict **one workflow = one team** (`workflow-<wf_id>`, keyed off `wf_<id>` in the transcript path), members de-duplicated by `cc_agent_id` (fixes 16 agents collapsing into 1 row), team auto-created without requiring a pre-existing active team (fixes 0-row registration), bound to the Leader's project. `Workflow` is added to `_DELEGATION_TOOLS` so calling a Workflow counts as delegation and resets the B0.9 "why aren't you delegating" counter; PreToolUse / PostToolUse matchers now include `Workflow`.
- **Workflow writeback governance** (`29eab2b`) — `workflow_reminder` adds a throttled (300s) soft reminder on `Workflow` calls: put the umbrella task on the wall (`task_create`) and instruct each workflow agent to write back through OS tools (`task_memo` / `report_save`), pointing at the new `/os-workflow` skill (`plugin/skills/os-workflow`). A "Workflow" badge is added to `workflow-*` teams on the Dashboard TeamsPage; the `CLAUDE.md` "using CC Workflow" section codifies the convention.
- **Strict one-workflow-one-team (per-run) + Step 4 plan pre-registration** (`aac902f`) — `_promote_workflow_team` migrates agents from the `workflow-session-<sid>` fallback team to the per-run `workflow-<wf_id>` team as soon as `wf_id` becomes visible (in `SubagentStop` / the agent's own tool calls); `_parse_workflow_plan` statically extracts declared phases + agent counts from `tool_input.script` and emits a `workflow.planned` pre-announcement event.

### Fixed

- **Teams list vanished on the project detail page**: `apiFetch` previously injected the `X-Project-Id` header on *every* request, so a stale global project pin in `localStorage` also scoped `/api/teams` to a single project. ProjectDetailPage fetches all teams and filters them client-side (`project_id === projectId`), so the scoped response matched nothing and both active and history teams disappeared for every project. The `X-Project-Id` / `X-Project-Dir` headers are now attached **only to `/api/ecosystem` requests**, so all other endpoints are never affected by the project scope.
- **New-project Leader stuck showing "idle"** (`dfe5f67`) — `project_id` used to be bound only at SessionStart, so a Leader created before its project was registered (or when the cwd didn't match) stayed `project_id=None`; every later tool call refreshed `last_active_at` (always "active") but never backfilled the project, so liveness never saw it. Fix: the **server-side `PreToolUse` hook** now resolves the cwd via a longest-prefix-match helper and rebinds any `project_id=None` Leader on its next tool call — self-healing that no longer depends on SessionStart timing.
- **SessionStart project resolution now uses longest-prefix match** (`b3f7cb6`) — a cwd can prefix-match several projects at once; the old first-match bound the Leader to the wider parent project. It now uses the same longest-`root_path` match as the team-mapping fallback, and rebinds whenever the resolved project differs from the current binding.
- **SessionStart reuse backfills a missing `project_id`** (`c7b4c6e`) — reusing an existing session Leader that was born without a project affiliation now backfills the project when the cwd resolves one (7 orphan Leader rows had accumulated, causing projects to read as idle).

### Changed

- **Ecosystem project filter moved to where it belongs**: the project dropdown is really a "view this project's ecosystem library" filter, but it had been mounted as a global Header switcher (`components/layout/ProjectSwitcher`) and commented as "Global project context" in `client.ts`, so it read as a global app switcher. The global Header switcher is removed; the component is renamed `EcosystemProjectFilter`, moved to `components/ecosystem/`, and shown only on the `/ecosystem` list page. Comments now record the history and a "do not globalize" guard to prevent regression.
- **Project detail header layout**: the description now spans the full width (best readability for long descriptions), and active-teams / history-teams / created-date are packed into one compact row instead of four equal columns that squeezed the description.

### Documentation

- **Dev-machine migration guide (Windows → Mac / VS Code)** (`7828e7b`) — new `docs/` guide (force-tracked, same pattern as `ecosystem-recipes.md`): cross-platform status (source code carries no version problem) + the three things that do **not** travel with git (unpushed commits / the `aiteam.db` database / `.mcp.json` · `.claude` machine-local config) + full Mac install steps including the DB copy command.

### Added — Workflow observability layer MVP (CC ultracode)

The self-built pipeline is deliberately deprecated (it duplicated CC's built-in orchestration); OS becomes the **persistent observability layer** for CC ultracode/Workflow. Hooks only supply timing + correlation anchors; the on-disk `wf_<id>.json` rich snapshot is the telemetry source of truth. Completion detection = a reaper safety-net poll reconciled against hook traffic, idempotent ingest, offline-gap self-healing. _(This batch folds into 1.6.2 or 1.7.0 at the author's discretion — see Notes.)_

- **New `workflow_runs` / `workflow_agents` tables + repository CRUD** (`6f9ceb4`) — rebuildable caches of the immutable snapshot files, UPSERT by natural key with monotonic progress (rows are never deleted; the audit trail stays in the `events` table). New `WorkflowRun` / `WorkflowAgent` models; orphan `TeamState` removed along the way.
- **`workflow.planned` / `workflow.started` / `workflow.completed` event types** — adding `planned` to `EventType` fixes the "Step 4: 0 valid data" root cause (it wasn't in the enum, so `create_event` raised a swallowed `ValueError`).
- **Ingestion + reconcile** (`workflow_ingest.py`) — receipt parsing / rich-snapshot ingest / reconcile (re-reads only when `mtime` beats `updated_at`, so steady state costs just a `stat`; a `resume` that rewrites the same file naturally re-triggers ingest). `hook_translator` gains a `PostToolUse(Workflow)` receipt-anchor branch + a SessionStart reconcile (with an `mtime` short-circuit for the full pass); `state_reaper` polls as a safety net (zero `stat` when nothing is running).
- **4 REST endpoints `/api/workflows` + 3 MCP tools** (`workflow_list` / `workflow_get` / `workflow_reconcile`) — brings the MCP tool total to **155**.
- **Dashboard `/workflows` page** — list card-stream + per-agent telemetry detail page, sidebar entry, bilingual i18n, real-time invalidation on `workflow.*` events, and the TeamsPage workflow badge is now a clickable link; both `dist` copies synced.
- **`killed` / `failed` terminal states** in `_WF_STATUS_RANK` — 10% of 69 real wf files hit these; missing them would pin a run at `running` forever and break the reaper short-circuit. Frontend status union / badge / filter synced.

### Changed

- **Pure-Python BM25 wired into the main retrieval chain** (`15e4fe3`) — `retriever.py` gets a built-in BM25 (TF saturation + non-negative IDF + length normalization, keeping the Chinese bigram + single-char tokenizer), replacing the optional `rank_bm25` dependency path (`keyword_search` kept as a fallback). `search_memories` moves from whole-string SQL `ilike` to "recent-window coarse recall within scope → BM25 rerank", so non-contiguous multi-word queries (e.g. `Python deploy`) now hit where the old implementation always missed.
- **LangGraph downgraded to an optional `[langgraph]` extra** (`f6b3140`) — `langgraph` / `langchain-anthropic` / `langchain-core` leave the core deps; they only serve the legacy graph-execution path of the CLI `aiteam task run` (CC Agent took over execution after `456512f`), and API / MCP / Dashboard never touch them. `team_manager.compile_graph` is lazy-loaded at runtime with a `pip install 'ai-team-os[langgraph]'` hint when the deps are missing.
- **Version lockstep to 1.6.2 across 5 places + backfilled historical CHANGELOG** (`7be8cd8`) — `__init__` / `pyproject` / `plugin.json` / two marketplace entries unified to 1.6.2 (previously diverged across 9 spots, 0.0.0–1.6.1, and 1.6.x was never tagged); `pyproject` gains pytest `pythonpath=['src']` (src-layout, no editable install needed for CI); CHANGELOG backfilled with 1.5.2 / 1.6.0 / 1.6.1 in both languages. `tag v1.6.2` left for the author to cut at release.
- **CI real gate restored** (`e2d725f`) — dropped the `2>&1 || true` on the test step (a firefighting leftover that stayed green even when pytest failed with exit code 4 for not importing `aiteam` — 0 tests actually ran); deps add `typer` / `rich` / `alembic`; importability comes from `pyproject pythonpath=['src']` (deliberately not `pip install -e .`, a historical red line).

### Fixed

- **Plugin manifest interpreter unified to `python3` + first-launch self-heal to `sys.executable`** (`715acc8`) — `plugin/hooks/hooks.json` (22 commands) and `plugin/.mcp.json` switch from bare `python` to `python3` (stock macOS has no `python` shim, otherwise MCP + every hook is `command-not-found`); `auto_install._self_heal_interpreter()` rewrites the manifest interpreter to the absolute `sys.executable` on first launch (idempotent / silent-fail), landing the `e2d0fbb` invariant on the static distribution path and resolving both the missing-shim and project-`.venv`-hijack dilemmas. `src/aiteam/hooks/install.py` likewise generates hook commands with `sys.executable` + absolute script paths.
- **Cross-project guard backfilled to the plugin copy** (`715acc8`) — `workflow_reminder.py` (the plugin execution copy) regains `_check_team_cross_project`; the v1.5.2 fix for the 2026-05-08 shallow-scan leak had until now only existed in the never-distributed `src` copy.
- **Dashboard artifact governance** (`fe5b682`) — the Dashboard is rebuilt and `plugin/dashboard-dist` re-synced (carrying the `7550f33` project-isolation fix that had been stuck at `29eab2b`); `dashboard/dist` is removed from the git index (a force-added partial snapshot — only `index.html` + fonts, no JS/CSS — was masking the complete `plugin/dashboard-dist` and white-screening the source-install path); `app.py` candidate-dir discovery now skips a directory missing its JS bundle; version references `aiteam.__version__` to kill OpenAPI drift; SettingsPage shows v1.6.2.
- **`/api/tasks/compare` route reachable again** (`2606297`) — since `082a0e7` it had been shadowed by the `{task_id}` param route (always 404); it is moved ahead of the param route (with a guard comment), and the `task_compare` MCP chain works again.
- **Self-started API `stderr` written to a file to prevent pipe deadlock** (`2606297`) — `_autostart`'s `stderr=PIPE` was never drained, so accumulated tracebacks would freeze the whole API once the ~64KB buffer filled; now appended to `~/.claude/data/ai-team-os/api-stderr.log` (premature-exit diagnosis tail-reads it); `stdout=DEVNULL` and the command array unchanged.
- **Installer self-check no longer fails falsely** (`b7e225b`) — `verify_installation` reads `~/.claude.json` (where `register_global_mcp` actually writes, after the CC read-location migration in `a050585`); `_check_package` uses `find_spec('aiteam')` (not `pip show aiteam`, which always failed on the `ai-team-os` dist name); the `_write_project_mcp_json` fallback writes `sys.executable`; `scripts/install.py` `project_root` fixed to the flat repo layout.
- **`greenlet>=3.0` added to core deps (Apple Silicon)** (`f6b3140`) — required by SQLAlchemy async, but its platform markers omit Apple Silicon macOS (`platform_machine=='arm64'`), so a missing greenlet made the async engine `ValueError` on first connect (hit during the real Mac migration).

### Removed

- **`semantic_cache` feature deleted** (`15e4fe3`) — `api/semantic_cache.py` + `routes/cache.py` + `mcp/tools/cache.py` + 30 tests removed; a ghost feature never wired since birth, whose `/api/cache/stats` always returned 0 and misled users. The bilingual README entry advertising semantic cache is dropped.

### Notes

- All the entries above ship together as **1.7.0**. Observability-layer integration tests 27/27; autostart + MCP suites 84/84; `tsc` + production build green; attribution / liveness / label-backfill all verified against live runs.

## [1.6.1] — 2026-06-12

### Added

- **Multi-source schema scaffolding** (`e1b3ddd`) — `ecosystem_repo_profiles` gains `sources` (JSON list) + `primary_source` (kept in sync across COLUMNS_TO_ENSURE + ORM + Pydantic); 678 GitHub profiles backfilled with `sources=[{kind:github,…}]`. A new `ecosystem_hf_fetcher.py` (HuggingFace Spaces public API) is archived for future reference only — the PoC `dry_run` measured 0% overlap with the Claude/agent/MCP ecosystem, so it is **not** wired into the main flow.
- **Per-project weekly cron automation** (`4e317f2`) — `deps.py` idempotently registers a weekly refresh cron per project (`interval = refresh_interval_days × 86400`, `action=emit_event`, `event=ecosystem.refresh.periodic`); 5 projects auto-provisioned with `next_run` 7 days out.
- **`os_restart_api` standardized restart tool + graceful shutdown** (`896d5b9`) — MCP `os_restart_api(force)`: refuses while agents are busy, **pins the port so it never drifts**, waits until the old process is fully dead, then health-verifies and returns old/new version + PID. New `POST /api/system/shutdown` self-exits after a 0.5s delay with a best-effort WAL checkpoint. Both wait loops get a hard 200-iteration cap so a frozen/mocked clock still terminates. This is a deliberate **fixed-port** restart — distinct from `_autostart`'s free-port auto-discovery (see the stdio-decoupling fix below).
- **Ecosystem Phase 2 — shallow-scan batch approval gate** (`c74d53b`) — new `ecosystem_shallow_batches` table + 6 endpoints (create / list / detail / items / **approve** / **cancel**); nothing is enqueued before approval (governance gate). New batch-management pages + `metadata_changed_count` on `ScanRun`.
- **Project status recognizes an online CC session** (`4370468`) — status is derived from real agent activity time and surfaced in the project dropdown.

### Changed

- **Default model `claude-opus-4-6` → `claude-opus-4-7`** (`5a0f9a2`) — 10 backend defaults + 4 test assertions refreshed; member-edit Select refreshed to Opus 4.7 / Sonnet 4.6 / Haiku 4.5; removed the non-functional "default LLM model" settings dropdown (its `handleSave` never persisted).
- **Deprecated heuristic fields removed from the wire** (`e1b3ddd`) — the API no longer returns `relevance_category` / `relevance_score`; the frontend drops "relevance X/10" and "active-set rank". Schema columns are kept (reads/writes stopped, no migration risk).
- **Project status wording** — "关闭" → "空闲" (Inactive → Idle) (`6cb4914`).

### Fixed

- **Old-repo rescan + tick budget + status API semantics** (`4e317f2`) — the shallow-queue candidate check now uses `pushed_at > last_shallow_refreshed_at`; the skip decision uses `stage_status` instead of `status` (678 `shallow_done` rows had been wrongly skipped); `tick` dispatches all candidates at once (concurrency enforced at worker-claim, no more 15-item budget); `queue_status` reports accurate `pending / in_progress / done / failed`. Verified `tick dispatched=134 / skipped=0`.
- **`context_tracker` default window is now 1M** (`af495c7`) — CC transcripts strip the `[1m]` suffix, so on a fresh machine with no 1M history the fallback used 200K and reported 17% real usage as 85.3%, firing constant false CONTEXT WARNINGs. `DEFAULT_CONTEXT_SIZE` 200K → 1M (the `>200K` fallback branch removed); the `CLAUDE_CONTEXT_SIZE` env override is retained.
- **MCP `X-Project-Dir` latin-1 header encoding** (`af495c7`) — a Chinese project path (e.g. `AI团队框架`) broke urllib's latin-1 header encoding, making `report_save` / `team_list` unusable in Chinese environments; the value is now percent-encoded on send (`urllib.parse.quote`) and decoded on receive.
- **`os_restart_api` spawn fully decoupled from MCP stdio** (`3e7d320`) — spawning from an MCP tool-call context inherited the busy MCP stdio pipe and hung **before import** (9MB, never progressing). Fixed with `stdin=DEVNULL`, `stderr` → `%TEMP%/aiteam-api-restart.log` (keeps diagnosability), `close_fds`, and `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows.
- **Project-liveness clock aligned to naive-local-time** (`cfa6861`) — agent timestamps are written naive-local everywhere (`datetime.now()` in `hook_translator` / `StateReaper`), but liveness compared against UTC; the constant local-UTC offset (measured 4h) meant the 15-min threshold was never met and projects showed "idle" forever. Comparison and serialization now use the same local clock.

### Notes

- `__init__.py` bumped to **1.6.1** in this cycle (`896d5b9`). GitHub-only development milestone (continuing the v1.4.0 / v1.5.0 pattern); not published to PyPI/marketplace.

## [1.6.0] — 2026-05-13

### Added — Ecosystem unified-design MVP (config-driven, multi-source framework)

Rebuilt the ecosystem scan around a **config-driven unified design** (v4). A user starts a scan by answering ~3 questions (data source / topics / default params), previews safely via `dry_run`, and gets an auto-generated diff report with a status-change timeline. This all stays inside the **single-DB `project_scope`** model — ecosystem tables are scoped by `project_id`, not split into per-project databases.

- **Config-driven schema (P0)** — new `DataSourceKind` (8 source kinds) / `RepoActiveStatus` / `NormalizedSignal` / `DataSource` / `ScanProfile` / `EcosystemIndexDiff` / `EcosystemStatusChange` types; `ecosystem_repo_profiles` +6 fields (`canonical_id` / `source_kind` / `last_active_status` / `last_status_change_at` / `popularity_percentile` / `activity_score`, with idempotent backfill preserving all 265 repos); 4 new tables (`ecosystem_data_sources` / `scan_profiles` / `index_diffs` / `status_changes`).
- **9 new REST endpoints + 5 new MCP tools** — data-source CRUD, `scan_profile` GET/PUT, `quick_setup`, `index_update` (real `dry_run`, no side effects), `index_diffs` latest/history; MCP `quick_setup` / `data_source_create` / `scan_profile_update` / `index_update` / `index_diff_latest`.
- **Simplified activation (P1.A)** — stars are only an **ingestion gate**; once ingested a repo participates in search forever. Activation reduces to: archived → `archived`, `manual_status='no_value'` → `manual_archived`, else `active`; repos not seen in a scan keep their status (append-only). New `manual_status` column + `POST /repos/{id}/manual_status` + `ecosystem_mark_no_value` / `clear_manual_status`.
- **Layered `list` endpoint (P1.B)** — summary serializer (18 fields, excludes `shallow_summary`), `limit` default 20 / max 100 + `has_more`, to prevent token blow-ups; `/teams/{id}/agents` capped at max 200 (guards against the 254-agent history incident).
- **Event sourcing** — new `ecosystem_repo_events` table becomes the single source of truth per repo (discovered / topics_changed / stars_jumped / status_changed / …); period diffs are computed dynamically via `GET /api/ecosystem/diff?from=&to=`. New `ecosystem_repo_events` / `ecosystem_diff_period` MCP tools + an "event history" detail tab. Legacy `index_diffs` / `status_changes` tables and endpoints kept for compatibility.
- **Real GitHub topics + global `topicRankMap`** — the fetcher was upgraded from query-hint topics to real `repositoryTopics` (per-repo `gh api repos/{owner}/{repo}`); `compute_ecosystem_facet_counts` adds a global `topics` facet (678 repos → 2425 unique topics; top: mcp 386 / claude-code 294 / ai 257 / claude 185 / …). A global `topicRankMap` gives each topic a stable color across every card and the stats bar.
- **SST — single source of truth for field mapping** — removed client-side `inferStage()` and `relevance_category` derivations; backend `_build_stage_map` / `_serialize_full` is authoritative. Deprecated `is_active` / `active_rank` / `relevance_*` (kept for compat).
- **Ingested 678 repos** — a real `dry_run=False` run grew the DB 265 → 678 (+413) and refreshed 252 existing repos' topics (e.g. `n8n-io/n8n`: `['mcp']` → 15 real topics); default `popularity_floor` lowered (github 5000 → 1000, huggingface 1000 → 200, npm / pypi 5000 → 1000) for broader inclusion.

### Fixed

- **`GET /api/ecosystem/diff` with `from > to`** now returns HTTP 400 instead of a misleading empty 200 (`4f1926e`).
- Frontend "failed to load ecosystem profile" + pagination UI; scan-history split into shallow vs deep; empty-placeholder filtering; the removal of the heuristic `lifecycle` tab and category badges from `RepoCard`.

### Notes

- `__init__.py` bumped to **1.6.0** (`355aae8`). GitHub-only development milestone (continuing the v1.4.0 / v1.5.0 pattern); not published to PyPI/marketplace.

## [1.5.2] — 2026-05-11

### Fixed — Cross-project isolation hotfix

Triggered by a real incident: on **2026-05-08 five shallow-summary agents** dispatched for the ecosystem platform were mis-routed into `topic-mapping-v8` (a separate quant exam-prep project) and endorsed / wrote research there, because the Leader's active-team resolution was not project-aware.

- **`context_resolve` is now project-aware** (`infra.py`) — it resolves the project by cwd first and only then filters teams, so a session can no longer pick up another project's active team.
- **New cross-project guard on `PreToolUse:Agent`** (`src/aiteam/hooks/workflow_reminder.py`) — when a dispatched team's `project_id` ≠ the current project, the hook hard-blocks with `exit 2`.
- **Stage 0 dual-channel writeback** — shallow-summary results write back via MCP primary + SendMessage fallback, so a flaky MCP tool surface no longer loses the whole batch.
- **`research_count` semantics fixed** — `_build_stage_map` counts only `architecture_done`+ rows, so a shallow summary no longer inflates the deep-review count; `apply_summary` no longer writes `status='completed'` (shallow done ≠ whole review done).
- **Ecosystem UI refactor** — "深扫摘要 / deep-scan summary" renamed to "评审记录 / review record" (the `ecosystem_deep_reviews` table carries all 4 stages); `ReviewCard` renders by `stage_status`; a `parseAsUtc` helper fixes a naive-datetime timezone bug in the elapsed-time display; new `RecentScanRunsBar`.

**Known issue**: this cross-project guard was added to `src/aiteam/hooks/workflow_reminder.py` only — the `plugin/hooks/` distribution copy does **not** yet carry `_check_team_cross_project`, so plugin users do not get the hard-block until that copy is backfilled.

## [1.5.0] — 2026-05-08

### Added — Ecosystem Research Platform v2: Progressive Deep-Review Funnel

Refactored the v1.4.0 "single-pass 5-section deep-review" into a **4-stage progressive knowledge-base funnel**, making ecosystem repos accumulate research findings over time instead of one-shot reports. Designed per user spec to prevent token waste on low-value repos and to enable knowledge recall across research cycles.

- **Stage 0 — Auto shallow-summary on archive (Stage A/B)**
  - New `EcosystemShallowQueueWorker` (552 LOC) auto-dispatches `ai-engineer` agents (5 concurrent) to summarise newly-archived repos
  - Each shallow summary captures: core function / positioning / advantages / use case (200-400 chars)
  - Worker runs every 5 min, returns `DispatchIntent[]` (no direct agent spawn — Leader uses Agent tool with `team_name=ecosystem-platform`)
  - 8-class failure handling: `404→mark_deleted` / `403_private→mark_private` / `rate_limit→backoff+retry` / `5xx→exp_retry` / `agent_read→retry+swap` / `agent_timeout` / `json_parse→retry+example` / `fetch_style→pattern_record`
  - **Self-learning mechanism**: same `fetch_style` failure ≥ 3 distinct repos → auto `pattern_record(type='failure')`, future agents read lessons via `pattern_search`

- **Stage 1 — On-demand architecture analysis (Stage C)**
  - New `ecosystem_deep_review_request_batch(tags, min_stars, limit)` MCP tool
  - User picks research direction (e.g., "memory_system") → batch-dispatches `backend-architect` to read architecture key files
  - Output: `architecture_md` field + `stage_status='architecture_done'`

- **Stage 2 — Multi-perspective debate (Stage C)**
  - New `ecosystem_trigger_debate(repo_ids, research_goal)` triggers existing `debate_start` (NOT a built-in debate engine — reuses meeting system)
  - Leader has full control over debate participants and rounds (existing meeting capabilities preserved)
  - **Meeting → Ecosystem reverse-writeback hook** (`meeting_ecosystem_writeback.py`): when meeting concludes with ecosystem keyword in topic, hook reminds Leader to dispatch agent that calls `ecosystem_apply_debate_result(risks_md / learnings_md / integration_md / integration_recommendation)`
  - Output: 4 fields per finalist + `stage_status='debated'`

- **Stage 3 — Reference / Integrate marking (Stage C)**
  - `ecosystem_mark_as_reference` adds `lifecycle:reference` tag + `stage_status='referenced'`
  - `ecosystem_start_integration` adds `lifecycle:integrated` tag + creates real integration task via `task_create` (existing task system, NOT a built-in implementation engine)
  - Both paths preserve research findings for future quick recall (avoid re-deep-scanning)

### Added — Project-customizable thresholds (Stage A)

- New `EcosystemProjectSettings` table per project: `min_stars` / `top_n` / `refresh_interval_days` / `focus_topics` / `focus_languages` / `shallow_concurrency` / `deep_concurrency`
- AI Team OS default: `min_stars=5000, top_n=200, focus_topics=['claude-code','mcp','agent-framework']`
- Other projects default: `min_stars=1000, top_n=100`

### Added — Active vs Full dual-view + append-only history (Stage A/D)

- **Decision (D)**: Data is **append-only forever** — repos that fall below threshold are not deleted, just `is_active=False`. Stars climb back → auto-promoted to active set + Stage 0 re-queue
- **Decision (E)**: Periodic shallow-refresh only scans active set (top_n by stars), saving GitHub API quota
- New `EcosystemRepoStatusSnapshot` table records every scan's stars/pushed_at/is_archived/is_active for historical analysis
- New `EcosystemRefresher` service (480 LOC) provides `shallow_refresh()` (diff-based, skips repos with no push since last refresh), `recompute_active_set()`, and `resurrect()` (revives deleted/private repos when GitHub returns 200 again)

### Added — Frontend stage funnel UI (Stage E)

- List page: stage colored badges (queued/shallow_done/architecture_done/debated/referenced/integrated/_failed) + 3 tabs (active/full/deleted)
- Detail page: new "研究历程" timeline tab showing stage progression + agent outputs + `shallow_summary_history` snapshots
- New `/ecosystem/research` candidate-filtering page: input research goal → tags-based candidate list with shallow_summary preview → multi-select to trigger Stage 1 → finalists trigger Stage 2
- Project settings tab on project detail page: 8 fields (min_stars / top_n / refresh_interval_days / focus_topics / focus_languages / shallow_concurrency / deep_concurrency / auto_shallow_on_archive)
- Failed repos: red badge + "Retry now" button (calls `POST /api/ecosystem/profiles/{id}/retry`)

### Added — 11 new MCP tools

- Stage 0: `ecosystem_apply_shallow_summary`, `ecosystem_shallow_queue_status`
- Stage 1: `ecosystem_deep_review_request_batch`, `ecosystem_apply_architecture_md`
- Stage 2: `ecosystem_trigger_debate`, `ecosystem_link_debate_meeting`, `ecosystem_apply_debate_result`
- Stage 3: `ecosystem_mark_as_reference`, `ecosystem_start_integration`, `ecosystem_link_integration_task`
- Refresh: (extended `ecosystem_scan_periodic` with refresh strategy)

### Added — 8 new REST endpoints

- `/api/ecosystem/lifecycle/*` (Stage 1/2/3 trigger paths)
- `/api/ecosystem/profiles/{id}/retry` (manual retry for failed)
- `GET/PUT /api/ecosystem/projects/{project_id}/settings`
- `POST /api/ecosystem/shallow_queue/{apply_summary,tick}`, `GET /api/ecosystem/shallow_queue/status`

### Schema changes (Stage A — fully backward compatible via COLUMNS_TO_ENSURE)

- `EcosystemDeepReview` +8 fields: `stage_status` enum / `integration_md` / 4 stage timestamps / `debate_meeting_id` / `integration_task_id`
- `EcosystemRepoProfile` +8 fields: `shallow_summary` / `last_shallow_refreshed_at` / `is_deleted` / `is_private_now` / `last_fetch_error` / `fetch_failure_count` / `is_active` / `active_rank`
- 2 new tables: `EcosystemRepoStatusSnapshot` (append-only history) / `EcosystemProjectSettings` (per-project config)
- Tag dictionary: 26 → 31 (added 5 lifecycle tags: `evaluating` / `reference` / `integrated` / `deleted` / `private_now`)
- Tag source enum +1: `lifecycle` (auto-managed by Stage 3 transitions)

### Tests

- 1283+ unit tests passing (1264 baseline + 110+ new ecosystem tests across A/B/C/D/E)
- 0 regressions
- 6 pre-existing failures (CLI version flag / debate template / mcp_autostart / pipeline) verified unrelated via `git stash`

### Architecture decisions (locked from user spec)

- **(A)** Ecosystem is a **knowledge base, not a workflow engine** — Stage 2 reuses `debate_start`, Stage 3 reuses `task_create`. Ecosystem only records, recalls, and tags.
- **(B)** Meeting → ecosystem reverse-writeback hook reminds Leader to record debate verdicts back to ecosystem (preserves Leader decision authority)
- **(C)** Each project has independent thresholds + active/full dual-view
- **(D)** Data append-only forever; stars-falling repos kept for potential future revival
- **(E)** Periodic refresh only scans active set
- **(F)** Rate-limit testing-driven concurrency tuning (no preset, observed and adjusted)

### Notes

- This release is **published to GitHub only** (continuing the v1.4.0 development-milestone pattern); PyPI publication deferred until further stability validation.

## [1.4.0] — 2026-05-07

### Added — Ecosystem Research Platform (Stage A-J)

A complete project-isolated platform for discovering, tagging, and deep-reviewing the Claude/MCP/agent open-source ecosystem. 188 repos indexed in initial scan, with multi-layer tagging achieving 2.05 tags/repo average and 1.5% zero-tag rate.

- **Storage layer (Stage B)** — 5 new tables (`EcosystemRepoProfile` extended + `EcosystemDeepReview` + `EcosystemTag` dictionary + `EcosystemRepoTag` association + `EcosystemRelation` + `EcosystemScanRun`); 21 seed tags; FK CASCADE for repo deletes, RESTRICT for tag deletes; 50/50 unit tests.

- **Periodic scanning (Stage C)** — `EcosystemScanner` service with incremental strategy (skips repos scanned <7 days), full strategy, ScanRun audit trail, GitHub API graceful degradation, owner blacklist + keyword whitelist secondary filtering. 3 new MCP tools (`ecosystem_scan_periodic`, `ecosystem_scan_status`, `ecosystem_scan_history`) + 5 REST endpoints + 31 new tests.

- **Three-layer tagging (Stage D)** — Layer 1 GitHub topics direct mapping (105 hits), Layer 2 keyword rules (70 hits), Layer 3 LLM dispatch_plan mode for sub-agent fan-out. 5 new MCP tools + EcosystemTag dictionary with 26 tags (incl. capability/tech_stack/maturity/positioning categories) + 48 unit tests.

- **Multi-dim search (Stage E)** — `ecosystem_search` upgraded to 11 parameters (query/tags AND/min_stars/language/sort_by/has_deep_review/etc.), `ecosystem_repo_get` returns full profile + tags + deep_reviews + relations + scan_run, `ecosystem_search_by_capability` for tag-driven retrieval. SQLite NULLS LAST emulation, EXISTS subquery for tag AND. 38 new tests, p95 < 50ms target.

- **Deep-review workflow (Stage F)** — 5-section report template (positioning / architecture / lessons / risks / integration), `EcosystemDeepReviewer` service dispatches Explore + backend-architect agents via `dispatch_plan` (CC subagent compatible), PostToolUse `deep_review_link.py` hook auto-links saved reports to `EcosystemDeepReview.report_id`. 4 new MCP tools + 5 REST endpoints + 19 new tests.

- **Auto-summary (Stage G)** — 4 markdown summary tools: `ecosystem_summary_weekly`, `ecosystem_summary_by_tag`, `ecosystem_summary_top_n`, `ecosystem_summary_health`. Auto `report_save` with `report_type=ecosystem-{weekly,by-tag,top-n,health}`. N+1 avoided via single-pass joins. 33 new tests.

- **Frontend (Stage H)** — `/ecosystem` list page (4-col card grid + filter bar + pagination), `/ecosystem/:repoId` detail page with 4 new components (`CapabilityTags` / `DeepReviewSection` / `RelationsSection` / `ScanRunSection`), v2 API consumed via `useEcosystemRepoFull` hook (UUID → full_name resolution + path encoding). Mobile responsive, Playwright screenshots verified.

- **Project isolation (Stage J)** — All 6 ecosystem tables get nullable `project_id` column with composite UNIQUE on `(project_id, repo_full_name)` for `EcosystemRepoProfile`. `EcosystemTag` dictionary keeps `project_id=NULL` for global seed (21 tags shared across projects). `X-Project-Id` HTTP header → `get_scoped_repository` routing; MCP `_api_call` auto-injects header from cwd-inferred session project. Auto `backfill_ecosystem_to_project` startup hook migrates legacy 188 repos to AI Team OS project. Dashboard `setCurrentProjectId` syncs on project switch. 10 new isolation tests, 1109 unit tests passing.

### Added — Tag quality polish (Stage K4)

- **`replace_auto` mode** for `ecosystem_tag_apply_batch` — Replace mode (default `False` for backward compat) deletes existing `auto_rule` and `github_topic` tags before re-applying, preserving `manual` and `auto_llm` tags. Solved the bug where new rules produced new tags but stale `mcp_framework` false positives (99 repos) remained from old rule passes.

- **5 new tags + edge-case rules** — Added `claude_code` / `agent_harness` / `javascript` / `java` / `docs_only` to seed dictionary. New `LANGUAGE_TAG_MAP`, `DOCS_ONLY_LANGUAGES`, `DOCS_ONLY_NAME_PATTERNS` for Layer 2 sub-rules. `mcp_framework` false-positive rate dropped from 37% (99/265) to **0.8% (2/265)**, average tags/repo from **1.01 → 2.05**, zero-tag from **28.7% → 1.5%**.

- **18+ edge-case research** — `docs/ecosystem-tag-edge-cases.md` documents real-world tagging anomalies (n8n / dify / awesome-mcp-servers / claude-cookbooks / hermes-agent / netdata / JavaGuide) with root causes and rule fixes.

### Performance — Search optimization (Stage K1)

- **5 composite indexes** on `ecosystem_repo_profiles`: `(project_id, stars)`, `(project_id, category, stars)`, `(project_id, language, stars)`, `(project_id, pushed_at)`, `(project_id, is_archived, stars)`. EXPLAIN QUERY PLAN verified all TEMP B-TREE eliminated.

- **search p95: 2057ms → 13.1ms (156x improvement)** measured on 100 random queries (real production data, 265 repos). p50 6.6ms / p99 25ms.

- **`ecosystem_search` default behavior fixed** — Empty `tags=[]` now bypasses the EXISTS subquery (prevents full-table scan) and returns the full result set sorted by stars instead of an empty list.

- `compute_ecosystem_facet_counts` refactored to single-pass aggregation (eliminates 2/3 IO).

- 6 new performance regression tests.

### Fixed

- **`context_tracker` 1M context window detection on new model variants** — `claude-opus-4-7` and other new opus variants were misreported as 200K when actually 1M, causing false 99% context warnings at 198K tokens. Two-level detection now: (1) exact `{model}[1m]` match, (2) family-level fallback (any `claude-{opus|sonnet|haiku}-*[1m]` history triggers 1M for that family). New `CLAUDE_CONTEXT_SIZE` env var for ultimate user override. 4 new tests with module-level autouse fixture isolating `~/.claude.json` from test machine.

- **Auto-revive completed teams on new agent registration** — Hook translator now flips `team.status=completed` back to `active` when a new CC agent registers against it, with loud warning log + `team.auto_revived` event. Replaces the previous hard-block which disrupted long-running tasks (e.g., scan jobs on archived teams).

### Frontend bug fix (Stage K2)

- **Detail page `深度档案区` placeholder removed** — Previously the detail page hardcoded "TODO: Stage E v2 API" placeholder text even though the v2 API (`/profiles/{name}/full`) existed since Stage E. `useEcosystemRepoFull` hook now consumes v2 directly with UUID → full_name resolution and path-segment encoding for slashes. v2 failure gracefully degrades to v1 list-based fallback.

### Changed

- **Plugin description updated** — Now reflects 140+ MCP tools (incl. 30+ ecosystem research) + Ecosystem Research Platform feature set. New marketplace tags: `ecosystem-research`, `github-discovery`, `code-mining`.

## [1.3.4] — 2026-04-14

### Fixed
- **Critical: `meeting_send_message` 500 on databases upgraded from <1.3.0** — The 1.3.3 `_sqlite_migrate()` added `meetings.meta_json` but omitted `meeting_messages.metadata_json`. Any database created before that column was added to the ORM model would raise `OperationalError` on every `INSERT`/`SELECT` against `meeting_messages`. Fixed by refactoring `_sqlite_migrate()` into a data-driven loop over `COLUMNS_TO_ENSURE`, which also covers `meetings.meta_json`. All entries are idempotent (guarded by `PRAGMA table_info`).
- **Migration framework now data-driven** — future schema additions require only a one-line append to `COLUMNS_TO_ENSURE`.

## [1.3.3] — 2026-04-14

### Fixed
- **Critical: `meeting_create` API 500 when called from external projects** — Three root causes fixed:
  1. **Missing `meta_json` column** — The `meetings` table lacked the `meta_json` column on databases created before this field was added to the ORM model. `init_db` uses `create_all` which does not add new columns to existing tables. Added an idempotent SQLite migration in `connection.py` that runs at startup and safely `ALTER TABLE`s the column if absent.
  2. **Team ID not resolved by name** — The `POST /api/teams/{team_id}/meetings` route accepted team names (e.g. `"repo-insight-build"`) but passed them straight to the repository without UUID resolution, causing downstream queries to silently fail. Route now tries UUID lookup first, then falls back to name lookup, returning HTTP 404 if neither matches.
  3. **Unhandled ORM exception caused worker hang** — Added `try/except` around `create_meeting` call so DB errors surface as HTTP 500 JSON instead of leaving the worker stuck.

## [1.3.2] — 2026-04-14

### Fixed
- **Critical: MCP auto port discovery broken** — `plugin/.mcp.json` hardcoded `AITEAM_API_URL=http://localhost:8000` as an env var, which overrode the dynamic port fallback in `_get_api_url()`. When autostart picked a free port (e.g. 59711) because 8000 was occupied, MCP tools still tried port 8000 and reported `unhealthy`, while hooks (using the same `_get_api_url()` code path) worked correctly. Removed the env var from plugin config, root `.mcp.json`, and all install scripts so MCP now falls back to reading `api_port.txt` dynamically. User-provided `AITEAM_API_URL` env still takes priority (for remote API use cases).

## [1.3.1] — 2026-04-13

### Fixed
- **Hotfix: context_tracker 1M context window detection** — Transcripts record model as `claude-opus-4-6` without the `[1m]` suffix, causing 1M-context sessions to be treated as 200K and report absurd percentages (e.g. 342%). Added token-count fallback: if `used_tokens > 200K`, auto-detect as 1M context window.

## [1.3.0] — 2026-04-13

### Added
- **CC native integration (Track A)**
  - `TaskCompleted` hook — hard gate that blocks task completion without memo/result via `task_completed_gate.py`; exit 2 on missing progress records
  - `TaskCreated` hook bridge — `cc_task_bridge.py` auto-mirrors CC native task creations into the OS task wall
  - `PermissionDenied` hook with classifier — `permission_denied_recovery.py` calls new `POST /api/hooks/diagnose_denial` endpoint for 4-way decisions: `recoverable_with_retry`, `recoverable_with_workaround`, `needs_user_approval`, `permanent_denial`
  - MCP tool `meta={"anthropic/maxResultSizeChars": 500000}` annotations on 8 data-heavy tools (`taskwall_view`, `task_list_project`, `report_list`, `report_read`, `event_list`, `meeting_read_messages`, `memory_search`, `team_knowledge`)
  - `wake_agent` `--bare` + `--exclude-dynamic-system-prompt-sections` optimization — expected ~50% startup latency reduction with long prompt temp file fallback for Windows cmdline length limit

- **Meeting system complete redesign (Track B)**
  - `meeting_create` returns full `dispatch_plan[]` with ready-to-paste `Agent()` launch parameters, eliminating Leader impersonation by providing explicit spawn instructions per participant
  - Structured `participants` input: `{name, agent_template, role, context_files, expected_output}` replaces legacy string list (backward compatible)
  - `meeting_attendance_check(meeting_id)` — query spoken/pending participants per round with timeout tracking
  - `meeting_send_message` new `caller_agent_id` parameter — impersonation audit; mismatched calls get `impersonation: true` metadata and event log entry
  - `meeting_conclude` default `validate_attendance: true` — returns 400 with missing participant list when not all spoken; `force=true` bypasses but logs `meeting.forced_conclude_with_missing` event
  - `Meeting.meta_json` persistent field stores `expected_participants` and round state

- **Meeting templates migrated to Plugin Skills (Track C)**
  - 8 templates moved from hardcoded `templates.py` dict (234 lines) to `plugin/skills/meeting-facilitate/templates/*.md` files (brainstorm/decision/review/retrospective/standup/debate/lean_coffee/council)
  - Each template has YAML frontmatter with structured round data + markdown body (when to use / participant guide / anti-patterns)
  - `templates.py` rewritten as lazy YAML loader (107 lines), backward-compatible API
  - **User extensibility**: drop a new `.md` file to add custom meeting templates without touching Python code
  - Uses CC's progressive disclosure pattern — templates only loaded when needed, zero token penalty
  - Completely rewrote `plugin/skills/meeting-facilitate/SKILL.md` (355 lines) with 7-step lifecycle aligned to new dispatch_plan API, template selection matrix, 3 end-to-end scenarios, 7 anti-pattern warnings

- **Context tracking via transcript parsing (Plan E)**
  - New `context_tracker.py` hook on `UserPromptSubmit` — reads `transcript_path` from hook payload and extracts `usage.input_tokens` + cache tokens from the last assistant message in the session jsonl for 100% accurate context usage
  - Automatic 1M context window detection via model identifier suffix (`[1m]`)
  - Warns at `>=80%` (CONTEXT WARNING) and `>=90%` (CONTEXT CRITICAL) with token breakdown
  - **Zero dependency on statusline** — works for plugin users who don't have our custom statusline installed
  - **Natural project isolation** — transcript path itself encodes project identity, eliminating cross-project monitor file bugs

- **Project auto-registration flow**
  - New `POST /api/context/resolve` endpoint with exact/prefix/auto-create matching strategies
  - `session_bootstrap.py` detects unregistered directories and injects registration prompt to Leader (non-blocking)
  - New `dismiss_project_registration(cwd)` MCP tool — users can opt out; persisted to `~/.claude/data/ai-team-os/dismissed_projects.json`
  - Fixes the bug where new project directories (e.g., `靖安笔试`, `repo-insight`) were never registered until manually triggered

### Changed
- **Task wall auto-sync in `workflow_reminder.py`**
  - PreToolUse: extracts agent prompt + description, performs keyword matching against project task wall pending items, warns when Leader-dispatched work doesn't correspond to any wall task
  - PostToolUse: new `_post_tool_taskwall_sync()` — Agent dispatch auto-updates matching task to `running`; completion SendMessage auto-updates to `completed`
  - Narrowed report data directory warning to only `.claude/data/ai-team-os/reports/` paths (no more false positives on source code)

- **Session bootstrap context engineering**
  - Removed broken instruction to read `~/.claude/context-monitor.json` (file no longer maintained)
  - New instruction: "hook has already monitored context; you only need to focus on task progression"
  - Added project auto-registration prompt block when current cwd is unregistered

- **Documentation updates**
  - `README.md` / `README.zh-CN.md` reflects new meeting system and template architecture
  - Skill docs reorganized per CC's progressive disclosure best practices

### Fixed
- **Distribution sync** — 4 hook scripts were out of sync between `src/aiteam/hooks/` and `plugin/hooks/` (missing `_get_api_url()`, project registration checks, task wall auto-sync). Plugin users would have experienced broken dynamic port detection and silent feature loss. All 4 files now byte-identical between dev and distribution copies.
- **`meeting.py:103`** — `_build_dispatch_plan` return type annotation aligned with actual three-tuple return (added `legacy_warnings`)
- **`context-monitor.json` cross-project pollution** — old `_find_monitor_file()` globbed all projects and picked most-recent by mtime, reading stale data from other sessions. Replaced entirely by `context_tracker.py` which uses `transcript_path.parent` for natural isolation.
- **Scheduled task false warnings** — auto-wake prompt no longer reads a 9-day-old global `context-monitor.json` that falsely reported `<10%` regardless of actual usage

### Removed
- `src/aiteam/hooks/context_monitor.py` and `plugin/hooks/context_monitor.py` — replaced by `context_tracker.py`
- Global `~/.claude/context-monitor.json` dependency — no longer read or written by OS

## [1.2.1] — 2026-04-07

### Added
- **Report system database migration** — Reports now stored in SQLite database instead of filesystem; eliminates permission issues and enables project isolation
- **ReportModel ORM** — New `reports` table with `project_id`, `author`, `topic`, `report_type`, `content` fields
- **Report REST API** — `POST/GET/DELETE /api/reports` with `project_id`, `report_type`, `author` query filters
- **Dashboard full project isolation** — All 9 dashboard pages now have project selector dropdowns:
  - Reports: project selector + author filter
  - Events & Failures: project_id query parameter in events API
  - Meetings & Agent Board: frontend team.project_id filtering
  - Analytics & Pipelines: project→team cascading selectors
- **Task wall auto-sync** — `_post_tool_taskwall_sync()` in workflow_reminder: Agent dispatch auto-links to matching task wall item and updates status (pending→running→completed)
- **PreToolUse task wall matching** — Keyword overlap check between Agent prompt and project task wall items; warns when work is not tracked on the wall
- **Project cascade deletion** — `delete_project()` now cleans up 11 related tables: meetings, meeting_messages, tasks, agents, teams, phases, reports, briefings, memories, events, cross_messages

### Changed
- **`report_save` MCP tool** — Now calls `POST /api/reports` instead of writing files directly; no filesystem permission needed
- **`report_list` MCP tool** — Now calls `GET /api/reports` with server-side filtering (report_type, author, topic)
- **`report_read` MCP tool** — Now reads from database by report ID instead of filename
- **Events API** — `list_events` endpoint accepts `project_id` query parameter; filters by project's team IDs
- **Subagent context injection** — Strengthened report_save instruction: "reports must be saved via report_save tool (direct Write won't be tracked by OS)"
- **Workflow reminder reports check** — Narrowed path matching to only `.claude/data/ai-team-os/reports/` data directories; no longer false-positives on source code files containing "reports"
- **i18n** — Added `allProjects`, `filterType`, `types.*` keys for both English and Chinese

### Fixed
- `app.py` — `_dist_dir` NoneType crash when no dashboard dist directory found
- `test_version_flag` — Updated assertion from `0.8.0` to `1.2.0`
- `test_teamcreate_reminds_task` — Relaxed warning count assertion to `>= 1` (accommodates new active-team warning)
- Report page couldn't switch categories or read reports — Complete rewrite with database backend
- 155 legacy filesystem reports migrated to database via `scripts/migrate_reports.py`

## [1.2.0] — 2026-04-05

### Added
- **Agent Watchdog heartbeat system** — `agent_heartbeat` / `watchdog_check` MCP tools with 5-minute TTL timeout detection, automatic identification of stuck agents
- **SRE error budget model** — GREEN/YELLOW/ORANGE/RED four-level response, 20-task sliding window, `error_budget_status` / `error_budget_update` tools
- **Completion verification protocol** — `verify_completion` checks task status + memo existence, prevents hallucinated completion reports
- **Alembic incremental migration** — Full v1.1 schema migration file (trust_score / channel_messages / entity_id / state_snapshot, etc.)
- **Ecosystem integration recipes documentation** — GitHub / Slack / Linear / full-stack team, 4 preset recipes (`docs/ecosystem-recipes.md`)
- **`ecosystem_recipes()` MCP tool** — Integration recipe discovery and query
- **MCP debug log enhancement** — Startup lock mechanism logging, API startup process now traceable
- **Auto port discovery** — API server automatically finds an available port to avoid multi-project conflicts; port written to `api_port.txt` for sharing
- **MCP HTTP Streamable endpoint** — `/mcp/` mounted on FastAPI (supplementary capability; CC connection remains stdio)
- **INSTALL.md** — CC-assisted installation guide with venv detection logic
- **PyPI 1.2.0 release** — `pip install ai-team-os` fetches the latest version

### Changed
- **Session bootstrap context engineering** — Rules reduced from 23 to 5 core rules (context injection reduced by 60%)
- **Subagent context injection** — Added 60-line cap with priority-based auto-discard of low-priority content
- **`_ensure_api_running` atomic startup lock** — Prevents multi-session port race conditions (`O_CREAT|O_EXCL` file lock)
- **Hooks dynamically read API port** — Port sourced from `api_port.txt` instead of hardcoded 8000
- **`__init__.py` version synced to 1.2.0**
- **`pyproject.toml` metadata** — Added classifiers, keywords, and project URLs

### Fixed
- Alembic integration caused `_run_migrations` to be skipped — changed to always execute (idempotent safe)
- Multiple CC sessions starting API simultaneously caused port conflicts — resolved with atomic file lock
- StateReaper cascade-closing active meetings incorrectly closed meetings with recent messages — added recent message check
- `_read_pid_file` threw `SystemError` on Windows — added catch
- `install.py` uses `sys.executable` absolute path — fixes project venv hijacking hooks/MCP
- `auto_install.py` installs from GitHub — ensures latest code when PyPI version lags
- Startup lock 60-second TTL — prevents stale lock file from blocking startup after CC abnormal exit
- MCP HTTP mount fix — lifespan passthrough + `path='/'` route + 308 redirect handling
- Plugin marketplace 15 install bugs fixed — hooks switched to `${CLAUDE_PLUGIN_ROOT}` paths + restored `.py` scripts

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
