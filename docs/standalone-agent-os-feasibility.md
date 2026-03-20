# 独立Agent OS可行性研究报告

**状态**: 研究完成 — 战略参考文档
**日期**: 2026-03-20
**作者**: org-architect (AI Team OS架构师)

---

## 1. 研究背景与目标

当前AI Team OS作为Claude Code(CC)的增强层运行，通过MCP工具协议+Hook系统+Agent模板实现团队协作管理。这种架构虽然快速落地，但受CC平台多项限制：

- 子agent不继承hooks（PreToolUse/PostToolUse对子agent无效）
- 一个session只能运行一个team
- 不能嵌套agent（agent不能再创建agent）
- Agent模板发现依赖 `~/.claude/agents/` 文件系统
- 上下文窗口管理完全由CC控制，OS无法干预

本研究评估：**AI Team OS脱离CC框架，直接接入LLM API构建完全自主Agent操作系统的可行性、成本和迁移路径。**

---

## 2. 当前CC依赖全景分析

通过对项目代码的逐文件审计，识别出以下6个维度的CC依赖：

### 2.1 MCP工具协议（耦合度：高）

| 依赖项 | 代码位置 | 说明 |
|--------|----------|------|
| FastMCP Server | `src/aiteam/mcp/server.py` | 28个MCP tools通过stdio与CC通信 |
| MCP stdio传输 | `mcp.run()` 入口 | CC以stdio模式调用MCP Server |
| 工具调用链 | MCP → HTTP → FastAPI | 所有工具调用经过MCP→API两跳 |

**影响**: MCP是CC与OS之间的唯一通信桥梁。MCP Server本质是一个HTTP代理层——所有28个tool最终都调用 `_api_call()` 转发到FastAPI。**这意味着FastAPI API层是完全独立的**，MCP层只是适配器。

### 2.2 Hook系统（耦合度：高）

| Hook脚本 | CC事件 | 功能 |
|----------|--------|------|
| `session_bootstrap.py` | SessionStart | 注入Leader简报、规则、Agent模板列表 |
| `send_event.py` | 所有7种事件 | 事件转发到OS API (`/api/hooks/event`) |
| `inject_context.py` | SubagentStart | 注入CLAUDE.md和OS注册指引 |
| `inject_subagent_context.py` | SubagentStart | 注入子Agent环境规则（2-Action、汇报格式等） |
| `workflow_reminder.py` | Pre/PostToolUse | 工作流提醒+安全护栏（14条规则） |
| `context_monitor.py` | UserPromptSubmit | 上下文使用率告警 |
| `statusline.py` | StatusLine | 状态栏显示+上下文监控JSON写入 |
| `pre_compact_save.py` | PreCompact | 上下文压缩事件记录 |

**影响**: Hook系统承担三大职责：
1. **事件桥接**: 将CC生命周期事件同步到OS（agent注册/注销、工具使用追踪）
2. **行为注入**: 向agent注入规则、上下文、提醒
3. **安全护栏**: 拦截危险命令、检测敏感信息

### 2.3 CC原生工具（耦合度：极高）

| CC工具 | 使用场景 | 替代难度 |
|--------|----------|----------|
| `Agent(team_name=...)` | 创建子agent | 极高——需自建Agent运行时 |
| `TeamCreate` | 创建CC团队 | 极高——需自建团队管理 |
| `SendMessage` | agent间通信 | 高——需自建消息总线 |
| `Read/Edit/Write` | 文件操作 | 中——可用Python标准库替代 |
| `Bash` | 终端执行 | 中——可用subprocess替代 |
| `Glob/Grep` | 文件搜索 | 低——可用pathlib/ripgrep替代 |
| `WebSearch/WebFetch` | 网络访问 | 低——可用requests/httpx替代 |

**影响**: `Agent`、`TeamCreate`、`SendMessage` 是最核心的依赖。它们构成了CC的多Agent协作基座，替代它们意味着**从零构建Agent运行时和通信系统**。

### 2.4 Agent模板发现机制（耦合度：低）

- 模板存储于 `~/.claude/agents/*.md`
- `session_bootstrap.py` 在启动时扫描并列出可用模板
- OS API `/api/agent-templates` 也独立提供模板列表

