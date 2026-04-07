# 变更日志

AI Team OS 的所有重要变更均记录在此文件中。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/)

## [1.2.1] — 2026-04-07

### 新增
- **报告系统数据库迁移** — 报告从文件系统迁入 SQLite 数据库，消除文件权限问题并支持项目隔离
- **ReportModel ORM** — 新增 `reports` 表，包含 `project_id`、`author`、`topic`、`report_type`、`content` 字段
- **报告 REST API** — `POST/GET/DELETE /api/reports`，支持 `project_id`、`report_type`、`author` 查询过滤
- **Dashboard 全页面项目隔离** — 全部 9 个 Dashboard 页面均有项目选择器：
  - 报告：项目选择器 + 作者过滤
  - 事件日志 & 失败分析：events API 新增 project_id 参数
  - 会议室 & Agent 看板：前端按 team.project_id 过滤
  - 活动分析 & Pipeline：项目→团队联动选择器
- **任务墙自动同步** — workflow_reminder 新增 `_post_tool_taskwall_sync()`：Agent 派遣自动关联任务墙项并更新状态（pending→running→completed）
- **PreToolUse 任务墙匹配** — Agent prompt 与项目任务墙的关键词重叠检查，未在墙上的工作会收到警告
- **项目级联删除** — `delete_project()` 清理 11 张关联表：meetings、meeting_messages、tasks、agents、teams、phases、reports、briefings、memories、events、cross_messages

### 变更
- **`report_save` MCP 工具** — 改为调用 `POST /api/reports` 存入数据库，不再直接写文件，无需文件系统权限
- **`report_list` MCP 工具** — 改为调用 `GET /api/reports`，支持服务端过滤（report_type、author、topic）
- **`report_read` MCP 工具** — 改为通过报告 ID 从数据库读取，不再按文件名读取
- **Events API** — `list_events` 端点接受 `project_id` 查询参数，按项目所属团队 ID 过滤
- **子 Agent 上下文注入** — 加强 report_save 指令："报告必须通过 report_save 工具保存到数据库（直接 Write 不会被系统追踪）"
- **Workflow reminder 报告检测** — 路径匹配精确到 `.claude/data/ai-team-os/reports/` 数据目录，不再对包含"reports"的源码文件误报
- **i18n** — 中英文新增 `allProjects`、`filterType`、`types.*` 翻译键

### 修复
- `app.py` — `_dist_dir` 为 None 时崩溃（无 dashboard dist 目录场景）
- `test_version_flag` — 版本断言从 `0.8.0` 更新为 `1.2.0`
- `test_teamcreate_reminds_task` — 放宽 warning 数量断言为 `>= 1`（适配新增的活跃团队提醒）
- 报告页面无法切换分类和读取报告 — 使用数据库后端完全重写
- 155 份旧文件系统报告通过 `scripts/migrate_reports.py` 迁入数据库

## [1.2.0] — 2026-04-05

### 新增
- **Agent 看门狗心跳系统** — `agent_heartbeat` / `watchdog_check` MCP 工具，5 分钟 TTL 超时检测，自动识别卡死的 Agent
- **SRE 错误预算模型** — 绿色/黄色/橙色/红色四级响应，20 任务滑动窗口，`error_budget_status` / `error_budget_update` 工具
- **完成验证协议** — `verify_completion` 检查任务状态与备忘录是否存在，防止幻觉完成报告
- **Alembic 增量迁移** — v1.1 完整 schema 迁移文件（trust_score / channel_messages / entity_id / state_snapshot 等）
- **生态集成配方文档** — GitHub / Slack / Linear / 全栈团队 4 个预设配方（`docs/ecosystem-recipes.md`）
- **`ecosystem_recipes()` MCP 工具** — 集成配方发现与查询
- **MCP 调试日志增强** — 启动锁机制日志，API 启动过程可追踪
- **自动端口发现** — API 服务器自动寻找空闲端口，避免多项目冲突；端口写入 `api_port.txt` 共享
- **MCP HTTP Streamable 端点** — `/mcp/` 挂载到 FastAPI（附加能力，CC 连接保持 stdio）
- **INSTALL.md** — CC 辅助安装指引，含 venv 检测逻辑
- **PyPI 1.2.0 发布** — `pip install ai-team-os` 可获取最新版

### 变更
- **会话启动上下文工程** — 规则从 23 条精简为 5 条核心规则（上下文注入量减少 60%）
- **子 Agent 上下文注入** — 新增 60 行上限裁剪，按优先级自动丢弃低优先内容
- **`_ensure_api_running` 原子启动锁** — 防止多会话端口竞争（`O_CREAT|O_EXCL` 文件锁）
- **Hooks 动态读取 API 端口** — 从 `api_port.txt` 读取端口，不再硬编码 8000
- **`__init__.py` 版本同步为 1.2.0**
- **`pyproject.toml` 元数据** — 添加 classifiers、keywords 和项目 URLs

