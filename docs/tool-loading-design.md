# 工具渐进式加载设计 — 调研判定与分期规划

> 状态：规划稿（待用户批准实施）｜2026-07-13
> 依据：三路一手调研（wf_ad494800：github-mcp toolsets / Anthropic Tool Search / 渐进式披露与 subagent 授权）
> 台账：任务 7ff4ee65；完整调研数据 /tmp/tool_loading_research.json + wf journal

## 0. 核心判定

**"懒加载"本体不做**——CC 客户端已在 harness 层完成（ToolSearch defer：schema 按需拉取），官方两大痛点（上下文膨胀、选择准确率）在 CC 组合下已被解决。OS 再自建检索式加载 = 重复造轮子；embedding 检索另有反证（ToolRet/ACL2025：传统 IR 强模型做工具检索表现差）。

**但官方硬数字确认我们踩线**：Claude 工具选择准确率在 **30-50 个工具后退化**（官方原文），OS 166 个远超；ecosystem 一族 47 个近义名（summary_*/deep_review_*/scan_*）正是官方点名的"similar names 误选"高危区。CC 的 defer 只拿走 schema，**166 个工具名清单仍常驻每个会话**。

**残留收益的正确归属**（调研方向三的关键结论）：按角色裁剪工具面在 CC 世界是**客户端职责**（subagent frontmatter 的 tools/disallowedTools，支持 `mcp__ai-team-os__*` 整组模式，结构性拒绝而非软护栏）；server 端缺少可靠的"我是谁"信号（MCP 无会话身份标准，gap 已确认）。

## 1. 分期规划（全部零新依赖、零向量）

### P1 — alwaysLoad 核心工具（官方杠杆，改动最小，**准入从严**）
对极少数工具打 `_meta: {"anthropic/alwaysLoad": true}`（CC v2.1.121+），豁免 defer 免检索直达。

**成本模型（用户 2026-07-13 裁定的从严依据）**：defer 工具只驻留名字（~10 token），alwaysLoad 驻留完整 schema（200-500 token/个），且是**每会话+每派出 agent 的固定支出**；而 ToolSearch 往返本身成本很低——收益小、成本固定，故门槛必须高。

**白名单 = 算法动态轮换（用户 2026-07-13 二次裁定：不叠加、不手调）**，且实现被刻意压到最轻以防过度工程：

- **轮换粒度 = 会话启动期**：OS 的 MCP server 随每个 CC 会话拉起，启动时重算一次名单——天然轮换点，无运行时变更（CC 不保证消费 listChanged，且中途变更破坏 prompt cache）。
- **算法全文 = 一条 SQL + 迟滞**（实测 0.1 秒，无常驻/无新依赖/无内存驻留）：
  `SELECT tool_name, COUNT(*) c, COUNT(DISTINCT date(timestamp)) d FROM agent_activities WHERE timestamp > -7d AND tool_name LIKE 'mcp__ai-team-os%' GROUP BY tool_name HAVING d>=2 ORDER BY c DESC LIMIT 5`
  - `d>=2` 跨天门槛挡时段性爆发（实测有效：task_memo_add 147次/6天 入选，ecosystem_apply_* 12次/1天 被挡）；
  - **迟滞防抖**：挑战者需超过在位者 ≥20% 才换入（上期名单存 project.config，同 last_reconcile_at 模式，不建新表）；
  - 硬顶 **≤5 起步 3**；数据不足（冷启动）→ 静态种子 `task_memo_add` 或空集。
- **过度工程护栏（用户担忧的显式回应）**：复杂度封顶于此——**不做**衰减曲线/实时计数/后台重算/ML 打分；统计查询失败 → 静默零 alwaysLoad，一切照旧走 ToolSearch（功能纯增益，坏了无损）；名单每次计算结果落 events 台账一行（可审计，治理层原则）。
- 与 reconcile 同一架构哲学：**确定性小算法 + 台账已有数据**，零新常驻。

前置验证：FastMCP(Python) 对 per-tool `_meta` 的支持方式（gap，实施首步实测）。

