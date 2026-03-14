# 任务拆解引擎 -- 行业方案调研报告

> 调研人: decompose-researcher-1
> 日期: 2026-03-14

## 1. 调研背景

AI Team OS 需要任务拆解功能: Leader 描述高级目标(如"实现用户认证系统"), OS 自动拆解为多个可分配给 CC Agent 的子任务。

**核心约束:**
- 不依赖 LLM 做拆解(避免 API key 依赖)
- 希望基于模板/规则的轻量方案
- 子任务要能直接分配给 CC Agent 执行

---

## 2. 行业方案逐一分析

### 2.1 LangGraph (LangChain)

**拆解策略: LLM驱动的 Plan-and-Execute 模式**

LangGraph 的核心方案是 **Plan-and-Execute** 模式: 先用 LLM 生成多步计划, 然后逐步执行。其最新的 **Deep Agents** 框架在此基础上增加了:

- `write_todos` 内置工具: Agent 将复杂任务分解为离散步骤, 跟踪进度, 根据新信息调整计划
- 子 Agent 委派: 主 Agent 可以 spawn 子 Agent 处理子任务
- 虚拟文件系统记忆: 任务状态持久化

**粒度控制:**
- Plan-and-Execute 的优势: 执行步骤可以用更轻量的模型/无需额外 LLM 调用
- ReWOO 变体: 通过变量赋值实现任务间依赖, 允许基于规则的任务拆分
- 但计划生成阶段仍然依赖 LLM

**与我们场景的适配性: 中等**
- 优点: Plan-and-Execute 的执行阶段不需要 LLM, 可借鉴其任务结构(步骤列表 + 依赖关系)
- 缺点: 计划生成阶段仍依赖 LLM, 与我们"不依赖LLM"的约束冲突
- 可借鉴: 任务结构定义(步骤、依赖、状态跟踪)、ReWOO 的变量赋值思路

### 2.2 CrewAI

**拆解策略: Manager Agent 分层委派**

CrewAI 提供两种流程模式:

1. **Sequential(顺序)**: 任务按线性顺序执行, 无自动拆解
2. **Hierarchical(分层)**: Manager Agent 作为编排器, 将主问题拆解为更小的子任务, 分配给专家 Agent

**核心机制:**
- Manager Agent 根据 Agent 角色和能力分配任务
- 任务不预先分配, Manager 动态决定
- Worker Agent 必须设置 `allow_delegation=False` 避免无限委派循环
- 分层模式下, 任务描述的是整体目标而非具体步骤, Manager 填充步骤

**粒度控制:**
- 完全依赖 Manager Agent(LLM) 的判断
- 无显式粒度控制机制

**与我们场景的适配性: 低**
- 分层委派的架构思路可借鉴(Leader 即 Manager)
- 但核心拆解完全依赖 LLM, 不符合我们的约束
- 可借鉴: "只有 Manager 能委派"的单向委派规则、角色能力匹配

### 2.3 AutoGen (Microsoft)

**拆解策略: 对话驱动 + 角色分工**

AutoGen 通过多 Agent 对话实现任务分解:

- Agent 抽象提供任务分解、专业化和工具使用
- 主要特性: 专业 Agent 角色(planner, executor)、human-in-the-loop、动态任务分解
- GroupChat 模式: 自动 speaker 选择, 不同 Agent 执行不同子任务

**重要变化:**
- AutoGen 已进入维护模式, Microsoft 将其与 Semantic Kernel 合并为 Microsoft Agent Framework
- v0.4 采用异步事件驱动架构, 但不再有新特性开发

**粒度控制:**
- 依赖 LLM 在对话中动态决定
- GroupChat 的 speaker 选择提供了一定的结构化

**与我们场景的适配性: 低**
- 框架已进入维护模式, 不建议作为参考基准
- 对话驱动模式过重, 不适合轻量模板方案
- 可借鉴: Planner/Executor 角色分离的思路

### 2.4 OpenHands (原 OpenDevin)

**拆解策略: 代码分析 + Agent 委派**

OpenHands 在大规模重构场景中的任务拆解策略值得关注:

- **Refactor SDK**: 包含依赖分析工具, 自动识别代码库中的独立部分
- 基于目录边界和依赖关系将代码库分解为可管理的小块
- `AgentDelegateAction`: Agent 将特定子任务委派给另一个 Agent
- V1 SDK 架构: 模块化设计, 可选沙箱, 可复用的 agent/tool/workspace 包

**粒度控制:**
- 基于代码结构分析(目录、依赖)的半自动拆分
- "最难的部分是将项目分解为单个 Agent 可以自主完成的任务"

**与我们场景的适配性: 中高**
- 基于代码结构分析的拆分思路非常适合软件开发任务
- 依赖分析 -> 独立子任务的路径不需要 LLM
- 但仅限于代码重构场景, 通用性不足
- 可借鉴: 依赖分析驱动的任务拆分、目录边界作为自然拆分点

### 2.5 GitHub 上的专门库

#### 2.5.1 ai-dev-tasks (snarktank)

**最值得关注的方案**, 因为它最接近我们的需求:

