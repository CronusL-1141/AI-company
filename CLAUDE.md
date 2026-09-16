# AI Team OS

**技术栈**: Python 3.12 + FastAPI | React 19 + Vite | SQLite
**架构**: Storage → API → Dashboard（详见 docs/architecture.md）

## 核心约束
- 中文是默认语言（对话、文档、任务/记忆条目）；例外：`README.md` 与 `CHANGELOG.md` 保持英文为正本（`README.zh-CN.md` / `CHANGELOG.zh-CN.md` 是镜像译本）、代码标识符用英文
- 共享类型只引用 `src/aiteam/types.py`
- IO 路径一律 async，请求处理里禁阻塞调用（0910 同步 Git 扫描曾拖住整个 API，v1.12.4 修）

## Leader核心行为
- 新需求先加入任务墙，系统级功能先写设计文档
- 用户给出偏好/纠正/设计意图时**当场** `memory_add` 落方向层；只影响单个任务的用 `task_memo_add`（字数上限、桶配额与整理流程见 docs/memory-v2-design.md）
- 发版走 skill **/os-release**（清单唯一落点）；不论走没走 skill：commit/tag 须用户批准，push 与 publish 由用户执行

## 多会话并行纪律（2026-07-10 事故后立规）
- 本仓库可能同时有多个 CC 会话在工作。**第二个及之后的会话改代码必须用 `git worktree` 隔离**，禁止共享同一 checkout 写代码。
- **worktree 一律建在仓库内 `.worktrees/<名字>`**（已 gitignore），禁止 `git worktree add ../…` 落到同级目录：OS 按「子目录归属」把仓库内子目录解析到本项目，同级目录解析不到。用完 `git worktree remove <路径>` 再 `git worktree prune`。
- 确需在主 checkout 操作：动手前 `git branch --show-current`，切分支前 `git log --oneline -3`——共享 checkout 切分支会带走别人未察觉的提交（实录见 docs/architecture.md 附录）。
- 提交前跑 `bash scripts/check_invariants.sh`（红线条目以脚本输出为准）。

## 刻意决策 — 禁止悄悄回退
以下设计**看着反常但全是故意的**（各有血泪史或机检背书），发现"可以修好"的冲动时先停手：
- **venv 禁令**：四类进程共享依赖，坚持系统 Python + sys.executable（I5 只扫 `src/aiteam/**.py`，在 shell 里建 venv 它抓不到）
- **hook 多副本是故意的**：`plugin/hooks` 与 `src/aiteam/hooks` 同名文件逐字节一致（I1，共享核 hook_core.py 在含 Codex 适配器的三处各存一份）——禁止「去重」，改一处必须同步所有副本
- **tasks.config.memo 是冻结档案**：记忆 v2 升表后新 memo 只进 task_memos 表，旧 JSON 保留作历史——不是脏数据，别清理也别再写入
- **README 里的工具数/页面数以实测为准**（I6 扫双语两份）——别手改回旧值
- **模型默认值留空（仅指 DB 观测字段）**：agents.model 未知就空着由观测回填，别补具体型号（写死必过时）
- **无定时器/后台守护**：CC 非常驻，周期 cron 已刻意退役，一律按需工具——别"补回"调度

## Council 四纪律（2026-07-28 会议 e7e90df0 决议，用户批准执行；2026-09-07 自方向层搬入，约束 Leader 裁决）
- **no-data≠zero**：任何基于"表 0 行/零调用"的裁撤，必须先排除采集断链/迁移丢失/口径隔离三种成因才可议——本库是迁移新库，历史分母不可信（曾差点在残库上砍掉会议功能）
- **排期闸=行动信号>表态信号**：外部 issue 👍 数只能排细节优先级，立项须有真实人类行动信号（动手提 PR/追问后翻转）
- **dogfooding 验收=故障注入非时长**：对外讲"崩溃不丢"前必须有一次真实杀 session 恢复实证，"自用一周"证明不了持久性
- **会议以决策上墙收尾**（decision 或任务墙条目），否则视同没开。**减法要留痕**：刻意决策回退 / 砍工具删表 / 削弱或删除机检红线，走会议或缔造者当场裁定都算数，但结论必须上墙——这类东西丢了难重建，血泪史全是这个形态。**加法不设闸**：新增机检条目、加严已有判据、新增工具或表，写清理由直接做。拿不准算加法还是减法，按减法办。会议能力保留但不进对外叙事第一屏

## 工程陷阱（实锤立规，写测试前过一遍）
- **stub 不得比生产宽松**：测试替身必须复用生产校验（如枚举/schema 校验），否则单测全绿、生产 API 拒收
- **断言要跨持久化边界**：内存对象拼出的响应"有值"不算数，须加跨请求查库的幂等用例才抓得到漏字段
- **测试装配要成套换**：依赖单例各持 repository，只覆盖一半会写真库读内存库，往返测试假性失败
- **机检类工作放批次最前**：计数/锚点先行，每一步漂移当场抓，别攒到最后
- **删数据前问「删了能不能重建」**：新增删除路径时逐项自问被删对象上有没有外部源已过期、只此一份的派生数据（token 账、解析产物、观测快照）。判据取宽：被测量过就算有账，测得 0 也是测量结果。（实录见 a6ccb67 与 docs/architecture.md 附录）
- **从旧备份/快照恢复数据前先比时钟制式**：先比 `PRAGMA user_version`，不等须显式换算，未知组合一律中止——只比 schema 不比内容抓不到这类错（换算与三源互证的实现见 `scripts/restore_purged_container_team.py`，设计见 docs/utc-unification-design.md）

<!-- codex:end -->

## 模型分层与派工纪律（Claude Code 专属；上面「刻意决策」里「模型默认值留空」条的续文）
- **模型默认值留空**不指模板 frontmatter：`plugin/agents/*.md` 已固化层级别名 `model: opus`（2026-07-10 裁定，别名浮动不算写死）
- 派工：Fable 编排、Opus 执行——Agent 与 workflow `agent()` 一律显式 `model:'opus'`，用 fable 须写理由（Agent prompt 首行 `[fable 理由: …]`，workflow 内 `// fable 理由:` 注释）。额度紧张时的放宽只算当次特例，须缔造者明令并注明有效期，**不得写进记忆变成常规**（0811 放宽被写成常规方向记忆，之后三周整批 fable，0902 才被实测发现）。细则 skill /os-workflow §3。
- **分级在前，模式在后**：L0 自测闭环 / L1 摘要审查 / L2 全文对抗（判据与七条细则见 skill /os-workflow §3.1）。L0-L1 自己做完，只有 L2 才开 workflow——ultracode 常开≠事事开 workflow，限制的是「任何问题都按最复杂方式做完」。一份文档超十处改动，写成改动清单派 opus 执行，别在大上下文里逐条 Edit（0905 实测 70 次 Edit ＝1.27 亿缓存读，比全部子 agent 加起来还多一半）。
- 用 CC Workflow：运行本身会被 hook 自动收编成 `workflow-<wf_id>` 团队，但**产出不会自己入库**——写脚本前先 `task_create` 上墙，每个 `agent()` 的 prompt 末尾嵌回写指令，模板见 skill /os-workflow。