**影响**: 低耦合。模板本质是markdown文件，可以用任何方式加载。

### 2.5 CC上下文窗口管理（耦合度：中）

- `statusline.py` 读取CC暴露的上下文使用率
- `context_monitor.py` 基于使用率发出告警
- `pre_compact_save.py` 记录压缩事件
- 2-Action规则本质上是对CC上下文压缩的防御

**影响**: 独立运行时需要自行管理token计数和上下文窗口策略。

### 2.6 HookTranslator桥接层（耦合度：高）

`src/aiteam/api/hook_translator.py`（1183行）是最核心的桥接组件：
- 将7种CC hook事件翻译为OS操作
- 处理agent自动注册/注销/状态同步
- 实现CC团队→OS团队的映射
- 文件编辑冲突检测
- Leader查找+self-heal
- 意图事件发射+活动追踪

**影响**: HookTranslator的逻辑在独立运行时中会**大幅简化**——因为Agent的生命周期将由OS直接管理，无需通过hook间接观测。

---

## 3. 可复用资产评估

### 3.1 完全可复用（无需修改）

| 组件 | 代码量 | 说明 |
|------|--------|------|
| FastAPI后端 | ~4000行 | 所有API路由、schemas、deps |
| SQLite存储层 | ~2000行 | repository.py + models.py + connection.py |
| React Dashboard | 整个前端 | Vite + React 19 |
| LoopEngine | ~300行 | 纯状态机，零外部依赖 |
| Watchdog | ~200行 | StateReaper定时任务 |
| FailureAlchemy | ~200行 | 失败分析逻辑 |
| EventBus | ~150行 | 内部事件总线 |
| Memory系统 | ~500行 | SQLite+Mem0后端 |
| 类型定义 | types.py | 所有数据模型 |

**复用率**: 约70%的后端代码可直接复用。

### 3.2 需要适配（修改接口层）

| 组件 | 改动 | 说明 |
|------|------|------|
| MCP Server | 替换为直接函数调用 | 去掉MCP协议层，28个tool变成普通Python函数 |
| Hook脚本 | 替换为内置中间件 | 行为注入→prompt模板；安全护栏→工具执行前置检查 |

### 3.3 需要重新构建

| 组件 | 工作量 | 说明 |
|------|--------|------|
| Agent运行时 | 大 | 替代CC的Agent/TeamCreate/SendMessage |
| LLM抽象层 | 中 | 统一Claude/GPT/Gemini API调用 |
| 工具执行层 | 中 | 文件操作+终端执行+Git操作 |
| 上下文管理器 | 中 | token计数+窗口策略+压缩 |

---

## 4. 独立Agent OS架构设计草案

### 4.1 总体架构

```
┌─────────────────────────────────────────────────────┐
│                    Dashboard (React)                  │
│                  WebSocket + REST API                 │
├─────────────────────────────────────────────────────┤
│                   API Layer (FastAPI)                 │
│    已有的28个端点 + 新增Agent控制端点               │
├──────────┬──────────────┬───────────────────────────┤
│  Agent   │   Task       │   Communication            │
│  Runtime │   Engine     │   Bus                      │
│          │  (LoopEngine)│   (EventBus+MessageQueue)  │
├──────────┴──────────────┴───────────────────────────┤
│              Orchestration Layer                      │
│  ┌────────────────────────────────────────────────┐ │
│  │  AgentSupervisor — 管理Agent生命周期            │ │
│  │  • 创建/销毁Agent goroutine/thread              │ │
│  │  • 分配LLM会话                                  │ │
│  │  • 注入system prompt + 工具集                   │ │
│  │  • 监控token使用 + 自动compact                  │ │
│  └────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────┤
│              LLM Abstraction Layer                    │
│  ┌──────────┬──────────┬──────────┬───────────────┐ │
│  │ Claude   │ Claude   │  GPT-4o  │   Gemini      │ │
│  │ Agent SDK│ Messages │  API     │   API         │ │
│  │ (首选)   │ API      │          │               │ │
│  └──────────┴──────────┴──────────┴───────────────┘ │
├─────────────────────────────────────────────────────┤
│              Tool Execution Layer                     │
│  ┌──────────┬──────────┬──────────┬───────────────┐ │
│  │ File Ops │ Terminal │   Git    │  Web/Search   │ │
│  │ (aiofiles│ (asyncio │ (pygit2/ │  (httpx/      │ │
│  │  pathlib)│  subprocess)│ gitpython)│  SerpAPI)  │ │
│  └──────────┴──────────┴──────────┴───────────────┘ │
├─────────────────────────────────────────────────────┤
│              Storage Layer (已有)                     │
│  SQLite + SQLAlchemy | MemoryStore | EventStore      │
└─────────────────────────────────────────────────────┘
```