- **纯模板驱动**: 三个 `.mdc` 文件作为命令模板(create-prd, generate-tasks, process-task-list)
- **严格依赖链**: 前一个命令的输出是下一个命令的必需输入
- **零安装、零依赖**: 不绑定特定 API 或运行时
- **规则式拆解**:
  - 明确的验收标准(可运行的测试或精确行为描述)
  - 通过 `@file/path` 引用附加最小上下文
  - 单任务完成强制执行
- **Prompt-as-Code**: 提示词作为可审计的仓库资产

**与我们场景的适配性: 高**
- 完全不依赖 LLM 做拆解结构决策
- 模板驱动 + 规则约束的思路与我们需求高度匹配
- 可直接参考其 PRD -> Tasks -> Process 的三阶段流水线

#### 2.5.2 GitHub Agentic Workflows

GitHub 官方的 Agentic Workflows(2026年2月技术预览):
- 在 GitHub Actions 中运行 AI Agent
- 自动化 issue 分类、PR 审查、CI 失败分析
- 但核心仍依赖 LLM, 更多是工作流自动化而非任务拆解

---

## 3. 拆解策略对比总结

| 方案 | 拆解策略 | 是否依赖LLM | 粒度控制 | 适配性 |
|------|---------|------------|---------|--------|
| LangGraph | Plan-and-Execute | 计划阶段依赖 | 中(ReWOO可变量绑定) | 中 |
| CrewAI | Manager Agent 委派 | 完全依赖 | 低(Manager决定) | 低 |
| AutoGen | 对话驱动 | 完全依赖 | 低(动态) | 低(已维护模式) |
| OpenHands | 代码结构分析+委派 | 分析不依赖,拆解部分依赖 | 高(基于依赖) | 中高(仅代码场景) |
| ai-dev-tasks | 模板/规则驱动 | 不依赖 | 高(模板定义) | **高** |

---

## 4. 推荐方案: 模板+规则的混合引擎

综合调研结果, 推荐基于 **模板/规则驱动** 的轻量方案, 借鉴多个框架的优点:

### 4.1 核心设计思路

```
高级目标 --> 模板匹配 --> 任务模板展开 --> 依赖分析 --> 子任务列表
```

**三层拆解策略:**

1. **模板层(Template)**: 预定义常见软件开发任务的拆解模板
   - 借鉴 ai-dev-tasks 的 PRD -> Tasks -> Process 流水线
   - 例如: "实现用户认证" 匹配 "auth-system" 模板 -> 展开为数据模型/API/前端/测试等子任务

2. **规则层(Rules)**: 通用拆解规则处理模板未覆盖的场景
   - 借鉴 OpenHands 的目录边界和依赖分析
   - 例如: 按模块边界拆分、前后端分离、测试独立等规则

3. **结构层(Structure)**: 子任务的标准化结构
   - 借鉴 LangGraph 的任务定义(描述、依赖、状态、验收标准)
   - 借鉴 CrewAI 的角色能力匹配(子任务标注所需 Agent 类型)

### 4.2 模板示例

```yaml
# 模板: web-feature (Web功能开发)
name: web-feature
match_keywords: ["实现", "开发", "添加功能", "新增"]
subtasks:
  - id: design
    name: "设计数据模型和API接口"
    type: backend
    priority: 1
    depends_on: []
    acceptance: "数据模型定义完成, API接口文档就绪"

  - id: backend
    name: "实现后端API"
    type: backend
    priority: 2
    depends_on: [design]
    acceptance: "API端点可访问, 返回正确数据"

  - id: frontend
    name: "实现前端界面"
    type: frontend
    priority: 2
    depends_on: [design]
    acceptance: "UI组件渲染正常, 与API对接成功"

  - id: test
    name: "编写和运行测试"
    type: testing
    priority: 3
    depends_on: [backend, frontend]
    acceptance: "所有测试通过"
```

### 4.3 与 CC Agent 的集成

- 每个子任务的 `type` 字段对应 Agent 的角色/能力标签
- Leader 根据 `depends_on` 确定执行顺序(可并行无依赖任务)
- `acceptance` 字段作为 Agent 完成的判定标准
- 子任务直接映射为 OS 的 task, 可通过 `task_run` 分配给 Agent

### 4.4 粒度控制

- 模板定义基础粒度(通常3-7个子任务)
- 规则引擎可对子任务进一步拆分(如"实现后端API"在大项目中可拆为多个端点)
- 配置参数: `max_depth`(最大拆分深度)、`min_granularity`(最小粒度描述字数)

---

## 5. 关键结论

1. **行业主流方案均依赖 LLM 做任务拆解**, 这是因为通用性需求; 但我们的场景(软件开发)足够垂直, 可以用模板覆盖大部分情况

2. **ai-dev-tasks 是最接近我们需求的现有方案**, 其"模板即代码"的理念值得直接借鉴

3. **OpenHands 的依赖分析思路适合代码级拆分**, 可作为规则层的补充

4. **不建议照搬 CrewAI/AutoGen 的方案**, 它们的核心价值在 LLM 协调, 与我们"不依赖LLM"的约束不符

5. **推荐的模板+规则混合引擎** 可以做到:
   - 零 LLM 依赖
   - 可预测、可审计的拆解结果
   - 通过扩展模板库持续提升覆盖率
   - 子任务直接对接 CC Agent 执行
