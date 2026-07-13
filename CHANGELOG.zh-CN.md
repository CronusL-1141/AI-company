# 变更日志

AI Team OS 的所有重要变更均记录在此文件中。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/)

## [1.9.0] — 2026-07-13

### 新增 — 记忆系统 v2（双层台账 + 按需整理）

- **情景层升表**（`573c0b8`）— task memo 从 tasks.config JSON 数组升为独立 `task_memos` 表（行级 ID/失效轴/质量分/scope_path），123 条历史零丢失回填，原 JSON 冻结为档案；`task_memo_add` 接口不变新增 `supersedes` 置换。
- **方向层激活**（`c2ffd31`）— memories 表承载用户偏好/纠正/设计意图（kind 四类），MCP `memory_add`/`memory_invalidate`/`memory_list`；体量红线 ≤40条×400字；SessionStart+SubagentStart 双 hook 常驻注入（≤2000字，API 不可达静默）——派出的 agent 出生即继承团队偏好。
- **按需整理**（`0666cdf`）— `memory_reconcile_candidates`（BM25 粗筛聚簇，零 LLM）+ `memory_reconcile_apply`（合并/失效/打分/蒸馏提升，幂等）；"agent 算、工具存"架构；量阈软提示。工具数 161→166。
- 设计文档 `docs/memory-v2-design.md` 入仓：三路工业调研（逐路对抗核验）+ Kun Chen 案例内容标准。

### 新增 — 工具渐进式加载（P1 alwaysLoad 轮换 + P2 分组开关/只读档 + P3 模板最小权限）

- **P1 - 会话启动期 alwaysLoad 轮换**（`dc5d652`）— 极少数近期高频工具挂 `_meta {"anthropic/alwaysLoad": true}`，CC 据此对它们豁免 ToolSearch。白名单每会话启动从 `agent_activities` 重算一次（7 天频次 + 跨天数≥2 挡时段爆发 + 20% 迟滞防抖，硬顶 ≤5）。纯增益：统计查询失败静默降级为全 defer。后端 `GET /api/tools/always-load`。
- **P2 - `AITEAM_TOOLSETS` 启动期分组开关** - 24 个能力域 toolset，启动时按环境变量决定注册哪些模块。`default` = task/team/memory/infra/reports（44 工具，硬顶 ≤50）；`default,ecosystem` 增量挂载；未知组名 stderr 警告并忽略（绝不因配置错拉不起 server）。无 env / `all` 保持全量 166 向后兼容。
- **P2 - `AITEAM_READONLY=1` 只读档** - 与分组正交叠加，注册后按显式 `WRITE_TOOLS` 清单（不靠命名模式猜）剔除全部写工具、保留读工具。`os_restart_api` 虽走 GET 但重启进程，手工补入写清单；`diagnose_task_failure` 等纯分析 POST 留在读侧。
- **P3 - agent 模板工具裁剪** - 首批保守圈定的 CC subagent `disallowedTools`（结构性 denylist）：会议主持/辩论正方/辩论反方裁掉 git 写 + 项目/团队删除 + os_restart_api + ecosystem 写族（各 34 个）；技术文档工程师另加 `task_run`；项目经理仅裁 git 写 + os_restart_api。全部读工具与会议/memo 记账工具保留，工程/测试类模板不动。ecosystem 写族取自 `toolsets.py::WRITE_TOOLS`。
- 设计文档 `docs/tool-loading-design.md` 入仓（三路调研 + 开源多项目收敛）。工具总数仍为 166 - 分组是运行期环境开关，非注册表变更。

### 修复

- 双审查 4 项 major 全修（`ca50607`）：检索路径失效过滤/supersedes 红线绕过/回填双实例竞态（uuid5+INSERT OR IGNORE）/hook 注入单行化清洗（跨 agent 提示注入面关闭）。
- delete_project 级联补 task_memos；Rule13/relevance_score 历史失败测试清零——全量 1710 passed / 0 failed（`40935fd`）。
- 集成测试真实文件污染隔离：team-defaults.json 重定向 tmp（conftest）。

### 变更 — 调度器退役

- 周期 cron 改按需 `ecosystem_refresh`（`6bbdc58`，CC 非常驻裁定）：删启动自动注册与 4 条死 cron；`POST /api/ecosystem/refresh` 实测 81 仓 107s 全通。install.py 补注册 deep_review_link/meeting_ecosystem_writeback 两漏装 hook。

## [1.8.1] — 2026-07-10

### 新增 — 模型治理（用户作用域，零强制）

- **可用模型文件真相源自动拉取**（`899217c`）— `model_discovery.py` 扫描本机全部 CC transcript 尾部，聚合你真实用过的模型（109 文件约 1s + 60s 缓存；过滤 `<synthetic>`；层级别名标注）。实扫发现 `deepreasoning-coding-max-4.7`——任何硬编码清单都不会收录的第三方模型。
- **默认启动模型写入 `~/.claude/settings.json`** 的 `model` 键，新开 CC 会话生效。三层写保护：只动该键 / `.bak-aiteam` 备份 / tmp+rename 原子写；settings 损坏时拒写不破坏原文件。
- **REST `/api/models/*` + MCP `model_config_get`/`model_config_set`**（工具数 158→160）；设置页新增「模型治理」卡（默认模型选择 + 自动发现清单表）。
- **仅软提示、绝不拦截** — 注册 agent 时模型不在清单只在响应附提示、照常注册。ultracode/Workflow 完全豁免：模型选择归 CC 编排器（用户裁定 2026-07-10）。
- **push 前预检脚本**（`a00973e`）— 把"提交后才发现 CI 红"提前到本地。

### 修复

- **ModelSelect 下拉退化**：当前值（如 `fable` 别名）不在清单时曾退化成纯输入框——现始终显示下拉、当前值置顶（`91180ab`）。
- **「保存（演示）」按钮**：通用/端口两节的历史遗留未接线按钮——已禁用并附悬停说明。
- **最后的硬编码模型默认清零**：`DefaultsConfig.model = "claude-opus-4-7"`（4-7 幽灵最后巢穴）与常驻成员默认 `claude-sonnet-4-6`——空串=继承默认启动模型，模型换代零维护。
- **CI 三红转绿**（`25f85f9`）— ruff 全清、108 个失败单测归零、eslint 0 error。
- 任务墙提醒在无活跃团队时改荐 `task_list_project`（`taskwall_view` 需团队）。

### 变更

- **模板灵活性明示**（`b27421e`）— `team_setup_guide` 提示与 SessionStart 简报注明三条自由通道：`general-purpose`+自定义 prompt（零模板组队）、编制按任务增删、`plugin/agents/*.md` 随时可改可增。模板是起点，绝非必须。

## [1.8.0] — 2026-07-10

> 自 v1.6.2 以来首个完整功能公开发布——整条 v1.7.0 线（此前仅私有）连同以下内容一并进入公开仓库。

### 新增 — 知识层 P1：引用图谱 + 统一检索

- **跨域引用图谱（P1a）**（`c0f49e8`）— 零 LLM 正则抽取器从任务 memo 和报告中挖出 OS 原生 ID 引用（`wf_` id / commit hash / 任务 uuid / `[[记忆]]`），落入 append-only 的 `knowledge_links` 表（UNIQUE 五元组去重）。图谱是派生视图——随时可从源文本重建；附历史数据回填脚本。
- **统一检索（P1b）**（`6ba4a5f`）— `/api/search` 三臂 RRF 融合（k=60）：BM25 全文（中文 bigram 原生）、知识图谱扩散（查一个 ID 连带拉出所有关联物）、精确 ID 前缀/标题匹配。Dashboard 顶栏全局搜索框；MCP 工具 `unified_search` / `link_query` / `link_trace`。

### 新增 — 治理

- **红线不变量机检**（`fe0d843`）— `scripts/check_invariants.sh` 机检五项事故沉淀的不变量（hook 双副本同步、版本五处锁步、双 dist 一致、dist 时效、venv 禁令），配 CI 快速失败 job。

### 安全

- **堵住 InputGuardrail 大 payload 绕过**（`2a5fd46`，公开 issue #1）— 超 16 KB 的请求体曾完全跳过 L1 guardrail，恶意 payload 填充过 16 KB 即可绕过全部检查。现在 2 MB 硬上限内全量检查（超限 413）；危险模式规则全文扫描（撤销每字符串 10 KB 截断窗口）；XSS 规则去 ReDoS（`<script[^>]*>` → `<script\b`——前者 O(n²) 回溯，2 MB 洪水输入实测 ~113 秒）。新增 15 项回归测试。