### 4.2 核心新组件：Agent Runtime

Agent Runtime是独立OS的核心，替代CC的 `Agent()` + `TeamCreate` + `SendMessage`：

```python
# 概念设计 — 非最终实现
class AgentRuntime:
    """管理所有Agent的生命周期和LLM交互循环."""

    async def create_agent(
        self,
        name: str,
        role: str,
        system_prompt: str,
        tools: list[Tool],
        model: str = "claude-opus-4-6",
    ) -> AgentHandle:
        """创建Agent并启动其agentic loop."""
        # 1. 初始化LLM会话（Messages API / Agent SDK）
        # 2. 注入system_prompt + tools定义
        # 3. 启动asyncio task运行agent loop
        # 4. 注册到AgentSupervisor
        ...

    async def send_task(
        self, agent: AgentHandle, task: str
    ) -> AsyncIterator[AgentEvent]:
        """向Agent发送任务，流式返回执行事件."""
        # 1. 将task作为user message发送到LLM
        # 2. 进入tool-use循环
        # 3. 每个tool调用 → yield AgentEvent
        # 4. 最终响应 → yield AgentEvent(type=COMPLETE)
        ...

    async def send_message(
        self, from_agent: str, to_agent: str, content: str
    ) -> None:
        """Agent间消息传递."""
        # 通过内部消息队列实现
        ...
```

### 4.3 两种实现路径对比

#### 路径A: 基于Claude Agent SDK

Anthropic已发布 `claude-agent-sdk`（Python/TypeScript），提供与Claude Code相同的agent loop、工具系统和上下文管理。

| 优势 | 劣势 |
|------|------|
| 官方维护，与Claude深度集成 | 锁定Anthropic生态 |
| 内置subagent支持和并发 | 多模型支持有限 |
| 自定义工具通过in-process MCP实现 | 相对新，生态较小 |
| 上下文管理开箱即用 | 可能有未公开的限制 |

#### 路径B: 基于原生Messages API自建

直接使用Anthropic Messages API的tool_use功能，自行实现agent loop。

| 优势 | 劣势 |
|------|------|
| 完全控制所有行为 | 开发工作量大 |
| 可原生支持多LLM供应商 | 需自行实现上下文管理 |
| 无平台限制 | 需自行处理流式、重试、错误恢复 |
| 可深度定制tool执行策略 | Agent loop的质量决定整体表现 |

#### 路径C: 混合方案（推荐）

- **Agent Loop**: 使用Claude Agent SDK作为主runtime（利用其成熟的agent loop）
- **多模型**: 通过LLM Abstraction Layer支持非Claude模型（用于cost-sensitive任务）
- **编排**: 自建AgentSupervisor管理多Agent并发和通信
- **工具**: 自建Tool Execution Layer（文件/终端/Git/网络）

---

## 5. 迁移路径设计

### Phase 1: 当前状态 — CC增强层（已完成）

```
用户 ↔ CC(Leader) ↔ MCP ↔ FastAPI ↔ SQLite
                   ↔ Hooks ↗
```

### Phase 2: 双模式运行（预计4-6周）

**目标**: OS可以同时在CC模式和独立模式下运行。

**关键改动**:
1. 将28个MCP tools抽取为纯Python函数模块 `src/aiteam/tools/`
2. 实现轻量AgentRuntime（单Agent+工具循环），可通过CLI启动
3. MCP Server变为thin adapter，调用同一套函数
4. 添加 `aiteam run --standalone` 命令入口

