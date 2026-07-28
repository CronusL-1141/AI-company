# 时间戳全库统一 UTC — 设计文档

- 状态：设计定稿，代码分层实施中；存量平移脚本 dry-run 就绪，`--apply` 待缔造者亲执行
- 决策来源：Leader Briefing `90c472dc`（用户 2026-07-28 裁定 **B：全库统一 UTC**，不走两步折中）
- 任务墙：`94559ebc-c945-4370-908e-9b207b9c0782`
- 取证基线：commit `af1dd66`；生产库只读副本 `/tmp/utc_probe.db`（2026-07-28 17:01 拷，184 MB）

---

## 1. 为什么要动它

改造之前，这个库同时跑着**两个墙钟**：

| 域 | 写入口径 | 列数 | 表数 |
|---|---|---|---|
| 核心域（projects/teams/agents/tasks/events/…） | `datetime.now()` — 宿主本地时间，naive | 24 有 default + 若干无 default | 21 |
| ecosystem 域（ecosystem_*、pipeline_stage_history） | `datetime.now(tz=UTC)` — UTC，aware | 19 有 default + 若干无 default | 15 |

SQLite 没有 tz-aware 列类型：SQLAlchemy 的 SQLite 方言把 datetime 渲染成
`'YYYY-MM-DD HH:MM:SS.ffffff'` 字符串，**offset 被静默丢弃**——传 aware 和传 naive
写出来的行逐字节相同，`DateTime(timezone=True)` 在该方言上是空操作。本次实测确认：

```
写入 datetime.now(UTC)  →  DB 内 '2026-07-28 09:00:34.563804'，读回 tzinfo=None
写入 DateTime(timezone=True) 列  →  同上，无任何差别
```

于是两个墙钟各自域内自洽、单独看都对，**只在跨域比较时出错**，而且不抛异常、只是
数字悄悄偏 8 小时。D1 一批就抓到 3 处（`aggregate_model_usage` 的"近 N 天"实际统计
N 天 + 8 小时；ecosystem 30 天过期判定提前 8 小时；另有两处注释与实测相反）。**一次
抓到三处，说明它在持续产坑，不是偶发。**

还有一个用户当下就看得见的症状：前端 ecosystem 页时间**偏早 8 小时**——后端给不带
offset 的 UTC 串，浏览器裸 `new Date()` 按本地解析。而核心域页面"显示正确"纯属巧合
（本地写 + 本地读恰好抵消），换时区或远程访问立刻错。

**结论：双墙钟不是设计，是历史漂移。** 本次不只换调用点，还要把这一整类 bug 变成
结构上不可表达的东西。

---

## 2. 目标态

> **一条规则，没有例外：系统里每一个时间戳都是 UTC。**

落成三层：

### 2.1 唯一时钟 `src/aiteam/clock.py`

纯 stdlib、零依赖，提供：

| 函数 | 语义 |
|---|---|
| `utc_now()` | 当前时刻，**aware UTC**。全系统唯一的"现在" |
| `naive_utc_now()` | 同上但 naive（仅供绕过 ORM 的裸 SQL / 迁移脚本） |
| `ensure_utc(v)` | 贴标签：naive 视为已是 UTC；aware 转 UTC |
| `to_naive_utc(v)` | 落库形态：aware 先转 UTC 再去 offset（是**换算**不是截断） |
| `from_timestamp(ts)` | POSIX 时间戳 → aware UTC（替代裸 `fromtimestamp`，后者会贴上本地 offset） |
| `from_epoch_ms(ms)` | 纪元毫秒（CC journal 用）→ aware UTC |
| `parse_utc(s)` | ISO8601 → aware UTC；**不带 offset 的串按 UTC 读**（= 存储约定） |

### 2.2 唯一换算点 `src/aiteam/storage/utc_type.py::UtcDateTime`

一个 `TypeDecorator`，全部 82 个 datetime 列统一用它：

- **写**：aware → `astimezone(UTC)` → 去 offset 存 naive；naive → 视为已是 UTC，原样存。
- **读**：naive 列值 → 贴 `tzinfo=UTC`。

