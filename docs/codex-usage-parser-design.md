# Codex rollout 用量归因解析器

本设计定义 `codex_rollout_usage`、Codex hook 采集和历史回采的共同契约。沿用 agents 的四层用量字段以及 reasoning 子集、来源和测量时刻，不改变 Claude 解析、套餐估价或宿主安装配置。

## 计量口径

原始 `input_tokens` 包含缓存读取和缓存写入。写入 agents 时，input = input − cached_input − cache_write，cache_read = cached_input，cache_creation = cache_write。`reasoning_output_tokens` 已包含在 output 中，只单独保存，永不与四层相加。

源字段不存在时保存 NULL，不能把未采集伪装成测得零。保留 types.py 对 Codex cache_creation 的 `WIRE_PRESENT_UNVERIFIED` 定义；线上字段存在且恒零，不足以证明缓存写入能力已经验证。任何 provider 的真实请求都可计量，不能继承套餐估价读取器的官方 provider 过滤条件。

## 身份、去重和完整性

首条 session_meta 是文件拥有者；后续父线程 meta 不改变它。请求账本按 response_id 去重，只计 thread_id 属于本线程的请求。重复请求冲突必须拒绝。多个同身份文件允许原样副本、追加前缀，或逐条 thread_token_usage 严格闭合的连续分段；断裂和分叉不能标成完整测量。

fork 首累计可能包含父线程继承基数。只有该基数精确匹配父完整合账在子创建之前的累计锚点，才能扣除。仅匹配值、不检查时间，或者仅检查父文件存在，都不足以证明基数属于父线程。

混合账本中的真实旧 token_count 必须按请求顺序匹配账本 usage。纯旧格式复用累计增量校验，去重累计快照；last 与累计差额不一致、回退、缺父、循环或部分前缀均保留未知。导入幻影及无法证明来源的导入基线不能成为调用账。

## 路径与隔离

配置根按显式 codex_home、CODEX_HOME、用户主目录的 `.codex` 依次选择，仅对配置根 resolve。根内的目录和文件读取继续使用 O_NOFOLLOW，不解析内部符号链接来绕过读取边界。

定位覆盖 sessions 和 archived_sessions；迁归档的原 basename 和 UUID 后带 suffix 的分段都须纳入，逐份验证首 meta 身份。归档表示原件存在，错误旧路径可以恢复。解析受目录、字节、行长和时间预算约束，诊断只返回静态原因码。

子进程使用父进程已加载的 aiteam 包根作为绝对 PYTHONPATH 首项，并通过 Python 的安全路径选项避免 changed cwd 抢先加载另一份包。环境中的相对导入路径以启动时的父 cwd 固定为绝对路径；PYTHONPATH 未设置或为空时不产生任何条目，与 Python 自身的读法一致，只有非空值里的空段才代表父 cwd；日志路径和配置根在启动前做词法绝对化，配置根的 resolve 及后续文件系统访问均留在 worker；父子不能静默混用不同 checkout，也不能把同步路径查找带回 API 协程。

## 活体采集边界

`read_codex_rollout_state` 在子进程中执行 IO 和 JSON 解析，返回用量以及是否找到了该线程的 rollout（`read_codex_rollout_usage` 只取用量），全局最多两个解析进程；超时或取消会 kill 并 wait。按 actor UUID 使用 single-flight；子 agent 的真实身份是 cc_tool_use_id，不能使用其 root session_id 归因。

Start 触发强制采集；子 agent 的 Stop/SubagentStop 分支也强制采集。Codex Leader 的 Stop 使用30秒节流。当前 Codex 安装面没有 SessionEnd 和 PostCompact，因此不能宣称 Leader 已有结束时强制终测；节流丢尾的改进属于单独任务。single-flight 仅为执行中的重复调度保留一次尾随请求，不会给被节流跳过的 Stop 新增定时补测。