```
模式A（CC）: 用户 ↔ CC ↔ MCP ↔ tools模块 ↔ FastAPI ↔ SQLite
模式B（独立）: 用户 ↔ CLI ↔ AgentRuntime ↔ tools模块 ↔ FastAPI ↔ SQLite
```

**交付物**:
- `src/aiteam/runtime/` — AgentRuntime核心
- `src/aiteam/runtime/llm_client.py` — LLM API封装
- `src/aiteam/runtime/tool_executor.py` — 工具执行器
- `src/aiteam/tools/` — 从MCP server.py提取的纯函数
- CLI新命令 `aiteam run`

### Phase 3: 完整独立运行（预计8-12周）

**目标**: 完全脱离CC，支持多Agent并发协作。

**关键改动**:
1. 实现AgentSupervisor（多Agent管理、生命周期、通信总线）
2. 实现File/Terminal/Git工具层
3. 实现上下文窗口管理器（token计数+自动compact）
4. Hook逻辑内化为中间件（行为注入→prompt模板，安全护栏→工具前置检查）
5. Dashboard集成独立模式的WebSocket事件流

```
用户 ↔ Dashboard/CLI ↔ AgentSupervisor
                        ├─ Agent1 ↔ Claude API ↔ Tools
                        ├─ Agent2 ↔ Claude API ↔ Tools
                        └─ Agent3 ↔ GPT-4o API ↔ Tools
                        ↕
                      FastAPI ↔ SQLite
```

**交付物**:
- `src/aiteam/runtime/supervisor.py` — 多Agent管理
- `src/aiteam/runtime/context_manager.py` — 上下文窗口管理
- `src/aiteam/runtime/message_bus.py` — Agent间通信
- `src/aiteam/runtime/tools/` — 文件/终端/Git/网络工具实现
- `src/aiteam/runtime/middleware/` — 安全护栏+行为注入

### Phase 4: 多模型支持（预计4-6周）

**目标**: 支持Claude/GPT/Gemini等多种LLM，按任务类型自动选择。

**关键改动**:
1. LLM Abstraction Layer统一接口
2. 模型路由策略（cost-aware、capability-aware）
3. 工具定义格式适配（Claude tool_use vs OpenAI function_calling）

---

## 6. 成本分析

### 6.1 CC订阅 vs 直接API调用

| 项目 | CC Pro订阅 | CC Max订阅 | 直接API调用 |
|------|-----------|-----------|-------------|
| 月费 | $20/月 | $100-200/月 | 按量付费 |
| 模型 | 受限制 | Opus可用 | 全部模型 |
| 并发 | 1 session | 有限 | 无限制 |
| Agent数 | 受CC限制 | 受CC限制 | 自主控制 |
| 上下文 | CC管理 | CC管理 | 自主管理 |

### 6.2 API成本估算

基于当前使用模式（假设每天8小时工作）：

| 场景 | 模型配置 | 日均token | 日成本 | 月成本 |
|------|----------|-----------|--------|--------|
| 轻量（1 Leader） | Opus | ~2M input + 500K output | ~$22.5 | ~$500 |
| 中等（Leader + 3 Agent） | Opus Leader + Sonnet Workers | ~5M input + 1.5M output | ~$40 | ~$880 |
| 重度（Leader + 5 Agent并发） | 混合 | ~10M input + 3M output | ~$80 | ~$1,760 |

**成本优化策略**:
- **Prompt Caching**: 缓存命中仅10%价格，system prompt和工具定义可持久缓存
- **Batch API**: 非实时任务用Batch API享50%折扣
- **模型分级**: Leader用Opus决策，Worker用Sonnet/Haiku执行
- **Haiku路由**: 简单任务（文件读取、格式化）路由到Haiku（$1/$5 per M）

**优化后估算**: 中等场景月成本可从$880降至$400-500。

### 6.3 开发成本

