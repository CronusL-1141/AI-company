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
- 发版走 skill **/os-release**（清单唯一落点；Release 正文与 `gh` 命令由 `scripts/release_notes.py` 生成，commit/tag 需用户批准，push 与 publish 由用户执行）

## 多会话并行纪律（2026-07-10 事故后立规）
- 本仓库可能同时有多个 CC 会话在工作。**第二个及之后的会话改代码必须用 `git worktree` 隔离**，禁止共享同一 checkout 写代码。
- 若确需在主 checkout 操作：动手前先 `git branch --show-current` 确认位置；切换分支/切回 master 前先 `git log --oneline -3` 确认没带走或丢下别人的提交。
- 事故实录：两 agent 共享 checkout，一方切分支干活，另一方的提交无察觉落在其分支上，切回时造成"代码消失"假象（靠 reflog 零丢失恢复）。
- 提交前跑 `bash scripts/check_invariants.sh`（红线机检：hook 副本同步/无遗留副本/版本锁步/双 dist 一致/venv 禁令/README 数字与实测一致/ruff 门禁/hook 注册面统一/MCP 工具参数描述/表集合一致——条目与细则以脚本输出为准）。

## 刻意决策 — 禁止悄悄回退
以下设计**看着反常但全是故意的**（各有血泪史或机检背书），发现"可以修好"的冲动时先停手：
- **venv 禁令**：四类进程共享依赖，坚持系统 Python + sys.executable（I5 机检；隔离方案已被否决）
- **hook 多副本**：plugin/hooks 与 src/aiteam/hooks 同名文件必须逐字节一致（I1 机检）——不是重复代码，禁止"去重"；改一处必须同步所有副本
- **tasks.config.memo 是冻结档案**：记忆 v2 升表后新 memo 只进 task_memos 表，旧 JSON 保留作历史——不是脏数据，别清理也别再写入
- **README 内的工具数/页面数**由 I6 对照实测机检——别手动"改回"旧值，加减 MCP 工具时同步双语 README
- **模型默认值留空（仅指 DB 观测字段）**：agents.model 未知就空着由观测回填，别补具体型号（写死必过时，2026-07-07 立规）。注意这**不指**模板 frontmatter——plugin/agents/*.md 已固化层级别名 `model: opus`（2026-07-10 裁定，别名浮动不算写死）；派工纪律见编排宪章：Fable 编排、Opus 执行，workflow `agent()` 默认显式 `model:'opus'`（skill /os-workflow §3）
- **无定时器/后台守护**：CC 非常驻，周期 cron 已刻意退役，一律按需工具——别"补回"调度

## 工程陷阱（实锤立规，写测试前过一遍）
- **stub 不得比生产宽松**：测试替身必须复用生产校验（如枚举/schema 校验），否则单测全绿、生产 API 拒收
- **断言要跨持久化边界**：内存对象拼出的响应"有值"不算数，须加跨请求查库的幂等用例才抓得到漏字段
- **测试装配要成套换**：依赖单例各持 repository，只覆盖一半会写真库读内存库，往返测试假性失败
- **机检类工作放批次最前**：计数/锚点先行，每一步漂移当场抓，别攒到最后
- **删数据前问"删了能不能重建"**：保留闸容易只问"还有谁会来看这份记录"，漏掉"删掉会不会毁掉再也采不回来的数据"。凡新增删除路径，逐项自问被删对象上有没有外部源已过期、只此一份的派生数据（token 账、解析产物、观测快照）。判据取宽：被测量过就算有账，测得 0 也是测量结果。实录：容器队清理上线两天后 token 五列才落到 agents 行，保留闸从未回头补，一支空壳挂着 8541 万 token 距进入删除射程只剩四小时（a6ccb67 补闸）
- **从旧备份恢复先问时钟制式**：恢复=把历史快照写进现在的库，两者可能是两个时钟（UTC 平移前/后）。判据必须机检化：比对 `PRAGMA user_version`，不等须显式换算（复用平移脚本同一份 LOCAL_COLUMNS/shift_for），未知组合一律中止；比对源与库只比 schema 不比内容抓不到这一类。实录：07-28 备份是本地墙钟，取证方案照抄恢复会往 UTC 库塞 843 行 +8h 时间戳且事后与真值不可分辨（0faacd8 三源结构+时钟闸拦下）

## 用 CC Workflow（ultracode）时
- OS 不拦 Workflow，定位为其持久化治理层。每次 Workflow 运行会被 hook **自动追踪成一个团队**（`workflow-<wf_id>`），追踪是自动的。
- 但 Leader 仍需：① 总任务 `task_create` 上墙；② 在每个 workflow agent 的 prompt 里嵌「回写指令」让其用 OS 工具(task_memo_add/report_save)记账。
- 标准模板见 skill **/os-workflow**（调 Workflow 时 hook 也会软提醒）。

