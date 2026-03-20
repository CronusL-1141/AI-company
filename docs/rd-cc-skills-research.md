# Claude Code 社区 Skill 深度调研报告

> R&D 研究员报告 | 2026-03-20

---

## 目录

1. [Superpowers 框架](#1-superpowers-框架)
2. [Planning with Files](#2-planning-with-files)
3. [NotebookLM Skill](#3-notebooklm-skill)
4. [额外发现的有价值 Skill](#4-额外发现的有价值-skill)
5. [综合整合建议](#5-综合整合建议)

---

## 1. Superpowers 框架

**仓库**: [obra/superpowers](https://github.com/obra/superpowers)
**Stars**: ~101,000 | **Forks**: ~8,000 | **许可**: MIT
**最新版本**: v5.0.5 (2026-03-17)
**技术栈**: Shell (57.8%), JavaScript (30.1%), HTML, Python, TypeScript

### 核心功能

Superpowers 是一个完整的 AI Agent 软件开发方法论框架,通过可组合的 "skills" 强制执行结构化工作流。它不是单一技能,而是一个**技能编排系统**。

**解决的痛点**: AI Agent 在没有约束时容易跳过测试、忽略设计、产出低质量代码。Superpowers 通过强制流程来保证代码质量。

### 13+ 内置技能

| 类别 | 技能 | 说明 |
|------|------|------|
| 测试 | test-driven-development | RED-GREEN-REFACTOR 循环,测试必须先失败 |
| 测试 | verification-before-completion | 完成前强制验证 |
| 调试 | systematic-debugging | 4阶段根因分析 |
| 协作 | brainstorming | 苏格拉底式设计细化,编码前先探讨 |
| 规划 | writing-plans | 将设计分解为2-5分钟的微任务 |
| 执行 | executing-plans | 批量执行+检查点 |
| 并行 | dispatching-parallel-agents | 并发子Agent工作流 |
| 代码审查 | requesting-code-review | 提交前合规检查 |
| 代码审查 | receiving-code-review | 反馈整合 |
| Git | using-git-worktrees | 隔离分支管理 |
| Git | finishing-a-development-branch | 合并/PR工作流 |
| 元 | subagent-driven-development | 两阶段审查(规格+质量) |
| 元 | writing-skills | 创建自定义技能 |

### 实现方式

**强制7阶段流水线**:
```
Brainstorming → Git Worktree → Planning → Subagent Dispatch → TDD → Code Review → Branch Completion
```

- 技能基于 `SKILL.md` 模板,放在 `skills/[name]/SKILL.md`
- 技能根据上下文自动触发,无需手动调用
- 子Agent系统支持并发执行,自主运行数小时不偏离计划
- 通过 hooks 机制实现 pre-tool/post-tool 自动化

### 用户评价

- 101K stars 说明极高的社区认可度
- 活跃 Discord 社区
- 被多个 "awesome" 列表推荐为首选框架
- 支持 Claude Code、Cursor、Codex、OpenCode、Gemini CLI 多平台

### 与 AI Team OS 的关系分析

| 维度 | 分析 |
|------|------|
| **核心功能** | 完整的Agent开发方法论框架,强制TDD/调试/代码审查流程 |
| **实现方式** | SKILL.md模板 + hooks + 子Agent分发 + Git worktree隔离 |
| **用户评价** | 极高(101K stars),社区最受欢迎的CC框架 |
| **与OS的关系** | **高度互补**。OS负责团队编排/任务分配,Superpowers负责单个Agent的开发质量保证。两者解决不同层面问题 |
| **整合方案** | 作为"推荐开发技能包"植入OS。当团队成员执行编码任务时,自动加载Superpowers的TDD/调试/审查技能 |
| **优化空间** | 我们可以基于其skill框架扩展团队协作技能(如跨Agent代码审查、集成测试协调) |
| **优先级** | **高** — 直接提升Agent编码质量,与OS的团队管理形成上下互补 |

### 关键洞察

Superpowers 的 `dispatching-parallel-agents` 和 `subagent-driven-development` 与我们的团队编排在概念上类似,但它工作在**单Agent内部的子任务级别**,而OS工作在**多Agent协作级别**。两者不冲突,反而形成层次化的质量保证:
- OS层: 谁做什么任务,如何协调
- Superpowers层: 每个Agent怎么高质量地完成任务

---

## 2. Planning with Files

**仓库**: [OthmanAdi/planning-with-files](https://github.com/OthmanAdi/planning-with-files)
**Stars**: ~16,600 | **许可**: MIT
**灵感来源**: Manus AI (Meta $2B 收购) 的工作流模式

### 核心功能

将文件系统作为 Agent 的"外部工作记忆",通过3个持久化 Markdown 文件实现跨会话的上下文保持。

**解决的痛点**: Agent 的上下文窗口有限(类比RAM),长任务中容易丢失状态。该技能用文件系统(类比磁盘)弥补这一缺陷。

### 三文件模式

| 文件 | 用途 | 类比 |
|------|------|------|
| `task_plan.md` | 阶段跟踪、进度、决策记录 | 项目计划 |
| `findings.md` | 研究发现、外部内容存储 | 研究笔记 |
| `progress.md` | 会话日志、测试结果 | 工作日志 |

### 关键规则

1. **先建计划**: 复杂任务必须先创建 task_plan.md
2. **2-Action规则**: 每2次浏览/搜索操作后,立即将发现保存到文件,防止多模态信息丢失
3. **决策前回读**: 重大决策前重新读取计划,保持目标聚焦
4. **行动后更新**: 每个阶段完成后标记状态 (pending → in_progress → complete)
5. **记录所有错误**: 每个错误写入计划文件,构建知识库防止重复
6. **3次失败协议**: 3次失败后必须升级给用户

### 安全边界设计

外部内容只写入 `findings.md` — `task_plan.md` 因为频繁被读取,是高价值的注入攻击目标。

### 实现方式

- 纯 SKILL.md 指令驱动,无需外部依赖
- Hook集成: PreToolUse 时读取计划,PostToolUse 时更新状态
- 支持 40+ Agent 框架 (Claude Code, Cursor, Copilot, Gemini CLI 等)
- 包含会话恢复脚本 (session-catchup)

### 基准测试结果

- 96.7% 通过率 (30个可客观验证的断言)
- 3/3 盲测 A/B 胜出 (100%)
- 平均评分 10.0/10 vs 无技能时 6.8/10

### 与 AI Team OS 的关系分析

| 维度 | 分析 |
|------|------|
| **核心功能** | 文件系统作为Agent外部记忆,3文件持久化规划 |
| **实现方式** | SKILL.md指令 + Hook触发 + Markdown文件模板 |
| **用户评价** | 高(16.6K stars),被誉为"整个CC生态最高星标技能" |
| **与OS的关系** | **中度重叠+高度互补**。OS已有task_memo系统和任务管理,但planning-with-files的"2-Action规则"和"3次失败协议"等具体执行策略是OS缺少的 |
| **整合方案** | **选择性吸收**而非直接植入。将其最佳实践(2-Action规则、错误累积记录、安全边界)融入OS的Agent执行规范 |
| **优化空间** | 我们的task_memo + 任务墙已经比3文件模式更强大。可以吸收其精华规则来增强现有系统 |
| **优先级** | **中** — 不需要直接引入(功能重叠),但其规则和最佳实践值得学习吸收 |

### 关键洞察

Planning with Files 的核心思想 "Context Window = RAM, Filesystem = Disk" 与我们 OS 的 task_memo 系统不谋而合。但它的实现更简单(3个文件 vs 我们的结构化MCP工具)。值得关注的差异点:

- **2-Action规则**: 我们的Agent目前没有这种强制性的"每N次操作必须持久化"机制,这是个好主意
- **安全边界**: 区分"可注入文件"和"安全文件"的设计思路值得OS层面采纳
- **3次失败升级**: 与我们的Watchdog功能互补,可以在Agent级别实现自动升级

---

## 3. NotebookLM Skill

**仓库**: [PleasePrompto/notebooklm-skill](https://github.com/PleasePrompto/notebooklm-skill)
**Stars**: ~4,700 | **Forks**: ~491 | **许可**: MIT
**相关项目**: [notebooklm-mcp](https://github.com/PleasePrompto/notebooklm-mcp) (MCP Server版本)

### 核心功能

让 Claude Code 直接查询 Google NotebookLM,获取基于文档源的、带引用的回答。利用 Gemini 的综合能力,答案仅来自用户上传的文档,最大限度减少幻觉。

**解决的痛点**: 开发时需要查阅大量文档(API文档、设计文档、技术规格),手动在NotebookLM和编辑器之间切换效率低下。

### 工作流程

```
认证检查 → 笔记本管理 → 查询执行 → 引用返回 → 后续追问
```

### 技术实现

| 组件 | 技术 |
|------|------|
| 浏览器自动化 | Patchright (Playwright-based) |
| 语言 | Python |
| 浏览器 | 真实Chrome (非Chromium) |
| 反检测 | 隐身技术+真实打字速度 |
| 环境 | 隔离Python venv |

### 可用命令

- **认证**: setup, status, reauth, clear
- **笔记本管理**: add, list, search, activate, remove, stats
- **查询**: ask_question.py (支持 --notebook-id, --notebook-url, --show-browser)
- **清理**: cleanup_manager.py

### 限制

- **仅本地Claude Code** — Web UI沙箱内无法运行
- **无会话持久化** — 每次查询独立
- **速率限制** — 免费账户50次/天
- **需手动上传** — 文档须预先上传到NotebookLM

### 与 AI Team OS 的关系分析

| 维度 | 分析 |
|------|------|
| **核心功能** | 连接Claude与NotebookLM,获取文档源引用的答案 |
| **实现方式** | Python脚本 + Chrome浏览器自动化 + SKILL.md指令 |
| **用户评价** | 中等(4.7K stars),细分领域有忠实用户 |
| **与OS的关系** | **低重叠,特定场景互补**。OS没有外部知识库集成功能,这提供了一种连接外部文档的途径 |
| **整合方案** | 作为**可选插件**推荐,不建议深度整合。原因: 依赖Google服务+浏览器自动化,稳定性和维护成本较高 |
| **优化空间** | 如果需要文档查询功能,更好的方案是构建本地RAG系统或对接更稳定的API(如直接用向量数据库) |
| **优先级** | **低** — 依赖外部服务,浏览器自动化不够稳定,且有更好的替代方案(本地RAG/向量记忆) |

### 关键洞察

NotebookLM Skill 的核心价值在于"基于源文档的零幻觉回答"。但其实现方式(浏览器自动化)过于脆弱。对于OS来说:
- **概念可取**: 让Agent查询外部知识库并带引用回答
- **实现不可取**: 浏览器自动化 + Google账号依赖太重
- **更好的路径**: 我们的记忆系统研究(Mem0/向量数据库)已经覆盖了类似需求,而且更稳定、更可控

---

## 4. 额外发现的有价值 Skill

通过搜索 "awesome claude code skills" 和社区推荐列表,发现以下值得关注的技能:

### 4.1 高价值发现

| Skill | Stars | 说明 | 与OS关系 |
|-------|-------|------|----------|
| **[frontend-design](https://github.com/anthropics/claude-skills)** (Anthropic官方) | 277K+周安装 | 给Claude设计系统和哲学,避免"AI通用美学" | 可作为前端任务的推荐技能 |
| **[VibeSec-Skill](https://github.com/travisvn/awesome-claude-skills)** | — | 帮Claude写安全代码,防止常见漏洞 | OS安全规则的技能层补充 |
| **[/simplify](https://github.com/travisvn/awesome-claude-skills)** | — | 生成3个并行审查Agent检查代码质量 | 多Agent审查模式参考 |
| **[Antigravity Awesome Skills](https://github.com/sickn33/antigravity-awesome-skills)** | 22K+ | 1272+战斗测试技能库,跨平台兼容 | 可作为OS的技能来源目录 |
| **[claude-code-toolkit](https://github.com/rohitg00/awesome-claude-code-toolkit)** | — | 135 agents, 35 skills, 42 commands, 150+ plugins | 全面的参考资源 |

### 4.2 生态趋势观察

1. **技能市场爆发**: 从0到28万+技能条目,不到6个月
2. **跨平台兼容**: 技能不再限于CC,SKILL.md标准被Cursor/Codex/Gemini CLI等广泛采用
3. **官方市场化**: Anthropic推出了官方Plugin Marketplace (`/plugin install`)
4. **SkillKit生态**: 400,000+技能通过SkillKit分发
5. **专业化细分**: 出现了针对安全(Shannon)、前端(frontend-design)、法务(compliance skills)等垂直领域的专业技能

---

## 5. 综合整合建议

### 优先级矩阵

| Skill | 整合优先级 | 整合方式 | 预期价值 |
|-------|-----------|---------|---------|
| **Superpowers** | **高** | 推荐技能包 + 选择性内置 | 显著提升Agent编码质量 |
| **Planning with Files** | **中** | 吸收最佳实践到现有系统 | 强化执行纪律性 |
| **NotebookLM** | **低** | 可选推荐,不深度整合 | 特定场景有用 |
| **frontend-design** | **中** | 前端任务推荐技能 | 提升UI产出质量 |
| **VibeSec** | **中** | 安全规则补充 | 提升代码安全性 |

### 具体整合建议

#### 短期行动 (可立即实施)

1. **吸收 Planning with Files 的执行规则到 OS Agent 规范**:
   - 2-Action规则: Agent每N次操作必须持久化关键发现到task_memo
   - 3次失败升级协议: Agent连续3次失败同一操作后自动升级到Leader
   - 安全边界: 区分可写入外部内容的文件和受保护的系统文件

2. **在 CLAUDE.md / Agent System Prompt 中推荐 Superpowers**:
   - 编码任务默认加载TDD和systematic-debugging技能
   - 代码审查任务加载requesting-code-review技能

#### 中期行动 (需要设计)

3. **构建 OS 技能推荐系统**:
   - 根据任务类型(编码/测试/设计/安全)自动推荐相关社区技能
   - 维护一个"推荐技能清单",类似OS生态指导体系的理念
   - 利用 SKILL.md 标准实现技能的即插即用

4. **从 Superpowers 学习子Agent编排**:
   - 其 `dispatching-parallel-agents` 的两阶段审查模式可参考
   - 可用于增强OS的QA-Observer功能

#### 长期考量

5. **参与SKILL.md生态标准**:
   - SKILL.md已成为事实标准,OS应该兼容这个标准
   - 考虑将OS的某些能力(如任务管理、团队协调)以SKILL.md格式发布
   - 这样OS既能消费社区技能,也能向社区贡献

### 核心结论

> AI Team OS 与社区 Skill 生态的关系应该是**编排层 vs 执行层**的互补关系:
> - **OS 负责**: 谁来做、做什么、何时做、如何协调 (团队层)
> - **Skills 负责**: 怎么做得好 (个体技能层)
>
> 我们不需要重造这些技能,而是建立一个**技能推荐/加载机制**,让合适的技能在合适的时机被合适的Agent使用。这完全符合"OS是生态指导体系,推荐/包含优秀第三方"的理念。

---

*报告完成 — R&D Researcher, 2026-03-20*