### 修复
- Alembic 集成后 `_run_migrations` 被跳过 — 改为始终执行（幂等安全）
- 多个 CC 会话同时启动 API 导致端口冲突 — 使用原子文件锁解决
- StateReaper 级联关闭活跃会议时误关有近期消息的会议 — 增加近期消息检查
- `_read_pid_file` 在 Windows 上抛出 `SystemError` — 增加异常捕获
- `install.py` 使用 `sys.executable` 绝对路径 — 解决项目 venv 劫持 hooks/MCP 问题
- `auto_install.py` 改为从 GitHub 安装 — PyPI 版本滞后时仍能获取最新代码
- 启动锁 60 秒 TTL — 防止 CC 异常退出后锁文件残留阻塞启动
- MCP HTTP 挂载修复 — lifespan 传递 + `path='/'` 路由 + 308 重定向处理
- Plugin marketplace 15 个安装 bug 修复 — hooks 改为 `${CLAUDE_PLUGIN_ROOT}` 路径 + 恢复 `.py` 脚本

## [1.1.0] — 2026-04-05

### 新增
- **Agent 信任评分系统** — `trust_score` 字段（0-1），任务成功/失败自动调整，`auto_assign` 加权匹配，`agent_trust_scores` / `agent_trust_update` MCP 工具
- **语义缓存层** — BM25 + Jaccard 相似度匹配，JSON 持久化，TTL 过期机制，`cache_stats` / `cache_clear` MCP 工具
- **工具分级定义** — 核心工具（15 个必备）与高级工具（46 个领域专用）分类，为未来上下文预算优化做准备

### 变更
- `TaskModel.status` 新增数据库索引（提升查询性能）
- `resolve_task_dependencies` 改用批量 IN 查询替换逐条查询（N+1 优化）
- `detect_dependency_cycle` 改为广度优先搜索 + 批量查询（大规模依赖图性能优化）
- `task_list_project` 分页 — 新增 `limit` / `offset` / `include_completed` / `status` 参数

### 修复
- `trust.py` 错误响应改为 `HTTPException`（此前返回裸字典）
- `git_ops.py` 敏感文件过滤改用 `basename`（避免路径包含关键字时误拦）
- `channels.py` 死代码清理
- 修复已存在的 `test_check_for_updates_no_git_repo_silent` 测试

## [1.0.0] — 2026-04-05

### 新增
- **错误类型到恢复策略映射** — `_api_call` 统一附加 `_recovery` 和 `_error_category`，自动推荐恢复动作
- **文件锁 / 工作区隔离** — `file_lock_acquire` / `release` / `check` / `list` 4 个 MCP 工具 + TTL=300 秒 + hook 警告，防止并发编辑冲突
- **频道通讯系统** — `team:` / `project:` / `global` 三种频道格式 + `@mention` 支持，`channel_send` / `channel_read` / `channel_mentions` MCP 工具
- **执行模式记忆** — 成功/失败模式记录 + BM25 检索 + 子 Agent 上下文注入，`pattern_record` / `pattern_search` MCP 工具
- **Git 自动化工具** — `git_auto_commit` / `git_create_pr` / `git_status_check` MCP 工具，自动过滤敏感文件
- **Guardrails 一级防护** — 7 种危险模式检测 + 个人信息警告 + `InputGuardrailMiddleware`，防止无监督运行时的破坏性操作
- **Alembic 数据库迁移系统** — 初始修订版本 + 双路径初始化（全新/已有数据库），迁移历史可追踪
- **MCP 调试日志系统** — `~/.claude/data/ai-team-os/mcp-debug.log`，工具调用链路可观测

### 变更
- **陷阱工具消除** — `team_create` / `agent_register` 描述首行添加警告 + `_warning` 返回值，防止误用
- **`task_id` 自动注入** — 子 Agent 上下文自动携带当前 task_id，无需手动传递
- **增强任务分配** — `auto_assign` 加入 `completion_rate` + `trust_score` 加权，优先分配可靠 Agent
- **`inject_subagent_context` 环境变量统一** — 统一为 `AITEAM_API_URL`

### 修复
- `context_monitor` 改为读取项目级监控文件（不再读取过时的全局文件）
- 修复已存在的 `test_check_for_updates_no_git_repo_silent` 测试

### 测试
- 28 个跨功能集成测试
- 总测试数：769（从 389 增长）