两个后果，都是本设计要的：

1. **API 契约白拿**：存储层之上（服务层、API 响应、Dashboard）拿到的一律是 aware UTC，
   `.isoformat()` 自带 `+00:00`，**无需逐路由改序列化**。
2. **让静默变响亮**：残留的裸 `datetime.now()` 与库值相比会当场 `TypeError`，而不是
   返回一个偏 8 小时的答案。旧故障模式是无声的，新的漏不掉。

**磁盘格式完全不变**——老行只是被重新解释。这正是存量平移只需改"值"不需改"形"的原因。

### 2.3 唯一解析点（前端）`dashboard/src/lib/datetime.ts`

单一入口 `parseServerTime(s)`：带 offset 的按原样解析；不带 offset 的按 UTC 解析
（兼容平移前后的老串与任何缓存响应）。铺开替代 `DeepReviewSection` 里那个只修了一个
组件的 `parseAsUtc` 单点补丁。

---

## 3. 全部时间列清单（82 列 / 38 表）

分类依据 = **写入方代码** 为主，**生产库实测值** 为佐证（见 §3.3 的判定铁证）。

### 3.1 本地墙钟侧 — 需 −8h 平移（20 表 / 46 列 / 320,866 个非空单元）

| 表 | 列 | 非空行数 |
|---|---|---|
| projects | created_at, updated_at | 4 / 4 |
| phases | created_at, updated_at | 1 / 1 |
| teams | created_at, updated_at, completed_at | 249 / 249 / 166 |
| agents | created_at, last_active_at, ctx_measured_at, tokens_measured_at | 2556 / 2556 / 1912 / 1 |
| tasks | created_at, started_at, completed_at | 154 / 124 / 132 |
| task_memos | created_at, invalid_at | 722 / 40 |
| memories | created_at, accessed_at, invalid_at | 206 / 206 / 31 |
| events | timestamp | **244,394** |
| meetings | created_at, concluded_at | 1 / 1 |
| meeting_messages | timestamp | 17 |
| agent_activities | timestamp | **51,246** |
| scheduled_tasks | created_at, last_run_at, next_run_at | 0 / 0 / 0 |
| cross_messages | created_at, read_at | 0 / 0 |
| wake_sessions | started_at, finished_at | 0 / 0 |
| leader_briefings | created_at, resolved_at | 32 / 10 |
| channel_messages | created_at | 11 |
| reports | created_at | 63 |
| workflow_runs | created_at, updated_at, started_at, completed_at, last_activity_at | 226 / 226 / 226 / 226 / 148 |
| workflow_agents | created_at, updated_at, started_at, queued_at, last_activity_at | 3176 / 3176 / 3163 / 3151 / 1566 |
| knowledge_links | created_at | 493 |

> `events.timestamp` + `agent_activities.timestamp` 两列占全部平移量的 **92%**。

### 3.2 UTC 侧 — 不动（15 表 / 36 列 / 810 个非空单元）

| 表 | 列 |
|---|---|
| pipeline_stage_history | transitioned_at |
| ecosystem_repo_profiles | last_scanned_at, first_seen_at, last_commit_at, pushed_at, last_shallow_refreshed_at, last_status_change_at, manual_status_set_at |
| ecosystem_deep_reviews | created_at, started_at, completed_at, shallow_completed_at, architecture_completed_at, debated_at, stage3_completed_at, claimed_at, reviewed_at |
| ecosystem_tags / ecosystem_repo_tags / ecosystem_relations | created_at |
| ecosystem_scan_runs | started_at, completed_at |
| ecosystem_repo_status_snapshots | snapshot_at, pushed_at |
| ecosystem_project_settings / ecosystem_data_sources | created_at, updated_at |
| ecosystem_scan_profiles | created_at |
| ecosystem_index_diffs | generated_at |
| ecosystem_status_changes / ecosystem_repo_events | triggered_at |
| ecosystem_shallow_batches | created_at, updated_at, approved_at, completed_at |

