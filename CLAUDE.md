# AI Team OS — 项目指令

## 项目概览
通用可复用的AI Agent团队操作系统框架，包含CLI工具、REST API、Web Dashboard。
- **包名**: `aiteam`
- **CLI入口**: `aiteam`
- **技术栈**:
  - **后端**: Python 3.12, LangGraph, SQLAlchemy 2.0 async, FastAPI, Typer+Rich
  - **前端**: React 19, Vite 7, TypeScript, React Router v7, Zustand, TanStack Query, shadcn/ui
  - **基础设施**: PostgreSQL+pgvector, Redis, Docker Compose, Alembic
  - **记忆**: Mem0 SDK + SQLite 双后端

## 架构
五层架构（Layer 1-5）：Storage → Memory → Orchestrator → CLI+API → Dashboard
详见 `docs/architecture.md`

## 当前阶段
Milestone 2.5 — 实时监控 + CC Hooks 集成

## 开发规则
1. **所有输出使用中文**
2. **文件驱动协调**: 每个工程师只写自己负责的目录，更新 `coordination.md` 状态
3. **共享类型**: 所有模块引用 `src/aiteam/types.py` 中的类型定义，不自行定义
4. **接口契约**: 遵循 `docs/api-contracts.md` 中定义的接口签名
5. **代码风格**: PEP 8，类型注解，async优先
6. **测试**: 每个模块需有对应单元测试
7. **状态消息**: ≤200字，写入coordination.md

## 目录职责分配
| 目录 | 负责人 | 说明 |
|------|--------|------|
| `src/aiteam/storage/` | storage-engineer | 数据模型、持久化 |
| `src/aiteam/memory/` | memory-engineer | 记忆管理、上下文恢复 |
| `src/aiteam/orchestrator/` | graph-engineer | LangGraph编排模式 |
| `src/aiteam/cli/` | cli-engineer | CLI命令 |
| `src/aiteam/api/` | integration-engineer | REST API + WebSocket |
| `src/aiteam/hooks/` | integration-engineer | CC Hooks事件处理 |
| `src/aiteam/types.py` | tech-lead | 全局共享类型（只读引用） |
| `src/aiteam/config/` | tech-lead | 配置管理 |
| `dashboard/` | frontend-engineer(s) | React Dashboard |
| `tests/` | qa-engineer | 测试 |
| `docs/` | tech-lead | 文档 |

## AI Team OS 集成（Claude Code Hooks）

### Hooks系统
- 项目已配置7种CC hook事件：`SubagentStart`, `SubagentStop`, `PreToolUse`, `PostToolUse`, `SessionStart`, `SessionEnd`, `Stop`
- hook脚本位于 `.claude/hooks/send_event.py`，读取stdin JSON并POST到 `http://localhost:8000/api/hooks/event`
- 安装命令: `aiteam hooks install`

### Agent主动注册（推荐）
当作为团队成员启动时，应主动向OS注册：
```
POST http://localhost:8000/api/teams/{team_id}/agents
{
  "name": "agent名称",
  "role": "agent角色",
  "model": "claude-opus-4-6",
  "system_prompt": "agent职责描述"
}
```

### 三层保险机制
1. **CLAUDE.md指引** — Agent启动时主动调用API注册自己（source=api）
2. **Hooks自动兜底** — SubagentStart事件自动捕获未注册的Agent（source=hook）
3. **SessionEnd对账** — session结束时比较hook捕获数 vs api注册数，记录差异

### 关键API端点
| 端点 | 说明 |
|------|------|
| `POST /api/hooks/event` | CC hook事件统一接收 |
| `GET /api/agents/{id}/activities` | Agent活动日志 |
| `PUT /api/agents/{id}/status` | 更新Agent状态 |
| `GET /api/events` | 事件列表 |
| `WS ws://localhost:8000/ws` | WebSocket实时推送 |

### 会议操作指引

创建会议：
POST http://localhost:8000/api/teams/{team_id}/meetings
body: {"topic": "讨论主题", "participants": ["agent_id_1", "agent_id_2"]}

发送消息：
POST http://localhost:8000/api/meetings/{meeting_id}/messages
body: {"agent_id": "你的agent_id", "agent_name": "你的名称", "content": "消息内容", "round_number": 1}

读取消息：
GET http://localhost:8000/api/meetings/{meeting_id}/messages

结束会议：
PUT http://localhost:8000/api/meetings/{meeting_id}/conclude

讨论规则：
1. Round 1: 各自提出观点
2. Round 2+: 必须先读取前人发言，引用并回应具体观点
3. 最后一轮: 汇总共识和分歧

## 约束
- 不自建LLM Provider抽象层，直接使用LangChain ChatModel
- 不做具体投资建议或交易信号（继承自研究项目约束）