### 修复

- **归属铁律**（`bb78aa0`）— session 启动目录是唯一归属权威：子目录不再另立幽灵项目、移除自动注册、回执迁移加负排除 + 孤儿收尸；SessionEnd 不再误杀运行中的 workflow 队。
- **per-run 建队提前到回执时点**（`3a31d8b`）— kill 中途（或超长 turn）的 run 不再在项目页隐形。
- **`project_delete` 级联 500**（`16dd004`）— 级联引用了不存在的 `EventModel.team_id`，删除恒返 500。
- **项目页 run 摘要恢复**（`f97be00`）— 请求 limit 500→200；被静默吞掉的 422 曾让整个行内摘要特性失效。

### 内部

- 每日流量归档 workflow（`7dd9d95`）— 仅在私有仓运行（仓库守卫）；公开发布前净化批次（`7b195c7`）。

## [1.7.0] — 2026-07-07

> 说明：1.6.2 为内部过渡版本号（五处版本锁步用，从未打 tag / 发布），其内容作为 1.7.0 的一部分随本版发布。

### 新增 — Workflow 观测层 Phase 2：live 追踪 + 相位泳道

- **journal 增量 tail**（`32becb5`）— 按字节 offset 尾读 `journal.jsonl`（只消费到最后一条完整行），Workflow 回执携带的 `transcript_dir` 持久化后直接寻址，运行期 `live_tokens` / `last_activity_at` 近似值（终态由 `wf_<id>.json` 文件值覆盖），900s 保守 `interrupted` 判定，`mtime_ns:size` 指纹短路让稳态对账近零成本。
- **相位泳道 UI**（`/workflows` 详情页）— 逐 agent 时间条按 phase 分组，running 期实时轮询推进，遥测表可排序。
- **回执 / 认养加固**（`10abd20`）— 早到的 workflow agent 先由会话兜底队（`workflow-session-<sid>`）认养，`wf_id` 一旦可见即迁入 per-run 团队；子 agent 模型不再落错误的硬编码默认值。
- **嵌套 workflow 布局支持** — 从 subagent 内启动的 run 落盘在 `<session>/subagents/workflows/<wf_id>/`；live tail 与终态文件对账均能解析该布局（agent 语义标签在终态自动补全；running 窗口期标签缺口属 CC 落盘时序限制，缓解方案已上任务墙）。

### 新增 — Leader 身份文件真相源直读（零注册依赖）

- **`session_probe` 模块**（`d75b3de`）— 项目的 Leader 就是 `~/.claude/projects/<slug>/` 下最新的 CC 主会话：文件 mtime = 活跃度（15 分钟窗），transcript 尾读 = 当前真实模型（`/model` 随时切换一轮内跟上；compact 写入的 `model:"<synthetic>"` 合成行被跳过，`05f4092`）。`project_summary` 直出磁盘探测的 `leader` 块，Dashboard LeaderCard 不再走团队链查找（Leader 行寄生的 workflow 队跨项目迁移时团队链会断裂）。
- **Leader 活性**（`44c7f91`、`1ab5c37`）— 工具事件在流即把 offline Leader 复活为 busy（60s 节流触摸）；state reaper 按角色豁免 `leader` 与 `workflow-subagent` 的配置探活（此前只按字面名 "team-lead" 豁免，行名实为 "Leader" 导致每个 tick 收割刚复活的行）。
- **每轮模型刷新**（`9327038`）— Stop hook 每轮尾读 transcript 更新 Leader 模型；曾因缺一行 `import json` 被静默吞掉两个提交周期，已修复并配离线复现回归测试（`44c7f91`）。

### 新增 — Dashboard ultracode 化改版

- **workflow 名称为主的展示**（`f01c590`）— 各处以 run 名称为主标题，`wf_` 编号降级为等宽淡色小标；运行列表按 `COALESCE(started_at, created_at)` 排序（历史收编曾把最新运行埋没）；`/pipelines` 展示层退役并重定向 `/workflows`。
- **项目详情行内摘要（方案 A）**（`87aecd3`、`daa2df0`）— 活跃区与历史区的 workflow 团队行内直接展示 run 摘要（泳道同色系状态徽章、agent 数、总耗时、完成时刻）+「查看泳道 →」直达链接；活跃 workflow 团队以 `run.summary` 为副标题，成员显示观测层阶段标签（`audit:…`），`wf-<ccid>` 编号降级小字。
- **Leader 卡与会话计数**（`3403e0f`、`20fff24`）— 模型全名直显（无别名映射，兼容未来非 Claude 模型接入）；项目会话数改文件真相源统计；空态卡替代整卡隐藏；磁盘迁移后的遗留项目注册清理。

### 变更

- **D5 双轴收敛**（`618e176`）— `stage_status` 唯一权威，`status` 改为派生只读投影，认领零窗口（DB 原子认领），幂等回填。
- **pipeline 退役 Phase 1–3**（`8fc3e2d`、`f01c590`）— 拆除新增入口硬拦、停用自动推进、展示层由 `/workflows` 接管；OS 定位为 CC ultracode 的持久化 / 观测层。
- **治理租约**（`00c861b`）— reaper + watchdog 共用单行 `governance_lease`（fail-open），跨进程同刻只有一个治理者；kill 路径先校验进程身份再动手。

### 修复

- **跨项目 workflow 归属**（`86c6900`、`44c7f91`）— 归属改随文件真相源：run 落盘路径的 slug 与注册项目的 `_project_slug(root_path)` 匹配（与 CC 目录命名逐字符一致）；团队跟随其 run 迁移；孤儿队 cwd 认领排除 workflow 队。59 条误归 run + 5 个队迁回真实项目；匹配不到的留空不猜。
- **`claude-opus-4-7` 幽灵模型**（`391a866`、`364060d`）— 四层烘焙默认值全部拔除（types 默认 / ORM 列默认 / `to_pydantic` 读注入 / MCP 工具参数）。读注入是真凶——DB 已清洗干净它仍在读路径上凭空再造幽灵。
- **重启僵尸误报**（`d6d6ed5`）— 优雅停机成功后残留 zombie PID 不再误报 `shutdown_timeout`；存活检查经 psutil status + `ps` state 前缀识别 `ZOMBIE`。
- **fastmcp 3.4.3 在 SOCKS 代理下启动崩溃**（`56733f5`）— 关闭启动期 PyPI 版本检查（`check_for_updates="off"`）；无 `socksio` 的 SOCKS 代理曾以 `-32000` 炸掉 stdio 重连。更新检测改 `git merge-base --is-ancestor`（严格落后才提示），根除本地领先时的「检测到新版本」误报。
- **`os_restart_api` 子进程 stderr 持久化**（`9d8f020`）— 重启拉起的 API 进程曾把 stderr 写进 tmpdir（重启机器即丢、巡检盲区）；统一并入持久的 `api-stderr.log`。
- **bootstrap 不再引导手动起第二个 uvicorn**（`f8da12b`）；autostart 跳过测试不再硬编码版本 `1.3.4`（`677c557`）。

### 新增 — Workflow 治理（跟随 CC ultracode）

OS 转向「CC 的持久化治理层」定位，不再与 CC 内置 Workflow 抢会话内编排。

