# AI Team OS 架构

> CLAUDE.md 中「Storage → API → Dashboard」一行摘要的展开版。
> 本文只记录真实存在的分层与文件路径，随代码演进同步维护。

## 总览：主数据流

```
Claude Code (MCP client)      Codex (MCP client，适配器落地中)
   │  stdio                      │  stdio
   └──────────────┬──────────────┘
                  ▼
src/aiteam/mcp/          MCP Server（fastmcp，113 工具）
   │  server.py 注册 tools/ 16 个子模块；_autostart.py 自动拉起 uvicorn
   │  HTTP (端口发现: ~/.claude/data/ai-team-os/api_port.txt)
   ▼
src/aiteam/api/          FastAPI 服务（app.py + routes/ 37 个路由模块）
   │  后台任务: state_reaper.py（状态回收/治理循环）、wake_manager.py、event_bus.py
   ▼
src/aiteam/storage/      存储层（StorageRepository + SQLAlchemy async + SQLite）
   │  connection.py（含手写幂等迁移）、engine_pool.py、models.py
   │  真相源: ~/.claude/data/ai-team-os/aiteam.db（WAL）
   ▼
dashboard/               React 19 + Vite 前端（23 页面，Zustand 状态管理）
                         构建产物双副本: dashboard/dist（本地构建，gitignore）
                         与 plugin/dashboard-dist（入库分发，I3 机检约束一致性）
```

## 旁路组件

| 目录 | 职责 |
|------|------|
| `plugin/hooks/` | CC 生命周期 hook 脚本**真相源**（send_event / session_bootstrap / context_tracker 等）。`src/aiteam/hooks/` 为同名镜像副本，I1 机检强制逐字节一致；入口 `send_event.py` 的字节由 I1c 冻结；install.py 安装到 `~/.claude/hooks/ai-team-os/` |
| `plugin/harness/codex/` | Codex 适配层（注册面 `surface.py` + 参照清单 `hooks.json` + 授信锁 `hook-trust.lock` + 共用核心第三副本 `hooks/hook_core.py`）。**不被任何宿主自动加载**，CC 安装器也从不遍历它（I20 断言取源集合恒为 hooks/agents/skills/commands/loop.md 五项）。详见下节 |
| `src/aiteam/services/` | 生态扫描子系统（ecosystem_scanner / tagger / summarizer / deep_reviewer 等） |
| `src/aiteam/meeting/` | 会议模板系统 |
| `src/aiteam/memory/` | 记忆系统 v2 双层：情景层 task_memos（agent 工作日志，BM25 按需检索）+ 方向层 memories（偏好/纠正，双 hook 常驻注入）+ reconcile 按需整理（粗筛 reconcile.py，无向量/无常驻 LLM）；设计见 docs/memory-v2-design.md |
| `src/aiteam/loop/` | 任务墙引擎与治理件（task_wall_engine / watchdog / auto_assign / completion_verifier / failure_alchemy / replay_engine / what_if）。loop 状态机本身已于 v1.10.3 退役，目录名保留 |
| `src/aiteam/cli/` | Typer CLI（`aiteam` 入口，commands/ 子命令） |
| `src/aiteam/config/` | pydantic-settings 配置（settings.py） |
| `src/aiteam/integrations/` | 外部集成（notifier.py Slack webhook 等） |
| `scripts/check_invariants.sh` | 红线机检的唯一编排器。**条数与条目清单不在本表维护**——这一行此前腐烂了两个大版本（写着 11 条到 I10，实为 14 条到 I14），凡是手抄一份就必然如此。真相源是脚本里的 I 段头：`grep -cE '^# ── I[0-9]+:' scripts/check_invariants.sh`；双语 README 顶部的条数由 I6 对着同一条命令核。字母后缀的子编号（I1b 遗留副本禁令 / I1c hook 入口哈希冻结）不占号。I1–I10 每条在脚本内都带着它的事故出处，此处不复述；I11 时钟约定 / I12 用量量纲白名单 / I13 覆盖率同屏 / I14 回采红线 / I15 Codex hook 清单 schema（`check_codex_hook_surface.py`）/ I16 execpolicy 校验（`check_codex_execpolicy.py`，本期 warn 级）/ I17 hook 授信锁（`check_codex_trust_lock.py`）/ I18 AGENTS.md 恒等（`check_agents_md.py`）/ I19 Codex 夹具接 golden（`check_codex_fixtures.py`）/ I20 适配器安装隔离（`check_codex_isolation.py`） |

