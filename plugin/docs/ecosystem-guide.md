# AI Team OS 生态推荐指南

> AI Team OS 的设计理念是作为生态指导体系——不必全部内建，而是推荐和包含优秀的第三方 Plugin 组合，让用户按需构建最适合自己的 AI 团队工作环境。
>
> **渐进加载**: 本指南支持 3 层渐进发现，配合 `find_skill` MCP tool 使用效果最佳。

---

## 使用方式

```
# Layer 1 — 快速推荐：描述任务，获取 top 匹配
find_skill(task_description="build secure REST API", level=1)

# Layer 2 — 分类浏览：按类别查看所有 skill
find_skill(level=2, category="security")

# Layer 3 — 完整详情：查看单个 skill 的完整文档
find_skill(level=3, skill_id="vibesec")
```

---

<!-- ============================================================ -->
<!-- LAYER 1: Quick Reference — 一句话描述 + 安装命令               -->
<!-- ============================================================ -->

## Layer 1: 快速推荐表

> 根据任务类型快速选择。`find_skill(level=1, task_description="...")` 返回此层数据。

| ID | 名称 | 一句话描述 | 安装命令 |
|----|------|-----------|---------|
| claude-mem | claude-mem | 会话自动捕获 + AI 95%压缩 + 跨session恢复 | `/plugin marketplace add thedotmack/claude-mem` |
| continuous-learning-v2 | continuous-learning-v2 | 观察行为→提取instinct→进化为可复用skill | `/plugin marketplace add continuous-learning-v2` |
| code-review | code-review | 自动PR审查，分析代码变更并给出建议 | `/plugin marketplace add code-review` |
| pr-review-toolkit | pr-review-toolkit | 多角度PR审查专家团队 | `/plugin marketplace add pr-review-toolkit` |
| superpowers | Superpowers | 完整开发工作流框架：设计→计划→TDD→Git | `/install obra/superpowers` |
| frontend-design | Frontend-Design | 官方前端Skill，生成有鲜明美学的生产级UI | `/install anthropics/claude-code#plugins/frontend-design` |
| vibesec | VibeSec | 安全守护：自动检测IDOR/XSS/SQLi/SSRF并嵌入防护 | `/install BehiSecc/VibeSec-Skill` |
| skill-creator | Skill-Creator | 将任意工作流转化为可复用Skill，适配14+平台 | `/install FrancyJGLisboa/agent-skill-creator` |
| jupyter-notebook | Jupyter集成 | 在JupyterLab/Notebook中使用Claude Code AI Agent | `pip install jupyter-cc` |

### Task-Type → Skill 快速映射

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

<!-- ============================================================ -->
<!-- LAYER 2: Category Browse — 按分类展示功能/标签/适用场景        -->
<!-- ============================================================ -->

## Layer 2: 分类浏览

> 按任务类型分组浏览。`find_skill(level=2, category="...")` 返回此层数据。

### 记忆增强 (memory)