空 Start、解析失败或原子写入失败会撤销该轮 claim，且不能删掉更新调度的 claim。每次写入是累计快照赋值；CAS 同时检查 agent ID、harness、session_id、cc_tool_use_id、旧 transcript_path 和旧 tokens_measured_at，防止旧解析结果覆盖重绑或并发测量，并清空 usage_unmeasurable：真实测量推翻"没有用量"的结论。Python 入口与 SQL 更新各自独立拒绝非 Codex 行。

找到了 rollout、里面却没有任何用量事件，是解析器给出的肯定结论；找不到 rollout 说明不了用量，不下结论。前者只对已经结束的行记为 usage_unmeasurable=no_usage_lines，判据是 `usage_coverage.is_in_flight`（busy 或 30 分钟内有活动即在飞），与未归因分类和维护脚本共用：在飞的线程首个用量事件可能还没落盘，Start 与 Stop 采集到的正是这种时刻。写入带与用量写入相同的身份条件，要求未测、结论列为空，并比较判定时读到的 status 与 last_active_at，判定之后行又活起来就不写。

## 历史回采

`scripts/backfill_codex_usage.py` 默认 dry-run；SQLite URI mode=ro 加 query_only，物理上禁止数据库写入。只处理 harness=codex 且未测行，Leader 和子 agent 分别按 session_id 和 cc_tool_use_id 精确定位。

同一真实线程已有测量时，其余重复行不再计量；全部未测但身份重复时保持未知，不按创建时间任意选行。计划保留允许列、before/after 和 UTC user_version；未知时钟或缺失 schema 立即中止，不隐式迁移。

rollout 找到但没有用量事件、且已经结束的行（判定用 `attribution_marks.identify_unmeasurable`），计划在 verdicts 里单列一条 usage_unmeasurable=no_usage_lines，只写空列；它的 before-image 另含 status 与 last_active_at，计划生成后行又活起来即整事务回滚。还在跑的行只计数、不下结论。

apply 消费已核对的计划，要求新备份和新 journal。BEGIN IMMEDIATE 内逐列比较 before-image，变化则整事务回滚。原 sessions 路径非空但已失效时，可改为已验证的 archive 原件路径；旧值保留在 journal 和备份中。这与 Claude 回采“仅补空路径”的约定不同，不能混称。禁止修改 workflow_agents.tokens、方向层、任务或宿主配置。

计划冻结生成时刻的数值，后续日志增长不会自动更新计划。回采只补未测行，不能用它宣称已修复活体旧快照的缺尾；已有测量的修补需要另行定义范围与守卫。

## 读侧口径

未归因分类对 Codex 行的判定顺序与 CC 行共用前三步：本库没有解析器的宿主、在飞、行上已写下的结论。之后才看文件：解析器按线程 id 在 sessions 与 archived_sessions 里找原件，所以没有登记路径或文件在都算尚未测量；登记的路径上没有、归档目录里有同名文件算已归档，回采会改指归档原件；两处都没有才是灭失。

聚合的方法标注按已测行的来源：全部来自 rollout 标 `codex_rollout_parse`，CC transcript 与 rollout 都有标 `mixed`，有任何别名兜底仍整体标降级。单次实测对 Codex 行走同一个子进程解析器，按线程 id 定位；rollout 不带的字段返回 null，界面显示为缺失而不是 0。

## 验证

合成与冻结夹具验证请求/累计去重、缓存重叠、reasoning 子集、缺字段、真实零、fork 和分段完整性、导入、路径恢复、内部符号链接拒绝、超时及取消。真实子进程测试隔离 HOME/CODEX_HOME，并显式覆盖 cwd 改变、相对 PYTHONPATH，以及 PYTHONPATH 未设置或为空时父 cwd 不进入 worker 的导入路径。

文件 SQLite 测试跨连接验证首次写入、子 agent 二次增长、路径和测量时刻的 CAS、Python/SQL 两层 harness 守卫、幂等、重复身份及并发回采保护。关键守卫须经撤回变异：删除判据时对应测试变红，恢复后变绿。

完整验收包括 Homebrew Python3.12 下的相关测试、项目全量 `python3 -m pytest tests/ -n 6 --dist loadfile` 和 `bash scripts/check_invariants.sh`。结果和会话级恢复记录保存到任务报告，不写入产品规格。