其中 `pushed_at` / `last_commit_at` 值来自 GitHub API（本就是真 UTC），双重意义上都不该动。

### 3.3 判定铁证（为什么敢说哪列是哪制）

取证时刻：本地 `17:01`，UTC `09:01`（Asia/Shanghai = UTC+8，**无夏令时**，所以偏移是常量 8h）。

| 观测 | 值 | 结论 |
|---|---|---|
| `agents.last_active_at` 最大值 | `2026-07-28 17:01:24` | ≈ 本地此刻 → **本地墙钟** |
| `agent_activities.timestamp` 最大值 | `2026-07-28 17:01:24` | 同上 |
| `events.timestamp` 最大值 | `2026-07-28 17:01:25` | 同上 |
| `ecosystem_repo_events.triggered_at` 最大值 | `2026-07-21 06:23:56` | 该批扫描发生在本地 14:23（D1 已实证与 events 表秒级对齐）→ **UTC** |
| `ecosystem_repo_profiles.last_scanned_at` 范围 | `07-10 07:06` ~ `07-21 06:23` | 对应本地 15:06 / 14:23 → **UTC** |

**活库的最新行必然只有几分钟龄**，所以"max(值) 更接近本地 now 还是 UTC now"是一个
不可辩驳的判据。平移脚本把这条判据固化成运行时护栏（见 §6.2）。

### 3.4 明确排除项（有理由的不动）

| 对象 | 形态 | 为什么不动 |
|---|---|---|
| `governance_lease.expires_at / updated_at` | VARCHAR，存 `...+00:00` 的 isoformat | **本就是带偏移的 UTC 串**，刻意不走 ORM DateTime（只做字典序比较）。已是目标态 |
| `loop_states.updated_at` | TEXT，198 行 | 退役 cron 引擎遗留，全仓**零写入方**（`check_schema_tables.py` 已登记为 leftover）。死数据，动它没有任何读者受益 |
| `events.data` 内嵌时间戳 | JSON，2266 行含 ISO 串 | 事件载荷是**hook 当时上报了什么**的原样记录，改它等于篡改证词。且键名不定、无 schema |
| `tasks.config.memo` 内嵌时间戳 | JSON，20 行 | CLAUDE.md 明令**冻结档案**（记忆 v2 升表后旧 JSON 保留作历史，不清理不写入） |
| `reports.content` / `task_memos.content` 内正文时间 | Markdown 正文 | 是人写的文字，不是数据 |
| `alembic_version` | 无时间列 | — |

---

## 4. API 契约

**变更**：所有响应里的 datetime 从 `2026-07-28T17:01:24.123456`（裸串，口径靠猜）
变为**自描述**形态。两种拼法都会出现，都带偏移、语义等价：

| 出处 | 形态 | 实测样例 |
|---|---|---|
| FastAPI / pydantic 的 JSON 序列化 | `Z` 后缀 | `2026-07-28T10:01:26.476961Z` |
| 代码里手写的 `.isoformat()`（74 处） | `+00:00` | `2026-07-10T07:06:38.220368+00:00` |

前端 `parseServerTime` 两种都认（`hasOffset` 同时匹配 `Z` 与 `±HH:MM`）。

实现方式：不写任何序列化代码。`UtcDateTime` 读出 aware 值，之后无论走哪条路径都自带
偏移。

**向后兼容性**：这是一个**破坏性契约变更**，任何按裸串解析的消费方都要跟着改。
本仓内的消费方只有两个，都在本次改造范围内：

1. Dashboard（§5）
2. wake watermark 契约（§4.1）

### 4.1 wake watermark 两端同步

| 端 | 改前 | 改后 |
|---|---|---|
| `GET /api/wake/actionable` 返回的 `watermark` | `datetime.now().isoformat()`（本地、无 Z） | `utc_now().isoformat()`（带 `+00:00`） |
| `wake_actionable.parse_since()` | 带 tz 者 `astimezone()` 转本地后去 tzinfo | 一律 `parse_utc()` → aware UTC；不带 offset 按 UTC 读 |
| `scripts/os-watch.sh` 首个 `SINCE` | `date +%Y-%m-%dT%H:%M:%S`（注释写死"绝不用 -u"） | `date -u +...+00:00` |

