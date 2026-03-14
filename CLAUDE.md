# AI Team OS

**技术栈**: Python 3.12 + LangGraph + FastAPI | React 19 + Vite 7 | PostgreSQL + Redis
**架构**: 五层 Storage → Memory → Orchestrator → CLI+API → Dashboard（详见 `docs/architecture.md`）
**当前阶段**: Milestone 2.5 — 实时监控 + CC Hooks 集成

## 核心约束
- 所有输出使用中文
- 共享类型只引用 `src/aiteam/types.py`，不自行定义
- 不自建 LLM Provider 抽象层，直接使用 LangChain ChatModel
- 代码风格: PEP 8，类型注解，async 优先

## 需人工判断的规则
- **文件驱动协调**: 每个工程师只写自己负责的目录，更新 `coordination.md` 状态（≤200字）
- **Kill vs 保留 Agent**: 一次性任务完成后 Kill；可能还有后续任务的保留
- **记忆权威层级**: CLAUDE.md > auto-memory > OS MemoryStore > claude-mem
- **记忆内容**: 只记"不可推导的人类意图"，技术细节交给代码和 git
- **上下文管理**: 收到 `[CONTEXT WARNING]` 时完成当前任务后保存进度；收到 `[CONTEXT CRITICAL]` 时立即停止并保存

## 自动化规则查询
系统已自动执行的规则（Hook兜底、冲突检测、状态自愈等）可通过 API 查询：
`GET /api/system/rules`
