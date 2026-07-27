# AI Team OS 生态集成配方

> AI Team OS 定位为"编排其他 Plugin/MCP 的元 Plugin"。本文档提供与主流开发工具的集成配方，帮助你快速构建适合自己团队的工作环境。
>
> **原则**: 不写代码集成，而是组合现有 MCP server —— AI Team OS 负责编排，第三方 MCP 负责执行。

---

## 目录

- [配方 1: GitHub 集成](#配方-1-github-集成)
- [配方 2: Slack 集成](#配方-2-slack-集成)
- [配方 3: Linear 集成](#配方-3-linear-集成)
- [配方 4: 全栈开发团队模板](#配方-4-全栈开发团队模板)
- [配方组合矩阵](#配方组合矩阵)
- [故障排查](#故障排查)

---

## 配方 1: GitHub 集成

将 GitHub 与 AI Team OS 结合，实现代码管理、PR 审查、Issue 追踪的自动化编排。

### 推荐 MCP

| MCP Server | 来源 | 说明 |
|-----------|------|------|
| `github` | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers/tree/main/src/github) | 官方 GitHub MCP server，提供完整的 GitHub API 能力 |

### 安装配置

在 `.mcp.json` 中添加 GitHub MCP server：

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "<your-token>"
      }
    }
  }
}
```

**Token 获取**: GitHub Settings > Developer Settings > Personal access tokens > 创建 token，勾选 `repo`、`issues`、`pull_requests` 权限。

### 与 AI Team OS 配合方式

#### 场景 A: 交付阶段自动提交 + 创建 PR

```
工作流:
  1. Agent 完成代码编写
  2. Leader 用 task_update 把任务推进到交付状态
  3. Agent 直接用 Bash 跑 git add/commit（agent 手里本来就有 Bash）
  4. GitHub MCP 创建 Pull Request 并推送到 GitHub
```

OS 工具链:
- `task_update` — 推进任务状态，交付节点在任务墙上可见
- `task_list_project` — 查看本项目待交付项
> Git 三件套（`git_auto_commit` / `git_create_pr` / `git_status_check`）已于 v1.10.3
> 退役——包了三层的 git 壳只会把 CC 自带的 Bash 能力削窄，提交与开 PR 直接交给
> Bash + GitHub MCP。

#### 场景 B: Code Review 双重审查

```
工作流:
  1. 开发 Agent 提交代码
  2. AI Team OS debate_code_review 发起内部代码审查
  3. 审查通过后，创建 GitHub PR
  4. GitHub MCP 读取 PR 评论，补充外部审查反馈
  5. 最终合并决策由 Leader 统筹
```

OS 工具链:
- `debate_code_review` — AI 内部代码审查（多视角辩论）
- `task_memo_add` — 记录审查结论

#### 场景 C: Issue 与任务墙双向映射

```
映射规则:
  GitHub Issue (open)      ↔  AI Team OS 任务 (todo/in_progress)
  GitHub Issue (closed)    ↔  AI Team OS 任务 (completed)
  GitHub Issue label       ↔  AI Team OS 任务 priority/tags
  GitHub Issue assignee    ↔  AI Team OS Agent assignee
```

实践建议:
- 每次 sprint 开始时，Leader 从 GitHub Issues 同步任务到任务墙
- Agent 完成任务后，由 Leader 关闭对应 GitHub Issue
- 使用 `task_memo_add` 在任务中记录 GitHub Issue 链接

---

## 配方 2: Slack 集成

将 Slack 与 AI Team OS 结合，实现团队通知、告警和站会摘要的自动推送。

### 推荐 MCP

| MCP Server | 来源 | 说明 |
|-----------|------|------|
| `slack` | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers/tree/main/src/slack) | 官方 Slack MCP server，支持频道消息读写 |

### 安装配置

在 `.mcp.json` 中添加 Slack MCP server：

```json
{
  "mcpServers": {
    "slack": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-slack"],
      "env": {
        "SLACK_BOT_TOKEN": "<your-bot-token>",
        "SLACK_TEAM_ID": "<your-team-id>"
      }
    }
  }
}
```

**Token 获取**: Slack App 管理页 > 创建 App > OAuth & Permissions > 添加 `chat:write`, `channels:read`, `channels:history` 权限 > 安装到工作区 > 复制 Bot Token。

### 与 AI Team OS 配合方式

#### 场景 A: Leader 简报推送

```
工作流:
  1. 一轮工作收尾时，Leader 调用 team_briefing 生成团队简报
  2. 简报内容通过 Slack MCP 推送到指定频道
  3. 团队成员在 Slack 中查看项目进展