watcher 本身仍是哑轮询器（原样回传 API 给的 watermark），**串自己带着口径走**，
两端不再各自约定。

> **过渡期已知影响**：改造上线时若有一个 watcher 进程还举着旧的本地 watermark，
> 它会比 UTC 快 8 小时，在这 8 小时内漏报增量。watcher 是会话作用域进程（随会话消亡，
> 且有 1h 硬超时），换一次会话即自愈；无需额外处理，但**部署时应知悉**。

---

## 5. 前端解析策略

### 5.1 现状

28 个组件/页面里散着 73 处 `new Date(...)`，绝大多数直接把后端裸串丢给浏览器按本地解析。
唯一的例外是 `DeepReviewSection.tsx` 里的 `parseAsUtc`——当年为同一个时差打的**单点补丁**，
只修了一个组件。

### 5.2 方案：共享解析层，不是补丁铺开

新建 `dashboard/src/lib/datetime.ts`：

```ts
parseServerTime(s)      // 带 offset → 原样；不带 → 按 UTC。返回 Date | null
serverTimeMs(s)         // → number（NaN 表示不可解析），给排序/差值用
formatDateTime(s, opts) // 本地化显示（浏览器本地时区）
formatTime / formatDate / formatRelative / formatElapsed
```

**为什么不直接把 `parseAsUtc` 提为 util 就完事**：那只解决"怎么解析"，不解决
"73 个调用点各写各的格式化"。统一入口后，"服务端串怎么读"只有一个答案，且下次
改口径只改一处。

**为什么不带 offset 的串仍按 UTC 读**（而不是报错）：平移完成前库里还有裸串，
浏览器可能持有缓存响应，第三方脚本也可能直接打 API。按 UTC 读是与存储约定一致的
唯一自洽解释。

---

## 6. 存量平移

脚本：`scripts/migrate_timestamps_utc.py`，**默认 dry-run**。

### 6.1 平移量

`UPDATE <表> SET <列> = datetime(<列>, '-8 hours') WHERE <列> IS NOT NULL`

作用于 §3.1 的 46 列 / 320,866 个非空单元。Asia/Shanghai 全年恒定 UTC+8（1991 年后
无夏令时），所以 −8h 是**常量换算，不是时区库换算**——不存在夏令时歧义。

### 6.2 列内混口径：逐行判定与排除

平移**按列**做：一个列的写入方在库的绝大部分生命期内口径恒定（核心域一直写本地），
所以不存在"哪天之后的行不动"这种按时间点切的分界。

但**列内不是同质的**。UTC 新代码已在 master 上，而 `aiteam` 是 editable 安装直接指向
仓库工作树，于是**任何在合并之后新起的进程**——新会话拉起的 MCP server、hook 子进程、
`aiteam` CLI、被自动拉活的 API——写进去的都是 UTC。实测（生产库 2026-07-28 18:49 只读
快照）：`events.timestamp` 的 246,409 行里已混入 11 行 UTC 值，rowid `244681` 与
`245787~245792 / 245796 / 245801 / 245805 / 245810`，每行相对写入序前缀的最大值恰好倒退
8.00 小时，且与本地行毫秒级交替出现（`245786`=18:24:15 本地 → `245787`=10:24:19 UTC →
`245793`=18:24:21 本地）——同一列上有两个并发写入方。**污染量随时间单调增长**，所以
检测必须在 `--apply` 执行的那一刻对全库现算，任何固定行号清单都是过期的。

这也是为什么"停掉 API 就没有写入方"这个运维前提不成立：系统内建多条把 API 自动拉活的
路径（MCP server 启动、`ai-team-os-serve` 入口、plugin bootstrap、`os_restart_api` 工具、
`/os-up`、CLI `up`），`aiteam` CLI 与若干 `scripts/` 还会绕开 API 直连生产库。真正兜底的
不是操作纪律，而是下面这套逐行检测。

