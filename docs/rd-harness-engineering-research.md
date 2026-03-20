# Harness Engineering 研究报告

> 研究日期: 2026-03-20
> 研究目标: 了解 Harness Engineering 对 AI Agent 框架搭建的参考价值

---

## 一、什么是 Harness Engineering？

**Harness Engineering（线束工程/约束工程）** 是2026年AI工程领域最重要的新兴学科之一。

核心定义：**设计和实现约束AI Agent行为的系统——告知Agent该做什么、限制Agent能做什么、验证Agent做对了什么、纠正Agent做错了什么。**

> "2025年证明了Agent能工作；2026年的课题是让Agent可靠地工作。" — 行业共识

关键洞察：**模型是商品，线束（Harness）才是护城河。** LangChain仅通过修改Harness（不换模型）就将Terminal Bench 2.0得分从52.8%提升到66.5%（Top 30 → Top 5）。

### 类比理解

Phil Schmid（Hugging Face）提出的计算机类比：
- **Model = CPU**（处理能力）
- **Context Window = RAM**（工作记忆）
- **Harness = Operating System**（上下文管理、提示编排、工具处理）
- **Agent = Application**（用户特定逻辑）

---

## 二、核心资源列表

### 2.1 原创/权威来源

| 来源 | 标题 | 核心价值 |
|------|------|----------|
| **OpenAI官方博客** | Harness Engineering: Leveraging Codex in an Agent-First World | 概念提出者，5个月用Codex构建100万行代码的实践 |
| **Martin Fowler** | Harness Engineering (Exploring Gen AI系列) | 权威技术分析，三大支柱框架，务实评估 |
| **Anthropic工程博客** | Effective Harnesses for Long-Running Agents | Claude长时运行Agent的Harness最佳实践 |
| **LangChain博客** | Improving Deep Agents with Harness Engineering | 最具说服力的定量证据，Middleware模式详解 |
| **HumanLayer博客** | Skill Issue: Harness Engineering for Coding Agents | Claude Code实操指南，Sub-agent/Hooks最佳实践 |

### 2.2 综合分析/指南

| 来源 | 标题 | 核心价值 |
|------|------|----------|
| **NxCode** | Harness Engineering: The Complete Guide (2026) | 最全面的实施指南，三级实施路径 |
| **Phil Schmid** | The Importance of Agent Harness in 2026 | 战略视角，为什么Harness比模型更重要 |
| **InfoQ** | OpenAI Introduces Harness Engineering | 技术新闻视角的深度解读 |
| **Aakash Gupta (Medium)** | 2025 Was Agents. 2026 Is Agent Harnesses. | 产品/商业视角的行业趋势分析 |

### 2.3 开源实现

| 项目 | 描述 | 核心价值 |
|------|------|----------|
| **claude-code-harness** (GitHub: Chachamaru127) | Claude Code专用开发线束 | Plan→Work→Review自治循环，9条安全规则(R01-R09)，多Agent团队模式 |

---

## 三、Harness Engineering 三大支柱

### 3.1 Context Engineering（上下文工程）

确保Agent在执行时能访问到所有必要信息。

**静态组件：**
- 仓库级文档（架构规范、API契约、风格指南）
- `AGENTS.md` / `CLAUDE.md` 文件编码项目特定规则
- 交叉链接的设计文档，由Linter验证

**动态组件：**
- 可观测性数据（日志、指标、Trace）对Agent可访问
- 启动时的目录结构映射
- CI/CD管线状态和测试结果

> **关键原则：从Agent视角看，它在上下文中访问不到的东西就不存在。**

### 3.2 Architectural Constraints（架构约束）

通过机械化规则强制执行代码质量边界，而非仅依赖建议。

**实施方法：**
- 确定性Linter + 自定义规则
- LLM-based审计Agent审查生成的代码
- 结构化测试（ArchUnit风格的依赖强制）
- Pre-commit hooks自动验证

**OpenAI的依赖流规则：** Types → Config → Repo → Service → Runtime → UI

> **关键洞察：约束解空间反而提升Agent生产力——减少在无效探索上浪费的Token。**

### 3.3 Entropy Management（熵管理）