- **Workflow 自动追踪为团队 + 识别为委派**（`abec404`）— `hook_translator._on_subagent_start` 新增 `workflow-subagent` 分支：按 transcript_path 里的 `wf_<id>` 严格「一 workflow 一团队」（`workflow-<wf_id>`）；成员按 `cc_agent_id` 去重（非 name）根治 16-agent 塌成 1 行；自动建团队、不要求已有 active team 根治 0 行注册；绑定到发起 workflow 的 Leader 项目。`_DELEGATION_TOOLS` 加入 `Workflow`——调 Workflow 即视为委派、重置 B0.9「为什么不委派」计数；PreToolUse / PostToolUse matcher 补 `Workflow` 让 hook 能看到调用。
- **Workflow 回写治理**（`29eab2b`）— `workflow_reminder` 新增 Workflow 软提醒（300s 节流）：调 Workflow 时提醒①总任务上墙（`task_create`）②让每个 workflow agent 用 OS 工具（`task_memo` / `report_save`）回写，指向新 skill `/os-workflow`（`plugin/skills/os-workflow`，含回写指令标准模板 + 脚本示例）；Dashboard TeamsPage 给 `workflow-*` 团队加「工作流」徽章；`CLAUDE.md` 新增「用 CC Workflow 时」一节固化约定。
- **严格一 workflow 一团队（per-run）+ Step 4 启动预登记解析**（`aac902f`）— `_promote_workflow_team`：`wf_id` 一旦在 `SubagentStop` / 子 agent 自身工具调用里可见，即把 agent 从 `workflow-session-<sid>` 兜底团队迁移到 per-run 的 `workflow-<wf_id>` 团队；`_parse_workflow_plan` 从 `tool_input.script` 静态提取声明式 phases + 字面 agent 数 + 动态 fan-out 节点数，`_on_pre_tool_use` 在 `tool_name==Workflow` 时 emit `workflow.planned` 预告事件。

### 修复

- **项目详情页团队列表消失**：`apiFetch` 原先把 `X-Project-Id` 头注入到*所有*请求，导致残留在 `localStorage` 的全局项目 pin 把 `/api/teams` 也限定到单一项目。ProjectDetailPage 是拉全量团队后客户端按 `project_id === projectId` 过滤，被限定后过滤结果为空，于是每个项目的活跃/历史团队全部消失。现把 `X-Project-Id` / `X-Project-Dir` 头**硬性限定为只给 `/api/ecosystem` 请求**，其它端点永不被项目作用域影响。
- **新项目 Leader 永久显示「空闲」**（`dfe5f67`）— 此前 `project_id` 仅在 SessionStart 绑定；新项目的 Leader 若在项目登记前 / cwd 未匹配时创建为 `project_id=None`，后续每次工具调用刷新 `last_active_at`（一直「活动」）却永不回填 `project_id`，项目判活便看不到它。修复：**服务端 `PreToolUse` hook** 抽 `_resolve_project_id_by_cwd` 最长前缀匹配 helper（与 SessionStart 复用），对 `project_id` 为空的 Leader 按 cwd 解析并补绑——任意工具调用即自愈，不再依赖 SessionStart 时机。
- **SessionStart 项目解析改最长前缀匹配 + Leader 绑定纠偏**（`b3f7cb6`）— cwd 可能同时前缀命中多个项目（父目录与其子目录项目），原 first-match 会把 Leader 绑到更宽的父项目（实测绑到 TUF 垃圾项目）。改用与 team-mapping fallback 相同的最长 `root_path` 匹配，并把补绑条件升级为「解析结果与现绑不一致即纠正」（session 只有一个 cwd，解析结果是权威）。
- **SessionStart 复用 session Leader 时补绑缺失的 `project_id`**（`c7b4c6e`）— session Leader 档案若生而无项目归属（实测累积 7 条孤儿 Leader 行），复用分支永不回填，导致项目判活匹配不到活跃 Leader、项目永远显示空闲；复用时若 cwd 已解析出项目且档案未绑定则补绑。

### 变更

- **生态库项目筛选归位**：项目筛选下拉本属于「按项目查看生态库」的功能，却被挂成全局 Header 切换器（`components/layout/ProjectSwitcher`）并在 `client.ts` 注释为「Global project context」，因而被误当成全局应用切换器。现移除全局 Header 处的切换器，组件改名 `EcosystemProjectFilter` 并迁至 `components/ecosystem/`，仅在 `/ecosystem` 列表页提供；相关注释补充历史教训与「勿全局化」硬约束以防复发。
- **项目详情头部布局重排**：描述独占整行（长描述可读性最优），当前团队 / 历史团队 / 创建时间三项紧凑成一排，不再四等分挤压描述。

### 文档

- **开发机迁移指南（Windows → Mac / VS Code）**（`7828e7b`）— 新增 `docs/` 指南（强制追踪，与 `ecosystem-recipes.md` 同例）：跨平台现状（代码无版本问题）+ 三样不随 git 走的东西（未推送提交 / `aiteam.db` 数据库 / `.mcp.json` · `.claude` 本机配置）+ Mac 完整安装步骤（含 DB 拷贝命令）。

### 新增 — Workflow 观测层 MVP（CC ultracode）

自带 pipeline 已定向废弃（与 CC 内置编排重复），OS 转型为 CC ultracode/Workflow 的**持久化观测层**。设计：hook 只当「时机 + 关联锚点」，落盘的 `wf_<id>.json` 富快照才是遥测真相源；完成检测 = reaper 保底轮询 + hook 流量对账，同一幂等 ingest，离线缺口自愈。_（本批将在发布时归入 1.6.2 或 1.7.0，由作者定——见备注。）_

- **新表 `workflow_runs` / `workflow_agents` + repository CRUD**（`6f9ceb4`）— 不可变快照文件的可重建缓存，按自然键 UPSERT 单调推进（绝不删行；审计轨仍走 `events` 表）；新增 `WorkflowRun` / `WorkflowAgent` 模型；顺带落地孤儿 `TeamState` / `TEAM_STATE_CHANNELS` 删除。
- **`workflow.planned` / `workflow.started` / `workflow.completed` 三个事件类型** — `EventType` 补 `planned` 成员，修复 planned 不在枚举导致 `create_event` 抛 `ValueError` 被吞的 Step 4「0 有效数据」真因。
- **摄取 + 对账**（`workflow_ingest.py`）— 回执解析 / 富快照摄取 / reconcile 对账（`mtime` 优于 `updated_at` 才重读——稳态只剩 `stat` 成本，`resume` 重写同名文件自然触发再摄取）。`hook_translator` 新增 `PostToolUse(Workflow)` 回执锚点分支 + SessionStart 全量对账（加 `mtime` 短路）；`state_reaper` 无 running 即零 `stat` 的保底轮询。
- **REST `/api/workflows` 4 端点 + 3 个 MCP 工具**（`workflow_list` / `workflow_get` / `workflow_reconcile`）— MCP 工具总数达到 **155**。
- **Dashboard `/workflows` 页** — 列表卡片流 + 逐 agent 遥测详情页、侧栏入口、双语 i18n、`workflow.*` 事件实时失效、TeamsPage 工作流徽章改为可点击链接；双侧 `dist` 同步。
- **`_WF_STATUS_RANK` 补 `killed` / `failed` 终态** — 实测 69 份真实 wf 文件 10% 命中，缺失会永久把运行卡在 `running` 并打破 reaper 短路；前端状态联合 / 徽章 / 筛选同步。

### 变更

- **纯 Python BM25 接入检索主链路**（`15e4fe3`）— `retriever.py` 内置 BM25（TF 饱和 + IDF `ln(1+(N-df+.5)/(df+.5))` 恒非负 + 长度归一，沿用中文 bigram + 单字分词），替换 `rank_bm25` 可选依赖路径（`keyword_search` 保留兜底）。`search_memories` 从整串 SQL `ilike` 改为「scope 内近期窗口粗召回 → BM25 重排」：多词非连续查询（如 `Python 部署`）现可命中，旧实现必 miss（审计 M11、备忘录 D4）。
- **LangGraph 降级为可选 `[langgraph]` extra**（`f6b3140`）— `langgraph` / `langchain-anthropic` / `langchain-core` 移出核心依赖，三者只服务 CLI `aiteam task run` 的遗留图执行路径（`456512f` 后 CC Agent 已接管执行权），API / MCP / Dashboard 全程用不到；`team_manager.compile_graph` 下沉为运行期懒加载，缺依赖时给出 `pip install 'ai-team-os[langgraph]'` 指引。实证：装有 langgraph 的环境 `import aiteam.api.app` 后 `sys.modules` 不含 langgraph/langchain（审计 M21/M44、备忘录 D1 方案一）。
- **版本五处锁步 1.6.2 + 补写历史 CHANGELOG**（`7be8cd8`）— `__init__` / `pyproject` / `plugin.json` / 两份 marketplace 条目统一 1.6.2（此前 9 处发散 0.0.0–1.6.1 且 1.6.x 从未打 tag，H19；marketplace `metadata.version` 保持 1.0.0 为目录格式版本，非插件版本）；`pyproject` 增 pytest `pythonpath=['src']`（src 布局免 editable install，配合 CI）；CHANGELOG 双语补写 1.5.2 / 1.6.0 / 1.6.1 三段。`tag v1.6.2` 留待发布时由作者打。
- **CI 真门禁恢复**（`e2d725f`）— 去掉单测步 `2>&1 || true`（`4288ce3` 救火遗留，根因次日已被 `a050585` 修复但从未回退：此前 pytest 因 import 不到 aiteam 以退出码 4 失败仍显绿，0 用例真实执行，H15/H16）；依赖补 `typer` / `rich` / `alembic`；`aiteam` 可导入性由 `pyproject pythonpath=['src']` 提供，刻意不用 `pip install -e .`（历史红线）。