## [0.9.0] — 2026-04-04

### 新增
- **Prompt Registry（提示词注册表）** — Agent 模板版本追踪 + 效果统计，3 个 API 端点 + `prompt_version_list` / `prompt_effectiveness` MCP 工具，与 `failure_alchemy` 关联
- **BM25 搜索升级** — 中文 bigram + 英文分词替代简单关键词匹配，搜索质量提升 3-5 倍，优雅降级（`jieba` 为可选依赖）
- **事件日志增强** — EventModel 新增 `entity_id` / `entity_type` / `state_snapshot` 三个字段，自动快照 + 实体过滤
- **辩论模式** — 4 轮结构化辩论（倡导者 -> 批评者 -> 回应 -> 裁判）+ `debate_start` / `debate_code_review` MCP 工具 + 2 个辩论角色模板
- **3 个仪表盘可观测性页面** — 流水线可视化 / 失败分析 / 提示词注册表
- **Agent 模板自动安装** — `install.py` 自动安装到 `~/.claude/agents/`（默认 opus 模型）
- **CC Marketplace 提交** — 正式提交到 Anthropic 官方插件市场

### 变更
- **server.py 模块化拆分** — 3050 行单文件拆分为 57 行入口 + 14 个工具模块 + 2 个基础模块，可维护性大幅提升
- **会话启动优化** — 从 15-25 秒缩短至 1-2 秒：并行化 + 异步 git 检查 + 减少重试次数
- **workflow_reminder 项目隔离** — 所有 API 调用添加 `X-Project-Id` 请求头
- **install.py 重构** — 支持多 hook 分组/事件、自动设置 `AGENT_TEAMS` 环境变量和 `effortLevel` 推荐配置
- **`_resolve_project_id` 缓存** — 5 分钟 TTL 文件缓存，减少高频 hook 的 HTTP 调用
- **inject_subagent_context 环境变量统一** — `AI_TEAM_OS_API` 更名为 `AITEAM_API_URL`
- **测试导入路径迁移** — `plugin/hooks/` 迁移至 `aiteam.hooks` 包导入

### 修复
- workflow_reminder 项目级任务查询缺少 `X-Project-Id` 请求头（B1）
- TeamDelete PUT 请求缺少 `X-Project-Id` 请求头（B2）
- 测试文件导入路径断裂（plugin/hooks 删除后）
- `context_monitor` 路径修复 — 改为读取项目级文件而非全局过时文件
- statusline.py 相关废弃测试清理

### 移除
- **plugin/hooks/ 死代码清理** — 删除 11 个过时的 `.py` / `.ps1` 文件，仅保留 `hooks.json` + `README`
- **重复 Agent 模板清理** — 删除旧版 `meeting-facilitator.md` 和 `tech-lead.md`（从 25 个减至 23 个模板）
- **移除 enforce_model hook** — 保留用户模型选择的灵活性
- **从 install.py 移除模型设置** — 不再强制新用户配置模型

## [0.8.0] — 2026-04-04

### 新增
- **成本追踪**：AgentActivity 新增 `tokens_input`/`tokens_output`/`cost_usd` 字段，`GET /api/analytics/token-costs` 接口，`token_costs` MCP 工具
- **执行追踪**：`GET /api/tasks/{id}/execution-trace` 统一时间线（事件 + 备忘录），`task_execution_trace` MCP 工具
- **Agent 实时面板**：`AgentLivePage` 仪表盘，状态标签（忙碌/等待/离线），30 秒自动刷新
- **故障自动诊断**：`FailureAlchemist.diagnose_failure()`，`POST /api/tasks/{id}/diagnose`，`diagnose_task_failure` MCP 工具
- **Slack/Webhook 通知**：`NotificationService`，EventBus 自动触发，`GET/PUT/DELETE /api/settings/webhook`，`send_notification` MCP 工具
- **流水线并行执行**：`parallel_with` 字段，完成门控，4 个新增并行测试（共 28 个）
- **执行回放引擎**：`ReplayEngine`（get_replay + compare_executions），`task_replay`/`task_compare` MCP 工具
- **成本预算与告警**：每周预算限额（默认 50 美元），80% 告警阈值，`GET /api/analytics/budget`，`budget_status` MCP 工具
- **Leader 简报页面**：双层标签页（项目 + 状态），项目名称标签，解决/忽略操作界面
- **79 个 MCP 工具**（原为 72 个）

