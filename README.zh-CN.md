[English](README.md) | [中文](README.zh-CN.md)

# AI Team OS

<!-- Logo placeholder -->
<!-- ![AI Team OS Logo](docs/assets/logo.png) -->

### 你的 AI 编程工具，停止提示就停止工作。我们的不会。

> ⚡ **v1.10.2** — 跨会话编排：唤醒体系 v2（动态 /loop·事件 watcher·turn-end guard）+ 定向驱动兄弟会话 + Agent 复用推荐 + 上下文水位观测。补丁：服务端事件写 exactly-once + 模型档位宪章（Fable 编排·Opus 执行）。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Protocol-orange)](https://modelcontextprotocol.io)
[![Stars](https://img.shields.io/github/stars/CronusL-1141/AI-company?style=flat)](https://github.com/CronusL-1141/AI-company)

**168** 个 MCP 工具 · **217** 个 REST 端点 · **22** 个 Dashboard 页面 · **1,758** 测试 · **25** 个 Agent 模板 · **47** 个生态研究工具 · **5** 项红线机检不变量

---

AI Team OS 将 Claude Code 变成一家**自运转 AI 公司**。
你是董事长，AI 是 CEO。设定方向——系统自主执行、学习、持续进化。

---

## 其他 AI 工具的问题

所有 AI 编程助手的工作模式都一样：你提问，它回答，然后停下来。你一离开，工作就停了。你回来面对的是一个空白的提示框。

AI Team OS 的工作方式不同。

你晚上离开。第二天早上打开电脑，发现：
- CEO 检查了任务墙，拿起了下一个最高优先级的任务并完成了它
- 遇到需要你审批的阻塞点时，它挂起了那条线程，切换到了并行工作流
- 研究部门的 Agent 扫描了三个竞品框架，发现了一个值得采用的技术
- 一场头脑风暴会议已经召开，5 个 Agent 讨论了 4 个方案，最佳方案已经进了任务墙

这些，你一个提示都没发。系统自己跑起来的。

---

## 它是怎么工作的

**你是董事长，AI Leader 是 CEO。**

CEO 不等待指令。它检查任务墙，挑出最高优先级的任务，分配给对应的专业 Agent，推进执行。遇到阻塞，它切换工作流。所有计划内的工作完成后，研究部门的 Agent 会激活——扫描新技术、组织头脑风暴会议，把改进方案反馈回系统。

每一次交互都让系统更懂你。**记忆系统 v2** 把你的偏好和纠正沉淀为团队方向层，每个派出的 Agent 出生即继承——你不必把同一件事说第二遍；踩过的坑，也不会有下一个 Agent 再踩。

---

## 核心能力

### 1. 跨会话编排（v1.10.0 新）

一个 CC 会话现在可以观测并驱动兄弟会话执行一个操作回合，而不再局限于只能新起会话：

- **Wake system v2**：`/api/wake/actionable` 单一判定端点同时供事件 watcher 和收尾 guard 消费；SessionStart 从固定 30 分钟 cron 改为动态 `/loop` 间隔；Stop hook 收尾 guard 让 `decision:block` 与用户停止关键词始终放行；会话级事件 watcher 带 1 小时硬超时。全程无常驻进程。
- **Fleet downlink 原语**：headless `claude -p --resume <session_id>` 驱动目标兄弟会话执行一个操作回合，复用既有 wake 机制（信号量、熔断、白名单、按会话去重、全量审计轨迹）。
- **`agent_reuse_recommend` MCP 工具**：三选一复用决策（复用 / 精简后复用 / 新起），按领域匹配度、可达性（存活 / 可恢复 / 跨会话 / 已过期）和上下文水位打分。
- **上下文水位台账**：从 transcript 尾部读取精确 token 用量（先做低成本检查），以三色水位条呈现在 agent 视图和新增的 fleet / worktree 观测卡片上。

使用指引：
- 新会话的 SessionStart 简报已经会指引你跑一次 `/loop`，照着做即可，不必自己猜间隔。
- 行动前先看项目详情页的 fleet 卡（逐会话 CEO / 模型 / 在制任务 / 水位）和 worktree 卡（分支归属 + 未落地工作状态）。
- 派跟进任务前先调 `agent_reuse_recommend`——复用存活或可恢复的兄弟会话比新起会话更省。
- S4 worktree 收尾 guard 与模板默认隔离自动生效，无需额外配置。

### 2. 记忆系统 v2 — 双层记忆，Agent 出生即继承（v1.9.0 新）

OS 的独有卖点：把团队的偏好、纠正和踩过的坑，自动传给每一个派出的 Agent。

- **方向层**（用户偏好 / 纠正 / 设计意图，kind 四类）：SessionStart + SubagentStart **双 hook 常驻注入**——每个子 Agent 一出生就继承团队的价值观和红线，不必你反复叮嘱。体量红线 ≤40 条 × 400 字，`supersedes` 置换防膨胀，失效不删除可审计。
- **情景层**（`task_memos` 台账）：任务级执行备忘独立成表（行级 ID / 失效轴 / 质量分 / scope_path），纯 Python **BM25 中文检索**按需召回，123 条历史零丢失回填。
- **按需整理**（`memory_reconcile`）：零 LLM 粗筛配对候选，Agent 确认后合并 / 失效 / 打分 / 蒸馏提升——"Agent 算、工具存"，不引入任何后台常驻进程。

落点：MCP `memory_add` / `memory_list` / `memory_invalidate` / `memory_search` / `memory_reconcile_candidates` / `memory_reconcile_apply`。

### 3. 工具渐进式加载治理（v1.9.0 新）

把常驻上下文预算当稀缺资源管理——工具再多，也不淹没你的 Agent。

- **alwaysLoad 动态轮换**：会话启动期用一条 SQL 按 **7 天真实调用频率**重算高频工具白名单（跨天数 ≥2 挡时段性爆发 + 20% 迟滞防抖，硬顶 ≤5），CC 据此对它们豁免 ToolSearch。不叠加、不手调；统计失败静默降级为全 defer，每次名单落台账可审计。
- **`AITEAM_TOOLSETS` 分组开关**：24 个能力域 toolset，启动期环境变量决定注册哪些模块。`default` 核心档 = task/team/memory/infra/reports（44 工具，硬顶 ≤50），可 `default,ecosystem` 增量挂载——适配有工具数上限的非 CC 客户端。
- **`AITEAM_READONLY` 只读档**：与分组正交叠加，按显式清单剔除全部写工具、只留读工具，适合审计 / 观察者会话。
- **5 个模板最小权限**：会议主持 / 辩论正反方 / 技术文档 / 项目经理挂 `disallowedTools` 结构性拒绝，工程 / 测试类模板不动。

### 4. Workflow / ultracode 持久化观测层（v1.7.0）

OS 不拦截 CC 内置的 **ultracode/Workflow**，而是做它的持久化治理层。每次 Workflow 运行都被自动追踪进 OS，无需手动 `team_create`：

- **自动追踪**：hook 在运行启动时把每次 Workflow 落成一个 OS "团队"（`workflow-<wf_id>`）
- **Dashboard `/workflows`**：运行卡片实时流 + 相位泳道时间线 + 逐 agent 遥测 —— tokens / 时长 / 状态 / 工具调用数，running 期经 journal 增量 tail 实时推进
- **实测标定的卡死检测**：stall 阈值基于 3,378 个真实 agent 间隔实测标定（p99 = 77.6s，健康 agent 最长静默 173.8s），取最坏健康值的 5.2 倍——宁可迟判，绝不误报
- **项目详情集成**：workflow 团队行内展示 run 摘要（状态 / agent 数 / 耗时 / 完成时刻）+「查看泳道」直达；成员显示语义阶段标签（如 `audit:数据源A`）而非编号
- **Leader 自动检测**：项目的 Leader 会话 / 模型 / 活跃状态由后端直读 `~/.claude/projects/` 文件真相源，零注册依赖，`/model` 切换实时跟进
- **MCP 工具**：`workflow_list`（浏览运行）、`workflow_get`（完整归档 + 逐 agent 明细）、`workflow_reconcile`（OS 离线后从落盘快照对账修复）
- **摄取自愈**：hook 回执锚点 + 落盘快照对账 + reaper 保底三重机制自动弥合离线缺口，落盘的已完成运行会被幂等摄取；跨项目归属按落盘路径 slug 匹配注册项目

### 5. 生态研究平台 — 47 个工具

项目隔离的**知识库**，研究产物随时间累加。每个仓走过 4 阶段（v1.5.0 起的渐进式漏斗），token 高效触发 + append-only 历史：

- **Stage 0 — 入档即浅扫**：新入档仓自动派 `ai-engineer` 出 200-400 字总结（核心功能 / 定位 / 优势）。8 类失败处理 + **自学习机制**（同类失败 ≥ 3 仓 → `pattern_record`，未来 agent 通过 `pattern_search` 读 lessons 优化策略）
- **Stage 1 — 按需架构分析**：用户挑研究方向（"memory_system"）→ 批量派 `backend-architect` 读架构关键文件
- **Stage 2 — 多角度辩论**：触发现有 `debate_start`（**不内建辩论引擎，复用会议系统**）
- **Stage 3 — 参考 / 集成标记**：`mark_as_reference` 加 tag 便于未来快速召回；`start_integration` 触发现有 `task_create` 启动实际集成任务
- **活跃/全量双视图**：数据**永不删除**。stars 跌出阈值的仓保留（仅 `is_active=False`）；涨回自动激活 + 重新入队 Stage 0
- **Dashboard `/ecosystem`**：列表带 stage 徽章 + 研究历程 timeline + 项目筛选下拉 + 候选筛选页 (`/ecosystem/research`) + 项目设置 tab —— OS 内最大的单一工具族

### 6. 知识层 — 引用图谱 + 统一检索（v1.8.0）

OS 记录的一切——任务 memo、报告、任务——都成为可召回的知识：

- **引用图谱（P1a）**：零 LLM 正则抽取器从 memo 和报告中挖出 OS 原生 ID 引用（wf_id / commit / 任务 uuid / `[[记忆]]`），落入 append-only 的 `knowledge_links` 表——图谱是派生视图，随时可从源文本重建
- **统一检索（P1b）**：`/api/search` 三臂 RRF 融合——BM25 全文（中文 bigram 原生）、知识图谱扩散（查一个 ID 连带拉出所有关联物）、精确 ID 前缀/标题匹配
- **Dashboard 顶栏全局搜索框**，配套 MCP 工具 `unified_search` / `link_query` / `link_trace`——用自然语言（"归属铁律怎么修的"）、`wf_` id 或 commit hash 召回过往工作

> **为什么坚持零 LLM？** 图谱是派生视图：正则抽取 ID、整张图随时可从源文本重建、抽取与检索全程零 token 成本。召回链路永远不动你的模型预算。

### 7. 任务墙 · 会议 · 22 页 Dashboard

治理台账与全景可视化，一切有迹可循：

- **任务墙**：待办 / 进行中 / 已完成实时看板，事件驱动 + 智能匹配 Agent + 卡死检测
- **8 种结构化会议模板**（关键词自动匹配，基于六顶思考帽 / DACI / Design Sprint 方法论）——每次会议必须产出可执行结论，"讨论了但没决定"不是有效结果
- **22 页 React 19 Dashboard**：指挥中心 / `/workflows` 泳道 / 决策时间线 / 会议室 / 生态套件 / 模型治理 Settings

### 8. 自主运转

CEO 从不空闲。它按任务墙优先级持续推进工作：

- 一个任务完成后，立即检查任务墙，拿起下一个最高优先级任务
- 遇到需要你审批的阻塞点，挂起该线程，切换到并行工作流
- 批量汇总所有战略问题，等你回来时统一汇报——不为每个战术决策打断你
- 卡死检测：循环停滞时，系统主动暴露阻塞原因，而不是原地空转

而且它不只是执行——它在进化：

- **研发循环**：研究 Agent 扫描竞品、新框架和社区工具；研究结果提交到头脑风暴会议，Agent 之间相互挑战辩论；结论变成实施计划进入任务墙

### 9. 文件真相源（File Truth as Source of Truth）

多数多 Agent 框架信任 agent 自注册、自报状态。AI Team OS 把自报当"主张"，把文件当"事实"——三个子系统已经运行在这套哲学上：

- **Leader 探测**：项目的 Leader 会话 / 模型 / 活跃状态直读 `~/.claude/projects/`——transcript 的 mtime 就是活跃度，transcript 尾部的模型名就是模型。不要求 agent 自报模型——从 transcript 里读出来的才是真的。
- **模型发现**："可用模型" = 在你的 CC transcript 里真实出现过的全部模型。零 API 依赖、零硬编码——硬编码清单永远收不到你的第三方网关模型，实扫 transcript 不会漏。
- **Workflow 遥测**：落盘运行文件是全量遥测真相源，OS 的投影表只是不可变文件的可重建缓存。归属铁律：run 落盘路径 slug 与注册项目根 slug 精确匹配才算数——绝不靠猜。

### 10. 模型治理（v1.8.1）

知道你真正能启动哪些模型，并决定会话默认用什么启动：

- **自动发现真实可用模型**：实扫本机全部 CC transcript（约 1 秒完成，60 秒缓存）——包括任何硬编码清单都不可能收录的第三方网关模型
- **一键设置全局默认启动模型**：写入 `~/.claude/settings.json`，三层写保护——只动 `model` 键、保留 `.bak-aiteam` 备份、原子写入、损坏文件拒写
- **零强制**：只做软提示、绝不拦截，CC Workflow 运行完全豁免

落点：REST `/api/models/{available,default}` · MCP `model_config_get` / `model_config_set` · Dashboard Settings 的模型治理卡。

### 11. 团队协作

不是一个 Agent，而是一个结构化组织：

- **25 个专业 Agent 模板**（23 个基础 + 2 个辩论角色），含推荐引擎——工程/测试/研究/管理，开箱即用
- **部门分组管理**——工程部/测试部/研究部，支持跨部门协作
- **Channel 通讯系统**：`team:` / `project:` / `global` 三种频道 + `@mention` 支持
- **辩论模式**：4 轮结构化辩论（Advocate→Critic→Response→Judge）+ `debate_start` / `debate_code_review`
- **Git 自动化**：`git_auto_commit` / `git_create_pr` / `git_status_check` 简化版本控制
- **执行模式记忆**：成功/失败模式记录 + BM25 检索 + subagent 上下文注入

### 12. 完全透明

没有黑盒：

- **决策驾驶舱**：事件流 + 决策时间线 + 意图透视，每个决策有迹可循
- **活动追踪**：实时展示每个 Agent 的状态和当前任务
- **What-If 分析器**：提交前对比多个方案，支持路径模拟和推荐

### 13. 安全与行为强制

内置护栏，系统在无人监督时也不会产生意外：

- **Guardrails L1**：7 种危险模式检测 + PII 警告 + `InputGuardrailMiddleware`
- **本地 Agent 拦截**：所有非只读 Agent 必须声明 `team_name`/`name`，防止游离后台 Agent
- **S1 安全规则**：正则扫描拦截破坏性命令（rm -rf、force push、硬编码密钥），覆盖大写标志和 heredoc 模式
- **四层防线规则体系**：48+ 条规则，覆盖工作流、委派、会话和安全层
- **文件锁/工作区隔离**：acquire/release/check/list + TTL=300s + hook 警告，防止并发编辑
- **Agent 信任评分**：trust_score (0-1) 随任务成功/失败自动调整，加权到 auto_assign
- **Agent Watchdog 心跳**：`agent_heartbeat` / `watchdog_check`，5 分钟 TTL，自动检测卡死或崩溃的 Agent
- **自巡检**：watchdog 租约巡检 + reaper 对账保底 + kill 前身份校验——OS 不只盯你的 agent，也盯它自己
- **SRE 错误预算模型**：GREEN/YELLOW/ORANGE/RED 四级响应，滑动窗口 20 任务，`error_budget_status` / `error_budget_update` 工具
- **完成验证协议**：`verify_completion` 检查 task 状态 + memo 存在，防止幻觉"已完成"报告
- **生态集成配方**：4 个预设配方（GitHub / Slack / Linear / 全栈团队），通过 `ecosystem_recipes()` 工具查询
- **`find_skill` 三层渐进发现**：快速推荐 → 分类浏览 → 完整详情，降低工具调用开销

### 14. 零额外成本

100% 运行在你现有的 Claude Code 订阅套餐内：

- 不调用外部 API，不烧额外 token
- MCP 工具、Hooks 和 Agent 模板全部本地运行
- 记忆系统与知识层从设计上就是零 LLM——方向层注入、图谱抽取、检索与整理粗筛全程零 token 成本
- 完全复用你的 CC 套餐

### 更多能力（旧时代与次要功能 · 仍在运行，按需可查）

- **失败炼金术**：`failure_analysis` 仍随 loop 子系统运行——每次任务失败照常提取根因，产出*抗体*（存入团队记忆防重蹈）/*疫苗*（高频失败转任务前预警）/*催化剂*（分析注入未来 Agent 的 system prompt）。已不再作招牌，但防御规则照常沉淀。
- **管道编排（Legacy）**：7 种模板的自带管道已于 v1.7.0 退役，由 CC Workflow + 观测层接替；`pipeline_create` / `pipeline_advance` 工具仍注册，存量 pipeline 数据只读可查。
- **AWARE 循环记忆 · `find_skill` 三层发现 · Prompt Registry · 跨项目消息 · 生态集成配方**：详见下方工具全表。调度器已退役为按需 `ecosystem_refresh`（CC 非常驻原则）。

---

## 它构建了自己

AI Team OS 管理着自身的开发——而且从 v1.7.0 起，它能用自己的遥测数据自证：

- v1.7.0 → v1.9.0 的每条功能线——观测层、知识层、模型治理、记忆系统 v2、工具加载治理——都是通过 OS 自己追踪的 CC Workflow 运行交付的。打开 `/workflows`，可以逐条泳道回放系统如何构建自己的功能。
- 对 CrewAI、AutoGen、LangGraph 和 Devin 的竞品研究，通过多 Agent 头脑风暴会议持续喂入路线图——会议纪要就存在 OS 自己的报告库里。
- 它也从自己的事故中学习：`scripts/check_invariants.sh` 里的每条机检不变量，都提炼自本仓库历史上的一次真实事故。

这个为你的项目构建东西的系统……构建了它自己。而且有据可查。

---

## 与主流方案对比

| 维度 | AI Team OS | CrewAI | AutoGen | LangGraph | Devin |
|------|-----------|--------|---------|-----------|-------|
| **定位** | CC 增强层 OS | 独立框架 | 独立框架 | 工作流引擎 | 独立 AI 工程师 |
| **集成方式** | MCP 协议接入 CC | 独立 Python 运行 | 独立 Python 运行 | 独立 Python 运行 | SaaS 独立产品 |
| **记忆系统** | 双层记忆：方向层出生即继承 + 情景层 BM25 台账 + 按需整理 | 短期上下文 | 短期上下文 | 检查点状态 | 会话内 |
| **工具加载治理** | alwaysLoad 动态轮换 + 分组开关 + 只读档 + 模板最小权限 | 无 | 无 | 无 | 无 |
| **自主运转** | 持续循环，从不空闲 | 逐任务执行 | 逐任务执行 | 工作流驱动 | 有限 |
| **会议系统** | 8 种结构化模板，支持关键词自动匹配 | 无 | 有限 | 无 | 无 |
| **失败学习** | 失败炼金术（抗体/疫苗/催化剂） | 无 | 无 | 无 | 有限 |
| **决策透明度** | 决策驾驶舱 + 时间线 | 无 | 有限 | 有限 | 黑盒 |
| **Workflow 可观测性** | CC Workflow 泳道时间线 + 逐 agent 遥测 + 离线对账 | 无 | 无 | 仅图内状态 | 无 |
| **状态来源** | 文件真相源——直读 transcript / journal | Agent 自报 | Agent 自报 | 进程内状态 | 黑盒 |
| **规则体系** | 四层防线（48+ 条）+ 行为强制 | 有限 | 有限 | 无 | 有限 |
| **Agent 模板** | 25 个开箱即用 + 推荐引擎 | 内置角色 | 内置角色 | 无 | 无 |
| **Dashboard** | React 19 可视化 | 商业版 | 无 | 无 | 有 |
| **开源** | MIT | Apache 2.0 | MIT | MIT | 否 |
| **Claude Code 原生** | 是，深度集成 | 否 | 否 | 否 | 否 |
| **额外成本** | $0（仅 CC 订阅） | 需 API 费用 | 需 API 费用 | 需 API 费用 | $500+/月 |

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                     用户（董事长）                                │
│                         │                                       │
│                         ▼                                       │
│                   Leader（CEO）                                  │
│            ┌────────────┼────────────┐                          │
│            ▼            ▼            ▼                          │
│       Agent模板      任务墙        会议系统                        │
│      (25个角色)    Loop引擎      (8种模板)                         │
│            │            │            │                          │
│            └────────────┼────────────┘                          │
│                         ▼                                       │
│              ┌──────────────────────┐                           │
│              │   OS 增强层           │                           │
│              │  ┌──────────────┐    │                           │
│              │  │  MCP Server  │    │                           │
│              │  │ (168 tools)  │    │                           │
│              │  └──────┬───────┘    │                           │
│              │         │            │                           │
│              │  ┌──────▼───────┐    │                           │
│              │  │  FastAPI     │    │                           │
│              │  │  REST API    │    │                           │
│              │  └──────┬───────┘    │                           │
│              │         │            │                           │
│              │  ┌──────▼───────┐    │                           │
│              │  │  Dashboard   │    │                           │
│              │  │ (React 19)   │    │                           │
│              │  └──────────────┘    │                           │
│              └──────────────────────┘                           │
│                         │                                       │
│              ┌──────────▼──────────┐                            │
│              │  Storage (SQLite)   │                            │
│              │  + WAL journaling   │                            │
│              │  + Memory System    │                            │
│              └─────────────────────┘                            │
└─────────────────────────────────────────────────────────────────┘
```

### 五层技术架构

```
Layer 5: Web Dashboard    — React 19 + TypeScript + Shadcn UI（22 个页面）
Layer 4: CLI + REST API   — Typer + FastAPI
Layer 3: Team Orchestrator — LangGraph StateGraph（可选 extra — 仅 CLI 图执行需要）
Layer 2: Memory Manager   — 内置 SQLite 存储 + 纯 Python BM25 检索
Layer 1: Storage          — SQLite（WAL 日志）· PostgreSQL 支持在路线图上
```

### Hook 系统（14 个脚本 / 12 个生命周期事件 — CC 与 OS 的桥梁）

```
SessionStart     → auto_install.py, session_bootstrap.py, send_event.py
                   — 自动安装依赖 + 注入 Leader 简报 / 核心规则 / 团队状态
SubagentStart    → inject_subagent_context.py, send_event.py   — 注入子 Agent OS 规则（2-Action 等）
SubagentStop     → send_event.py                 — 记录子 Agent 生命周期事件
PreToolUse       → workflow_reminder.py, pipeline_gate.py, send_event.py
                   — Workflow 追踪提醒 + 管道门禁 + 事件转发
PostToolUse      → workflow_reminder.py, pipeline_gate.py, deep_review_link.py,
                   meeting_ecosystem_writeback.py, send_event.py
TaskCreated      → cc_task_bridge.py             — 把 CC 原生任务桥接到 OS 任务墙
TaskCompleted    → task_completed_gate.py        — 完成门禁校验
UserPromptSubmit → context_tracker.py, autopilot_auto_stop.py  — 上下文追踪 + autopilot 自动停止
SessionEnd       → send_event.py                 — 记录会话结束事件
Stop             → send_event.py                 — 记录停止事件
PermissionDenied → permission_denied_recovery.py — 权限拒绝自愈
PreCompact       → pre_compact_save.py           — 上下文压缩前自动保存进度
```

---

## 快速安装（AI 辅助）

告诉 Claude Code：
> "Read https://github.com/CronusL-1141/AI-company/blob/master/INSTALL.md and follow the instructions to install AI Team OS"

Claude Code 会自动读取安装指南并引导你完成配置。

---

> **重要提示**：请将 AI Team OS 安装到系统 Python，而不是项目虚拟环境中。
> 如果安装在 venv 中，AI Team OS 将只在该特定项目中可用。
> 如果当前已激活 venv，请先执行 `deactivate`，再进行安装。

---

## 快速开始

### 前置要求

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/getting-started/installation/)（`pip install uv`）
- Claude Code（需要 MCP 支持）
- Node.js >= 20（Dashboard 前端，可选）

> **国内用户提示**：如果访问 GitHub 较慢，建议配置代理或使用 Gitee 镜像（如有）。

### 方式 A：Plugin 安装（推荐 — 普通用户）

```bash
# 安装 uv（Python 包运行器，MCP 服务器需要）
pip install uv

# 添加 marketplace + 安装
claude plugin marketplace add CronusL-1141/AI-company
claude plugin install ai-team-os

# 重启 Claude Code — 首次启动约 30 秒加载依赖
# 后续启动秒级完成

# 随时更新到最新版
claude plugin update ai-team-os@ai-team-os
```

> **提示**：首次启动需要约 30 秒自动配置依赖，仅此一次。后续每次启动 168 个 MCP 工具即时可用。

### 方式 B：源码安装（开发者 — editable，跟最新源码）

```bash
# Step 1: 克隆仓库
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company

# Step 2: 安装（自动配置 MCP + Hooks + Agent 模板 + API）
python3 install.py

# Step 3: 重启 Claude Code，一切自动激活
# API 服务器在 MCP 加载时自动启动，无需手动操作
# 验证：在 CC 中运行 /mcp 查看 ai-team-os 工具是否挂载
```

> **依赖说明**：`greenlet`（SQLAlchemy async 在 Apple Silicon 上必需）已默认内置。`LangGraph` 为可选 extra —— 仅 CLI 图执行路径需要：`pip install 'ai-team-os[langgraph]'`。

### 验证安装

```bash
# 检查 OS 健康状态（API 必须已启动 — 端口可能变化，查看 api_port.txt）
curl http://localhost:8000/api/health
# 期望: {"status": "ok"}

# 通过 CC 创建第一个团队
# 在 Claude Code 中输入：
# "帮我创建一个 web 开发团队，包含前端、后端和测试工程师"
```

### 工具加载配置（可选）

MCP server 默认注册全部 **168 个工具**。两个启动期环境变量可裁剪工具面，用于精简会话，或应对有工具数上限的非 CC 客户端（如 Cursor 只转发前 40 个工具）。二者均在 server 启动时读取一次 - 无运行期状态，改动不需重启即生效于下次启动。

**`AITEAM_TOOLSETS`** - 选择注册哪些能力域分组：

- 未设置或 `all` - 全量 168（向后兼容）
- `default` - 仅核心组（`task,team,memory,infra,reports` = 44 工具，硬顶 <=50）
- 逗号分隔的组名列表，可混入 `default` 做增量加载，如 `AITEAM_TOOLSETS=default,ecosystem`
- 未知组名 stderr 警告并忽略（配置写错绝不拉不起 server）

**`AITEAM_READONLY=1`** - 与分组正交叠加，注册后剔除全部写工具（create/update/delete/apply/send/... 及 `os_restart_api`），只留读工具。适合审计/观察者会话。

24 个分组（带 * 为 default 组）：

| 组名 | 工具数 | 组名 | 工具数 | 组名 | 工具数 |
|---|---|---|---|---|---|
| task * | 12 | briefing | 4 | trust | 2 |
| team * | 7 | scheduler | 4 | watchdog | 3 |
| memory * | 9 | task_analysis | 5 | error_budget | 2 |
| infra * | 13 | agent | 6 | file_lock | 4 |
| reports * | 3 | meeting | 10 | git | 3 |
| project | 8 | loop | 7 | channels | 3 |
| pipeline | 3 | analytics | 3 | guardrails | 2 |
| links | 3 | ecosystem | 47 | workflows | 3 |

```bash
# 示例：精简核心 + ecosystem，只读档
AITEAM_TOOLSETS=default,ecosystem AITEAM_READONLY=1 <启动 CC / MCP server>
```

### 卸载

```bash
# Plugin 安装：
claude plugin uninstall ai-team-os
# 然后手动清理残留数据：
# Windows: rmdir /s %USERPROFILE%\.claude\plugins\data\ai-team-os-ai-team-os
# Unix:    rm -rf ~/.claude/plugins/data/ai-team-os-*
# 重启 Claude Code 以停用仍在运行的 hooks。

# 源码安装 — 完整清理：
python scripts/uninstall.py
# 先预览：
python scripts/uninstall.py --dry-run
```

### 启动 Dashboard（可选）

```bash
cd dashboard
npm install
npm run dev
# 访问 http://localhost:5173
```

---

## Dashboard 截图

### 指挥中心
![Command Center](docs/screenshots/dashboard-home.png)

### 团队实时工作 — 活动追踪
![Team Working](docs/screenshots/team-working-en.png)

### 任务看板
![Task Board](docs/screenshots/task-board-en.png)

### 项目详情 — 决策时间线
![Decision Timeline](docs/screenshots/decision-timeline.png)

### 会议室
![Meeting Room](docs/screenshots/meeting-room.png)

### 生态研究平台
![Ecosystem](docs/screenshots/ecosystem-list-desktop.png)

### 活动分析
![Analytics](docs/screenshots/analytics.png)

### 事件日志
![Events](docs/screenshots/events.png)

### 自主唤醒系统 — 无人值守任务推进
![Auto-Wake Demo](docs/screenshots/auto-wake-demo.png)

---

## 自主唤醒系统 (Auto-Wake)

AI Team OS 的 Leader 支持定时自动唤醒，在无人值守时自主推进任务：

- 每 10 分钟自动检查上下文使用率和待办任务
- 有待办任务时自主创建团队并分配工作
- 需要用户决策时通过 Briefing 系统异步记录
- 上下文 > 80% 时自动保存进度并提醒开新 session

---

## 生态集成配方

AI Team OS 的定位是**元 Plugin** — 编排其他 MCP server，而非重新实现它们的功能。预设配方让你在几分钟内集成流行工具：

| 配方 | 集成对象 | 能力 |
|------|---------|------|
| **GitHub** | `@modelcontextprotocol/github` | 自动创建 PR、Issue 跟踪、代码审查协调 |
| **Slack** | `@anthropics/slack-mcp` | 团队通知、决策升级、状态广播 |
| **Linear** | `linear-mcp-server` | 任务同步、Sprint 跟踪、Bug 分流自动化 |
| **全栈团队** | GitHub + Slack + Linear | 完整开发工作流，跨工具编排 |

使用 `ecosystem_recipes` MCP 工具发现配方，或查看完整指南：[docs/ecosystem-recipes.md](docs/ecosystem-recipes.md)

---

## CC-First 设计原则

AI Team OS 专为 Claude Code 设计，不是独立框架：

- **MCP 协议原生**：168 个 MCP 工具全部原生注册 — 无自定义客户端，无 API 包装器
- **Hook 驱动生命周期**：12 个 CC 生命周期事件（SessionStart → PreCompact）提供深度集成，无需修改 CC 内部
- **Agent 模板即 `.md` 文件**：安装到 `~/.claude/agents/`（全局）或 `.claude/agents/`（项目级）— CC 原生 Agent 系统，非自定义抽象
- **运行时零外部依赖**：不调用外部 API，不依赖云服务 — 100% 在你的 CC 订阅内运行
- **上下文感知**：Session bootstrap 仅注入 5 条核心规则（从 23 条精简），subagent 上下文限制 60 行，最大化减少上下文预算占用

---

## MCP 工具一览

<details>
<summary>展开查看工具全景（168 个 MCP 工具，分布在 24 个模块）</summary>

> 下表为精选摘录——全量清单在 `src/aiteam/mcp/tools/`，由 `scripts/check_readme_numbers.sh` 机器计数校验。

### 团队管理

| 工具 | 说明 |
|------|------|
| `team_create` | 创建 AI Agent 团队，支持 coordinate/broadcast 模式 |
| `team_status` | 获取团队详情和成员状态 |
| `team_list` | 列出所有团队 |
| `team_briefing` | 一次调用获取团队全景简报（成员+事件+会议+待办） |
| `team_setup_guide` | 根据项目类型推荐团队角色配置 |

### Agent 管理

| 工具 | 说明 |
|------|------|
| `agent_register` | 注册新 Agent 到团队 |
| `agent_update_status` | 更新 Agent 状态（idle/busy/error） |
| `agent_list` | 列出团队成员 |
| `agent_template_list` | 获取可用的 Agent 模板列表 |
| `agent_template_recommend` | 根据任务描述推荐最适合的 Agent 模板 |

### 任务管理

| 工具 | 说明 |
|------|------|
| `task_run` | 执行任务并记录全程 |
| `task_decompose` | 将复杂任务分解为子任务 |
| `task_status` | 查询任务执行状态 |
| `taskwall_view` | 查看任务墙（全部待办+进行中+已完成） |
| `task_create` | 创建新任务（支持 `auto_start` 和 `task_type` 管道参数） |
| `task_update` | 局部更新任务字段，自动打时间戳 |
| `task_list_project` | 列出项目下所有任务 |
| `task_auto_match` | 基于任务特征智能匹配最佳 Agent |
| `task_memo_add` | 为任务添加执行备忘记录 |
| `task_memo_read` | 读取任务历史备忘 |

### 管道编排（Legacy — v1.7.0 退役，工具仍注册，存量数据只读可查）

| 工具 | 说明 |
|------|------|
| `pipeline_create` (Legacy) | 为任务挂载工作流管道（7 种模板：feature/bugfix/research/refactor/quick-fix/spike/hotfix） |
| `pipeline_advance` (Legacy) | 推进管道到下一阶段，返回下一阶段的 Agent 模板推荐 |

### Loop 循环引擎

| 工具 | 说明 |
|------|------|
| `loop_start` | 启动自动推进循环 |
| `loop_status` | 查看循环状态 |
| `loop_next_task` | 获取下一个待处理任务 |
| `loop_advance` | 推进循环到下一阶段 |
| `loop_pause` | 暂停循环 |
| `loop_resume` | 恢复循环 |
| `loop_review` | 生成循环回顾报告（含失败分析） |

### 会议系统

| 工具 | 说明 |
|------|------|
| `meeting_create` | 创建结构化会议（8 种模板，关键词自动匹配） |
| `meeting_send_message` | 发送会议消息 |
| `meeting_read_messages` | 读取会议记录 |
| `meeting_conclude` | 总结会议结论 |
| `meeting_template_list` | 获取可用会议模板列表 |
| `meeting_list` | 列出所有会议 |
| `meeting_update` | 更新会议元数据 |

### Channel 通讯

| 工具 | 说明 |
|------|------|
| `channel_send` | 向频道发送消息（team:/project:/global），支持 @mention |
| `channel_read` | 读取频道消息 |
| `channel_mentions` | 获取 Agent 的未读 @提及 |

### 文件锁/工作区隔离

| 工具 | 说明 |
|------|------|
| `file_lock_acquire` | 获取文件锁（TTL=300s），防止并发编辑 |
| `file_lock_release` | 释放文件锁 |
| `file_lock_check` | 检查文件是否被锁定及锁定者 |
| `file_lock_list` | 列出所有活跃的文件锁 |

### Git 自动化

| 工具 | 说明 |
|------|------|
| `git_auto_commit` | 自动提交暂存变更并生成提交消息 |
| `git_create_pr` | 从当前分支创建 Pull Request |
| `git_status_check` | 检查 Git 仓库状态 |

### 辩论系统

| 工具 | 说明 |
|------|------|
| `debate_start` | 启动 4 轮结构化辩论（Advocate→Critic→Response→Judge） |
| `debate_code_review` | 启动代码审查辩论会话 |

### 护栏系统

| 工具 | 说明 |
|------|------|
| `guardrail_check` | 对命令字符串执行护栏检查 |
| `guardrail_check_payload` | 对结构化载荷执行护栏检查 |

### 执行模式

| 工具 | 说明 |
|------|------|
| `pattern_record` | 记录成功/失败执行模式 |
| `pattern_search` | 通过 BM25 检索执行模式，用于上下文注入 |

### 智能分析

| 工具 | 说明 |
|------|------|
| `failure_analysis` | 失败炼金术——分析失败根因，生成抗体/疫苗/催化剂 |
| `what_if_analysis` | What-If 分析器——多方案对比推荐 |
| `decision_log` | 记录决策到驾驶舱时间线 |
| `context_resolve` | 解析当前上下文，获取相关背景信息 |

### 记忆系统

| 工具 | 说明 |
|------|------|
| `memory_search` | 检索团队记忆 — scope 内近期窗口粗召回 + 纯 Python BM25 重排（中文 bigram，无向量/embedding） |
| `team_knowledge` | 获取团队知识摘要 |
| `memory_add` | 写方向层记忆（偏好/纠正/设计意图，kind 四类；体量红线 ≤40条×400字，supersedes 置换） |
| `memory_invalidate` | 显式失效一条方向层记忆（失效不删除，可审计） |
| `memory_list` | 列方向层有效条目（kind 过滤；双 hook 常驻注入的数据源） |
| `memory_reconcile_candidates` | 按需整理·粗筛（零 LLM）：BM25 配对候选组 + 方向层清单 + 蒸馏素材 + 操作说明 |
| `memory_reconcile_apply` | 应用 agent 确认后的整理操作（合并 / 失效 / 打分 / 提升）；幂等，promote 走体量红线 |

### 知识层（v1.8.0）

| 工具 | 说明 |
|------|------|
| `unified_search` | 跨 memo / 报告 / 任务的三臂 RRF 检索 — BM25 全文 + 知识图谱扩散 + 精确 ID 匹配 |
| `link_query` | 按节点查询跨域引用图谱（谁引用了它 / 它引用了谁） |
| `link_trace` | 从任意 OS ID（wf_id / commit / 任务 uuid）追踪引用链，附证据片段 |

### 模型治理（v1.8.1）

| 工具 | 说明 |
|------|------|
| `model_config_get` | 读取已发现的可用模型（transcript 实扫）+ 当前默认启动模型 |
| `model_config_set` | 设置全局默认启动模型（对 `~/.claude/settings.json` 三层写保护） |

### 信任与可靠性

| 工具 | 说明 |
|------|------|
| `agent_trust_scores` | 查看所有 Agent 的信任评分 |
| `agent_trust_update` | 手动调整 Agent 的信任评分 |
| `agent_heartbeat` | 发送运行中 Agent 的心跳信号 |
| `watchdog_check` | 检查卡死的 Agent（5 分钟 TTL 超时） |
| `error_budget_status` | 查看 SRE 错误预算（GREEN/YELLOW/ORANGE/RED） |
| `error_budget_update` | 记录任务结果到错误预算 |
| `verify_completion` | 验证任务完成状态（状态 + memo 检查，防幻觉） |

### 分析

| 工具 | 说明 |
|------|------|
| `task_execution_trace` | 获取任务的统一执行时间线 |
| `task_replay` | 回放任务执行历史 |
| `task_compare` | 并排对比两次任务执行 |
| `diagnose_task_failure` | 自动诊断任务失败原因 |

### 简报系统

| 工具 | 说明 |
|------|------|
| `briefing_add` | 添加待用户审查的决策项 |
| `briefing_list` | 列出待处理的简报项 |
| `briefing_resolve` | 以决策解决简报项 |
| `briefing_dismiss` | 忽略简报项 |

### 报告（数据库存储）

| 工具 | 说明 |
|------|------|
| `report_save` | 保存报告到数据库，支持项目隔离（研究/设计/分析/会议纪要） |
| `report_list` | 列出报告，支持按项目、类型、作者、主题过滤 |
| `report_read` | 通过报告 ID 读取报告 |

### 调度器

| 工具 | 说明 |
|------|------|
| `scheduler_create` | 创建定时周期任务 |
| `scheduler_list` | 列出定时任务 |
| `scheduler_delete` | 删除定时任务 |
| `scheduler_pause` | 暂停定时任务 |

### 生态研究（47 个工具）

OS 内最大的单一工具族——从扫描到集成的完整研究漏斗：

| 工具 | 说明 |
|------|------|
| `ecosystem_scan` / `ecosystem_scan_periodic` | 按项目画像（stars / topics）扫 GitHub，单次或周期 |
| `ecosystem_search` / `ecosystem_search_by_capability` | 检索已入档的研究知识库 |
| `ecosystem_deep_review_request` / `..._request_batch` | 派发架构深评 agent，单发或批量 |
| `ecosystem_tag_list` / `..._apply_batch` / `..._dispatch_llm` | 标签规则引擎 + LLM 辅助打标 |
| `ecosystem_summary_weekly` / `..._top_n` / `..._health` | 周报速览、Top-N 与知识库健康报告 |
| `ecosystem_diff_period` / `ecosystem_index_diff_latest` | 期间对比 diff + 索引对账 |
| `ecosystem_mark_as_reference` / `ecosystem_start_integration` | Stage 3 标记：留作参考，或直接发起集成任务 |
| … | 47 个工具全家族见 `src/aiteam/mcp/tools/ecosystem.py` |

### 集成与跨项目

| 工具 | 说明 |
|------|------|
| `ecosystem_recipes` | 发现集成配方（GitHub/Slack/Linear/全栈） |
| `send_notification` | 通过 Slack/webhook 发送通知 |
| `cross_project_send` | 发送跨项目消息 |
| `cross_project_inbox` | 读取跨项目收件箱 |

### Prompt Registry

| 工具 | 说明 |
|------|------|
| `prompt_version_list` | 列出 Agent 模板版本 |
| `prompt_effectiveness` | 查看模板效果指标 |

### 项目管理

| 工具 | 说明 |
|------|------|
| `project_create` | 创建项目 |
| `project_list` | 列出所有项目 |
| `project_update` | 更新项目设置 |
| `project_delete` | 删除项目 |
| `project_summary` | 获取项目快速状态摘要 |
| `phase_create` | 创建项目阶段 |
| `phase_list` | 列出项目阶段 |

### 系统运维

| 工具 | 说明 |
|------|------|
| `os_health_check` | OS 健康检查 |
| `os_restart_api` | 重启 OS API 服务器（带安全校验） |
| `event_list` | 查看系统事件流 |
| `os_report_issue` | 上报问题 |
| `os_resolve_issue` | 标记问题已解决 |
| `agent_activity_query` | 查询 Agent 活动历史和统计数据 |
| `find_skill` | 三层渐进技能发现（快速推荐 / 分类浏览 / 完整详情） |
| `team_close` | 关闭团队并级联关闭其所有活跃会议 |
| `team_delete` | 删除团队 |

</details>

---

## Agent 模板库

25 个开箱即用的专业 Agent 模板，含推荐引擎，覆盖完整软件工程团队配置。模板安装到 `plugin/agents/`（项目级）和 `~/.claude/agents/`（全局，跨项目可用）。

### 工程部（13 个模板）

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `engineering-software-architect` | 软件架构师 | 系统设计、架构评审 |
| `engineering-backend-architect` | 后端架构师 | API 设计、服务架构 |
| `engineering-frontend-developer` | 前端开发工程师 | UI 实现、交互开发 |
| `engineering-ai-engineer` | AI 工程师 | 模型集成、LLM 应用 |
| `engineering-mcp-builder` | MCP 构建专家 | MCP 工具开发 |
| `engineering-code-reviewer` | 代码审查工程师 | 代码质量审查、PR 审查 |
| `engineering-database-optimizer` | 数据库优化师 | 查询优化、Schema 设计 |
| `engineering-devops-automator` | DevOps 自动化工程师 | CI/CD、基础设施 |
| `engineering-sre` | 站点可靠性工程师 | 可观测性、故障处理 |
| `engineering-security-engineer` | 安全工程师 | 安全审查、漏洞分析 |
| `engineering-rapid-prototyper` | 快速原型工程师 | MVP 验证、快速迭代 |
| `engineering-mobile-developer` | 移动端开发工程师 | iOS/Android 开发 |
| `engineering-git-workflow-master` | Git 工作流专家 | 分支策略、代码协作 |

### 测试部（4 个模板）

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `testing-qa-engineer` | QA 工程师 | 测试策略、质量保障 |
| `testing-api-tester` | API 测试专家 | 接口测试、契约测试 |
| `testing-bug-fixer` | Bug 修复专家 | 缺陷分析、根因排查 |
| `testing-performance-benchmarker` | 性能基准测试师 | 性能分析、压测 |

### 研究与支持（3 个模板）

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `specialized-workflow-architect` | 工作流架构师 | 流程设计、自动化编排 |
| `support-technical-writer` | 技术文档工程师 | API 文档、用户指南 |
| `support-meeting-facilitator` | 会议主持人 | 结构化讨论、决策推进 |

### 管理层（2 个模板）

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `management-tech-lead` | 技术 Lead | 技术决策、团队协调 |
| `management-project-manager` | 项目经理 | 进度管理、风险跟踪 |

### 辩论角色（2 个模板）

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `debate-advocate` | 辩论倡导者 | 在结构化辩论中提出和捍卫方案 |
| `debate-critic` | 辩论评论者 | 挑战提案、发现弱点 |

### 通用模板（1 个）

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `team-member` | 通用团队成员 | 通用型任务的默认角色 |

---

## 路线图

### 已完成

- [x] 核心 Loop 引擎（LoopEngine + 任务墙 + Watchdog + 回顾）
- [x] 失败炼金术（抗体 + 疫苗 + 催化剂）
- [x] 决策驾驶舱（事件流 + 时间线 + 意图透视）
- [x] 事件驱动任务墙 2.0（实时推送 + 智能匹配）
- [x] 团队活记忆（知识查询 + 经验共享）
- [x] What-If 分析器（多方案对比推荐）
- [x] 8 种结构化会议模板，支持关键词自动匹配
- [x] 25 个专业 Agent 模板（23 基础 + 2 辩论角色），含推荐引擎
- [x] 四层防线规则体系（48+ 条规则）+ 行为强制
- [x] Dashboard 指挥中心（React 19）— 22 个页面，含 `/workflows` 泳道、Workflow 详情、Ecosystem 套件与模型治理 Settings
- [x] 168 个 MCP 工具，分布在 24 个模块中
- [x] CC Workflow 观测层（自动追踪 + /workflows Dashboard + workflow_list / workflow_get / workflow_reconcile）
- [x] 知识层——零 LLM 引用图谱 + 三臂 RRF 统一检索（v1.8.0）
- [x] 模型治理——transcript 实扫模型发现 + 全局默认启动模型（v1.8.1）
- [x] 红线不变量机检 + 一键预检（`scripts/preflight.sh`）
- [x] AWARE 循环记忆系统
- [x] find_skill 三层渐进发现
- [x] task_update API，支持程序化任务管理
- [x] 工作流管道编排（7 种模板 + 自动阶段推进）——已于 v1.7.0 退役，由 CC Workflow 观测层接替
- [x] 1,758 自动化测试，CI 全绿
- [x] Prompt Registry（版本追踪 + 效果统计）
- [x] BM25 接入检索主链路（纯 Python Okapi BM25，中文 bigram，近期窗口粗召回 + 重排）
- [x] 事件日志增强（entity_id / entity_type / state_snapshot 字段）
- [x] CC Plugin Marketplace 正式提交
- [x] 文件锁/工作区隔离（acquire/release/check/list + TTL=300s）
- [x] Channel 通讯系统（team:/project:/global + @mention）
- [x] 执行模式记忆（成功/失败记录 + BM25 检索）
- [x] Git 自动化工具（git_auto_commit / git_create_pr / git_status_check）
- [x] Guardrails L1（7 种危险模式 + PII 警告）
- [x] Alembic 数据库迁移系统
- [x] 辩论模式（4 轮结构化辩论 + 代码审查）
- [x] Agent 信任评分系统（任务成功/失败自动调整）
- [x] 工具分层草案（informational CORE/ADVANCED 清单——为上下文预算优化预留）
- [x] Agent Watchdog 心跳系统（5 分钟 TTL 超时检测）
- [x] SRE 错误预算模型（GREEN/YELLOW/ORANGE/RED 四级响应）
- [x] 完成验证协议（防幻觉完成检查）
- [x] 生态集成配方（GitHub/Slack/Linear/全栈团队预设）
- [x] Session bootstrap 规则压缩（23 → 5 条核心规则，上下文减少 60%）
- [x] API 原子启动锁（多 session 端口冲突防护）
- [x] 自动端口发现（API 自动寻找空闲端口，写入 `api_port.txt`）
- [x] MCP HTTP Streamable 端点（`/mcp/` 挂载到 FastAPI）
- [x] PyPI 发布——冻结于 1.2.0，已弃用（请改用 plugin/源码安装）
- [x] INSTALL.md CC 辅助安装指引

### 进行中 / 计划中

- [ ] 多用户隔离（Multi-tenant 支持）
- [ ] 实战验证与性能优化
- [x] Claude Code Plugin Marketplace 上架
- [ ] 完整集成测试套件
- [ ] 文档网站（Docusaurus）
- [ ] 视频教程系列

---

## 项目结构

```
ai-team-os/
├── src/aiteam/
│   ├── api/           — FastAPI REST 端点（217 条路由）
│   ├── mcp/
│   │   ├── server.py  — MCP 服务器入口
│   │   └── tools/     — 24 个工具模块（共 168 个 MCP 工具）
│   ├── loop/          — Loop 引擎
│   ├── meeting/       — 会议系统
│   ├── memory/        — 团队记忆
│   ├── orchestrator/  — 团队编排器
│   ├── storage/       — 存储层（SQLite，WAL 日志）
│   ├── templates/     — Agent 模板基类
│   ├── hooks/         — CC Hook 脚本（12 个生命周期事件）
│   └── types.py       — 共享类型定义
├── plugin/
│   ├── agents/        — 25 个 Agent 模板（.md）
│   └── .claude-plugin/ — Plugin 清单
├── dashboard/         — React 19 前端（22 个页面）
├── scripts/           — 预检 + 红线不变量机检（含 README 数字机检）
├── docs/              — 设计文档 + 生态集成配方
├── tests/             — 测试套件（1,758 测试）
├── install.py         — 一键安装脚本
└── pyproject.toml
```

---

## 贡献指南

欢迎贡献！特别期待以下方向：

- **新 Agent 模板**：如果你有专业角色的提示词设计，欢迎 PR
- **会议模板扩展**：新的结构化讨论模式
- **Bug 修复**：提 Issue 或直接 PR
- **文档改善**：发现文档与代码不一致，欢迎纠正

```bash
# 开发环境搭建
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company
python3 install.py

# 一条命令 = CI 全部门禁（ruff + eslint + 单测 + 红线机检）
bash scripts/preflight.sh
```

提 PR 前请确保 `bash scripts/preflight.sh` 通过——它跑的就是 CI 强制的全部门禁：ruff、eslint、单测套件，以及 `scripts/check_invariants.sh` 的红线不变量机检。其中每一条不变量（hook 双副本同步、版本锁步、dist 一致、venv 禁令、README 数字漂移）都提炼自本仓库历史上的一次真实事故——请保持它们全绿。

---

## License

MIT License — 详见 [LICENSE](LICENSE)

---

<div align="center">

**AI Team OS** — 你睡觉，它还在工作。

*Built with Claude Code · Powered by MCP Protocol*

[文档](docs/) · [Issues](https://github.com/CronusL-1141/AI-company/issues) · [讨论区](https://github.com/CronusL-1141/AI-company/discussions)

</div>

<!-- README 数字由机检脚本对照代码实测校验：scripts/check_readme_numbers.sh（scripts/check_invariants.sh 的 I6 不变量）。数字漂移即 CI 变红。 -->