| Phase | 工作量 | 关键风险 |
|-------|--------|----------|
| Phase 2（双模式） | 4-6周 | Agent loop质量、工具执行可靠性 |
| Phase 3（完整独立） | 8-12周 | 多Agent并发稳定性、上下文管理 |
| Phase 4（多模型） | 4-6周 | 工具定义格式差异、模型行为差异 |
| **总计** | **16-24周** | — |

---

## 7. 关键技术决策点

### ADR-DRAFT-001: Agent Loop实现选择

**上下文**: 独立OS需要一个稳定的Agent Loop来驱动每个Agent的LLM交互循环。

**方案对比**:

| 因素 | Claude Agent SDK | 自建(Messages API) | LangGraph |
|------|-----------------|--------------------|-----------|
| 开发速度 | 快（2周） | 慢（6周） | 中（4周） |
| 控制力 | 中 | 极高 | 高 |
| 多模型 | 有限 | 原生 | 原生 |
| 维护成本 | 低（官方维护） | 高（全部自维护） | 中 |
| 成熟度 | 新（2026.1发布） | 取决于实现 | 高（34.5M下载） |
| Agent间通信 | subagent内置 | 需自建 | 需自建 |

**初步推荐**: Phase 2使用Claude Agent SDK快速验证，Phase 3评估是否需要切换到自建或LangGraph。

### ADR-DRAFT-002: 工具执行安全模型

**上下文**: CC提供沙箱执行环境，独立OS需要自行实现安全边界。

**方案**:
- A. Docker容器隔离（每个Agent一个容器）— 安全性最高，开销最大
- B. 进程级隔离（subprocess + seccomp）— 平衡安全和性能
- C. 应用层检查（当前workflow_reminder.py的方式）— 最轻量，安全性依赖规则完备性

**初步推荐**: Phase 2用方案C（快速验证），Phase 3升级到方案B。

### ADR-DRAFT-003: 上下文窗口管理策略

**上下文**: CC自动管理上下文压缩，独立OS需要自行决定何时/如何压缩。

**方案**:
- A. 固定阈值压缩（80%时触发摘要压缩）
- B. 滑动窗口（只保留最近N轮对话+摘要）
- C. 分层记忆（工作记忆+短期记忆+长期记忆自动迁移）

**初步推荐**: Phase 2用方案A（简单可靠），Phase 3演进到方案C。

---

## 8. 风险评估

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| Agent Loop质量不如CC | Agent执行效果下降 | 中 | 使用Claude Agent SDK而非自建 |
| 多Agent并发状态管理复杂 | 死锁、竞态、资源泄漏 | 高 | 渐进式实现，从单Agent开始 |
| API成本超预期 | 月费过高 | 中 | 实现模型分级+缓存+Batch |
| 工具执行安全漏洞 | 文件系统/进程被滥用 | 中 | 白名单+沙箱+审计日志 |
| Anthropic API变更 | 需要跟进适配 | 低 | LLM抽象层隔离变更影响 |
| 开发周期拉长 | 延误其他功能开发 | 高 | Phase 2先验证核心假设 |

---

## 9. 结论与建议

### 9.1 可行性判定：**可行，但应渐进推进**

独立Agent OS在技术上完全可行，核心理由：
1. **70%代码可复用** — FastAPI后端、存储层、LoopEngine、Dashboard等核心组件与CC无耦合
2. **Claude Agent SDK降低了门槛** — 2026年初发布的官方SDK提供了成熟的agent loop和工具系统
3. **MCP层只是适配器** — 所有28个MCP tools本质是HTTP代理，底层API完全独立
4. **行业趋势支持** — 2026年多个成熟的agent framework可供参考（LangGraph 34.5M下载、OpenAI Agents SDK 19K star）

### 9.2 推荐策略

**短期（1-3个月）**: 维持CC增强层模式，但开始Phase 2的准备工作：
- 将MCP tools重构为纯Python函数模块（解耦第一步）
- 评估Claude Agent SDK的实际能力和限制
- 建立LLM调用成本监控基线

**中期（3-6个月）**: 实施Phase 2双模式运行：
- 可在CC中运行（已有用户体验不变）
- 也可通过CLI独立运行（验证核心假设）
- 用实际数据验证API成本模型