**判据的骨架是一条硬约束**：一个 UTC 写入的值 `v`，其真实时刻是 `v + 8h`，而这个真实
时刻必须落在 `[T0, now]` 之内（`T0` = 新代码最早可能运行的时刻）。反过来说——

> 只有落在 **`[T0 − 8h, now − 8h]`** 这条**风险带**里的值才可能是 UTC 写的。

带外的值一律**被证明**为本地口径，照常平移；带内的值再逐行判：

| 列类 | 预言机 |
|---|---|
| 追加型列（每表一个"出生列"，插入时写定、此后不再更新） | rowid 即物理写入序：下界取前面所有带外行的最大值，上界取后面第一个带外行的值，再叠加同行"后继列"（`AFTER_BIRTH_COLUMNS`：结构上保证不早于创建的列）给出的上界；把 `v` 与 `v + 8h` 两种读法分别套进去，只有一种自洽才定案 |
| 原地更新列（`updated_at` / `last_active_at` 之类，rowid 与值的时序脱钩） | 拿同行的出生列当锚，用"**更新不可能早于创建**"这条不变量判：更新值比创建时刻早了将近一个时区，就只能是 UTC 口径 |

两种读法都自洽、或都与证据冲突的行，**逐行进 journal 的 undecidable 清单交人审**，
必须显式 `--ack-undecidable` 才放行——**绝不静默平移**。`T0` 由库内取证自动推断（也可用
`--utc-writer-since` 按新代码合入时刻指定）；每列的候选数、排除 rowid 集合、待人审清单
全部进 dry-run 报告与 journal。

**容差按分钟给（5 分钟），不是按小时**：生产库 `task_memos.created_at` 存在 7.1~8.7 小时
的**合法**乱序（记忆 v2 升表时的批量回填），小时级容差会把这批回填行当成时区指纹整批
误排；`memories.created_at` 更有 3143 小时的乱序（Windows 库迁入行）。

`AFTER_BIRTH_COLUMNS` 是白名单而不是黑名单，因为反例是实打实存在的：
`workflow_runs/agents` 的 `started_at` / `queued_at` / `completed_at` 来自 CC 状态文件的
epoch，实测最早到 2026-06-23，比该行 `created_at`（2026-07-06）早好几周；
`scheduled_tasks.next_run_at` 干脆记的是未来时刻。把它们当成"不早于创建"的列，预言机就
是在对着一条假不变量下判断。

#### 全库护栏

护栏放在**库级**而不是列级——逐列去猜只会对冷数据制造假警报（实测：`teams.created_at`
最新行是 7 小时前的 10:24，逐列判据会把它误判成"已是 UTC"）。判据由两层组成：

1. **口径见证**：取**最新鲜的那个追加型列**，看它最近 200 行的**中位数**落在哪一侧。
   * 只有追加型列有资格作证——它的 rowid 就是写入序，"最近这批写入"这个概念对它才成立。
   * 用中位数而不是 `max`。`max` 天生只反映极少数最新行：库已整体平移成 UTC、旧代码又
     补写了一行本地值时，那一行就足以让 `max` 贴住本地此刻——这正是"已平移的库"会被判成
     "可以平移"的机理（已故障注入复现）。中位数要求**多数**近期写入同口径，少数派掀不翻它。
   * 正面确证仍是单边无歧义的：UTC 时钟**写不出未来的值**，所以见证中位数晚于 UTC 此刻就
     必然是本地墙钟写的。
2. **混口径确证**：混口径不再是"放行/拦停"的二元判断——检测与排除方案本身处理混杂，护栏的
   职责变成确证"污染行已被识别且将被排除"（逐列报告排除行数），以及"undecidable 非空时必须
   `--ack-undecidable` 才放行"。后者 `--force` **越不过去**。

逐列仍然逐条报告"确证本地 / 冷数据无法自证"，但不再单独否决。