### 修复
- **P0 API 进程管理**：PID 文件替换文件锁，`_is_api_healthy()` 替换 `_is_port_open()`，卡死进程 15 秒自动终止
- **全局项目隔离**：`Repository._apply_project_filter()`，MCP 自动注入 `X-Project-Id` 请求头
- **会话启动**：使用工作目录匹配的项目（不再使用 `projects[0]`）
- **简报列表隔离**：使用限定范围的仓储
- **上下文监控**：按项目隔离文件（不再跨会话覆盖）

### 变更
- **Hook 脚本**：改用 `python -m aiteam.hooks.*` 模块调用方式（不再使用文件路径）
- **插件 hooks.json + .mcp.json**：统一为 python -m 命令
- **install.py**：基于模块的 hook，`~/.mcp.json` 用于跨项目 MCP

## [0.7.2] — 2026-04-02

### 新增
- **MCP 工具**：`project_update`、`project_delete`、`project_summary`、`task_subtasks`、`team_delete`、`briefing_dismiss`（共 72 个）
- **仪表盘项目改版**：状态标签（活跃/非活跃），可展开的详情行，唤醒设置标签页
- **项目摘要 API**：`GET /api/projects/{id}/summary` — 快速状态 + 优先任务

### 变更
- **项目隔离重新设计**：移除按项目分库方案（死代码，减少 180 行），统一 `context_resolve()` 使用进程级缓存
- **SQLite WAL 模式**：通过引擎事件监听器启用，支持多会话并发
- **禁用自动项目注册**：SessionStart 不再自动创建项目，提示用户通过 `project_create` 手动注册
- **context_resolve()**：移除危险的 `projects[0]` 回退策略，无匹配时返回空值

### 修复
- 多会话数据库锁：SQLite `journal_mode=WAL` + `busy_timeout=10s` 防止并发写入失败
- 数据回填：272 个孤立 Agent、57 个任务、72 个会议分配到正确项目
- 垃圾项目清理：移除 6 个自动创建的项目，去重量化项目
- 仪表盘 `ProjectSwitcher` 下拉框移除（原先会跳转到空白页）
- 唤醒 Agent `--output-format stream-json` 错误移除（与 `-p` 标志不兼容）
- 唤醒熔断器：仅统计真实失败（错误/超时），不统计跳过

## [0.7.1] — 2026-04-02

### 新增
- **Leader 简报系统** — 自主运行时的决策上报机制
  - 数据库表 `leader_briefings` + Pydantic 模型 + ORM
  - 3 个 MCP 工具：`briefing_add`、`briefing_list`、`briefing_resolve`
  - API 端点：GET/POST `/api/leader-briefings`，PUT `/{id}/resolve`，PUT `/{id}/dismiss`
  - Leader 在自主工作期间记录待决事项，用户返回后统一审阅
- **通过 CronCreate 自动唤醒** — SessionStart 启动时注入 CronCreate 指令
  - 每 3 分钟 Leader 自动检查任务墙并推进工作
  - 通过 `briefing_add` 上报决策，用户返回时汇报待处理事项
- **install.py** — 一键安装 hook、MCP 和验证
  - `python scripts/install.py` — 完整安装（hook + MCP + settings.json）
  - `python scripts/install.py --check` — 验证 9 个 hook、MCP、API、包
  - `python scripts/install.py --uninstall` — 移除配置，保留数据

## [0.7.0] — 2026-04-02

### 新增
- **唤醒 Agent 调度器** — 通过 `claude -p` 子进程自动唤醒 Agent
  - WakeAgentManager：子进程生命周期管理（communicate + 两阶段终止）
  - WakeSession 数据模型 + ORM + 7 个仓储 CRUD 方法
  - 7 层安全机制：数组参数、UUID 验证、按 Agent 加锁、全局信号量（最大=2）、熔断器、提示/数据 XML 分离、环境变量清理
  - 分诊预检：无可执行任务时跳过唤醒（约 70% 跳过率）
  - 紧急停止 API：`PUT /wake-pause-all`、`PUT /wake-resume-all`
  - StateReaper 集成（即发即忘 + 优雅关闭）
  - allowedTools 预设：安全模式（无 Bash）/ 含 Bash 模式（显式启用）
- **CronCreate 会话唤醒** — 验证 CC 内置定时任务用于唤醒当前会话
- 20 个 wake_manager 单元测试（全部通过）
- 唤醒会话结果追踪（已完成/超时/错误/熔断/分诊跳过）

