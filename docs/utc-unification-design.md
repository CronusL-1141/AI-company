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

### 6.2 分界判定与运行时护栏

分界不靠"按时间点切"，而是**按列**：一个列的写入方在整个库生命期内口径是恒定的
（核心域从来只写本地，ecosystem 域从来只写 UTC），所以整列平移，不需要"哪天之后的行不动"。

> **为什么不存在"D1 合并后新代码已写入部分 UTC 行"这个边界情况**：本次改造的新代码
> **只在本 worktree 分支上**，从未合并、从未被生产 API 进程加载。生产 API 现在跑的
> 仍是旧代码（核心域写本地）。取证时刻的实测复核了这一点——`agents.last_active_at`
> 与 `events.timestamp` 的最大值都是 `17:01`（本地此刻），若已有 UTC 行混入，最大值
> 附近会出现 `09:0x` 的簇。**没有这样的簇。** 换言之：只要 `--apply` 在"新代码上线并
> 重启 API"**之前**执行，全列同质，无分界问题。

因此脚本内置一条**硬护栏**（`--apply` 时强制中止，dry-run 时报告）。护栏放在
**库级**而不是列级——真正的风险是部署顺序颠倒，而部署是全局的：新代码一旦跑起来，
所有核心域列一起改口径。逐列去猜只会对冷数据制造假警报（实测：`teams.created_at`
最新行是 7 小时前的 10:24，逐列判据会把它误判成"已是 UTC"）。

判据由两条组成：

1. **正面确证（单边、无歧义）**：UTC 时钟**写不出未来的值**，所以只要有任何一列的
   `max` 晚于 UTC 此刻，该列就必然含本地墙钟写入的行。生产库实测 46 列中 **27 列**
   给出该确证。
2. **全库最新写入的落点**：活着的系统里"最近一次写入"必然贴着当前时刻。贴本地 now
   ⟹ 写入方仍是旧代码（可平移）；反而贴 UTC now ⟹ 新代码已在写库，**中止**。

逐列仍然逐条报告"确证本地 / 冷数据无法自证"，但不再单独否决。

**护栏已做故障注入验证**：对已平移过的副本再跑一次，护栏正确拦下（27 列确证归零，
全库最新写入落到 UTC 侧）。

### 6.2.1 平移是无损的

`datetime(col, '-8 hours')` 会把结果**截到整秒**——那等于顺手抹掉 32 万行的微秒位，
而事件账本按 `timestamp` 排序，同一秒内的先后就靠那几位。脚本改为只搬"到分钟"的
前缀、秒与亚秒位原样接回：

```sql
substr(datetime(substr(col,1,16) || ':00', '-480 minutes'), 1, 16) || substr(col, 17)
```

位移量必然是整分钟（现存所有时区偏移都是整分钟，含 +05:30 / +05:45），所以这个切法
对任何时区都成立。执行前另有一道 `assert_uniform_width`：任何短于 `YYYY-MM-DD HH:MM`
的值都会让前缀切分把数据清成 NULL，遇到就拒绝执行。

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

每列一行：表 / 列 / 非空行数 / 平移前 min-max / 平移后 min-max，外加 3 行抽样前后对照。
末尾给总计与护栏结论。

### 6.5 执行纪律

- `--apply` **由缔造者亲自执行**，本次改造的实施方绝不执行（任务书铁律）。
- 执行前：新备份 `cp ~/.claude/data/ai-team-os/aiteam.db ~/aiteam.db.bak-utc-<ts>`。
- 执行顺序（**顺序不可颠倒**）：
  1. 停/不重启生产 API（保证不再有旧代码写本地行）
  2. 备份
  3. `--apply --journal ~/utc-migration-journal.json`
  4. 复核 journal 与脚本的平移后 min/max
  5. 部署新代码 + 重启 API
- 若顺序颠倒（先部署新代码再平移），新代码写入的 UTC 行会被再减 8 小时。§6.2 的护栏
  正是为拦住这种情况而存在。

> **⚠️ 代码与数据必须成对上线。** 这不是理论风险，已实测：把新代码接到**未平移**的
> 库上，一支本地 10:24 建的队，API 给出 `2026-07-28T10:24:46Z`，浏览器渲染成
> **18:24** —— 整整晚 8 小时，且页面看不出任何异常。
>
> 换句话说：**代码合并后、平移执行前的这段窗口里，核心域时间在界面上全是错的**
> （方向与用户原先反映的 ecosystem 偏早相反，是偏晚）。所以合并与 `--apply` 应当
> 连着做，中间不要长时间停留，更不要在这段窗口里对着界面判断时间对错。

### 6.6 回滚

平移是**纯值变换、可逆**：`--rollback` 对同一列集做 `+8 hours`，用同一份 journal 校验
（回滚后行数与 min/max 必须与 journal 记录的平移前状态逐列相等）。

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