**故障注入已覆盖**（`tests/unit/test_migrate_timestamps_utc.py`）：少数派 UTC 混入被逐行
排除、已平移库 + 1 条本地新行不再放绿、合法乱序回填不被误认成时区指纹、双 apply 被标记
拦下、journal 已存在被拒、排除行在 rollback 时同样不被 +8h。

#### 只做一次

* **幂等标记**：`--apply` 成功后在库上写 `PRAGMA user_version = 20260728`（选它是因为侵入
  最小：不建表、不加列、不产生任何业务可见对象，且本仓 storage 层从不读写它）。再次
  `--apply` 硬拒；`--rollback` 校验标记存在才执行，执行后清零。
* **journal 防覆盖**：`--apply` / `--rollback` 强制要求 `--journal`，且目标文件已存在即
  拒绝执行。journal 是唯一的恢复凭证（含逐行排除清单），被二次 apply 覆盖成"平移后状态"
  之后，`--rollback` 会对着被污染的基线报"复核全过"，而库实际仍整体偏早 8 小时。

### 6.2.1 平移是无损的

`datetime(col, '-8 hours')` 会把结果**截到整秒**——那等于顺手抹掉 32 万行的微秒位，
而事件账本按 `timestamp` 排序，同一秒内的先后就靠那几位。脚本改为只搬"到分钟"的
前缀、秒与亚秒位原样接回：

```sql
substr(datetime(substr(col,1,16) || ':00', '-480 minutes'), 1, 16) || substr(col, 17)
```

位移量必然是整分钟（现存所有时区偏移都是整分钟，含 +05:30 / +05:45），所以这个切法
对任何时区都成立。执行前另有一道 `assert_uniform_width`，两道检查：宽度短于
`YYYY-MM-DD HH:MM` 的值会被前缀切分毁掉；更重要的是**直接判据**——拿真正要执行的表达式
跑一遍，凡是"原值非空但结果为 NULL"的行都会被静默清空，遇到就拒绝执行。宽度够并不等于
能解析（纪元浮点串、非法月日、斜杠格式都长于 16 位却照样被清成 NULL），所以宽度是必要
不充分条件，真正管用的是后一条。`--apply` 之后另有一道非空行数回检：`rowcount` 只数匹配
行，数不出"被清成 NULL"这件事；对不上就抛异常，此时事务尚未提交，库保持原状。

**往返已验证逐字节相等**：`--apply` 后再 `--rollback`，四张表各取 200 行比对，值完全一致。

### 6.3 冻结对象在平移范围内

`tasks` 表的 `created_at/started_at/completed_at` 三列**在平移范围内**（列本身不是
冻结对象，冻结的是 `tasks.config.memo` 这个 JSON 字段）。同理 `task_memos` 表的
DATETIME 列要平移，而 `tasks.config` 里的旧 memo JSON 不动——这会造成一个**已知且
可接受的不一致**：旧 JSON 档案里的时间戳仍是本地，与升表后的 `task_memos.created_at`
（平移后 UTC）差 8 小时。理由：那份 JSON 是历史档案不是活数据，且 CLAUDE.md 明令不再写入。
本文档在此显式登记该不一致，避免后人当作 bug 去"修"。

平移前脚本对**每一个**受影响列留档：行数、min、max（`--journal <path>` 落 JSON），
冻结相关表同样留档。

### 6.4 dry-run 输出

每列一行：表 / 列 / 非空行数 / **排除行数** / 平移前 min-max / 平移后 min-max，外加 3 行
抽样前后对照。随后是**逐行口径判定**栏：取证基准 `T0`、每列的候选行数 / 排除 rowid 集合 /
待人审行（逐行给出 rowid、值与判不了的理由）。末尾给总计与护栏结论。

dry-run 给 `--journal` 时会把完整逐行清单落盘（`mode: "dry-run"`）供人审——它同样受
"文件已存在即拒绝"约束，所以人审用的 dry-run 留档要和正式 apply 的 journal 分开命名。

### 6.5 执行纪律

