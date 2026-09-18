<!-- 本文件由 scripts/gen_agents_md.py 自动生成，请勿手改。 -->

# AGENTS.md（自动生成 · 请勿手改）

本文件是仓库根目录 `CLAUDE.md` **共享段**的等价副本（`<!-- codex:end -->` 标记之前的部分），供读取 `AGENTS.md` 约定的编码助手使用。
下方正文逐字节复制自 `CLAUDE.md` 的共享段；标记之后是 Claude Code 宿主专属段（派工模型分层、Workflow 编排等），不在本文件内。

- 改规则请改 `CLAUDE.md`，再跑 `python3 scripts/gen_agents_md.py` 重新生成本文件，与源改动同批提交。
- 标准头正文在 `plugin/harness/codex/agents_md_header.md`，生成器与机检共读这一份。
- 恒等式由红线机检 I18 对钉（`scripts/check_agents_md.py`）：`AGENTS.md ≠ 标准头 + CLAUDE.md 共享段` 即红，共享段含宿主专属词亦红。

---

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
- **大参数必须显式给 `id`**：`parametrize` 会把字面量整个渲染进 test ID，`b"x"*65537` ⇒ 65,627 字符的 ID ⇒ `-v` 下一条 65KB 日志行，**GitHub Actions 的日志流被它撑断**，`gh run view --log` 从此固定截断在那一行。后果不是测试红，是**此后所有失败都看不见**（v1.13.0 那次 CI 红，真正的 FAILED 藏在截断之后，只能下载原始 zip 才挖得到）。判定截断与成败无关的方法：比对一次成功与一次失败的日志，若停在同一行同样行数即是它
- **并发用例的并发度要能压垮开发机**：判据是"在本机跑得过"还是"在争 CPU 的 runner 上也跑得过"——8 路并发在开发机上永远绿，抓不到只在 CI 现形的锁竞争。修完必须反向验证：把旧值放回去，用例必须红（实录：SQLite 锁等待 0.25s，48 路并发才稳定翻车）
- **删数据前问「删了能不能重建」**：新增删除路径时逐项自问被删对象上有没有外部源已过期、只此一份的派生数据（token 账、解析产物、观测快照）。判据取宽：被测量过就算有账，测得 0 也是测量结果。（实录见 a6ccb67 与 docs/architecture.md 附录）
- **从旧备份/快照恢复数据前先比时钟制式**：先比 `PRAGMA user_version`，不等须显式换算，未知组合一律中止——只比 schema 不比内容抓不到这类错（换算与三源互证的实现见 `scripts/restore_purged_container_team.py`，设计见 docs/utc-unification-design.md）