### 修复

- **插件清单解释器统一 `python3` + 首启自愈 `sys.executable`**（`715acc8`）— `plugin/hooks/hooks.json`（22 条命令）与 `plugin/.mcp.json` 由裸 `python` 改 `python3`（stock macOS 无 `python` shim 时 MCP + 全部 hook 整层 command-not-found，审计 H17/H18/H25）；`auto_install._self_heal_interpreter()` 首启把清单解释器改写为绝对 `sys.executable`（幂等 / 静默失败），把 `e2d0fbb` 铁律落地到静态分发路径，同时解决 macOS 缺 shim 与项目 `.venv` 劫持两难；`src/aiteam/hooks/install.py` 生成的 hook 命令同改用 `sys.executable` + 绝对脚本路径。
- **跨项目守卫回填 plugin 副本**（`715acc8`）— `workflow_reminder.py`（plugin 执行副本）回填 `_check_team_cross_project`；2026-05-08 浅扫泄漏事故的 v1.5.2 修复此前只存在于从不分发的 `src` 副本（H9）。
- **Dashboard 产物治理**（`fe5b682`）— 重建 Dashboard 并整体同步 `plugin/dashboard-dist`（含 `7550f33` 项目隔离修复，此前已提交产物停在 `29eab2b`，文档承诺的修复不在线上 UI 里，H21）；`dashboard/dist` 移出 git 索引（先前 force-add 的残缺快照——仅 `index.html` + 字体、无 JS/CSS——会遮蔽完整的 `plugin/dashboard-dist`，源码安装路径白屏，H10/H14）；`app.py` 候选目录发现加「缺 JS bundle 即跳过」硬化；version 改为引用 `aiteam.__version__` 根除 OpenAPI 版本漂移；SettingsPage 版本显示 v1.6.2。
- **`/api/tasks/compare` 路由恢复可达**（`2606297`）— 自 `082a0e7` 诞生即被 `{task_id}` 参数路由抢占恒 404（H2）；移到参数路由之前并加注释防回退，`task_compare` MCP 工具链恢复。
- **自启 API `stderr` 落文件防管道死锁**（`2606297`）— `_autostart` 的 `stderr=PIPE` 终身无人排空，traceback 累积满 ~64KB 缓冲将冻结整个 API（H22）；改为追加写 `~/.claude/data/ai-team-os/api-stderr.log`（premature-exit 诊断改尾读该文件）；`stdout=DEVNULL` 与命令数组不动（`0d7a063`/`e2d0fbb`/`84059f8` 铁律）。
- **install 自检恒假失败修复**（`b7e225b`）— `verify_installation` 改读 `~/.claude.json`（与 `register_global_mcp` 实际写入处一致，`a050585` 引入 CC 读取位置迁移后 verify 一直读错文件）；`_check_package` 改 `find_spec('aiteam')`（`pip show aiteam` 因发行名 ai-team-os 恒失败，H12）；`_write_project_mcp_json` 回退分支改 `sys.executable`（此前恰在全局注册失败的兜底路径上写裸 python 埋雷）；`scripts/install.py` `project_root` 修正为扁平仓库布局（自 `baf8eba` 诞生即假设从未入库的嵌套布局，STEP1 cwd 不存在直接崩，H11）。
- **`greenlet>=3.0` 入核心依赖（Apple Silicon 修复）**（`f6b3140`）— SQLAlchemy async 必需，但其自带平台标记不含 Apple Silicon macOS（`platform_machine=='arm64'` 不在 aarch64/x86_64 列表），漏装则 async 引擎首次连接即 `ValueError`——本机迁移实测踩中。

### 移除

- **删除 `semantic_cache` 全链**（`15e4fe3`）— `api/semantic_cache.py` + `routes/cache.py` + `mcp/tools/cache.py` + 30 条测试；自诞生即未接线的幽灵特性，`/api/cache/stats` 恒返回 0 误导使用者（审计 H3/M60）；README 双语撤下语义缓存宣传条目。

### 备注

- 以上全部条目随 **1.7.0** 一同发布。验证：观测层集成测试 27/27；autostart + MCP 套件 84/84；`tsc` + 生产构建双绿；归属 / 活性 / 标签补全均经真实运行实战验证。

## [1.6.1] — 2026-06-12

### 新增

- **多源 schema 准备**（`e1b3ddd`）— `ecosystem_repo_profiles` 加 `sources`（JSON list）+ `primary_source` 两字段（COLUMNS_TO_ENSURE + ORM Mapped + Pydantic 三处同步）；678 个 GitHub profile 已 backfill `sources=[{kind:github,…}]`。新增 `ecosystem_hf_fetcher.py`（HuggingFace Spaces 公开 API fetcher）**仅归档保留供未来参考**——PoC `dry_run` 实测 HF Spaces 与 Claude/agent/MCP 生态重叠率 0%，故**不接入主流程**。
- **每项目周期 cron 自动化**（`4e317f2`）— `deps.py` 启动时幂等注册 per-project 每周刷新 cron（`interval = refresh_interval_days × 86400s`，`action_type=emit_event`，`event=ecosystem.refresh.periodic`）；5 个项目自动装好，`next_run` 为 7 天后。
- **`os_restart_api` 标准化重启工具 + 优雅停机**（`896d5b9`）— MCP `os_restart_api(force)`：有 busy-agent 时拒绝；**端口钉死不漂移**；等旧进程死透才拉新的；健康验证后返回新旧版本与 PID。新增 `POST /api/system/shutdown`：返回后延迟 0.5s 自退，best-effort WAL checkpoint。两个等待循环加 200 次迭代硬上限（时钟被冻结/被 mock 时也必然终止）。这是刻意的**端口钉死**重启，与 `_autostart` 的空闲端口自动发现是两套策略（stdio 脱钩见下方修复）。
- **生态库 Phase 2 — 浅扫批次审批 gate**（`c74d53b`）— 新增 `ecosystem_shallow_batches` 表 + 6 个批次端点（create / list / detail / items / **approve** / **cancel**），批准前不入队（治理闸门）；新增批次管理页 + 6 个 react-query hooks；`ScanRun` 加 `metadata_changed_count`（「更新 N（含 M 真实变化）」展示）。
- **项目状态识别「CC 会话在线」**（`4370468`）— 状态改由真实 agent 活动时间派生，并在项目下拉框显示真实活动时间。

### 变更

- **默认模型 `claude-opus-4-6` → `claude-opus-4-7`**（`5a0f9a2`）— 后端 10 处硬编码默认值升级、4 个单测断言同步刷新；成员编辑 Select 刷新为 Opus 4.7 / Sonnet 4.6 / Haiku 4.5 三档；删除 SettingsPage 不生效的「默认 LLM 模型」下拉（其 `handleSave` 从不落库）。
- **弃用启发式字段停止读写**（`e1b3ddd`）— API `_profile_to_dict` / `_profile_to_list_dict` 不再返回 `relevance_category` / `relevance_score`；前端删「相关性 X/10」与「活跃集排名」。schema 列保留（仅停止读写，避免 migration 风险）。
- **项目状态文案** — 「关闭」→「空闲」（en: Inactive → Idle）（`6cb4914`）。

### 修复