- `--apply` **由缔造者亲自执行**，本次改造的实施方绝不执行（任务书铁律）。
- 新代码已在 master 且 `aiteam` 是 editable 安装，所以"先平移、后部署"这个顺序**已经不
  可能**满足：平移窗口内新起的任何进程都在写 UTC 行。这不再由运维纪律兜底，而是由 §6.2
  的逐行检测兜底——窗口内写进去的 UTC 行会被识别并排除。
- 执行顺序：
  1. 备份：`cp ~/.claude/data/ai-team-os/aiteam.db ~/aiteam.db.bak-utc-$(date +%Y%m%d%H%M%S)`
  2. dry-run 留档并**人审 undecidable 清单**：
     `--db <备份副本> --journal ~/utc-dryrun-<ts>.json`
  3. 尽量减少写入方（关掉多余 CC 会话、不主动重启 API）。注意这只是降噪，不是保证：
     多条自动拉活路径 + CLI / scripts 直连都绕得过去。
  4. `--apply --journal ~/utc-migration-<ts>.json`（journal 必须是**新文件名**；若第 2 步
     确有判不了的行，追加 `--ack-undecidable`）
  5. 复核 journal 与脚本的平移后 min/max，确认排除行清单与 dry-run 一致
  6. 重启 API 并**确认旧进程确已死透**——两侧 `__version__` 相同、`/api/health` 不含 commit
     hash，autostart 会把还活着的旧进程直接 adopt，它会继续往已平移的库里写本地行（方向与
     平移相反，且不会告警）。判据：新写入行的时间戳应等于 UTC 此刻而非本地此刻。

> **⚠️ 代码与数据必须成对上线。** 这不是理论风险，已实测：把新代码接到**未平移**的
> 库上，一支本地 10:24 建的队，API 给出 `2026-07-28T10:24:46Z`，浏览器渲染成
> **18:24** —— 整整晚 8 小时，且页面看不出任何异常。
>
> 换句话说：**代码合并后、平移执行前的这段窗口里，核心域时间在界面上全是错的**
> （方向与用户原先反映的 ecosystem 偏早相反，是偏晚）。所以合并与 `--apply` 应当
> 连着做，中间不要长时间停留，更不要在这段窗口里对着界面判断时间对错。

### 6.6 回滚

平移是**纯值变换、可逆**：`--rollback` 对同一列集做 `+8 hours`，用同一份 journal 校验
（回滚后行数与 min/max 必须与 journal 记录的平移前状态逐列相等）。三点约束：

- 位移量取 journal 记录的 `shift_hours`，不按当前宿主时区现算；journal 里的 `db` 路径不匹配
  即拒绝执行——张冠李戴的回滚会按错误量搬动数据。
- **排除清单只认 journal**：平移之后库里已经看不出谁是谁了，所以哪些行当初没被 `−8h`，
  回滚时就同样不 `+8h`。
- 回滚要求库上带平移标记，执行成功后把标记清零。

三层回退，按代价从低到高：

| 层 | 手段 | 代价 |
|---|---|---|
| 数据 | `--rollback`（或直接恢复备份） | 秒级 |
| 代码 | `git revert` 本分支的实施 commit | 分钟级 |
| 组合 | 恢复备份 + revert + 重启 API | 回到改造前 |

**回滚窗口的约束**：一旦新代码上线并开始写 UTC 行，单独回滚数据（不回滚代码）会造成
新旧混杂。所以回滚必须**数据与代码成对**进行。

---

## 7. 验收清单