### 修复
- `context_resolve()` 自动项目选择：通过工作目录匹配 root_path，不再盲目选择第一个项目
- Hook 路径编码：将 hook 脚本移至 ASCII 路径（`~/.claude/plugins/ai-team-os/hooks/`）
- Hook 豁免列表：将 claude-code-guide、tdd-guide、refactor-cleaner 添加到非阻塞 Agent 类型
- 调度器路由中 `valid_actions` 缺少 "wake_agent"（导致无法创建 API）
- 信号量私有 API 访问（`_value`）替换为 `locked()`
- 熔断器：仅统计真实失败（错误/超时），不统计跳过
- `duration_seconds` 现已正确计算并记录
- `shutdown()` 字典迭代安全（取消前先快照值）
- 全局 MCP 配置：添加 `cwd` 字段以支持跨目录使用
- 数据迁移：将 19 个任务 + 1 个团队从错误项目移至正确项目

### 变更
- `_clean_env()` 从白名单策略改为黑名单策略（继承全部，排除密钥）
- 插件清单：添加 `hooks` 字段指向 `hooks/hooks.json`
- 插件 `.mcp.json`：本地开发模式使用 `python -m aiteam.mcp.server` 并指定 `cwd`

## [0.6.0] — 2026-03-22

### 新增
- 工作流编排流水线（7 个模板，自动阶段推进）
- 流水线强制执行：task_type 参数 + 逐步阻塞
- 跨项目消息系统（v1，单机版）
- 自动更新机制（scripts/update.py）
- 团队清理提醒（SessionStart + 规则 15）
- 独立安装方式（hook 复制到 ~/.claude/hooks/）
- CC 插件包结构
- 卸载脚本（scripts/uninstall.py）
- 仪表盘：活动表格 + 决策时间线增强

### 修复
- 全局 MCP 配置：使用 ~/.claude.json（而非 settings.json）
- 安装依赖（fastapi、uvicorn、fastmcp 改为必需依赖）
- SessionStart API 重试（针对时序问题重试 3 次）
- B0.9 噪音降低（首次提醒后每 10 次调用提醒一次）
- Windows UTF-8 编码修复（所有 hook 脚本）

## [0.5.0] — 2026-03-22

### 新增
- 跨项目消息系统（2 个 MCP 工具 + 4 个 API 端点 + 全局数据库）
- 自动更新机制（scripts/update.py + install.py --update）
- SessionStart 24 小时冷却更新检查
- 独立安装：hook 复制到 ~/.claude/hooks/ai-team-os/
- 全局 MCP 注册到 ~/.claude/settings.json

### 变更
- 安装步骤缩减为 3 步（API 随 MCP 自动启动，无需手动启动）

## [0.4.0] — 2026-03-21

### 新增
- 按项目数据库隔离（阶段 1-4）
- EnginePool 带 LRU 缓存的多数据库管理
- ProjectContextMiddleware（X-Project-Dir 请求头路由）
- 迁移脚本：按 project_id 拆分全局数据库
- StateReaper + 看门狗多数据库适配
- 仪表盘项目切换器
- install.py：完整入门流程（hook + Agent + MCP + 验证）
- GET /api/health 健康检查端点

### 修复
- Windows UTF-8 编码修复（所有 hook 脚本从 gbk 转为 utf-8）
- 团队模板引用实际的 Agent 模板名称

## [0.3.0] — 2026-03-21

### 新增
- 工作流强制执行：规则 2 任务墙检查 + 模板提醒
- 本地 Agent 阻塞（B0.4）：所有非只读 Agent 必须有 team_name
- Council 会议模板（3 轮多视角专家评审）
- 会议自动选择：跨 8 个模板的关键词匹配
- 团队关闭时级联关闭会议
- find_skill MCP 工具，3 层渐进式加载
- task_update MCP 工具 + PUT /api/tasks/{id}
- 6 个新增 MCP 工具（共 55 个）
- 467+ 个测试

### 修复
- S1 安全正则捕获大写 -R 标志
- S1 heredoc 误报
- 规则 7 任务墙计时器初始化
- 会议过期时间从 2 小时调整为 45 分钟
- B0.9 基础设施工具豁免于委派计数器

## [0.2.0] — 2026-03-20

### 新增
- LoopEngine 与 AWARE 循环
- 任务墙（评分排序 + 看板视图）
- 调度器系统（周期性任务）
- React 仪表盘（6 个页面）
- 会议系统（7 个模板）
- 26 个 Agent 模板，覆盖 7 个类别
- 失败炼金术（抗体 + 疫苗 + 催化剂）
- 假设分析
- 国际化支持（中文/英文）
- 研发监控系统（10 个信息源）

## [0.1.0] — 2026-03-12

### 新增
- MCP 服务器 + FastAPI 后端
- CC Hooks 集成（7 个生命周期事件）
- 团队/Agent/任务/项目管理
- SQLite 存储 + 异步仓储
- 会话启动时行为规则注入
- 事件总线 + 决策日志
- 记忆搜索
