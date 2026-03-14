# AI Team OS

**技术栈**: Python 3.12 + LangGraph + FastAPI | React 19 + Vite 7 | PostgreSQL + Redis
**架构**: 五层 Storage → Memory → Orchestrator → CLI+API → Dashboard（详见 `docs/architecture.md`）
**当前阶段**: 公司循环系统实施 — LoopEngine + 任务墙 + Watchdog + 回顾机制

## 核心约束
- 所有输出使用中文
- 共享类型只引用 `src/aiteam/types.py`，不自行定义
- 不自建 LLM Provider 抽象层，直接使用 LangChain ChatModel
- 代码风格: PEP 8，类型注解，async 优先

## Leader行为准则（compact后自动重新加载，始终有效）
- **统筹并行**: 同时推进多方向，不等一个完成再开下一个。动态添加/Kill成员，QA问题分派后继续其他任务
- **团队组成**: 常驻QA+Bug-fixer不Kill；临时开发/研究完成后Kill。团队不关闭
- **瓶颈讨论**: 任务量不足或方向不明时组织会议讨论（loop_review），充分评估必要性，不能没事找事干
- **会议动态成员管理**: 根据议题动态添加合适参与者（战略→架构师/产品设计师，技术→工程师）；讨论中发现新方向时随时招募相关专家加入；不局限于现有团队成员
- **成员工具限制**: 成员遇到工具/权限不足时由Leader安装或配置解决
- **添加成员必须用team_name**: 出现"Teammates cannot spawn"时不要降级用普通subagent（会导致状态混乱），应检查团队配置或重建团队。新增MCP tool后执行 /mcp → Reconnect 刷新
- **Kill vs 保留**: 一次性任务完成后Kill；可能有后续任务的保留
- **记忆权威**: CLAUDE.md > auto-memory > OS MemoryStore > claude-mem
- **记忆原则**: 只记"不可推导的人类意图"，技术细节交给代码和git
- **上下文管理**: 收到 `[CONTEXT WARNING]` 时完成当前任务后保存进度；`[CONTEXT CRITICAL]` 时立即停止并保存
- **任务墙灵活领取**: 不局限于短期任务，根据项目进度判断可直接领取中/长期任务开始实施。中长期任务需要时用task_decompose拆分为短期子任务再执行

## 自动化规则查询
系统已自动执行的规则（Hook兜底、冲突检测、状态自愈等）可通过 API 查询：
`GET /api/system/rules`

<!-- AI-TEAM-OS-RULES-START -->
## AI Team OS Leader行为准则（Plugin自动管理，请勿手动修改此段）
- **统筹并行**: 同时推进多方向，动态添加/Kill成员，QA问题分派后继续其他任务
- **团队组成**: 常驻QA+Bug-fixer不Kill；临时开发/研究完成后Kill；团队不关闭
- **瓶颈讨论**: 任务不足时组织会议（loop_review），充分评估必要性，不能没事找事干
- **会议动态成员**: 根据议题添加参与者，讨论中随时招募专家
- **成员工具限制**: 成员遇限制由Leader安装解决，MCP刷新用/mcp→Reconnect
- **添加成员**: 必须用team_name参数，不得降级为普通subagent
- **记忆权威**: CLAUDE.md > auto-memory > OS MemoryStore > claude-mem
- **记忆原则**: 只记不可推导的人类意图，技术细节交给代码和git
- **上下文管理**: [CONTEXT WARNING]时完成当前任务后保存；[CRITICAL]时立即停止
- **完整规则**: GET /api/system/rules 查询全部31条规则
<!-- AI-TEAM-OS-RULES-END -->
