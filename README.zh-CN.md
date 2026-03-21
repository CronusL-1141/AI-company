[English](README.md) | [中文](README.zh-CN.md)

# AI Team OS

<!-- Logo placeholder -->
<!-- ![AI Team OS Logo](docs/assets/logo.png) -->

### 你的 AI 编程工具，停止提示就停止工作。我们的不会。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react)](https://react.dev)
[![MCP](https://img.shields.io/badge/MCP-Protocol-orange)](https://modelcontextprotocol.io)
[![Stars](https://img.shields.io/github/stars/CronusL-1141/AI-company?style=flat)](https://github.com/CronusL-1141/AI-company)

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

每次失败都让系统变得更聪明。"失败炼金术"提取防御规则，为未来的 Agent 生成培训案例，提交改进提案——系统对自身的错误产生抗体。

---

## 核心能力

### 1. 自主运转（核心卖点）

CEO 从不空闲。它按任务墙优先级持续推进工作：

- 一个任务完成后，立即检查任务墙，拿起下一个最高优先级任务
- 遇到需要你审批的阻塞点，挂起该线程，切换到并行工作流
- 批量汇总所有战略问题，等你回来时统一汇报——不为每个战术决策打断你
- 卡死检测：循环停滞时，系统主动暴露阻塞原因，而不是原地空转

### 2. 自我进化

系统不只是执行——它在进化：

- **研发循环**：研究 Agent 扫描竞品、新框架和社区工具。研究结果提交到头脑风暴会议，Agent 之间相互挑战辩论。结论变成实施计划进入任务墙。
- **失败炼金术**：每次任务失败都触发根因提取、归类，并产出三类输出：
  - *抗体* — 失败经验存入团队记忆，防止同类错误重现
  - *疫苗* — 高频失败模式转化为任务前预警
  - *催化剂* — 失败分析结果注入 Agent 的 system prompt，改善下次执行

### 3. 团队协作（不是单 Agent）

不是一个 Agent，而是一个结构化组织：

- **22 个专业 Agent 模板**——工程/测试/研究/管理，开箱即用
- **7 种结构化会议模板**，基于六顶思考帽、DACI 框架和 Design Sprint 方法论
- **部门分组管理**——工程部/测试部/研究部，支持跨部门协作
- 每次会议必须产出可执行结论，"讨论了但没决定"不是一个有效结果

### 4. 完全透明

没有黑盒：

- **决策驾驶舱**：事件流 + 决策时间线 + 意图透视，每个决策有迹可循
- **活动追踪**：实时展示每个 Agent 的状态和当前任务
- **What-If 分析器**：提交前对比多个方案，支持路径模拟和推荐

### 5. 零额外成本

100% 运行在你现有的 Claude Code 订阅套餐内：

- 不调用外部 API，不烧额外 token
- MCP 工具、Hooks 和 Agent 模板全部本地运行
- 完全复用你的 CC 套餐

---

## 它构建了自己

AI Team OS 管理了自身的开发过程：

- 组织了 5 场多 Agent 辩论式头脑风暴创新会议
- 对 CrewAI、AutoGen、LangGraph 和 Devin 进行了竞品分析
- 完成了 5 个重大创新功能方向的 67 个任务
- 生成了 14 份设计文档，共 10,000+ 行

这个为你的项目构建东西的系统……构建了它自己。

---

## 与主流方案对比

| 维度 | AI Team OS | CrewAI | AutoGen | LangGraph | Devin |
|------|-----------|--------|---------|-----------|-------|
| **定位** | CC 增强层 OS | 独立框架 | 独立框架 | 工作流引擎 | 独立 AI 工程师 |
| **集成方式** | MCP 协议接入 CC | 独立 Python 运行 | 独立 Python 运行 | 独立 Python 运行 | SaaS 独立产品 |
| **自主运转** | 持续循环，从不空闲 | 逐任务执行 | 逐任务执行 | 工作流驱动 | 有限 |
| **会议系统** | 7 种结构化模板 | 无 | 有限 | 无 | 无 |
| **失败学习** | 失败炼金术（抗体/疫苗/催化剂） | 无 | 无 | 无 | 有限 |
| **决策透明度** | 决策驾驶舱 + 时间线 | 无 | 有限 | 有限 | 黑盒 |
| **规则体系** | 四层防线（48+ 条） | 有限 | 有限 | 无 | 有限 |
| **Agent 模板** | 22 个开箱即用 | 内置角色 | 内置角色 | 无 | 无 |
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
│      (22个角色)    Loop引擎      (7种模板)                         │
│            │            │            │                          │
│            └────────────┼────────────┘                          │
│                         ▼                                       │
│              ┌──────────────────────┐                           │
│              │   OS 增强层           │                           │
│              │  ┌──────────────┐    │                           │
│              │  │  MCP Server  │    │                           │
│              │  │  (40+ tools) │    │                           │
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
│              │  + Memory System    │                            │
│              └─────────────────────┘                            │
└─────────────────────────────────────────────────────────────────┘
```

### 五层技术架构

```
Layer 5: Web Dashboard    — React 19 + TypeScript + Shadcn UI
Layer 4: CLI + REST API   — Typer + FastAPI
Layer 3: Team Orchestrator — LangGraph StateGraph
Layer 2: Memory Manager   — Mem0 / File fallback
Layer 1: Storage          — SQLite（开发环境）/ PostgreSQL（生产环境）
```

### Hook 系统（CC 与 OS 的桥梁）

```
SessionStart     → session_bootstrap.py          — 注入Leader简报 + 规则集 + 团队状态
SubagentStart    → inject_subagent_context.py    — 注入子Agent OS规则（2-Action等）
PreToolUse       → workflow_reminder.py          — 工作流提醒 + 安全护栏
PostToolUse      → send_event.py                 — 事件转发到 OS API
UserPromptSubmit → context_monitor.py            — 上下文使用率监控
```

---

## 快速开始

### 前置要求

- Python >= 3.11
- Claude Code（需要 MCP 支持）
- Node.js >= 20（Dashboard 前端，可选）

> **国内用户提示**：如果访问 GitHub 较慢，建议配置代理或使用 Gitee 镜像（如有）。

### 三步启动

```bash
# Step 1: 克隆仓库
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company/ai-team-os

# Step 2: 安装（自动配置 MCP + Hooks）
python install.py

# Step 3: 重启 Claude Code，OS 自动激活
# 验证：在 CC 中运行 → /mcp 查看 ai-team-os 工具是否挂载
```

### 验证安装

```bash
# 检查 OS 健康状态
curl http://localhost:8000/health
# 期望: {"status": "ok", "version": "0.1.0"}

# 通过 CC 创建第一个团队
# 在 Claude Code 中输入：
# "帮我创建一个 web 开发团队，包含前端、后端和测试工程师"
```

### 启动 Dashboard

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

### 任务看板
![Task Board](docs/screenshots/task-board.png)

### 项目详情 & 决策时间线
![Decision Timeline](docs/screenshots/decision-timeline.png)

### 会议室
![Meetings](docs/screenshots/meetings.png)

### 活动分析
![Analytics](docs/screenshots/analytics.png)

### 事件日志
![Events](docs/screenshots/events.png)

---

## MCP 工具一览

<details>
<summary>展开查看全部 40+ MCP 工具</summary>

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
| `task_create` | 创建新任务 |
| `task_auto_match` | 基于任务特征智能匹配最佳 Agent |
| `task_memo_add` | 为任务添加执行备忘记录 |
| `task_memo_read` | 读取任务历史备忘 |

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
| `meeting_create` | 创建结构化会议（支持7种模板） |
| `meeting_send_message` | 发送会议消息 |
| `meeting_read_messages` | 读取会议记录 |
| `meeting_conclude` | 总结会议结论 |
| `meeting_template_list` | 获取可用会议模板列表 |

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
| `memory_search` | 全文检索团队记忆库 |
| `team_knowledge` | 获取团队知识摘要 |

### 项目管理

| 工具 | 说明 |
|------|------|
| `project_create` | 创建项目 |
| `phase_create` | 创建项目阶段 |
| `phase_list` | 列出项目阶段 |

### 系统运维

| 工具 | 说明 |
|------|------|
| `os_health_check` | OS 健康检查 |
| `event_list` | 查看系统事件流 |
| `os_report_issue` | 上报问题 |
| `os_resolve_issue` | 标记问题已解决 |

</details>

---

## Agent 模板库

22 个开箱即用的专业 Agent 模板，覆盖完整软件工程团队配置：

### 工程部（Engineering）

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `engineering-software-architect` | 软件架构师 | 系统设计、架构评审 |
| `engineering-backend-architect` | 后端架构师 | API 设计、服务架构 |
| `engineering-frontend-developer` | 前端开发工程师 | UI 实现、交互开发 |
| `engineering-ai-engineer` | AI 工程师 | 模型集成、LLM 应用 |
| `engineering-mcp-builder` | MCP 构建专家 | MCP 工具开发 |
| `engineering-database-optimizer` | 数据库优化师 | 查询优化、Schema 设计 |
| `engineering-devops-automator` | DevOps 自动化工程师 | CI/CD、基础设施 |
| `engineering-sre` | 站点可靠性工程师 | 可观测性、故障处理 |
| `engineering-security-engineer` | 安全工程师 | 安全审查、漏洞分析 |
| `engineering-rapid-prototyper` | 快速原型工程师 | MVP 验证、快速迭代 |
| `engineering-mobile-developer` | 移动端开发工程师 | iOS/Android 开发 |
| `engineering-git-workflow-master` | Git 工作流专家 | 分支策略、代码协作 |

### 测试部（Testing）

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `testing-qa-engineer` | QA 工程师 | 测试策略、质量保障 |
| `testing-api-tester` | API 测试专家 | 接口测试、契约测试 |
| `testing-bug-fixer` | Bug 修复专家 | 缺陷分析、根因排查 |
| `testing-performance-benchmarker` | 性能基准测试师 | 性能分析、压测 |

### 研究与支持（Research & Support）

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `specialized-workflow-architect` | 工作流架构师 | 流程设计、自动化编排 |
| `support-technical-writer` | 技术文档工程师 | API 文档、用户指南 |
| `support-meeting-facilitator` | 会议主持人 | 结构化讨论、决策推进 |

### 管理层（Management）

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `management-tech-lead` | 技术 Lead | 技术决策、团队协调 |
| `management-project-manager` | 项目经理 | 进度管理、风险跟踪 |

### 专项模板

| 模板名 | 角色 | 适用场景 |
|--------|------|---------|
| `python-reviewer` | Python 代码审查 | Python 项目代码质量 |
| `security-reviewer` | 安全审查 | 代码安全扫描 |
| `refactor-cleaner` | 重构清理专家 | 技术债清理 |
| `tdd-guide` | TDD 引导 | 测试驱动开发 |

---

## 路线图

### 已完成

- [x] 核心 Loop 引擎（LoopEngine + 任务墙 + Watchdog + 回顾）
- [x] 失败炼金术（抗体 + 疫苗 + 催化剂）
- [x] 决策驾驶舱（事件流 + 时间线 + 意图透视）
- [x] 事件驱动任务墙 2.0（实时推送 + 智能匹配）
- [x] 团队活记忆（知识查询 + 经验共享）
- [x] What-If 分析器（多方案对比推荐）
- [x] 7 种结构化会议模板
- [x] 22 个专业 Agent 模板
- [x] 四层防线规则体系（30+ B规则 + 18 A规则）
- [x] Dashboard 指挥中心（React 19）
- [x] 40+ MCP 工具
- [x] AWARE 循环记忆系统

### 进行中 / 计划中

- [ ] 多用户隔离（Multi-tenant 支持）
- [ ] 实战验证与性能优化
- [ ] Claude Code Plugin Marketplace 上架
- [ ] 完整集成测试套件
- [ ] 文档网站（Docusaurus）
- [ ] 视频教程系列

---

## 项目结构

```
ai-team-os/
├── src/aiteam/
│   ├── api/           — FastAPI REST 端点
│   ├── mcp/           — MCP Server（40+ tools）
│   ├── loop/          — Loop 引擎
│   ├── meeting/       — 会议系统
│   ├── memory/        — 团队记忆
│   ├── orchestrator/  — 团队编排器
│   ├── storage/       — 存储层（SQLite/PostgreSQL）
│   ├── templates/     — Agent 模板基类
│   ├── hooks/         — CC Hook 脚本
│   └── types.py       — 共享类型定义
├── dashboard/         — React 19 前端
├── docs/              — 设计文档（14份）
├── tests/             — 测试套件
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
cd AI-company/ai-team-os
pip install -e ".[dev]"
pytest tests/
```

提 PR 前请确保：
- `ruff check src/` 通过
- `mypy src/` 无新增错误
- 相关测试通过

---

## License

MIT License — 详见 [LICENSE](LICENSE)

---

<div align="center">

**AI Team OS** — 你睡觉，它还在工作。

*Built with Claude Code · Powered by MCP Protocol*

[文档](docs/) · [Issues](https://github.com/CronusL-1141/AI-company/issues) · [讨论区](https://github.com/CronusL-1141/AI-company/discussions)

</div>