| # | 项 | 判据 |
|---|---|---|
| 1 | 单元/集成测试 | `python3 -m pytest tests -q` 全绿，且通过数不低于改造前基线（2026 passed / 3 skipped @ af1dd66） |
| 2 | 红线机检 | `bash scripts/check_invariants.sh` I1–I10 全绿（含 hook 双副本、双 dist 一致） |
| 3 | 时钟约定机检 | 新增 I11：`src/` 内除 `clock.py` 外零处裸 `datetime.now()` / `datetime.fromtimestamp(` |
| 4 | 列类型全覆盖 | 测试断言 `Base.metadata` 内每个 DateTime 列都是 `UtcDateTime` |
| 5 | 往返口径 | 写 aware `+08:00` → 读回 aware UTC 且**时刻不变**；写 naive → 读回 aware UTC 且**值不变** |
| 6 | API 契约 | 真实路由响应里的时间串带 `+00:00` |
| 7 | watermark 契约 | `parse_utc(watermark)` 往返恒等；`os-watch.sh` 首个 SINCE 带偏移 |
| 8 | 前端构建 | `npm run build` 通过；`dashboard-dist` 双份一致（I4） |
| 9 | 前端活体 | ecosystem 页时间**不再偏早 8 小时**；核心页时间**不回退** |
| 10 | 平移脚本 | dry-run 全表报告可读；护栏在库已是 UTC 时确实中止；往返逐字节相等 |
| 11 | 混口径逐行判定 | dry-run 对生产库副本点名排除已是 UTC 的行；undecidable 清单逐行可审；双 apply / journal 覆盖被硬拒 |

### 7.1 活体回归实录（隔离实例，2026-07-28）

隔离实例 = 生产库只读副本 → 平移 → `AITEAM_DB_PATH` 指向它的独立 API（:8931）+
dashboard dev server（:8932，浏览器时区固定 Asia/Shanghai）。取证时刻本地 18:0x /
UTC 10:0x。

| 页面 | 观测 | 判定 |
|---|---|---|
| 生态档案 | 批次扫描记录显示 `2026/07/10 16:06`；库内该值为 `08:06:01`（UTC） | ✅ 08:06 UTC + 8h = 16:06 本地。**改造前此处显示 08:06，即用户反映的偏早 8 小时** |
| 活动分析 · 小时桶 | API 给 `03:00/04:00/06:00/07:00/08:00/09:00 +00:00`，页面渲染 `11时/12时/14时/15时/16时/17时` | ✅ 逐桶 +8，无平移、无错位 |
| 事件日志 | 最新行显示"6 分钟前"（该行由隔离实例本身在 UTC 10:01 写入，截图于 10:07） | ✅ 相对时间无 8 小时偏差 |
| 项目详情 | 创建时间 `2026/7/6`；成员卡片"6 天前 / 1 小时前 / 1 天前" | ✅ 与实际相符 |
| 控制台 | 零时间相关报错（仅存量 Base UI / React key 警告） | ✅ |

---

## 8. 已知遗留与后续

1. **JSON 内嵌时间戳**（`events.data` 2266 行）不平移，见 §3.4。若将来要按事件载荷内的
   时间做分析，需先明确该字段的口径。
2. **`tasks.config.memo` 与 `task_memos` 表的 8 小时错位**，见 §6.3，已登记非 bug。
3. **`loop_states`** 死表未平移。若哪天要复活 cron 引擎，先补平移。
4. **前端时区**：目前一律按浏览器本地时区显示。若将来要支持"固定按某时区显示"，
   在 `datetime.ts` 一处加参数即可——这正是收敛到单一解析层的收益。
5. **原地更新列的 undecidable 残量**：`agents.last_active_at` 这类列没有上界不变量可用
   （没有哪一列结构上保证晚于它），落在风险带里的行只能靠人审。风险带宽度 = 从 `T0` 到执行
   时刻，所以**拖得越久要人审的行越多**——这是催促尽快执行 `--apply` 的机制性理由。
6. **`ROW_BIRTH_COLUMN` / `AFTER_BIRTH_COLUMNS` 与 ORM 是同一份事实**：哪列插入即定、哪列
   结构上不早于创建，改 ORM 时必须同步这两张表，否则预言机会对着假事实判断。测试已断言出生
   列排在同表首位（原地更新列拿它当锚，必须先被判定）。
7. **`SQLite CURRENT_TIMESTAMP` 是第三个墙钟**：`scripts/cleanup_orphan_pipeline_subtasks.py`
   用它写 `completed_at`，而 SQLite 的 `CURRENT_TIMESTAMP` 求的是 UTC。§1 的两域划分没登记
   它；这类直连脚本要么改走 `aiteam.clock`，要么在各自文件里显式登记口径。
