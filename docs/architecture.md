# AI Team OS 架构

> CLAUDE.md 中「Storage → API → Dashboard」一行摘要的展开版。
> 本文只记录真实存在的分层与文件路径，随代码演进同步维护。

## 总览：主数据流

```
Claude Code (MCP client)
   │  stdio
   ▼
src/aiteam/mcp/          MCP Server（fastmcp，160 工具）
   │  server.py 注册 tools/ 24 个子模块；_autostart.py 自动拉起 uvicorn
   │  HTTP (端口发现: ~/.claude/data/ai-team-os/api_port.txt)
   ▼
src/aiteam/api/          FastAPI 服务（app.py + routes/ 40 个路由模块）
   │  后台任务: state_reaper.py（状态回收/治理循环）、wake_manager.py、event_bus.py
   ▼
src/aiteam/storage/      存储层（StorageRepository + SQLAlchemy async + SQLite）
   │  connection.py（含手写幂等迁移）、engine_pool.py、models.py
   │  真相源: ~/.claude/data/ai-team-os/aiteam.db（WAL）
   ▼
dashboard/               React 19 + Vite 前端（24 页面，Zustand 状态管理）
                         构建产物双副本: dashboard/dist（本地构建，gitignore）
                         与 plugin/dashboard-dist（入库分发，I3 机检约束一致性）
```

## 旁路组件

| 目录 | 职责 |
|------|------|
| `plugin/hooks/` | CC 生命周期 hook 脚本**真相源**（send_event / session_bootstrap / context_tracker 等）。`src/aiteam/hooks/` 为同名镜像副本，I1 机检强制逐字节一致；install.py 安装到 `~/.claude/hooks/ai-team-os/` |
| `src/aiteam/services/` | 生态扫描子系统（ecosystem_scanner / tagger / summarizer / deep_reviewer 等） |
| `src/aiteam/meeting/` | 会议模板系统 |
| `src/aiteam/memory/` | 记忆系统 v2 双层：情景层 task_memos（agent 工作日志，BM25 按需检索）+ 方向层 memories（偏好/纠正，双 hook 常驻注入）+ reconcile 按需整理（粗筛 reconcile.py，无向量/无常驻 LLM）；设计见 docs/memory-v2-design.md |
| `src/aiteam/loop/` | Loop 引擎与治理件（watchdog / trust_scoring / error_budget / failure_alchemy 等） |
| `src/aiteam/cli/` | Typer CLI（`aiteam` 入口，commands/ 子命令） |
| `src/aiteam/config/` | pydantic-settings 配置（settings.py） |
| `src/aiteam/integrations/` | 外部集成（notifier.py Slack webhook 等） |
| `scripts/check_invariants.sh` | 红线机检：I1 hook 双副本 / I1b 遗留副本禁令 / I2 版本五处锁步 / I3 双 dist 一致 / I5 venv 禁令 |

## Legacy / 已退役

- `src/aiteam/orchestrator/` + `src/aiteam/pipeline/`：LangGraph 图执行路径，
  仅 CLI `aiteam task run` 可达，依赖可选 extra `[langgraph]`。
- 自带 pipeline 已退役（2026-07 决策）：OS 转型为 ultracode/CC Workflow 的
  **持久化观测与治理层**，Workflow 运行由 hook 自动追踪为 `workflow-<wf_id>` 团队。

## 共享类型铁律

所有跨模块共享类型只定义/引用 `src/aiteam/types.py`（CLAUDE.md 核心约束）。

## 运行时文件位置

| 文件 | 路径 |
|------|------|
| 主数据库 | `~/.claude/data/ai-team-os/aiteam.db` |
| API 端口发现 | `~/.claude/data/ai-team-os/api_port.txt` |
| MCP 调试日志 | `~/.claude/data/ai-team-os/mcp-debug.log` |
| API PID 文件 | `<tmpdir>/aiteam-api.pid`（_autostart.py） |
| API 启动锁 | `<tmpdir>/aiteam-api-startup.lock` |
| 已安装 hooks | `~/.claude/hooks/ai-team-os/` |

## 安装与分发

- 推荐源码安装：`python install.py`（复制 hooks、注册 settings.json、系统 Python 无 venv——四类进程共享依赖，venv 隔离已被否决）。
- Plugin/marketplace：根 `.claude-plugin/marketplace.json` 的 source 指向 `./plugin`。
