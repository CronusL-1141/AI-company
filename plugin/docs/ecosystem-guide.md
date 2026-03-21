# AI Team OS 生态推荐指南

> AI Team OS 的设计理念是作为生态指导体系——不必全部内建，而是推荐和包含优秀的第三方 Plugin 组合，让用户按需构建最适合自己的 AI 团队工作环境。

---

## 推荐的第三方 CC Plugin

### 记忆增强

#### 1. claude-mem (thedotmack/claude-mem)

- **Stars**: 21.5k+
- **核心能力**: 自动捕获会话操作 + AI 95% 压缩 + 跨 session 恢复
- **安装方式**:
  ```bash
  /plugin marketplace add thedotmack/claude-mem
  ```
- **与 OS 互补关系**:
  - OS 负责**团队级协调**——任务分配、Agent 间通信、工作流编排
  - claude-mem 负责**个人级记忆**——单个 Agent 的会话历史、操作习惯、上下文延续
  - 两者结合实现「团队协调 + 个体记忆」双层覆盖，互不冲突

### 持续学习

#### 2. continuous-learning-v2 (基于 instinct 的学习系统)

- **核心能力**: 观察 session 行为 → 提取原子 instinct → 进化为 skill
- **学习流程**:
  1. **观察阶段**: 监控 Agent 在 session 中的操作模式
  2. **提取阶段**: 将重复模式抽象为原子级 instinct
  3. **进化阶段**: 多个 instinct 聚合为可复用的 skill
- **与 OS 互补关系**:
  - OS 管理团队结构和任务流转
  - continuous-learning 负责团队积累的知识自动沉淀
  - 团队在多次项目迭代中逐步变得更「聪明」

### 代码质量

#### 3. code-review

- **核心能力**: PR 审查工具，自动分析代码变更并给出建议
- **适用场景**: 单人开发时的自动代码审查

#### 4. pr-review-toolkit

- **核心能力**: PR 审查专家团队，提供多维度的代码评审
- **适用场景**: 团队协作中需要多角度 review 的场景

---

## 配合使用最佳实践

### OS + claude-mem: 团队级 + 个人级记忆双层覆盖

```
┌─────────────────────────────────────────┐
│            AI Team OS (团队层)            │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │ Agent A  │  │ Agent B  │  │ Agent C  │ │
│  │┌───────┐│  │┌───────┐│  │┌───────┐│ │
│  ││claude ││  ││claude ││  ││claude ││ │
│  ││ -mem  ││  ││ -mem  ││  ││ -mem  ││ │
│  │└───────┘│  │└───────┘│  │└───────┘│ │
│  └─────────┘  └─────────┘  └─────────┘ │
│         团队协调 + 个体记忆               │
└─────────────────────────────────────────┘
```

- OS 维护团队级共享上下文（任务状态、Agent 角色、协作历史）
- claude-mem 维护每个 Agent 的个人记忆（操作偏好、会话延续）
- 新 Agent 加入团队时，OS 提供团队背景，claude-mem 提供个人积累

### OS + continuous-learning: 团队知识自动沉淀

- 团队在多个项目中反复执行类似任务时，learning 系统自动提取模式
- 提取的 skill 可通过 OS 的知识共享机制分发给团队中的其他 Agent
- 形成「实践 → 提取 → 共享 → 进化」的正向循环

### 安装顺序建议

1. **先装 AI Team OS** — 建立团队基础设施
2. **创建团队和 Agent** — 明确角色分工
3. **再装辅助 Plugin** — 按需为特定 Agent 启用增强能力

```bash
# Step 1: 确保 OS 已安装并运行
# Step 2: 创建团队
# Step 3: 按需添加辅助 plugin
/plugin marketplace add thedotmack/claude-mem        # 记忆增强
/plugin marketplace add continuous-learning-v2         # 持续学习
```

---

## Claude Code Skills 精选

> Claude Code Skills 是通过 SKILL.md 文件定义的可组合指令集，可以动态加载以增强特定任务的执行能力。以下是经过社区验证的推荐 Skills。

### 开发流程增强

#### 5. Superpowers (obra/superpowers)