**长期（6-12个月）**: 根据Phase 2验证结果决定是否推进Phase 3/4：
- 如果Agent SDK+独立模式表现良好 → 全力推进独立化
- 如果CC体验仍有明显优势 → 维持双模式，逐步增加独立模式能力

### 9.3 不建议的做法

- **不要一步到位跳到Phase 3** — 多Agent并发运行时是最大的技术风险，必须先用Phase 2验证单Agent Loop
- **不要放弃CC模式** — 双模式兼容可以降低迁移风险，用户可以自由选择
- **不要过早引入多模型** — 先用单一LLM（Claude）把Agent Runtime做稳定，再扩展到多模型

---

## 10. 参考资料

- [Claude Agent SDK文档](https://platform.claude.com/docs/en/agent-sdk/overview)
- [Anthropic Agent Loop原理](https://platform.claude.com/docs/en/agent-sdk/agent-loop)
- [Claude API Tool Use实现](https://platform.claude.com/docs/en/agents-and-tools/tool-use/implement-tool-use)
- [Anthropic高级工具使用](https://www.anthropic.com/engineering/advanced-tool-use)
- [Claude Agent SDK构建指南](https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk)
- [LangGraph + Claude Agent SDK多Agent指南](https://www.mager.co/blog/2026-03-07-langgraph-claude-agent-sdk-ultimate-guide/)
- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
- [2026年最佳AI Agent框架对比](https://www.firecrawl.dev/blog/best-open-source-agent-frameworks)
- [从LLM API构建Agent](https://muneebsa.medium.com/want-to-build-ai-agents-start-by-calling-an-llm-api-yourself-d6a9fb02fdf0)
- [Claude API定价](https://platform.claude.com/docs/en/about-claude/pricing)
- [Claude Code LLM流量追踪](https://medium.com/@georgesung/tracing-claude-codes-llm-traffic-agentic-loop-sub-agents-tool-use-prompts-7796941806f5)

---

## 附录A: CC依赖点完整清单

```
依赖维度           具体依赖项                        替代方案
─────────────────────────────────────────────────────────────
MCP协议            FastMCP stdio通信                 直接函数调用
                   28个MCP tool定义                  Python函数模块
                   工具描述/参数schema               tool定义JSON

Hook系统           SessionStart注入                  程序化初始化
                   SubagentStart注入                 Agent构造时注入
                   PreToolUse拦截                    工具执行前中间件
                   PostToolUse追踪                   工具执行后中间件
                   StatusLine监控                    内置监控指标
                   PreCompact记录                    内置compact策略
                   UserPromptSubmit告警              内置上下文管理

CC原生工具         Agent()创建子agent                AgentRuntime.create_agent()
                   TeamCreate创建团队                AgentSupervisor.create_team()
                   SendMessage agent通信             MessageBus.send()
                   Read/Edit/Write文件操作           aiofiles + pathlib
                   Bash终端执行                      asyncio.subprocess
                   Glob/Grep搜索                    pathlib.glob + ripgrep

模板发现           ~/.claude/agents/*.md             内置模板注册表
                   agent_template_list API           保持不变

上下文管理         CC自动compact                     自建ContextManager
                   context_window usage              手动token计数
                   /compact命令                      自动compact策略

会话管理           session_id追踪                    自建session管理
                   CC团队→OS团队映射                 直接团队管理
                   Leader自动发现                    显式Leader注册
```

## 附录B: Phase 2最小可行产品(MVP)范围

Phase 2的目标是验证"单Agent独立运行"的核心假设，MVP范围：

1. **AgentRuntime MVP**: 使用Claude Agent SDK创建单个Agent，执行工具循环
2. **工具集MVP**: File Read/Write/Edit + Bash执行 + 内部OS API调用
3. **CLI入口**: `aiteam run --task "描述" --model claude-sonnet-4-6`
4. **tools模块提取**: 从server.py提取纯Python函数到 `src/aiteam/tools/`
5. **验证指标**:
   - Agent能否独立完成一个简单的开发任务（如修复一个bug）
   - 工具执行可靠性 > 95%
   - 上下文管理是否足够（单任务不超出窗口）
   - API成本是否在预期范围内