周期性维护Agent维持代码库健康：
- 文档一致性验证
- 约束违规扫描
- 模式强制和偏差纠正
- 依赖审计（循环或不必要的导入）

> OpenAI称之为"垃圾回收Agent"——定期运行以发现不一致性。

---

## 四、关键实践模式

### 4.1 LangChain的Middleware模式

LangChain通过可组合的中间件层处理Agent请求：

```
请求 → LocalContextMiddleware → LoopDetectionMiddleware → ReasoningSandwichMiddleware → PreCompletionChecklistMiddleware → 响应
```

**各中间件职责：**

| 中间件 | 功能 | 效果 |
|--------|------|------|
| **LocalContextMiddleware** | 启动时映射工作目录、发现可用工具 | 减少搜索/规划错误 |
| **LoopDetectionMiddleware** | 跟踪文件编辑次数，识别"死循环" | N次编辑后建议改变方法 |
| **ReasoningSandwichMiddleware** | 规划阶段高推理→实现阶段中推理→验证阶段高推理 | 平衡正确性与超时约束 |
| **PreCompletionChecklistMiddleware** | 在Agent退出前拦截，提醒运行验证 | 强制自我验证 |

### 4.2 Anthropic的长时运行Agent模式

**双Agent架构：**
1. **Initializer Agent**：首次运行时建立基础环境
2. **Coding Agent**：后续会话中处理增量进度

**Session启动流程：**
1. 运行诊断命令确定工作目录
2. 回顾进度日志和git历史
3. 检查功能需求
4. 在新工作前执行基础端到端测试
5. 选择单个最高优先级的未完成功能

**三大连续性文件：**
- **Feature List (JSON)**：200+条目化需求，标记通过/失败
- **Progress File**：Agent动作和决策的时序日志
- **Git Commits**：带描述性消息的历史记录，支持回滚

> **核心洞察：关键在于找到一种方式让Agent在新的上下文窗口启动时快速理解工作状态。**

### 4.3 claude-code-harness 的Plan→Work→Review循环

**五阶段开发流程：**

```
Setup → Plan → Work → Review → Release
```

| 阶段 | 职责 | 要点 |
|------|------|------|
| **Setup** | 初始化项目约定和配置文件 | 确保所有阶段行为一致 |
| **Plan** | 将需求转化为结构化Plans.md | 包含明确的验收标准 |
| **Work** | 可配置并行度的任务实现 | 每个Worker自行实现+自评+报告 |
| **Review** | 多视角评估（安全/性能/质量/无障碍） | 4维度审查 |
| **Release** | 打包为changelog、git tag、GitHub release | 自动化发布 |

**安全护栏（R01-R09）：**
- 阻止`sudo`命令和破坏性路径
- 防止写入`.git/`、`.env`和密钥文件
- 拒绝`git push --force`操作
- 对跳过测试和断言篡改发出警告

### 4.4 自我验证四阶段框架（LangChain）

```
1. Planning & Discovery（规划与发现）
2. Build with Testing Mindset（带测试思维的构建）
3. Verify against Task Specs（根据任务规范验证）
4. Fix based on Feedback（基于反馈修复）
```

> 关键发现：**模型不会自然地验证自己的工作**——必须在Harness层面强制执行。

---

## 五、实施层级

### Level 1: 个人开发者（1-2小时）

- `.cursorrules` 或 `CLAUDE.md` 包含编码约定
- Pre-commit Linting Hooks
- 可运行的测试套件用于自验证
- 一致的目录命名

### Level 2: 小型团队（1-2天）

- `AGENTS.md` 包含团队约定
- CI强制执行的架构约束
- 共享的Prompt模板
- Documentation-as-code验证
- Agent专用的PR Review检查清单

### Level 3: 生产级组织（1-2周）

- 自定义中间件层（循环检测、推理优化）
- 可观测性集成
- 定时运行的熵管理Agent
- Harness版本化和A/B测试
- Agent性能仪表板
- 升级策略

---

## 六、常见错误与反模式

1. **过度工程化控制流** — 模型更新时会崩溃
2. **将Harness视为静态** — 应随模型能力演进
3. **模糊的文档** — 无法作为Agent的Ground Truth
4. **缺少反馈机制** — Agent无法自我纠正
5. **关键知识存在于人类专用格式中** — Agent无法访问（Slack、Google Docs等）
6. **连接过多MCP Server "以防万一"** — 增加噪音
7. **自动生成Agent配置文件** — 应人工精心设计
8. **每次会话结束运行完整测试套件** — 浪费上下文窗口
9. **微观管理子Agent的工具访问** — 增加复杂性

