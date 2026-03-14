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