- **老库重扫 + tick 去 budget + status API 语义**（`4e317f2`）— 浅扫队列候选检查改用 `pushed_at > last_shallow_refreshed_at`；跳过判定从 `status` 改用 `stage_status`（此前 678 个 `shallow_done` 脏数据行全被误跳过）；`tick` 一次性 dispatch 全部候选（并发控制移到 worker claim 阶段，不再限 15）；`queue_status` 用真实 stage + `claimed_by` 精确计数并返回 `pending / in_progress / done / failed`。实测 `tick dispatched=134 / skipped=0`。
- **`context_tracker` 默认窗口改为 1M**（`af495c7`）— CC transcript 剥离 model 的 `[1m]` 后缀，新机器 `~/.claude.json` 无 1M 历史条目时 fallback 误用 200K，把 17% 真实用量报成 85.3%，频繁假 CONTEXT WARNING 打断工作。`DEFAULT_CONTEXT_SIZE` 200K → 1M（删去 `>200K` 兜底分支）；保留 `CLAUDE_CONTEXT_SIZE` env 覆盖作为降级机制。
- **MCP `X-Project-Dir` header 中文 latin-1 编码错误**（`af495c7`）— `CLAUDE_PROJECT_DIR` 含中文路径（如 `AI团队框架`）时 urllib 用 latin-1 编码 HTTP header 失败，导致 `report_save` / `team_list` 等在中文环境下不可用；现发送侧 `urllib.parse.quote()` percent-encode，接收侧 `urllib.parse.unquote()` 解码。
- **`os_restart_api` spawn 与 MCP stdio 完全脱钩**（`3e7d320`）— 从 MCP 工具调用上下文 spawn 时，子进程继承了正被占用的 MCP stdio 管道，实测**卡死在导入前（9MB 永不推进）**。修复：`stdin=DEVNULL`、`stderr` 落 `%TEMP%/aiteam-api-restart.log`（保留可诊断性）、`close_fds`、Windows 加 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 分离标志。
- **项目判活时钟对齐「裸本地时间」约定**（`cfa6861`）— agent 时间戳全库以 naive local 写入（`hook_translator` / `StateReaper` 均用 `datetime.now()`），判活却按 UTC 比较，恒差本机 UTC 偏移（实测 4h），15min 阈值永不满足，项目永远「空闲」；比较与序列化均改为同一本地钟。

### 备注

- 本周期 `__init__.py` bump 到 **1.6.1**（`896d5b9`）。仅 GitHub 开发里程碑（延续 v1.4.0 / v1.5.0 模式），未发布到 PyPI/marketplace。

## [1.6.0] — 2026-05-13

### 新增 — 生态库统一设计 MVP（配置驱动 + 多源框架）

围绕**配置驱动的统一设计**（v4）重建生态扫描。用户通过最少约 3 个问题（数据源 / topics / 默认参数）启动扫描，`dry_run` 安全预览，自动生成 diff 报告 + 状态变化时间线。全部仍在**单库 `project_scope`** 模型内——生态表按 `project_id` 作用域隔离，**不分库**。

- **配置驱动 schema（P0）** — 新增 `DataSourceKind`（8 类源）/ `RepoActiveStatus` / `NormalizedSignal` / `DataSource` / `ScanProfile` / `EcosystemIndexDiff` / `EcosystemStatusChange` 类型；`ecosystem_repo_profiles` +6 字段（`canonical_id` / `source_kind` / `last_active_status` / `last_status_change_at` / `popularity_percentile` / `activity_score`，幂等 backfill 保留全部 265 仓）；4 张新表（`ecosystem_data_sources` / `scan_profiles` / `index_diffs` / `status_changes`）。
- **9 个新 REST 端点 + 5 个新 MCP 工具** — data_source CRUD、`scan_profile` GET/PUT、`quick_setup`、`index_update`（真实 `dry_run`，0 副作用）、`index_diffs` latest/history；MCP `quick_setup` / `data_source_create` / `scan_profile_update` / `index_update` / `index_diff_latest`。
- **判活简化（P1.A）** — stars 仅作**入库门槛**，入库后所有仓永久参与搜索。判活简化为：archived → `archived`；`manual_status='no_value'` → `manual_archived`；其他 → `active`；本次没扫到的老仓保留原状态（append-only）。新增 `manual_status` 列 + `POST /repos/{id}/manual_status` + `ecosystem_mark_no_value` / `clear_manual_status`。
- **`list` 端点分层（P1.B）** — 摘要序列化器（18 字段，不含 `shallow_summary`），`limit` 默认 20 / max 100 + `has_more`，防 token 爆炸；`/teams/{id}/agents` 加 max 200 上限（防 254-agent 历史事故）。
- **事件溯源** — 新表 `ecosystem_repo_events` 成为每个仓的单一数据源（discovered / topics_changed / stars_jumped / status_changed / …），时段 diff 通过 `GET /api/ecosystem/diff?from=&to=` 从事件动态算出。新增 `ecosystem_repo_events` / `ecosystem_diff_period` MCP 工具 + 详情页「事件历史 / 扫描研究历程」timeline tab。旧 `index_diffs` / `status_changes` 表与端点保留兼容。
- **真实 GitHub topics + 全局 `topicRankMap`** — fetcher 从「搜索 query 当 proxy 的 hint topics」升级到真实 `repositoryTopics`（per-repo `gh api repos/{owner}/{repo}`）；`compute_ecosystem_facet_counts` 加全局 `topics` 维度（678 仓产出 2425 unique topics，top：mcp 386 / claude-code 294 / ai 257 / claude 185 / …）。全局 `topicRankMap` 让每个 topic 在所有卡片和 StatsBar 顶部配色一致。
- **SST — 字段映射单一源** — 删除客户端 `inferStage()` 与 `relevance_category` 派生，后端 `_build_stage_map` / `_serialize_full` 为唯一权威；`is_active` / `active_rank` / `relevance_*` 标 `@deprecated`（保留兼容）。
- **入库 678 仓** — 一次真实 `dry_run=False` 把 DB 从 265 → 678（+413），并真实化 252 个老仓的 topics（如 `n8n-io/n8n`：`['mcp']` → 15 个真实 topics）；默认 `popularity_floor` 降低（github 5000 → 1000、huggingface 1000 → 200、npm / pypi 5000 → 1000）以更宽松收录。

### 修复

- **`GET /api/ecosystem/diff` 当 `from > to`** 现返回 HTTP 400，不再返回误导性的空 200（`4f1926e`）。
- 前端「加载生态档案失败」+ 分页 UI；scan_history 区分浅扫 / 深扫；空占位过滤；`RepoCard` 删除启发式 `lifecycle` tab 与类别徽章。

### 备注

- `__init__.py` bump 到 **1.6.0**（`355aae8`）。仅 GitHub 开发里程碑（延续 v1.4.0 / v1.5.0 模式），未发布到 PyPI/marketplace。

## [1.5.2] — 2026-05-11

### 修复 — 跨项目隔离 hotfix

由一次真实事故触发：**2026-05-08 派出的 5 个浅扫 agent** 本应服务生态平台，却误入 `topic-mapping-v8`（一个独立的量化备考项目）并在其中背书 / 写入研究产物——根因是 Leader 的 active-team 解析不是 project-aware 的。

- **`context_resolve` 改为 project-aware**（`infra.py`）— 先按 cwd 找 project 再 filter teams，避免会话拿到别项目的 active team。
- **`PreToolUse:Agent` 新增跨项目守卫**（`src/aiteam/hooks/workflow_reminder.py`）— 被派团队的 `project_id` ≠ 当前项目时，hook `exit 2` 硬阻断。
- **Stage 0 浅扫双通道写回** — 浅扫结果 MCP primary + SendMessage fallback 双通道回写，避免 MCP 工具暴露不稳定时整批全失败。
- **`research_count` 语义修复** — `_build_stage_map` 只数 `architecture_done`+ 的行，浅扫不再抬高深扫计数；`apply_summary` 不再写 `status='completed'`（浅扫完成 ≠ 整个评审完成）。
- **生态库 UI 重构** — 「深扫摘要」→「评审记录」（`ecosystem_deep_reviews` 表承载全部 4 stage，命名歧义修复）；`ReviewCard` 按 `stage_status` 智能渲染；`parseAsUtc` helper 修裸 datetime 字符串的时区 bug（「耗时 4h 1m」）；新增 `RecentScanRunsBar`。

**已知问题**：该跨项目守卫**仅**加进了 `src/aiteam/hooks/workflow_reminder.py`——`plugin/hooks/` 分发副本尚未回填 `_check_team_cross_project`，插件用户在该副本回填前不享有此硬阻断。

## [1.5.0] — 2026-05-08

### 新增 — 生态研究平台 v2：渐进式深扫漏斗