### P2 — AITEAM_TOOLSETS 启动期分组开关（github-mcp 式）
- 29 个工具模块映射为 toolsets（task/team/meeting/memory/ecosystem/...），环境变量 `AITEAM_TOOLSETS=default|all|<组名列表>` 在 `mcp/tools/__init__.py` 注册循环处过滤——纯启动期静态 gating，不做运行时 listChanged（CC 是否消费该通知未证实）。
- `default` 建议 ≈ 核心六组（task/team/memory/infra/reports/project，~60 工具）；`all`=现状（默认值，向后兼容）。
- 同步 `AITEAM_READONLY=1` 只读档：隐藏全部写类工具（Sentry 25/20 只读默认、Playwright 默认最小暴露、github --read-only 同惯例）。
- 收益排序：非 CC 客户端硬上限（Cursor 只送前 40 个工具——166 全量下 3/4 不可达，公开仓用户真实痛点）> 名字清单瘦身 > tool confusion 缓解。

### P3 — agent 模板工具面（CC 侧，结构性最小权限）
给 plugin/agents/*.md 按角色加 frontmatter 工具授权：调研/审计型模板 `disallowedTools` 掉写类与 git 工具；专职模板用 allowlist（如 technical-writer 不需要 ecosystem 写族）。官方语义："Every other tool call fails immediately"——结构性约束。注意官方警告：过度限制会让任务以困惑方式失败，先从明显高危项开始。

**已裁剪模板清单（首批，保守圈定"宁少勿多"，2026-07-13）**——用 `disallowedTools`（denylist）掉明显不需要的写工具，全部读工具与会议/memo 记账工具保留；ecosystem 写族取 `mcp/tools/toolsets.py::WRITE_TOOLS` 中 ecosystem 一族（29 个）为权威口径：

| 模板 | 裁剪集 | 理由 |
|---|---|---|
| support-meeting-facilitator | git 写(2) + project_delete/team_delete + os_restart_api + ecosystem 写族(29) = 34 | 会议协调，不写代码/不删项目团队/不重启服务 |
| debate-advocate | 同上 34 | 辩论正方，纯文本产出 |
| debate-critic | 同上 34 | 辩论反方，纯文本产出 |
| support-technical-writer | 上述 34 + task_run = 35 | 写文档不派活（task_run） |
| management-project-manager | git 写(2) + os_restart_api = 3 | 管理角色不动代码不重启服务（保留 task/team/project 编排写权） |

每模板正文头部加一行 `<!-- 工具裁剪… -->` 注释说明裁剪理由与申诉方式（找 Leader 放行）。工程/测试类模板不动（真需写权限）。验证：`yaml.safe_load` 逐个 parse frontmatter + `mcp__ai-team-os__` 前缀断言 + git diff 仅含 5 模板 + 本文档 + CHANGELOG。

### 长期（不排期）— ecosystem 族聚合瘦身
47 个细粒度工具按 job-to-be-done 聚合（Cloudflare 思路），属重构非开关，等 P2 数据（哪些组真被用）再议。

## 1.5 P4 — 列表类返回体精简投影视图（2026-07-14 实施，用户圈选）

工具面治理（P1-P3）解决"哪些工具进上下文"，P4 解决"工具返回体带多少进上下文"——同一架构哲学的另一半：工具面"名字常驻、schema 按需"（ToolSearch defer），返回体"摘要常驻、详情按需"。

**实证依据（基准任务 4267426d，真实负载+o200k 分词）**：
- 直换 TOON 为负收益（events -11%/eco -10%）——嵌套异构结构令表格化失效，缩进即税；**裁字段才是主杠杆**：task-wall 省 84.3%、eco 列表 80.7%、events 50.7%（保守版，含摘要列）。
- 准确度三档全景：opus/sonnet 全臂 100%（机队真实档位，格式无影响）；haiku 精简版反升（71%→78%，噪音字段减少）。TOON 仅在 haiku 档有计数题红利（100% vs 63-90%），对 opus 机队不适用——**TOON 不在本批**，未来若引入弱模型消费者再议。

**实施形态（mcp/tools/views.py + 三工具）**：
- `task_list_project` / `event_list` / `ecosystem_search` 增加 `fields="compact"|"all"` 参数，默认精简投影；**投影只在 MCP 工具层（LLM 表示层），API 路由与 Dashboard 消费的 JSON 一字不动**。
- **自标识（用户裁定 2026-07-14）**：精简响应携带 `view:"compact"` + `hint`（明示"非字段缺失"并指路单体详情工具与 `fields="all"` 逃生舱），防 agent 误判字段缺失。
- 投影三铁律：后续调用键（id）永远完整；语义内容降级为截断摘要（desc 80 字/event payload 派生一行/eco 摘要 120 字）不删除；选择动作信号字段（score/assigned_to/depends_on/subtask_count）保留或按稀疏出现。
- 多一次调用的账：知道要全量 → `fields="all"` 零额外往返；列表后钻取 1-3 个 → 单体 get 本就是既有推荐流程；读精简版才发现缺 → 新增一次往返（这是裁错的退化成本上限，非失败）。

**同批 prompt 级采纳**：深扫/浅扫派单 prompt 建议 `npx -y gh-axi@0.1.27 api repos/X`（实测省 78%、字段等价；钉版本防 v0.1.x 漂移；明示避开 repo view——其丢 pushed_at/license）+ 离群数据怀疑指令（affaan-m/ECC 搜索投毒事件教训，见基准任务 issue memo）。

## 2. 明确不做

| 不做 | 依据 |
|---|---|
| 检索式工具加载（bigtool 式） | CC ToolSearch 已覆盖；重复建设 |
| embedding 工具检索 | ToolRet：IR 强模型做工具检索"poor performance"；红线零向量 |
| enable_toolset 动态元工具 | github 自家 beta 且社区反馈 LLM 常忘加载正确组；CC 非常驻不适合会话内状态机 |
| 运行时动态增删（listChanged） | CC 是否消费该通知无一手证据（gap）；启动期 gating 已够 |
| server 端按会话身份 gating | MCP 无会话身份标准信号；角色裁剪归 CC subagent frontmatter（P3） |

## 2.5 开源专项调研增量（wf_3acde6a3：网关 6 项 + 平台 5 项 + 学术 4 项，2026-07-13）

**P2 数据模型定稿**（多项目收敛验证）：
- 开关形态 = `toolFilter{mode: allow|block, list}`（TBXark/mcp-proxy）+ **命名集合别名**（BFCL `TEST_COLLECTION_MAPPING` / github-mcp `all|default` 同款）：`AITEAM_TOOLSETS=default|all|readonly|<组名,组名>` 一行选组；
- 分组键按**能力域**而非动词或物理来源（Composio/JARVIS/ToolBench 全行业一致）；
- 执行双保险 = ListTools 过滤 + CallTool 拒绝（MetaMCP `filter-tools` 中间件蓝本，TS→Python 翻写风险低）；
- default 组目标 **≤50 工具**（候选集硬顶是普适护栏：AnyTool 64 上限、JARVIS top-10→5、官方 30-50 拐点同源）。

**P1 工业对照**：Composio「important 标签」——按 app 拉工具默认只返精选子集、全量显式索取（官方原文 "prevents overwhelming the LLM"），即 alwaysLoad 核心集的同构先例。

**新增绿地机会——使用统计驱动（全行业真空，OS 有独特数据）**：调研确认没有任何项目把调用统计反哺成自动分组/降权/弃用（IBM/lasso 只观测；唯一先例 Smithery 用 useCount/score 驱动 registry 排序）。OS 的 agent_activities 台账已有工具调用记录 → 零依赖统计频次，用于 **(a) 数据驱动 P1 核心集成员的人工调整 (b) default 组划定**。保持"统计→人工决策"，不做自动开关（治理层可审计原则）。

**工程警示**（改名反查表）：若为提升搜索命中改工具 name/description，必须保留 original_name→dispatch 反查（MetaMCP tool-overrides / IBM ADR-0011 双字段存储的共同教训）。

**零向量红线再获三重背书**：MCP 官方 registry 刻意不设 categories 字段（"deliberately unopinionated"）；Composio 语义搜索 experimental 自承不稳；学术 embedding 检索全部是 16000 API 量级的产物（166 量级属过度工程）。行业共识：**工具选择准确率的第一杠杆是 name+description 质量**——OS 精力应投在描述打磨而非分类机制。

**不适用清单增补**：三级类目树（O(100) 量级无一手项目用 3 级）、门面压缩（lasso get_metadata+run_tool ≈ CC ToolSearch 等价物）、OCI catalog 分发、运行时 mcp-find/add 动态注册、多 endpoint 按受众暴露（OS 单客户端退化为 env 选组即可）。

## 3. 关键数字备查（全部一手）

- 准确率拐点：>30-50 工具退化（platform.claude.com tool-search-tool docs）
- Tool Search 实测：Opus 4 49%→74%、Opus 4.5 79.5%→88.1%（anthropic.com/engineering/advanced-tool-use）
- token：五 server 58 工具 ≈55k token，defer 削减 >85%
- Cursor 硬上限：只送前 40 个工具（github-mcp issue #275）
- github-mcp：162+ 工具默认只开 5 组；Playwright 70+ 工具默认开 3/7 组；Sentry 25 工具 20 只读默认