#### claude-mem
- **标签**: `memory` `context` `session` `compression`
- **GitHub**: [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | Stars: 21.5k+
- **功能**:
  - 自动捕获会话操作
  - AI 95% 压缩
  - 跨 session 上下文恢复
- **适用场景**: 跨session记忆持久化、单个Agent上下文延续

### 持续学习 (learning)

#### continuous-learning-v2
- **标签**: `learning` `instinct` `skill-evolution` `knowledge`
- **功能**:
  - 观察 session 行为模式
  - 将重复模式抽象为原子级 instinct
  - 多个 instinct 聚合为可复用 skill
- **适用场景**: 团队知识蒸馏、从重复任务中自动提取模式

### 代码质量 (code-quality)

#### code-review
- **标签**: `code-review` `pr` `quality`
- **功能**: 自动PR分析、代码变更建议
- **适用场景**: 单人开发时的自动代码审查

#### pr-review-toolkit
- **标签**: `code-review` `pr` `quality` `team`
- **功能**: 多维度代码评审、专家团队视角
- **适用场景**: 团队协作中需要多角度review的场景

### 开发流程 (dev-workflow)

#### Superpowers
- **标签**: `workflow` `tdd` `git` `planning` `engineering`
- **GitHub**: [obra/superpowers](https://github.com/obra/superpowers)
- **功能**:
  - `brainstorming` — 编码前先通过提问细化需求
  - `using-git-worktrees` — 自动创建隔离分支工作区
  - `writing-plans` — 拆分为2-5分钟精确步骤（含文件路径和验证方法）
  - `test-driven-development` — 强制 RED-GREEN-REFACTOR 循环
- **适用场景**: 生产级功能开发、强制TDD、多人协作严格质量要求

### 前端设计 (frontend)

#### Frontend-Design
- **标签**: `frontend` `ui` `design` `aesthetic` `vibe-coding`
- **GitHub**: [anthropics/claude-code — plugins/frontend-design](https://github.com/anthropics/claude-code/tree/main/plugins/frontend-design)
- **功能**:
  - 引导 Claude 创建鲜明视觉风格
  - 支持极端审美方向：极简主义、复古未来、工业风、编辑杂志风
  - 避免"AI千篇一律"通用外观
- **适用场景**: 全栈UI原型、VibeCoding快速Web应用、打破"默认Tailwind蓝"
- **社区变体**: [Koomook/claude-frontend-skills](https://github.com/Koomook/claude-frontend-skills)

### 安全检测 (security)

#### VibeSec
- **标签**: `security` `xss` `sqli` `ssrf` `idor` `audit`
- **GitHub**: [BehiSecc/VibeSec-Skill](https://github.com/BehiSecc/VibeSec-Skill) | 官网: [vibesec.sh](https://vibesec.sh/)
- **功能**:
  - "漏洞猎手"视角审查代码
  - 自动实现访问控制、安全标头、输入验证与净化
  - 主动拦截 IDOR、XSS、SQL注入、SSRF、弱认证
- **适用场景**: Web应用开发、VibeCoding后安全加固、安全嵌入代码生成
- **兼容性**: Claude Code, Cursor, GitHub Copilot 等所有支持自定义指令的AI工具
- **变体**: [raroque/vibe-security-skill](https://github.com/raroque/vibe-security-skill)

### 开发工具 (dev-tools)

#### Skill-Creator / Skill-Factory
- **标签**: `skill-builder` `workflow` `template` `meta`
- **GitHub**: [alirezarezvani/claude-code-skill-factory](https://github.com/alirezarezvani/claude-code-skill-factory), [FrancyJGLisboa/agent-skill-creator](https://github.com/FrancyJGLisboa/agent-skill-creator)
- **功能**:
  - Skill-Factory: 生产级Skill构建工具包，结构化模板生成
  - Agent-Skill-Creator: 将工作流转化为Skill，一个SKILL.md适配14+平台
- **适用场景**: 固化团队工作流为可共享Skill、将内部规范嵌入Agent行为
- **技巧**: 官方文档 [code.claude.com/docs/en/skills](https://code.claude.com/docs/en/skills) 提供Skill结构规范

### 数据科学 (data-science)

#### Jupyter / NotebookLM 集成
- **标签**: `jupyter` `notebook` `data-science` `ml` `visualization`
- **GitHub**: [notebook-intelligence](https://github.com/notebook-intelligence/notebook-intelligence), [jupyter-cc](https://github.com/vinceyyy/jupyter-cc), [jupyter-notebook-mcp](https://github.com/jjsantos01/jupyter-notebook-mcp)
- **功能**:
  - Notebook Intelligence: JupyterLab中的Claude Code Agent模式
  - jupyter-cc: IPython magic命令调用Claude
  - jupyter-notebook MCP: 通过MCP协议控制Jupyter Notebook
  - NotebookLM Skill: Claude与Google NotebookLM通信
- **适用场景**: 数据科学和ML工作流、Notebook环境AI Agent、研究快速原型

---

<!-- ============================================================ -->
<!-- LAYER 3: Full Detail — 完整文档含OS互补关系                    -->
<!-- ============================================================ -->

## Layer 3: 完整详情

> 单个 skill 的完整文档。`find_skill(level=3, skill_id="...")` 返回此层数据。

### claude-mem — 完整文档

- **Stars**: 21.5k+
- **安装方式**:
  ```bash
  /plugin marketplace add thedotmack/claude-mem
  ```
- **与 OS 互补关系**:
  - OS 负责**团队级协调**——任务分配、Agent间通信、工作流编排
  - claude-mem 负责**个人级记忆**——单个Agent的会话历史、操作习惯、上下文延续
  - 两者结合实现「团队协调 + 个体记忆」双层覆盖，互不冲突

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

### continuous-learning-v2 — 完整文档

- **学习流程**:
  1. **观察阶段**: 监控Agent在session中的操作模式
  2. **提取阶段**: 将重复模式抽象为原子级instinct
  3. **进化阶段**: 多个instinct聚合为可复用的skill
- **与 OS 互补关系**:
  - OS 管理团队结构和任务流转
  - continuous-learning 负责团队积累的知识自动沉淀
  - 形成「实践 → 提取 → 共享 → 进化」的正向循环

### Superpowers — 完整文档

- **GitHub**: [obra/superpowers](https://github.com/obra/superpowers) | 社区 Skills: [obra/superpowers-skills](https://github.com/obra/superpowers-skills)
- **安装方式**:
  ```bash
  /install obra/superpowers
  ```
- **包含的核心 Skills**:
  - `brainstorming` — 编码前先通过提问细化需求，探索替代方案
  - `using-git-worktrees` — 设计通过后自动创建隔离分支工作区
  - `writing-plans` — 将工作拆分为2-5分钟的精确步骤（含文件路径和验证方法）
  - `test-driven-development` — 强制执行 RED-GREEN-REFACTOR 循环
- **与 OS 互补关系**: OS编排团队；Superpowers在每个Agent内部强制工程纪律。对生产级代码和严格质量团队尤其有价值。

### Frontend-Design — 完整文档

- **GitHub**: [anthropics/claude-code — plugins/frontend-design](https://github.com/anthropics/claude-code/tree/main/plugins/frontend-design)
- **安装方式**:
  ```bash
  /install anthropics/claude-code#plugins/frontend-design
  ```
- **社区变体**: [Koomook/claude-frontend-skills](https://github.com/Koomook/claude-frontend-skills) — 提供更多扩展设计风格
- **与 OS 互补关系**: OS处理项目管理和团队协作；Frontend-Design专注于让每个前端组件具有独特的视觉风格和审美深度。

### VibeSec — 完整文档

- **GitHub**: [BehiSecc/VibeSec-Skill](https://github.com/BehiSecc/VibeSec-Skill) | 官网: [vibesec.sh](https://vibesec.sh/)
- **安装方式**:
  ```bash
  /install BehiSecc/VibeSec-Skill
  ```
- **兼容性**: 支持 Claude Code、Cursor、GitHub Copilot 等所有支持自定义指令的AI工具
- **相关变体**: [raroque/vibe-security-skill](https://github.com/raroque/vibe-security-skill) — 专注于审计AI生成代码中的安全漏洞
- **与 OS 互补关系**: OS管理工作流和团队协调；VibeSec在代码生成阶段就嵌入安全意识，漏洞在到达生产环境前就被拦截。

### Skill-Creator / Skill-Factory — 完整文档

- **GitHub (Skill Factory)**: [alirezarezvani/claude-code-skill-factory](https://github.com/alirezarezvani/claude-code-skill-factory)
- **GitHub (Agent Skill Creator)**: [FrancyJGLisboa/agent-skill-creator](https://github.com/FrancyJGLisboa/agent-skill-creator)
- **安装方式**:
  ```bash
  # Skill Factory
  git clone https://github.com/alirezarezvani/claude-code-skill-factory
  # Agent Skill Creator
  /install FrancyJGLisboa/agent-skill-creator
  ```
- **技巧**: 官方文档 [code.claude.com/docs/en/skills](https://code.claude.com/docs/en/skills) 提供Skill结构规范，SKILL.md仅需YAML frontmatter + 指令即可生效
- **与 OS 互补关系**: OS定义团队角色和工作流；Skill-Creator将团队最佳实践固化为可分发的skill，新成员自动继承团队标准。

### Jupyter / NotebookLM 集成 — 完整文档

- **Notebook Intelligence**: [notebook-intelligence/notebook-intelligence](https://github.com/notebook-intelligence/notebook-intelligence) — 为JupyterLab提供Claude Code AI Agent模式
- **jupyter-cc**: [vinceyyy/jupyter-cc](https://github.com/vinceyyy/jupyter-cc) — IPython magic命令扩展
- **jupyter-notebook MCP**: [jjsantos01/jupyter-notebook-mcp](https://github.com/jjsantos01/jupyter-notebook-mcp) — 通过MCP协议控制Jupyter
- **NotebookLM Skill**: [PleasePrompto/notebooklm-skill](https://github.com/PleasePrompto/notebooklm-skill) — Claude与Google NotebookLM通信
- **安装方式（jupyter-cc）**:
  ```bash
  pip install jupyter-cc
  # 在 Notebook 中使用
  %load_ext jupyter_cc
  %%claude 分析这份数据并生成可视化
  ```
- **与 OS 互补关系**: OS管理项目和团队；Jupyter集成让数据科学Agent在notebook环境中原生使用AI能力，无需离开实验环境。

---

## 外部工具集成配方

> 详细的集成配方请参考 **[docs/ecosystem-recipes.md](../../docs/ecosystem-recipes.md)**。

AI Team OS 作为"编排其他 MCP 的元 Plugin"，可以与外部开发工具的 MCP server 组合使用。以下是已验证的集成方案：

| 集成目标 | MCP Server | 典型场景 | 查询命令 |
|---------|-----------|---------|---------|
| GitHub | `@modelcontextprotocol/server-github` | PR 管理、Issue 同步、代码提交 | `ecosystem_recipes(recipe_id="github")` |
| Slack | `@modelcontextprotocol/server-slack` | 团队通知、告警推送、站会摘要 | `ecosystem_recipes(recipe_id="slack")` |
| Linear | `@modelcontextprotocol/server-linear` | 任务同步、Sprint↔Pipeline 映射 | `ecosystem_recipes(recipe_id="linear")` |
| 全栈团队 | GitHub + Slack + AI Team OS | 预设多角色团队一键启动 | `ecosystem_recipes(recipe_id="fullstack-team")` |

### 快速查询

```
# 列出所有集成配方
ecosystem_recipes()

# 查看特定配方详情（含推荐 MCP、场景、OS 工具链）
ecosystem_recipes(recipe_id="github")
```

---

## 配合使用最佳实践

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

### OS + claude-mem: 团队级 + 个人级记忆双层覆盖

- OS 维护团队级共享上下文（任务状态、Agent 角色、协作历史）
- claude-mem 维护每个 Agent 的个人记忆（操作偏好、会话延续）
- 新 Agent 加入团队时，OS 提供团队背景，claude-mem 提供个人积累

### OS + continuous-learning: 团队知识自动沉淀

- 团队在多个项目中反复执行类似任务时，learning 系统自动提取模式
- 提取的 skill 可通过 OS 的知识共享机制分发给团队中的其他 Agent
- 形成「实践 → 提取 → 共享 → 进化」的正向循环

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