把 v1.4.0 的"一次性 5 段深扫"重构为 **4 阶段渐进式知识库漏斗**，让生态仓研究产物**累加而非一次性产出**。基于用户敲定语义实施，避免低价值仓浪费 token，并支持研究产物跨周期复用召回。

- **Stage 0 — 入档即浅扫（Stage A/B）**
  - 新建 `EcosystemShallowQueueWorker`（552 行），自动派 `ai-engineer` agent（5 并发）总结新入档仓
  - 浅扫内容：核心功能 / 定位 / 优势 / 适用场景（200-400 字）
  - Worker 每 5 分钟跑一次，返回 `DispatchIntent[]`（不直接 spawn agent — Leader 用 Agent 工具 + `team_name=ecosystem-platform` 实际派遣）
  - **8 类失败分类处理**：404→mark_deleted / 403_私密→mark_private / rate_limit→backoff+重试 / 5xx→指数退避重试 / agent_read→重试+换 agent / agent_timeout / json_parse→带示例重试 / fetch_style→pattern_record
  - **失败自学习机制**：同类 `fetch_style` 失败 ≥ 3 个不同仓 → 自动 `pattern_record(type='failure')`，未来 agent 通过 `pattern_search` 读到 lessons 优化策略

- **Stage 1 — 按需架构分析（Stage C）**
  - 新增 `ecosystem_deep_review_request_batch(tags, min_stars, limit)` MCP 工具
  - 用户挑研究方向（如 "memory_system"）→ 批量派 `backend-architect` 读架构关键文件
  - 输出：`architecture_md` 字段 + `stage_status='architecture_done'`

- **Stage 2 — 多角度辩论（Stage C）**
  - 新增 `ecosystem_trigger_debate(repo_ids, research_goal)` 直接调现有 `debate_start`（**不内建辩论引擎**，复用会议系统）
  - Leader 完全掌控辩论参与者和轮次（保留现有会议系统能力）
  - **会议→生态库反向写入 hook**（`meeting_ecosystem_writeback.py`）：会议结束 + topic 含生态关键词时，hook 提醒 Leader 派 agent 调 `ecosystem_apply_debate_result(risks_md / learnings_md / integration_md / integration_recommendation)`
  - 输出：每个 finalist 4 字段 + `stage_status='debated'`

- **Stage 3 — 参考 / 集成标记（Stage C）**
  - `ecosystem_mark_as_reference` 加 `lifecycle:reference` tag + `stage_status='referenced'`
  - `ecosystem_start_integration` 加 `lifecycle:integrated` tag + 通过 `task_create` 创建真实集成任务（**不内建实施引擎**，复用任务系统）
  - 两路径都保留研究产物便于未来快速召回（避免重复深扫）

### 新增 — 项目可定制阈值（Stage A）

- 新建 `EcosystemProjectSettings` 表（每项目独立）：`min_stars` / `top_n` / `refresh_interval_days` / `focus_topics` / `focus_languages` / `shallow_concurrency` / `deep_concurrency`
- AI Team OS 默认：`min_stars=5000, top_n=200, focus_topics=['claude-code','mcp','agent-framework']`
- 其他项目默认：`min_stars=1000, top_n=100`

### 新增 — 活跃/全量双视图 + 数据 append-only（Stage A/D）

- **决策 D**：数据**永不删除** — stars 跌出阈值的仓不删，仅 `is_active=False`；stars 涨回 → 自动激活 + 重新入队 Stage 0
- **决策 E**：周期浅扫**只扫活跃集**（top_n by stars），节省 GitHub API 配额
- 新建 `EcosystemRepoStatusSnapshot` 表，每次 scan 记录 stars/pushed_at/is_archived/is_active 供历史分析
- 新建 `EcosystemRefresher` service（480 行）：`shallow_refresh()`（diff 跳过 last_pushed_at 未变的仓）+ `recompute_active_set()` + `resurrect()`（删库/私密复活）

### 新增 — 前端漏斗 UI（Stage E）

- 列表页：stage 颜色徽章（queued/shallow_done/architecture_done/debated/referenced/integrated/_failed）+ 3 tab（活跃/全量/已删除）
- 详情页：新增"研究历程"timeline tab，显示 stage 推进 + agent 输出 + `shallow_summary_history` 历史快照
- 新页面 `/ecosystem/research` 候选筛选：输入研究目标 → tags 候选列表 + 浅扫 summary 预览 → 多选触发 Stage 1 → finalists 触发 Stage 2
- 项目详情页加 "Ecosystem 设置" tab，8 字段（min_stars / top_n / refresh_interval_days / focus_topics / focus_languages / shallow_concurrency / deep_concurrency / auto_shallow_on_archive）
- 失败仓：红色徽章 + "立即重试"按钮（调 `POST /api/ecosystem/profiles/{id}/retry`）

### 新增 — 11 个新 MCP 工具

- Stage 0: `ecosystem_apply_shallow_summary`, `ecosystem_shallow_queue_status`
- Stage 1: `ecosystem_deep_review_request_batch`, `ecosystem_apply_architecture_md`
- Stage 2: `ecosystem_trigger_debate`, `ecosystem_link_debate_meeting`, `ecosystem_apply_debate_result`
- Stage 3: `ecosystem_mark_as_reference`, `ecosystem_start_integration`, `ecosystem_link_integration_task`
- Refresh: (扩展 `ecosystem_scan_periodic` 加 refresh 策略)

### 新增 — 8 个新 REST 端点

- `/api/ecosystem/lifecycle/*`（Stage 1/2/3 触发链路）
- `/api/ecosystem/profiles/{id}/retry`（失败仓手动重试）
- `GET/PUT /api/ecosystem/projects/{project_id}/settings`
- `POST /api/ecosystem/shallow_queue/{apply_summary,tick}`, `GET /api/ecosystem/shallow_queue/status`

### Schema 变更（Stage A — 通过 COLUMNS_TO_ENSURE 完全向后兼容）

- `EcosystemDeepReview` +8 字段：`stage_status` enum / `integration_md` / 4 个 stage 时间戳 / `debate_meeting_id` / `integration_task_id`
- `EcosystemRepoProfile` +8 字段：`shallow_summary` / `last_shallow_refreshed_at` / `is_deleted` / `is_private_now` / `last_fetch_error` / `fetch_failure_count` / `is_active` / `active_rank`
- 2 张新表：`EcosystemRepoStatusSnapshot`（append-only 历史）/ `EcosystemProjectSettings`（每项目配置）
- 标签字典：26 → 31（新增 5 个 lifecycle 标签：`evaluating` / `reference` / `integrated` / `deleted` / `private_now`）
- Tag source enum +1：`lifecycle`（Stage 3 转换自动管理）

### 测试

- 1283+ 单元测试通过（1264 baseline + 110+ 新 ecosystem 测试覆盖 A/B/C/D/E）
- 0 回归
- 6 pre-existing failures（CLI version flag / debate template / mcp_autostart / pipeline）经 `git stash` 验证与本次无关

### 架构决策（用户敲定）

- **(A)** Ecosystem 是**知识库不是工作流引擎** — Stage 2 复用 `debate_start`，Stage 3 复用 `task_create`。Ecosystem 只做记录、召回、标注。
- **(B)** 会议→生态库反向写入 hook 提醒 Leader 把辩论结论回写生态库（保留 Leader 决策权）
- **(C)** 每个项目独立阈值 + 活跃/全量双视图
- **(D)** 数据 append-only 永不删除；星标跌出的仓保留以备未来复活
- **(E)** 周期浅扫只扫活跃集
- **(F)** Rate limit 测试驱动并发调优（不预设，实测调整）

### 备注

- 本版本**仅发布到 GitHub**（延续 v1.4.0 开发里程碑模式），PyPI 发布留待后续稳定性验证

## [1.4.0] — 2026-05-07

### 新增 — 生态研究平台（Stage A-J）

完整的项目隔离开源生态发现/打标/深度审查平台。专为 Claude/MCP/agent 开源生态设计。首扫入档 188 仓，三层打标平均 2.05 tags/repo，0 标签率仅 1.5%。

- **数据层（Stage B）** — 5 张新表：`EcosystemRepoProfile` 扩展 + `EcosystemDeepReview` 深扫报告 + `EcosystemTag` 标签字典 + `EcosystemRepoTag` 关联 + `EcosystemRelation` 仓与仓关系 + `EcosystemScanRun` 扫描批次。21 个 seed 标签；删除仓档案 CASCADE 删除关联，删标签 RESTRICT；50/50 单元测试。