```

OS 工具链:
- `team_briefing` — 生成团队工作简报
- `briefing_list` — 查看历史简报（支持按项目/标签筛选）
> 推送本身交给 Slack MCP：OS 内置的 `send_notification` 已于 v1.10.3 退役，它依赖的
> webhook 配置文件从来没被创建过，调必失败。

#### 场景 B: 任务失败告警

```
工作流:
  1. Watchdog 巡检发现 BUSY 超时 agent / 长期 PENDING 任务
  2. Leader 用 diagnose_task_failure 取根因
  3. 通过 Slack MCP 向 #ops-alerts 频道发送告警消息
  4. 包含：失败任务、根因、建议行动
```

OS 工具链:
- `diagnose_task_failure` — 单任务失败诊断（只回诊断，不写状态）
- `failure_analysis` — 提取根因并把抗体写入项目记忆
- `briefing_add` — 需要你拍板时入决策队列（带 tags 便于筛选）
> SRE 错误预算模型（`error_budget_*`）与 `os_report_issue` 已于 v1.10.3 退役：
> 前者的数据目录终其一生没有过一个文件，后者史上 0 条 issue。

#### 场景 C: 每日站会摘要

```
工作流:
  1. Leader 调用 task_list_project 获取任务墙状态
  2. 汇总：昨日完成 / 今日计划 / 阻塞项
  3. 格式化为站会摘要，推送到 #standup 频道
  4. 可结合 meeting_conclude 的会议纪要一起发送
```

OS 工具链:
- `task_list_project` — 查看任务墙全局视图（传 team_id 即单队视图）
- `meeting_conclude` — 生成会议纪要
- `task_status` — 查看单个任务的当前状态

---

## 配方 3: Linear 集成

将 Linear 与 AI Team OS 结合，实现项目管理工具之间的任务同步。

### 推荐 MCP

| MCP Server | 来源 | 说明 |
|-----------|------|------|
| `linear` | [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers/tree/main/src/linear) | Linear MCP server，支持 Issue 和 Project 操作 |

### 安装配置

在 `.mcp.json` 中添加 Linear MCP server：

```json
{
  "mcpServers": {
    "linear": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-linear"],
      "env": {
        "LINEAR_API_KEY": "<your-api-key>"
      }
    }
  }
}
```

**API Key 获取**: Linear Settings > API > Personal API keys > 创建新 key。

### 与 AI Team OS 配合方式

#### 场景 A: Linear Issue 与 AI Team OS 任务同步

```
映射规则:
  Linear Issue          ↔  AI Team OS task_create
  Linear Issue status   ↔  task_update status
  Linear assignee       ↔  Agent assignee
  Linear priority       ↔  task priority
  Linear label/project  ↔  task tags
```

实践建议:
- Sprint 开始时从 Linear 同步 Issue 到 AI Team OS 任务墙
- Agent 完成任务后更新 Linear Issue 状态
- 使用 task_memo 记录 Linear Issue ID 建立关联

#### 场景 B: Sprint 与任务墙映射

```
Linear Sprint 阶段      →  AI Team OS 任务状态
  Backlog               →  pending
  In Progress           →  running
  In Review             →  running（评审中，用 tags 标注）
  Done                  →  completed
```

OS 工具链:
- `task_create` / `task_update` — 建任务与推进状态
- `task_list_project` — 按项目查看整墙进度
> Pipeline 子系统（`pipeline_create` / `pipeline_advance` / `pipeline_status`）已于
> v1.10.3 整域退役，阶段编排改由 CC 原生 Workflow 承担，OS 侧用 `workflow_list` /
> `workflow_get` 做持久化观测。

#### 场景 C: 状态双向同步

```
工作流:
  1. Linear 中创建新 Issue → Leader 在 OS 中 task_create 对应任务
  2. OS 中任务状态变更 → Leader 通过 Linear MCP 更新 Issue 状态
  3. OS 中任务完成 → Leader 关闭 Linear Issue 并记录完成摘要
```

---

## 配方 4: 全栈开发团队模板

一个完整的全栈开发团队配置，组合 AI Team OS + GitHub + Slack，预设前端、后端、测试、DevOps 角色。

### 推荐组合

| 组件 | 用途 |
|------|------|
| AI Team OS | 团队编排、任务管理、循环驱动 |
| GitHub MCP | 代码管理、PR、Issue |
| Slack MCP | 团队通知、告警、站会 |
| Superpowers Skill | 强制 TDD + Git 工作流纪律 |
| VibeSec Skill | 代码安全审查 |

### MCP 配置

`.mcp.json` 完整示例：

```json
{
  "mcpServers": {
    "ai-team-os": {
      "command": "python",
      "args": ["-m", "aiteam.mcp"],
      "env": {
        "AITEAM_DB_PATH": "./aiteam.db"
      }
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "<your-token>"
      }
    },
    "slack": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-slack"],
      "env": {
        "SLACK_BOT_TOKEN": "<your-bot-token>",
        "SLACK_TEAM_ID": "<your-team-id>"
      }
    }
  }
}
```

### 预设团队配置

使用 AI Team OS 创建全栈团队:

```
# Step 1: 创建项目（唯一需要你显式做的一步）
project_create(name="my-fullstack-app", root_path="/path/to/project")