## harness 适配层

OS 要同时给两个 harness（CC 与 Codex）当持久化治理层，切法是**一核两适配器**：

- **核心只有一份**。`src/aiteam/` 全域与 hook 侧的载荷加工逻辑 `hook_core.py` 都是
  harness 中立的，任何一侧的需求都不许在核心里长出 `if harness == ...` 的分叉。
- **适配器各占一个目录**，互不 import：`plugin/hooks/` 是 CC 面（注册面由 install.py
  的 `HOOK_SURFACE` 与 `plugin/hooks/hooks.json` 双向对钉，I8），`plugin/harness/codex/`
  是 Codex 面（注册面由 `surface.py` 单一真相源渲染出 `hooks.json` 与 `hook-trust.lock`，
  I15/I17）。两个 harness 的事件名、工具名形态与授信模型都不同，合表只会让每一次 Codex
  改动都落到一个 CC 分发文件上。
- **送事件的入口有两个，核心只有一个**。CC 入口是 `plugin/hooks/send_event.py`，它的
  字节被 `scripts/hook_entry_freeze.json` 冻结（I1c）——用户机器上那份脚本已被授信，
  悄悄改它的行为不会有任何提示。Codex 入口用自己的文件名（`surface.py` 的
  `CODEX_HOOK_SCRIPTS` 是唯一登记处），与 CC 侧零重名，I1 反过来断言这些名字不许出现在
  两个 CC hook 目录里。两个入口共享的加工逻辑住在 `hook_core.py`：仓内三处各一份，
  I1 三方逐字节对钉。刻意的重复只有一处——`send_event.py` 里那几段与 `hook_core.py`
  同源的代码没有改成 import，因为 import 会把共享文件拖进 CC 运行时目录；**这不是待
  清理的重复代码**，改任何一处都必须同步全部副本。
- **DB 是唯一共享面**。两个 harness 的观测都汇入同一套表，harness 维度靠 `agents.harness`
  等可空列区分，未知就留空由观测回填。适配器之间不通过任何进程内接口互相看见。

机检背书：I1（三方副本 + 反向同名污染）、I1c（CC 入口冻结）、I15（Codex 清单 schema）、
I17（授信锁与清单一致）、I18（AGENTS.md ≡ CLAUDE.md + 标准头）、I20（安装隔离静态半边）。
运行时那一半的隔离断言（装两次逐字节相同、CC 配置零改动）随 Codex 安装器分支落地。

## Legacy / 已退役

- `src/aiteam/orchestrator/`：LangGraph 图执行路径，仅 CLI `aiteam task run`
  可达，依赖可选 extra `[langgraph]`。
- 自带 pipeline 已**整域删除**（2026-07 执行，决策同期）：OS 转型为 ultracode/CC
  Workflow 的**持久化观测与治理层**，Workflow 运行由 hook 自动追踪为
  `workflow-<wf_id>` 团队。删除范围含 `src/aiteam/pipeline/`、`loop/pipeline.py`、
  pipeline REST 路由、`pipeline_gate` / `autopilot_auto_stop` 两个 hook、autopilot
  skill 与 3 个 MCP 工具。**保留项**（勿清理）：`pipeline_stage_history` 表与其
  ORM（append-only 历史，只停写不 drop）、`tasks.config['pipeline']` 历史字段位、
  `task_create(task_type=...)` 参数（软退役 no-op）。

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
| Codex hook 清单 | `~/.codex/hooks.json`（宿主注册面；仓内 `plugin/harness/codex/hooks.json` 只是参照渲染，不可直投） |
| Codex 已安装 hooks | `~/.codex/hooks/ai-team-os/` |
| Codex skill 落点 | `~/.agents/skills/` |
| Codex 插件缓存 | `~/.codex/plugins/cache/`（装插件时宿主自动迁移 commands 的落点） |

上表后四行是 **Codex 侧的目标路径**，随 Codex 安装器分支落地；本期仓内只有适配层的
声明与参照渲染，安装器尚未写这四处中的任何一处。

## 安装与分发

- 推荐源码安装：`python install.py`（复制 hooks、注册 settings.json、系统 Python 无 venv——四类进程共享依赖，venv 隔离已被否决）。
- Plugin/marketplace：根 `.claude-plugin/marketplace.json` 的 source 指向 `./plugin`。