- **周期扫描（Stage C）** — `EcosystemScanner` 服务，增量策略（< 7 天扫过的跳过）+ 全量策略 + ScanRun 审计 + GitHub API 优雅降级 + owner 黑名单 + 关键词白名单二次过滤。3 个新 MCP 工具（`ecosystem_scan_periodic` / `ecosystem_scan_status` / `ecosystem_scan_history`）+ 5 个 REST 端点 + 31 个新测试。

- **三层打标（Stage D）** — Layer 1 GitHub topics 直接映射（命中 105 仓）+ Layer 2 关键词规则（命中 70 仓）+ Layer 3 LLM dispatch_plan 模式派子 agent。5 个新 MCP 工具 + 26 个标签字典（capability/tech_stack/maturity/positioning 四类）+ 48 个单元测试。

- **多维搜索（Stage E）** — `ecosystem_search` 升级到 11 个参数（query/tags AND/min_stars/language/sort_by/has_deep_review 等），`ecosystem_repo_get` 返回 profile+tags+deep_reviews+relations+scan_run 全息详情，`ecosystem_search_by_capability` 按标签反向检索。SQLite NULLS LAST 模拟 + EXISTS subquery 实现 tag AND 语义。38 新测试，p95 < 50ms 目标。

- **深扫工作流（Stage F）** — 5 段式报告模板（真实定位 / 架构 / 借鉴点 / 风险 / 集成建议）+ `EcosystemDeepReviewer` 服务通过 `dispatch_plan` 派 Explore + backend-architect 子 agent（兼容 CC 子进程模型）+ PostToolUse `deep_review_link.py` hook 自动关联 report 到 `EcosystemDeepReview.report_id`。4 个新 MCP 工具 + 5 个 REST 端点 + 19 个新测试。

- **自动汇总（Stage G）** — 4 个 markdown 汇总工具：`ecosystem_summary_weekly`（周报）/ `ecosystem_summary_by_tag`（按方向）/ `ecosystem_summary_top_n`（Top N 排行）/ `ecosystem_summary_health`（平台自检）。自动 `report_save`（报告类型 `ecosystem-{weekly,by-tag,top-n,health}`）。Join 一次拉完避免 N+1。33 个新测试。

- **前端（Stage H）** — `/ecosystem` 列表页（4 列卡片 + 筛选栏 + 分页），`/ecosystem/:repoId` 详情页 + 4 个新组件（`CapabilityTags` / `DeepReviewSection` / `RelationsSection` / `ScanRunSection`），通过 `useEcosystemRepoFull` hook 消费 v2 API（UUID → full_name 反查 + path 段编码）。响应式 + Playwright 截图验证。

- **项目隔离（Stage J）** — 6 张 ecosystem 表全部加 nullable `project_id` 列；`EcosystemRepoProfile` 用 `(project_id, repo_full_name)` 联合 UNIQUE。`EcosystemTag` 字典保持 `project_id=NULL`（全局共享 21 seed）。`X-Project-Id` HTTP header → `get_scoped_repository` 路由；MCP `_api_call` 自动注入（基于 cwd 推断的 session 项目）。启动时自动 `backfill_ecosystem_to_project` 钩子迁移历史 188 仓到 AI Team OS 项目。前端 `setCurrentProjectId` 切换项目时同步。10 个隔离测试 + 1109 全套 unit 测试通过。

### 新增 — 标签质量打磨（Stage K4）

- **`ecosystem_tag_apply_batch` 加 `replace_auto` 模式** — 替换模式（默认 `False` 向后兼容）：先删除该仓所有 `auto_rule` 和 `github_topic` 来源的 RepoTag 行（保留 `manual` 和 `auto_llm`），再插入新规则的结果。修复了"新规则虽产生新标签但旧 `mcp_framework` 假阳性 99 仓没清"的 bug。

- **5 个新标签 + 边界规则** — `claude_code` / `agent_harness` / `javascript` / `java` / `docs_only` 加入 seed 字典。新增 `LANGUAGE_TAG_MAP`、`DOCS_ONLY_LANGUAGES`、`DOCS_ONLY_NAME_PATTERNS` 三组 Layer 2 子规则。`mcp_framework` 假阳性率从 37%（99/265）降到 **0.8%（2/265）**，平均标签数从 **1.01 → 2.05**，0 标签率从 **28.7% → 1.5%**。

- **18+ 边界仓实地调研** — `docs/ecosystem-tag-edge-cases.md` 记录真实 anomaly（n8n / dify / awesome-mcp-servers / claude-cookbooks / hermes-agent / netdata / JavaGuide 等）+ 根因 + 规则修复。

### 性能 — 搜索优化（Stage K1）

- **`ecosystem_repo_profiles` 加 5 个复合索引**：`(project_id, stars)` / `(project_id, category, stars)` / `(project_id, language, stars)` / `(project_id, pushed_at)` / `(project_id, is_archived, stars)`。EXPLAIN QUERY PLAN 验证 TEMP B-TREE 全部消除。

- **search p95：2057ms → 13.1ms（156x 提升）** — 100 次 random query 实测（真实生产数据，265 仓）。p50 6.6ms / p99 25ms。

- **`ecosystem_search` 缺省行为修复** — `tags=[]` 现在跳过 EXISTS subquery（避免全表扫），返回按 stars 排序的全集而非空。

- `compute_ecosystem_facet_counts` 重构为单次 SELECT 三列 + Python 聚合（IO 减 2/3）。

- 6 个新性能 regression 测试。

### 修复

- **`context_tracker` 新 model 变体的 1M 检测** — `claude-opus-4-7` 等新 opus 模型在 1M 模式下被误判为 200K window，导致 198K tokens 时报告 99% 假警告。两层检测：(1) 精确 `{model}[1m]` 匹配；(2) 同 family 兜底（任意 `claude-{opus|sonnet|haiku}-*[1m]` 历史 → 该 family 视为 1M）。新增 `CLAUDE_CONTEXT_SIZE` env 终极覆盖。4 个新测试 + module 级 autouse fixture 隔离 `~/.claude.json`。

- **新 agent 注册到 completed 团队自动恢复** — hook_translator 现在检测到新 agent 注册到 `status=completed` 的团队时自动恢复为 `active` 并发出 `team.auto_revived` 事件 + 警告日志。替代了之前的硬阻断方式（曾导致历史团队上的长任务被中断）。

### 前端 bug 修复（Stage K2）

- **详情页 `深度档案区` 占位符移除** — 之前详情页硬编码 "TODO: Stage E v2 API" 占位文案，但 v2 API（`/profiles/{name}/full`）从 Stage E 起就已存在。`useEcosystemRepoFull` hook 现在直接消费 v2（含 UUID → full_name 反查 + path 段斜杠编码）。v2 失败时优雅降级到 v1 列表数据。

### 变更

- **Plugin description 升级** — 反映 140+ MCP 工具（含 30+ 生态研究工具）+ 生态研究平台。新增 marketplace 标签：`ecosystem-research`、`github-discovery`、`code-mining`。

## [1.3.4] — 2026-04-14

### 修复
- **紧急：升级自 1.3.0 之前版本的数据库上 `meeting_send_message` 500** — 1.3.3 的 `_sqlite_migrate()` 补了 `meetings.meta_json`，但漏掉了 `meeting_messages.metadata_json`。老数据库（在该字段加入 ORM 模型前创建）上的所有 `meeting_messages` INSERT/SELECT 都会抛 `OperationalError`。修复方案：将 `_sqlite_migrate()` 重构为数据驱动循环，统一遍历 `COLUMNS_TO_ENSURE` 列表（同时覆盖 `meetings.meta_json`）。所有迁移项均通过 `PRAGMA table_info` 保护，幂等安全。
- **迁移框架改为数据驱动** — 未来新增字段只需在 `COLUMNS_TO_ENSURE` 列表 append 一行。

## [1.3.3] — 2026-04-14