- **GitHub**: [obra/superpowers](https://github.com/obra/superpowers) | 社区 Skills: [obra/superpowers-skills](https://github.com/obra/superpowers-skills)
- **核心能力**: 完整的软件开发工作流框架，通过可组合 Skills 强制 Agent 遵循人类开发者积累的工程纪律（先设计、写计划、TDD、Git 管理），而不是单纯提升模型能力。
- **安装方式**:
  ```bash
  /install obra/superpowers
  ```
- **包含的核心 Skills**:
  - `brainstorming` — 编码前先通过提问细化需求，探索替代方案
  - `using-git-worktrees` — 设计通过后自动创建隔离分支工作区
  - `writing-plans` — 将工作拆分为 2-5 分钟的精确步骤（含文件路径和验证方法）
  - `test-driven-development` — 强制执行 RED-GREEN-REFACTOR 循环
- **适用场景**: 需要 Agent 产出生产级代码、防止 AI "发散式"编码的项目；尤其适合多人协作或对代码质量要求严格的团队

### 前端设计

#### 6. Frontend-Design (anthropics/claude-code)

- **GitHub**: [anthropics/claude-code — plugins/frontend-design](https://github.com/anthropics/claude-code/tree/main/plugins/frontend-design)
- **核心能力**: 官方前端设计 Skill，引导 Claude 创建有鲜明美学风格的生产级前端界面，避免"AI 千篇一律"的通用外观，强调选择极端审美方向（极简主义、复古未来、工业风、编辑杂志风等）。
- **安装方式**:
  ```bash
  /install anthropics/claude-code#plugins/frontend-design
  ```
- **社区变体**: [Koomook/claude-frontend-skills](https://github.com/Koomook/claude-frontend-skills) — 提供更多扩展设计风格
- **适用场景**: 全栈开发中需要生成高质量 UI 原型；VibeCoding 风格项目（快速搭建有设计感的 Web 应用）；厌倦了 AI 生成的"默认 Tailwind 蓝"的场景

### 安全检测

#### 7. VibeSec (BehiSecc/VibeSec-Skill)

- **GitHub**: [BehiSecc/VibeSec-Skill](https://github.com/BehiSecc/VibeSec-Skill) | 官网: [vibesec.sh](https://vibesec.sh/)
- **核心能力**: 安全优先的代码守护 Skill，让 Claude 以"漏洞猎手"视角审查代码，自动实现访问控制、安全标头、输入验证与净化，主动拦截 IDOR、XSS、SQL 注入、SSRF、弱认证等常见漏洞。
- **安装方式**:
  ```bash
  /install BehiSecc/VibeSec-Skill
  ```
- **兼容性**: 支持 Claude Code、Cursor、GitHub Copilot 等所有支持自定义指令的 AI 工具
- **相关变体**: [raroque/vibe-security-skill](https://github.com/raroque/vibe-security-skill) — 专注于审计 AI 生成代码中的安全漏洞
- **适用场景**: 任何 Web 应用开发；VibeCoding 快速构建后的安全加固；需要在代码生成阶段就嵌入安全意识的团队

### Skill 开发工具

#### 8. Skill-Creator / Skill-Factory

- **GitHub (Skill Factory)**: [alirezarezvani/claude-code-skill-factory](https://github.com/alirezarezvani/claude-code-skill-factory)
- **GitHub (Agent Skill Creator)**: [FrancyJGLisboa/agent-skill-creator](https://github.com/FrancyJGLisboa/agent-skill-creator)
- **核心能力**:
  - **Skill-Factory**: 生产级 Skill 构建工具包，提供结构化模板生成、工作流自动化集成，加速 AI Agent 开发
  - **Agent-Skill-Creator**: 将任意工作流转化为可复用 Skill，一个 SKILL.md 文件适配 14+ 平台（Claude Code、Cursor、Copilot、Windsurf、Codex、Gemini CLI 等）
- **安装方式**:
  ```bash
  # Skill Factory
  git clone https://github.com/alirezarezvani/claude-code-skill-factory
  # Agent Skill Creator
  /install FrancyJGLisboa/agent-skill-creator
  ```
- **技巧**: 官方文档 [code.claude.com/docs/en/skills](https://code.claude.com/docs/en/skills) 提供 Skill 结构规范，SKILL.md 仅需 YAML frontmatter + 指令即可生效
- **适用场景**: 团队有重复工作流需要固化为可共享 Skill；需要将内部规范（编码风格、安全策略、部署流程）嵌入 Agent 行为

### Notebook 集成

#### 9. Jupyter / NotebookLM 集成

- **Notebook Intelligence**: [notebook-intelligence/notebook-intelligence](https://github.com/notebook-intelligence/notebook-intelligence) — 为 JupyterLab 提供 Claude Code AI Agent 模式，将 Claude Code 的内建工具、Skills、MCP Server 引入 JupyterLab 环境
- **jupyter-cc**: [vinceyyy/jupyter-cc](https://github.com/vinceyyy/jupyter-cc) — IPython magic 命令扩展，在 Notebook 中直接调用 Claude Code，Claude 读取变量、执行工具并生成新代码单元格
- **jupyter-notebook MCP**: [jjsantos01/jupyter-notebook-mcp](https://github.com/jjsantos01/jupyter-notebook-mcp) — 通过 MCP 协议让 Claude 直接控制 Jupyter Notebook 执行代码和数据分析
- **NotebookLM Skill**: [PleasePrompto/notebooklm-skill](https://github.com/PleasePrompto/notebooklm-skill) — 使 Claude Code 与 Google NotebookLM 直接通信，基于用户上传文档提供引用支撑的回答
- **安装方式（jupyter-cc）**:
  ```bash
  pip install jupyter-cc
  # 在 Notebook 中使用
  %load_ext jupyter_cc
  %%claude 分析这份数据并生成可视化
  ```
- **适用场景**: 数据科学和机器学习工作流；需要在 Notebook 环境中使用 AI Agent 能力；研究人员快速原型验证

---

## Task-Type → Skill 映射表

> 根据任务类型快速选择合适的 Skill 组合。

| 任务类型 | 推荐 Skill | 说明 |
|---------|-----------|------|
| 生产级功能开发 | Superpowers | 强制工程纪律：设计→计划→TDD→Git |
| 前端 UI 原型 | Frontend-Design | 避免通用 AI 外观，生成有美学的界面 |
| Web 安全加固 | VibeSec | 代码生成阶段嵌入安全审查 |
| VibeCoding 快速构建 | Frontend-Design + VibeSec | 快速好看 + 基础安全保障 |
| 数据分析 / ML 实验 | jupyter-cc / Notebook Intelligence | 在 Notebook 中直接使用 Claude 能力 |
| 团队工作流固化 | Skill-Creator / Skill-Factory | 将内部规范转化为可共享 Skill |
| 知识库问答 | NotebookLM Skill | 基于上传文档的引用式回答 |
| 记忆持久化 | claude-mem | 跨 session 保留 Agent 操作历史 |
| 团队知识沉淀 | continuous-learning-v2 | 自动提取重复模式为可复用 Skill |
| PR 代码审查 | code-review / pr-review-toolkit | 多维度代码质量把关 |

---

## 注意事项

### Hooks 冲突风险

- 多个 Plugin 都可能注册 `PostToolUse` 等 hooks
- 当多个 hooks 同时触发时，可能出现执行顺序不确定或互相干扰的情况
- **建议**: 安装新 plugin 后检查 `.claude/hooks.json`，确认 hooks 没有冲突

### Context 消耗控制

- 每个 Plugin 的 system prompt 和工具定义都会占用 context 窗口
- **按需启用，不要全部装** — 只启用当前阶段需要的 Plugin
- 如果 context 接近上限，优先保留 OS 核心功能，暂时禁用辅助 Plugin

### 兼容性说明

- 本指南中的 Plugin 推荐基于当前生态调研，版本和功能可能随时间变化
- 安装前请确认 Plugin 与当前 Claude Code 版本的兼容性
- 遇到问题时优先查阅各 Plugin 的官方文档和 issue 列表