---

## 七、关键量化证据

| 案例 | 指标 | 说明 |
|------|------|------|
| **OpenAI Codex** | 100万行代码 / 5个月 / 0手写行 | 3人团队，3.5 PR/人/天，1500+ PR merged |
| **LangChain** | 52.8% → 66.5%（+13.7pp） | 仅改Harness不换模型，Terminal Bench 2.0 |
| **Claude Code Harness** | 9条安全规则 + 4维度Review | 自治Plan→Work→Review循环 |
| **Anthropic** | 200+条目化Feature List | 长时运行Agent的状态追踪 |

---

## 八、对AI Team OS的具体建议

基于Harness Engineering研究成果，以下是对我们AI Team OS可以直接借鉴的实践：

### 8.1 直接可落地的改进

#### 1) 引入Middleware/Hook层

**现状对应：** AI Team OS已有Hooks系统
**借鉴点：** 参考LangChain的四层中间件架构

```
建议新增的中间件：
- LoopDetectionMiddleware → 检测Agent死循环（对应我们的"3次失败规则"升级版）
- PreCompletionChecklist → Agent完成前强制自验证
- ContextOnboardingMiddleware → 新会话启动时自动加载环境信息
- ReasoningBudgetMiddleware → 不同阶段分配不同推理强度
```

#### 2) 强化Context Engineering

**现状对应：** AI Team OS有CLAUDE.md + 记忆系统
**借鉴点：**

- **Feature List JSON化**：将任务分解为200+可机器读取的条目，每个标记pass/fail
- **Progress File**：每个Agent维护时序日志（我们的task_memo已有此功能，可增强）
- **Directory Structure Mapping**：Agent启动时自动映射项目结构
- **Repository-first Documentation**：所有架构决策必须在仓库中，而非外部文档

#### 3) 建立Entropy Management机制

**现状：** AI Team OS缺少此层
**建议：**

- 创建周期性"维护Agent"角色
- 定期扫描文档一致性
- 检测架构约束违规
- 清理过时的配置和文档

#### 4) 升级质量门禁

**现状对应：** QA Observer角色
**借鉴点：**

- 采用claude-code-harness的**4维度Review**模式（安全/性能/质量/无障碍）
- 实施**自验证强制化**：Agent不能在未验证的情况下声称完成
- 建立**Evidence Pack**机制：可重新运行的验证脚本确认完成

### 8.2 架构层面的借鉴

#### 5) Initializer + Worker分离模式

参考Anthropic的双Agent架构：
- **Initializer Agent**：首次启动时建立环境（安装依赖、配置工具、创建初始结构）
- **Worker Agent**：后续会话中处理增量工作

这与我们的Leader+Member模式天然契合，可以让Leader在会话开始时执行Initializer职责。

#### 6) 约束解空间策略

**核心洞察：** 约束不是限制，而是提升生产力的手段。

建议为AI Team OS的Agent模板中加入：
- 明确的架构边界（哪些文件Agent可以修改，哪些不可以）
- 依赖流规则（模块间的调用方向）
- 命名约定和代码风格的机械化强制（Linter规则）

#### 7) Session Handoff机制

参考muraco.ai的四文档模式：
1. `design.md` — 过程文档
2. `task_checklist.md` — 带验收标准的任务跟踪
3. `session_handoff.md` — 会话间上下文保持
4. `AGENTS.md` — 操作规则和工作流标准

这可以增强我们跨会话的连续性。

### 8.3 战略层面的思考

#### 8) "模型是商品，Harness是护城河"

AI Team OS本质上就是一个**团队级Agent Harness**。这意味着：
- 我们的核心竞争力不在于使用哪个模型，而在于我们的编排系统
- 应投入更多精力在约束系统、反馈循环、文档工程上
- 模型可替换设计（Multi-provider support）是正确方向

#### 9) 从"CI/CD of Code"到"CI/CD of Agents"