### 修复
- **紧急：外部项目调用 `meeting_create` API 崩溃 500** — 三个根因一次修复：
  1. **`meta_json` 列缺失** — 旧数据库 `meetings` 表没有该列（该字段加入 ORM 模型前创建的库），`init_db` 用 `create_all` 不会为已存在表补列，INSERT 直接报 `OperationalError`。新增 `connection.py` 启动时幂等 SQLite 迁移，缺列则 `ALTER TABLE` 补全。
  2. **team_id 未按名称解析** — `POST /api/teams/{team_id}/meetings` 路由接收团队名（如 `"repo-insight-build"`）但未转 UUID，直接传给仓储层导致后续查询静默失败。路由现在先按 UUID 查、再按 name 查，都找不到返回 HTTP 404。
  3. **ORM 异常未捕获导致 worker 假卡** — `create_meeting` 调用外围加 `try/except`，DB 错误以 HTTP 500 JSON 返回，不再让 worker 卡死。

## [1.3.2] — 2026-04-14

### 修复
- **紧急：MCP 动态端口发现失效** — `plugin/.mcp.json` 把 `AITEAM_API_URL=http://localhost:8000` 硬编码为 env var，覆盖了 `_get_api_url()` 中的动态端口 fallback。当 autostart 因 8000 被占用而选择空闲端口（如 59711）时，MCP 工具仍连接 8000 并报告 `unhealthy`，而 hook 走同一代码路径却正常工作。现已从 plugin 配置、根目录 `.mcp.json` 及所有安装脚本中删除该 env var，MCP 现在正确回退到读取 `api_port.txt` 动态发现端口。用户手动设置的 `AITEAM_API_URL` 仍具最高优先级（用于远程 API 场景）。

## [1.3.1] — 2026-04-13

### 修复
- **Hotfix: context_tracker 1M context window 检测** — transcript 中 model 字段为 `claude-opus-4-6`（无 `[1m]` 后缀），导致 1M context session 被误判为 200K，出现 342% 等异常百分比。新增 token 数量 fallback：若 `used_tokens > 200K`，自动识别为 1M context window。

## [1.3.0] — 2026-04-13

### 新增
- **CC 原生集成（Track A）**
  - `TaskCompleted` hook 硬门控 — `task_completed_gate.py` 在任务缺失 memo/result 时 exit 2 拒绝完成，把 verify_completion 从"软提示"变"硬拦截"
  - `TaskCreated` hook 桥接 — `cc_task_bridge.py` 把 CC 原生任务自动镜像到 OS 任务墙
  - `PermissionDenied` hook 接入分类器 — `permission_denied_recovery.py` 调用新 `POST /api/hooks/diagnose_denial` 端点，返回 4 类决策：`recoverable_with_retry` / `recoverable_with_workaround` / `needs_user_approval` / `permanent_denial`
  - 8 个大数据 MCP 工具添加 `meta={"anthropic/maxResultSizeChars": 500000}` 注解（`taskwall_view` / `task_list_project` / `report_list` / `report_read` / `event_list` / `meeting_read_messages` / `memory_search` / `team_knowledge`）
  - `wake_agent` 启用 `--bare` + `--exclude-dynamic-system-prompt-sections` 优化，预期启动延迟降 50%；长 prompt 走临时文件 fallback 绕过 Windows 命令行长度限制

- **会议系统完整重设计（Track B）**
  - `meeting_create` 返回完整 `dispatch_plan[]` — 每个参与者带 ready-to-paste 的 `Agent()` 启动参数，彻底消除 Leader 代打问题
  - 结构化 `participants` 输入：`{name, agent_template, role, context_files, expected_output}` 替代旧字符串列表（向后兼容）
  - `meeting_attendance_check(meeting_id)` — 查询当前轮次已发言/未发言参与者 + 超时跟踪
  - `meeting_send_message` 新增 `caller_agent_id` 参数 — 代打审计，调用者与 agent_id 不一致时打 `impersonation: true` 元数据并记录事件日志
  - `meeting_conclude` 默认 `validate_attendance: true` — 未全员发言返回 400 + missing 清单；`force=true` 绕过但记录 `meeting.forced_conclude_with_missing` 事件
  - `Meeting.meta_json` 持久化字段存储 `expected_participants` 和轮次状态

- **会议模板迁移到 Plugin Skills（Track C）**
  - 8 个模板从硬编码 `templates.py` dict（234 行）迁移到 `plugin/skills/meeting-facilitate/templates/*.md` 文件（brainstorm/decision/review/retrospective/standup/debate/lean_coffee/council）
  - 每个模板含 YAML frontmatter 结构化轮次数据 + markdown 正文（何时使用 / 参与者建议 / 反模式）
  - `templates.py` 重写为懒加载 YAML loader（107 行），保持 API 向后兼容
  - **用户可扩展**：drop 一个 `.md` 文件即可新增自定义会议模板，无需改 Python 代码
  - 利用 CC 的 progressive disclosure 模式 — 模板仅在需要时加载，零 token 消耗
  - 完全重写 `plugin/skills/meeting-facilitate/SKILL.md`（355 行）：7 步生命周期对接新 dispatch_plan API + 模板选择决策矩阵 + 3 个端到端场景 + 7 条反模式警告

- **上下文追踪改为 transcript 直读（Plan E）**
  - 新增 `context_tracker.py` hook 注册到 `UserPromptSubmit` — 从 hook payload 读 `transcript_path`，解析 session jsonl 最后一条 assistant message 的 `usage.input_tokens` + cache tokens，获得 100% 精确的上下文使用率
  - 自动识别 1M 上下文窗口（通过 model 标识符 `[1m]` 后缀）
  - `>=80%` 触发 CONTEXT WARNING，`>=90%` 触发 CONTEXT CRITICAL，带 token 明细
  - **完全不依赖 statusline** — 分发版用户无需安装自定义 statusline 也能工作
  - **天然项目隔离** — transcript path 本身就编码了项目身份，彻底消除跨项目 monitor 文件 bug

- **项目自动注册流程**
  - 新增 `POST /api/context/resolve` 端点，支持精确匹配/前缀匹配/自动创建三种策略
  - `session_bootstrap.py` 检测未注册目录并注入注册询问提示给 Leader（非阻塞）
  - 新增 `dismiss_project_registration(cwd)` MCP 工具 — 用户可拒绝注册；持久化到 `~/.claude/data/ai-team-os/dismissed_projects.json`
  - 修复新项目目录（如 `靖安笔试`、`repo-insight`）必须手动触发才能注册的 bug

### 变更
- **任务墙自动同步（`workflow_reminder.py`）**
  - PreToolUse：提取 agent prompt + description，与项目任务墙 pending 项做关键词匹配，Leader 派遣未匹配墙上任务时发出警告
  - PostToolUse：新增 `_post_tool_taskwall_sync()` — Agent 派遣时自动更新匹配任务为 `running`；完成 SendMessage 时自动更新为 `completed`
  - 报告数据目录警告精确到 `.claude/data/ai-team-os/reports/` 路径，不再对源码误报

- **会话启动上下文工程**
  - 移除损坏的"读取 `~/.claude/context-monitor.json`"指令（文件已不再维护）
  - 新指令：hook 已自动监控上下文，Leader 只需专注任务推进
  - 未注册目录检测到时注入项目自动注册提示块

- **文档更新**
  - `README.md` / `README.zh-CN.md` 反映新会议系统和模板架构
  - Skill 文档按 CC progressive disclosure 最佳实践重组

### 修复
- **分发版同步** — 4 个 hook 脚本在 `src/aiteam/hooks/` 和 `plugin/hooks/` 之间失同步（缺失 `_get_api_url()`、项目注册检查、任务墙自动同步逻辑）。分发版用户会遭遇动态端口失效和功能静默缺失。所有 4 个文件现在在 dev 和分发副本之间字节级一致。
- **`meeting.py:103`** — `_build_dispatch_plan` 返回类型注解对齐实际三元组（补上 `legacy_warnings`）
- **`context-monitor.json` 跨项目污染** — 旧 `_find_monitor_file()` 用 glob 扫所有项目按 mtime 取最新，会读到其他 session 的过期数据。已被 `context_tracker.py` 完全替代，后者用 `transcript_path.parent` 天然隔离
- **定时唤醒误报** — 自动唤醒 prompt 不再读取 9 天前的全局 `context-monitor.json`（它错误地总是报告 <10% 无论实际用量如何）

### 移除
- `src/aiteam/hooks/context_monitor.py` 和 `plugin/hooks/context_monitor.py` — 被 `context_tracker.py` 取代
- 全局 `~/.claude/context-monitor.json` 依赖 — OS 不再读也不再写

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