# Step 2: 直接派 agent —— 队和成员行由 hook 链自动建立
#   本会话的容器队在第一个 agent 出生时自动收编，无需手工建队/注册。
Agent(subagent_type="frontend-developer", name="frontend-dev", model="opus",
      prompt="开发 UI 组件……")
Agent(subagent_type="backend-architect",  name="backend-dev",  model="opus",
      prompt="开发 API 端点……")
Agent(subagent_type="testing-qa-engineer", name="qa-engineer", model="opus",
      prompt="补 E2E 覆盖……")
Agent(subagent_type="engineering-devops-automator", name="devops", model="opus",
      prompt="打镜像与 CI……")

# Step 3: 看看都有谁（可选）
agent_list()
agent_template_recommend(task_type="fullstack")
```

> `team_create` 与 `agent_register` 已于 v1.10.3 退役：前者造出的队没有 CC 侧目录，
> 建一个下一轮就被回收；后者会造出没有进程的空壳行（全库 `source='api'` 的 agent
> 是 0 个）。收编由 SubagentStart hook 全自动完成。

### 典型工作流

```
Sprint 开始
  │
  ├── Leader: 从 GitHub Issues / Linear 同步任务到任务墙
  ├── Leader: task_list_project 按优先级取下一件事
  │
  ├── 开发阶段
  │   ├── frontend-dev: 开发 UI 组件 (配合 Frontend-Design Skill)
  │   ├── backend-dev: 开发 API 端点 (配合 Superpowers TDD)
  │   ├── VibeSec: 代码安全扫描
  │   └── Leader: 通过 task_list_project 监控进度
  │
  ├── 审查阶段
  │   ├── debate_code_review: AI 内部代码审查
  │   ├── GitHub MCP: 创建 GitHub PR
  │   └── qa-engineer: 运行 E2E 测试
  │
  ├── 部署阶段
  │   ├── devops: 构建 Docker 镜像
  │   ├── Bash: git commit 提交最终代码
  │   └── Leader: task_update 推进到交付完成
  │
  └── 回顾
      ├── meeting_create: 开复盘会
      ├── team_briefing: 生成简报 → Slack 推送
      └── meeting_conclude: 记录会议纪要
```

---

## 配方组合矩阵

不同团队规模和场景的推荐组合：

| 场景 | 必需 | 推荐 | 可选 |
|------|------|------|------|
| 个人开发者 | AI Team OS | GitHub MCP | Superpowers |
| 小团队 (2-5人) | AI Team OS + GitHub | Slack MCP | Linear, VibeSec |
| 全栈团队 | AI Team OS + GitHub + Slack | Superpowers + VibeSec | Linear, Frontend-Design |
| 开源项目 | AI Team OS + GitHub | code-review Skill | Slack MCP |
| 数据科学团队 | AI Team OS | Jupyter MCP | claude-mem |

---

## 故障排查

### Q: MCP server 启动失败

**A**: 确认依赖已安装:
```bash
# GitHub/Slack/Linear MCP 都需要 Node.js
node --version  # 需要 >= 18

# 检查 npx 是否可用
npx --version
```

### Q: GitHub Token 权限不足

**A**: 确认 token 包含以下 scope:
- `repo` — 仓库访问
- `read:org` — 组织信息（如需要）
- `workflow` — GitHub Actions（如需要）

### Q: Slack 消息发送失败

**A**: 检查以下几点:
1. Bot Token 是否有 `chat:write` 权限
2. Bot 是否已被邀请到目标频道（`/invite @your-bot`）
3. `SLACK_TEAM_ID` 是否正确（在 Slack 管理面板查看）

### Q: 多个 MCP server 的 context 消耗过大

**A**: AI Team OS 的建议是**按需启用**:
- 不要同时启用所有 MCP server
- 只在需要时在 `.mcp.json` 中添加对应 server
- 如果 context 接近上限，优先保留 AI Team OS 核心功能

### Q: 如何查看可用的集成配方

**A**: 使用 AI Team OS 内置工具:
```
find_skill(level=2, category="integration")              # 查看所有配方
find_skill(level=3, skill_id="github-integration")       # 查看特定配方
find_skill(level=3, skill_id="fullstack-team")           # 查看全栈团队模板
```

> 配方已于 2026-07-27 并入 `find_skill` 的 `integration` 分类（原 `ecosystem_recipes`
> 工具退役）——"生态里有什么、怎么装"本来就是同一件事，不再各占一个工具名。