Harness Engineering被称为"代码本身的CI/CD"。AI Team OS可以定位为：
- 不仅管理Agent的执行，还管理Agent产出的质量
- 建立Agent行为的可观测性（Trace、日志、指标）
- 实现Agent配置的版本化和A/B测试

#### 10) 渐进式实施路径

按照NxCode的三级路径，AI Team OS可以提供：
- **入门模板**（Level 1）：个人开发者快速上手
- **团队配置**（Level 2）：小型团队协作模式
- **企业级编排**（Level 3）：完整的可观测性+熵管理+升级策略

---

## 九、与AI Team OS现有架构的对比

| Harness Engineering概念 | AI Team OS对应 | 差距/机会 |
|------------------------|---------------|-----------|
| Context Engineering | CLAUDE.md + 记忆系统 | 可增加Feature List JSON化、Directory Mapping |
| Architectural Constraints | Safety Rules + Hooks | 可增加ArchUnit式结构化测试、依赖流规则 |
| Entropy Management | 无 | **需要新建**：周期性维护Agent |
| Middleware Layer | Hooks系统 | 可增加LoopDetection、ReasoningBudget等 |
| Multi-Agent Coordination | Team+Leader+Member模式 | 已领先，可增加Initializer分离 |
| Session Continuity | task_memo系统 | 可增加session_handoff文档模式 |
| Quality Gates | QA Observer | 可升级为4维度Review + Evidence Pack |
| Self-Verification | 2-Action规则 | 可增加PreCompletionChecklist |
| Observability | 基础日志 | 可增加Agent行为Trace、性能仪表板 |
| A/B Testing | 无 | **远期目标**：Harness配置的实验化 |

---

## 十、总结

Harness Engineering 是2026年AI工程领域的范式转移。核心信息是：

1. **竞争力来自基础设施，而非智能** — 模型能力趋于同质化，差异化在于Harness
2. **约束提升生产力** — 减少解空间反而让Agent更高效
3. **验证必须强制化** — 模型不会自发验证，需要Harness层面的机制
4. **文档即基础设施** — Repository-first，Machine-readable
5. **渐进式实施** — 从简单开始，基于观察到的失败迭代增加配置

**AI Team OS已经在做Harness Engineering的很多事情**（团队编排、Safety Rules、Hooks、task_memo），但尚未用这个框架来系统化思考。将Harness Engineering的方法论整合进来，可以让AI Team OS从"团队管理工具"升级为"生产级Agent操作系统"。

---

## 参考来源

1. [OpenAI - Harness Engineering: Leveraging Codex in an Agent-First World](https://openai.com/index/harness-engineering/)
2. [Martin Fowler - Harness Engineering](https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html)
3. [Anthropic - Effective Harnesses for Long-Running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
4. [LangChain - Improving Deep Agents with Harness Engineering](https://blog.langchain.com/improving-deep-agents-with-harness-engineering/)
5. [HumanLayer - Skill Issue: Harness Engineering for Coding Agents](https://www.humanlayer.dev/blog/skill-issue-harness-engineering-for-coding-agents)
6. [NxCode - Harness Engineering: The Complete Guide (2026)](https://www.nxcode.io/resources/news/harness-engineering-complete-guide-ai-agent-codex-2026)
7. [Phil Schmid - The Importance of Agent Harness in 2026](https://www.philschmid.de/agent-harness-2026)
8. [InfoQ - OpenAI Introduces Harness Engineering](https://www.infoq.com/news/2026/02/openai-harness-engineering-codex/)
9. [GitHub - claude-code-harness (Chachamaru127)](https://github.com/Chachamaru127/claude-code-harness)
10. [Muraco.ai - Harness Engineering 101](https://muraco.ai/en/articles/harness-engineering-claude-code-codex/)
11. [Aakash Gupta - 2025 Was Agents, 2026 Is Agent Harnesses](https://aakashgupta.medium.com/2025-was-agents-2026-is-agent-harnesses-heres-why-that-changes-everything-073e9877655e)
12. [Harness.io - AI Blog Category](https://www.harness.io/blog-category/harness-ai)
13. [Cobus Greyling - The Rise of AI Harness Engineering](https://cobusgreyling.medium.com/the-rise-of-ai-harness-engineering-5f5220de393e)
