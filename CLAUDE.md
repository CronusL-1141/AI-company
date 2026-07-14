# AI Team OS

**技术栈**: Python 3.12 + FastAPI | React 19 + Vite | SQLite
**架构**: Storage → API → Dashboard（详见 docs/architecture.md）

## 核心约束
- 所有输出使用中文
- 共享类型只引用 `src/aiteam/types.py`
- 代码风格: PEP 8，类型注解，async优先

## Leader核心行为
- 专注统筹，实施工作委派团队成员
- 新需求先加入任务墙，系统级功能先写设计文档
- 完整规则通过SessionStart自动注入，也可查询 GET /api/system/rules
- 用户给出偏好/纠正/设计意图时，当场用 `memory_add` 落方向层（≤400字；只影响单个任务的去 `task_memo_add`）；记忆整理用 `memory_reconcile_candidates/apply`（设计见 docs/memory-v2-design.md）

## 多会话并行纪律（2026-07-10 事故后立规）
- 本仓库可能同时有多个 CC 会话在工作。**第二个及之后的会话改代码必须用 `git worktree` 隔离**，禁止共享同一 checkout 写代码。
- 若确需在主 checkout 操作：动手前先 `git branch --show-current` 确认位置；切换分支/切回 master 前先 `git log --oneline -3` 确认没带走或丢下别人的提交。
- 事故实录：两 agent 共享 checkout，一方切分支干活，另一方的提交无察觉落在其分支上，切回时造成"代码消失"假象（靠 reflog 零丢失恢复）。
- 提交前跑 `bash scripts/check_invariants.sh`（红线机检 I1-I6：hook 副本同步/无遗留副本/版本五处锁步/双 dist 一致/venv 禁令/README 数字与实测一致——以脚本输出为准，此处不复述细则）。

## 刻意决策 — 禁止悄悄回退
以下设计**看着反常但全是故意的**（各有血泪史或机检背书），发现"可以修好"的冲动时先停手：
- **venv 禁令**：四类进程共享依赖，坚持系统 Python + sys.executable（I5 机检；隔离方案已被否决）
- **hook 多副本**：plugin/hooks 与 src/aiteam/hooks 同名文件必须逐字节一致（I1 机检）——不是重复代码，禁止"去重"；改一处必须同步所有副本
- **tasks.config.memo 是冻结档案**：记忆 v2 升表后新 memo 只进 task_memos 表，旧 JSON 保留作历史——不是脏数据，别清理也别再写入
- **README 内的工具数/页面数**由 I6 对照实测机检——别手动"改回"旧值，加减 MCP 工具时同步双语 README
- **模型默认值留空（仅指 DB 观测字段）**：agents.model 未知就空着由观测回填，别补具体型号（写死必过时，2026-07-07 立规）。注意这**不指**模板 frontmatter——plugin/agents/*.md 已固化层级别名 `model: opus`（2026-07-10 裁定，别名浮动不算写死）；派工纪律见编排宪章：Fable 编排、Opus 执行，workflow `agent()` 默认显式 `model:'opus'`（skill /os-workflow §3）
- **无定时器/后台守护**：CC 非常驻，周期 cron 已刻意退役，一律按需工具——别"补回"调度

## 用 CC Workflow（ultracode）时
- OS 不拦 Workflow，定位为其持久化治理层。每次 Workflow 运行会被 hook **自动追踪成一个团队**（`workflow-<wf_id>`），无需手动 TeamCreate。
- 但 Leader 仍需：① 总任务 `task_create` 上墙；② 在每个 workflow agent 的 prompt 里嵌「回写指令」让其用 OS 工具(task_memo_add/report_save)记账。
- 标准模板见 skill **/os-workflow**（调 Workflow 时 hook 也会软提醒）。

