# token 用量归因 v1 设计

**状态**：Proposed（设计先行，过审后另起实施）
**任务**：b1a1df19-36ff-44ef-bb71-6f92abd3ea06
**代码基线**：master@2b651f8
**取证时间**：2026-07-29（生产库全程只读 `mode=ro`，未做任何写入）

---

## 0. 摘要与三条不可协商前提

### 0.1 一句话

把"哪一段工作烧了多少 token"从**文件真相**（transcript）算出来，按 project / session / workflow / agent 逐级归因；**归因覆盖率与口径标签是数据本身的一部分**，不是页面上的一行小字。

### 0.2 本设计与既有认知的最大差异（请先读这一节）

立项时引用的核心数字——"1.862 亿 token 已经躺在库里，只差展示层"——**口径是错的**。

`workflow_agents.tokens` 不是计费口径。真相源在 `src/aiteam/api/workflow_ingest.py:220` 的 `_last_assistant_ctx_tokens`，其注释写得很清楚：

> D1 裁决口径：input + cache_creation_input + cache_read_input + output 四字段和（与 wf_\<id\>.json 终态 per-agent tokens 直接对账：error agent 精确相等，done agent 仅偏高 3~12%）。**否决跨轮累加（cache_read 重复计入膨胀 ~445 倍）**。

也就是说它取的是**最后一条 assistant 消息的四字段和 = 一个瞬时上下文水位快照**，不是这个 agent 一共消耗了多少。而 `src/aiteam/services/token_attribution.py` 的 `parse_transcript_usage` 走的是**跨 requestId 累加**，是真正的用量口径。两者语义正交。

同一批 agent 的实测对照（6 个既有 `workflow_agents.tokens` 又已被 D1 采到五列的行）：

| agent | `workflow_agents.tokens`（水位快照） | 四层累加（用量口径） | 倍数 |
|---|---:|---:|---:|
| wf-abc2a662bb | 117,800 | 632,504 | 5.4x |
| wf-a707a925bc | 133,177 | 1,471,825 | 11.1x |
| wf-ac51f07a6c | 119,007 | 1,551,237 | 13.0x |
| wf-a4729e6909 | 111,311 | 2,195,791 | 19.7x |
| wf-af2d08f9a2 | 152,050 | 3,445,696 | 22.7x |
| wf-ad1bd33148 | 127,089 | 3,236,088 | 25.5x |

**结论**：两个口径差 5~25 倍（11 行合计层面 42 倍）。把它们相加、并列、或放进同一个"总 token"，就是本仓刚刚在时间戳上栽过的同类事故——混口径。本设计因此把「口径」提为与「覆盖率」并列的一等维度：**任何一个 token 数值，脱离口径标签就没有意义**。

### 0.3 三条不可协商前提

| # | 前提 | 落法 | 来源 |
|---|---|---|---|
| P1 | **命名纪律**：一切呈现只以 token 用量表达，不做任何跨量纲换算 | 用**量纲白名单机检**（§4.4）而非人工自觉 | 会议决议 |
| P2 | **覆盖率一等公民**：未归因部分必须如实呈现，禁止局部冒充全貌 | 落在**类型层**（§2.5），页面物理上拿不到孤立总量 | 会议决议 + 纪律① no-data≠zero |
| P3 | **无定时器/后台守护** | 全部按需触发：hook 事件点 + 显式工具调用 + 惰性翻滚 | 刻意决策 |

补充两条从取证中长出来的前提：

| # | 前提 | 理由 |
|---|---|---|
| P4 | **不新建表** | I10 机检"ORM 声明但库中缺失 = FAIL"，而 `create_all` 只在启动时跑、生产实例不能为建表重启。`request_ledger.py` 已确立此先例：用 events 承载聚合、用扩列承载事实 |
| P5 | **不写死型号，只观测回填** | 2026-07-07 刻意决策。别名解析只在读侧兜底，绝不回写 `model` 列（`MODEL_ALIAS_LEDGER` 已按此实现） |

---

## 1. 术语与口径定义（本设计的地基）

### 1.1 两个正交口径

| 口径 id | 名称 | 定义 | 现有产出者 | 用途 |
|---|---|---|---|---|
| `usage_sum` | 用量累加 | 一份 transcript 内，按 `requestId` 分组、每组取**最后一条** usage 快照、再跨组累加的四层 token | `token_attribution.parse_transcript_usage` | 回答"这段工作一共用掉多少" |
| `ctx_last` | 末轮上下文水位 | 最后一条 assistant 消息的 `input + cache_creation + cache_read + output` | `workflow_ingest._last_assistant_ctx_tokens` → `workflow_agents.tokens` | 回答"这个 agent 结束时上下文有多满" |

另有一个已存在但不参与本设计的第三口径：`agents.ctx_tokens / ctx_pct`（上下文水位百分比，`agent_context.measure` 产出），用于 agent 复用治理，与用量归因无关。**三者互不相加。**

### 1.2 四层分列是硬要求，不是可选项

`usage_sum` 的四层在实测中极不均衡（11 行已采样本）：

| 层 | 量 | 占比 |
|---|---:|---:|
| `cache_read_tokens` | 78,866,965 | **95.6%** |
| `cache_creation_tokens` | 3,019,782 | 3.7% |
| `output_tokens` | 605,669 | 0.7% |
| `input_tokens` | 1,717 | 0.0% |

非缓存部分（input + output）只占 **0.74%**。

含义：任何"总量"数字实际上是"缓存读取量"的同义词。若只呈现总量，跨模型、跨派工路径、跨阶段的比较全部被缓存读主导，结论会系统性失真。**因此四层必须始终可分列，"总量"不得作为唯一呈现，也不得作为默认排序键。**

### 1.3 一处待修缺陷

`parse_transcript_usage` 取 model 时不过滤 compact 合成行。实测对一份 35.1 MB 的主会话 transcript 解析，返回的 `model` 是 `<synthetic>`。而 `session_probe.read_session_model` 是正确的——它显式跳过 `SYNTHETIC_MODEL`。

子 agent transcript 一般不含合成行，所以 D1 的既有路径没被咬到；但主会话采集（§3.3）一上就会踩。**修法**：`parse_transcript_usage` 复用 `session_probe` 的同一常量与跳过逻辑，不再各写一份。

---

## 2. 数据模型与归因链

### 2.1 归因链的真实形状

```
                    ┌──────────────────────────────────────────┐
                    │  transcript 文件（唯一权威用量来源）       │
                    │  ~/.claude/projects/<slug>/<session_id>/  │
                    │       subagents/[workflows/<wf_id>/]      │
                    │            agent-<cc_id>.jsonl            │
                    └────────────────────┬─────────────────────┘
                                         │ 解析（路径 + 内容）
             ┌───────────────┬───────────┼───────────┬──────────────┐
             ▼               ▼           ▼           ▼              ▼
        project_id      session_id     wf_id     agent 行      四层 token
             │               │           │           │              │
             │               │           │           │              └─ 落 agents 五列
             │               │           │           └─ agents.id（cc_tool_use_id 匹配）
             │               │           └─ workflow_runs.wf_id → workflow_agents
             │               └─ 会话容器队 teams.config.owner_session_id
             └─ projects.root_path（slug 反查仅作校验）

                                    ┆ 弱边（当前几乎无数据，见 §2.4）
                                    ▼
                                  task
```

**关键洞察：链路的前四段不需要新增任何采集，它们全部编码在 `transcript_path` 里。**

实测路径样本：

```
~/.claude/projects/<project-slug>/80d0cc5e-.../subagents/agent-a672b51d.jsonl
~/.claude/projects/<project-slug>/80d0cc5e-.../subagents/workflows/wf_8cd4fced-95a/agent-af2d08f9.jsonl
```

对生产库中 1,922 个已登记 `transcript_path` 做正则解析，**成功率 100%**（其中 94.5% 带 `wf_id`，覆盖 12 个 session、6 个 project slug）。这与 `hook_translator._extract_workflow_run_id` 已在用的招式同源——那里只提了 `wf_id`，本设计把同一条信息用满。

### 2.2 现有实体的边与实测覆盖（这是排优先级的依据）

| 边 | 承载字段 | 实测覆盖 | 判定 |
|---|---|---|---|
| agent → team | `agents.team_id` | 2,450 / 2,450 = **100%** | 可用 |
| agent → project | `agents.project_id` | 2,404 / 2,450 = **98.1%** | 可用 |
| agent → session | `agents.session_id` | **1 / 2,567** | **形同虚设**，改由路径派生 |
| agent → workflow | `workflow_agents.os_agent_id` | 2,331 / 2,450 = 95.1% | 可用 |
| agent → transcript | `agents.transcript_path` | 1,922 / 2,450 = **78.4%** | 可用（且抽样 150/150 文件仍在磁盘） |
| team → task | `tasks.team_id` | **1 / 161** | **不可用** |
| agent → task | `agents.current_task` | **0 / 2,567** | 死字段 |
| agent ↔ task（证据） | `task_memos.author` × `task_id` | 306 对；但只有 19 个 agent 行名字能匹配、12 个唯一对应 | 弱 |

两条必须说破的事实：

* **`agents.session_id` 是空的。** 全表 2,567 行仅 1 行非空，117 个 Leader 行全部为 NULL。`hook_translator.py:1904` 的注释也承认存在"session_id was cleared/never set"的行。所以决议里写的 `session → agent` 这一跳，**在 DB 里当前不存在**。
* **`task` 边基本不存在。** 248 个 team 里只有 1 个挂了 task；落在"有 task 的 team"上的子 agent 行数是 **0**。`tasks.assigned_to` 存的是 agent **名字符串**（`'tagger-fix'` / `'leader'`）而非 id，且只有 28/161 行有值。

### 2.3 派工时刻拿不到 task 绑定（为什么不能靠"加一列"解决）

`SubagentStart` 的 payload 里只有 `agent_id` / `agent_type` / `session_id` / `cc_team_name`（见 `hook_translator._on_subagent_start`），**没有 prompt、没有任何任务标识**。也就是说：

> 在 agent 出生的那一刻，OS 无从得知它是为哪个 task 干活的。

因此"给 `agents` 加一个 `task_id` 列"这个直觉方案是错的：**这一列采不到值，会长期为 NULL，而 NULL 在归因语境里极易被读成"这段工作没花 token"**——正好是 no-data≠zero 纪律点名要防的形态。加一个填不上的列，比不加更糟。

### 2.4 task 边的正确解法：寄生在既有记账行为上

约束很硬：内部数据显示 22 天内 `task_memo_add` 被调 711 次、`task_memo_read` 240 次、`task_update` 179 次，而"任何需要人主动多调一次工具的设计都很难存活"已被反复验证。所以**不能新增一个"绑定 agent 与 task"的动作**。

方案：**在已经必然发生的记账行为里顺手把边写下来。**

* 触发点：`task_memo_add` / `task_update` / `report_save` —— 这三个工具的调用参数里**天然同时带着 `task_id` 与 `author`（agent 名）**。
* 动作：MCP/API 服务端在处理这三个调用时，若能把 author 解析到一个 agent 行，就写一条 `agent --worked_on--> task` 的边。
* 存储：复用**已存在**的 `knowledge_links` 表（`from_kind/from_id/to_kind/to_id/link_type` + 五元组 UNIQUE 去重 + append-only），新增 `from_kind='agent'`、`link_type='worked_on'`。**不新建表**（满足 P4），也不改任何 schema。
* 语义诚实：这条边是"这个 agent 在这个 task 上留过账"，不是"这个 agent 的全部 token 都属于这个 task"。一个 agent 跨多个 task 时按边的时间序切分区间；切不开的部分**如实计为"task 级未归因"**，不做平均分摊——分摊会造出无法证伪的数字。

**v1 对 task 级归因的承诺必须收窄到**：提供 task 级归因的**数据结构与推导器**，并如实报出其覆盖率（当前基线接近 0，见 §4.3）。这与"差异化表述降级为『数据结构已就位』"的会议裁决一致。

### 2.5 类型层护栏：让"拿到孤立总量"在物理上不可能

这是本设计里唯一一条**必须落在类型层、不接受落在视图层**的约束。理由已在会议上被确认：页面标注是软约束，三个月后的自己会忽略它。

storage 层只暴露一个返回结构，**不提供任何返回裸 total 的接口**：

```python
@dataclass(frozen=True)
class TokenAttribution:
    """一次归因查询的完整答案 —— 数值与其分母、口径同生共死。

    没有 `total` 这个字段，也没有任何方法返回它。调用方要渲染数值，
    就必须同时拿到 dispatches_total 与 metric —— 分母和口径是数据的
    一部分，不是可选的装饰。
    """
    scope: AttributionScope        # project / session / workflow_run / agent / task
    scope_id: str
    metric: TokenMetric            # usage_sum | ctx_last —— 强制标注，无默认值
    input_tokens: int              # 四层分列，不提供合计字段
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    dispatches_attributed: int     # 分子：本 scope 内已测到用量的派工数
    dispatches_total: int          # 分母：本 scope 内的派工总数
    unattributed_reasons: dict[str, int]   # 未归因派工按原因分类计数（§3.4）
    measured_window: tuple[datetime, datetime] | None
    method: AttributionMethod      # transcript_parse | self_report | alias_fallback
```

配套契约测试（属于验收条件，不是可选）：

1. `TokenAttribution` **不存在** `total` / `total_tokens` 字段或属性；
2. 任何 API 响应体中，token 数值字段必与 `dispatches_total`、`metric` 同层出现，缺一即测试失败；
3. `metric` 无默认值——不标注口径就构造不出对象。

### 2.6 schema 变更清单（极小）

| 变更 | 表 | 说明 | 铁律 |
|---|---|---|---|
| 回填（非加列） | `agents.session_id` | 由 `transcript_path` 派生补齐，历史行一次性回填 + 新行在 SubagentStop 顺手写 | — |
| 回填（非加列） | `agents.transcript_path`（Leader 行） | 当前 0/117，由 `session_probe` 按 slug + session_id 反查补齐 | — |
| **加 1 列** | `agents.tokens_source` `VARCHAR(20)` | 取值 `transcript` / `alias_fallback`；用于审计"这行的数是怎么来的"。NULL = 未采集 | **必须同步 `COLUMNS_TO_ENSURE`**（ORM 加字段必须同步迁移，项目铁律） |
| 复用（非建表） | `knowledge_links` | 新增 `from_kind='agent'` + `link_type='worked_on'` 边 | 无 schema 变更 |
| **不动** | `workflow_agents.tokens` | 它是 `ctx_last` 口径，保持原样。回采得到的 `usage_sum` **绝不写进这一列** | 见 §6 |
| **不加** | `agents.task_id` | 采不到，见 §2.3 | — |
| **不建** | 任何新表 | P4 | — |

`tokens_source` 是本设计新增的唯一一列。加它的理由：`method` 要能被审计（"这个数是从 transcript 定真的，还是别名兜底推的"），而这个事实必须随行持久化，不能每次查询重算。加列的代价是一条 `COLUMNS_TO_ENSURE` 条目 + ORM + types + 双向转换四处同步——D1 的五列已经演示过完整招式，照抄即可。

---

## 3. 采集缺口分析

### 3.1 当前采集面全景

| 采集面 | 承载 | 口径 | 实测覆盖 | 缺口性质 |
|---|---|---|---|---|
| 子 agent（SubagentStop） | `agents` 五列 | `usage_sum` | **11 / 2,450 = 0.4%** | D1 于 2026-07-28 16:26 上线，只覆盖此后新派工 |
| workflow agent（wf JSON / journal） | `workflow_agents.tokens` | `ctx_last` | 2,909 / 3,182 = 91.4% | 口径不同，不可与上一行相加 |
| workflow run | `workflow_runs.total_tokens / live_tokens` | `ctx_last` 聚合 | 226 / 228 | 同上 |
| Leader 主会话 | **无** | — | **0 / 117** | 完全空白，见 §3.3 |
| agent 工具活动 | `agent_activities` | **无 token 字段** | 0 / 52,119 | 结构性缺失，见 §3.2 |

### 3.2 `agent_activities` 无 token：**建议不补**

52,119 条活动记录一条 token 都没有，看起来是最大的窟窿。但从架构上讲，**这个洞不该在这里补**：

* `agent_activities` 是**逐工具调用**粒度，而 token 计费是**逐 API 请求**粒度，两者不是一一对应（一次工具调用可能横跨多个请求，一个请求也可能不含工具调用）；
* hook payload 里不带 token，要按活动归因就得对每条活动去反查 transcript，写放大巨大且必然引入估算；
* agent 级的用量已由 `agents` 五列覆盖，而"这个 agent 花在哪个工具上"这个问题，用 `duration_ms` 已经能回答八成。

**结论**：`agent_activities` 保持无 token 字段，在覆盖率呈现里明确标注"工具调用级用量：设计上不采集"。这是一个**主动选择**，不是遗漏——写进文档以免半年后有人当成 bug 来"修"。

### 3.3 Leader 主会话：最大的真实缺口，且可解

**现状**：117 个 Leader 行，token 五列全空，`transcript_path` 也全空。

**可行性实测**（本次直接跑了）：

* 现有 `parse_transcript_usage` **无需修改**即可解析主会话 transcript；
* 一份 35.1 MB 的主会话 transcript，**全量解析耗时 158 ms**，得到 2,012 次 API 调用、累计 852,524,309 token（其中 `cache_read` 811,684,198）；
* 主会话 transcript 由 `session_probe` 按 `<project-slug>/<session_id>.jsonl` 定位，机制已存在（`session_last_active` 就是这么做的）；`SessionStart` 与 `Stop` 的 payload 都带 `transcript_path`。

**三个必须写进实现的陷阱**：

1. **必须覆写，不能累加。** 主会话 transcript 是累计文件，每次解析得到的是"从会话开始到此刻"的总量。落库语义必须是 **snapshot 覆写**。若照抄子 agent 那种"stop 时写一次"的心智做成累加，一个会话有多少个 turn 就会多算多少倍。
2. **必须节流。** `Stop` 每轮对话都触发。158 ms × 每轮 是可感知的开销，且随会话增长线性上升。建议：`Stop` 时只在（a）距上次测量超过 N 分钟，或（b）文件 mtime 变化超过阈值 时才解析；`SessionEnd` 与 `PostCompact` 各强制测一次（后者尤其重要——compact 之后 transcript 会被重写，不在 compact 点定格就会丢失该段）。
3. **必须过滤合成行**（§1.3），否则 Leader 的 `model` 会被写成 `<synthetic>`。

**呈现上的强制要求**：Leader 主会话的量级（单会话 8.5 亿）远超全部子 agent 之和。若把二者混在一个排行榜里，子 agent 的归因结果会被彻底淹没。因此**主会话与子 agent 必须分列呈现，且默认不合并**。

### 3.4 未归因原因分类（供覆盖率呈现使用）

未归因不是一种状态，是四种，处置方式各不相同：

| 原因码 | 含义 | 当前量级 | 可否补救 |
|---|---|---|---|
| `no_transcript_path` | agent 行从未登记 transcript 路径 | 528 / 2,450 | 否（历史行），新行已覆盖 |
| `transcript_gone` | 路径有但文件已不在 | 抽样 0 / 150 | 否，且**随时间只增不减** |
| `not_yet_measured` | 路径在、文件在，只是还没跑过采集 | ~1,911 | **是**，见 §6 |
| `by_design` | 设计上不采集（如工具调用级） | 52,119 条活动 | 不适用 |

这张表是"未归因"抽屉的内容物。任何呈现面在报出覆盖率时，必须能下钻到这张表——否则"未归因 22%"是一句无法行动的话。

---

## 4. 覆盖率：定义、测量与闸值

### 4.1 覆盖率是向量，不是标量

链路有多跳，每跳各有覆盖率，端到端覆盖率是各跳的乘积。用一个标量表达会掩盖真正的瓶颈。定义如下：

```
C_measure(scope)  = 已测到用量的派工数 / 该 scope 内派工总数
C_hop(edge)       = 该边可解析的行数 / 需要该边的行数
C_e2e(scope)      = 能归到该 scope 的已测用量 / 全部已测用量
```

**分母的定义必须写死在代码里并有单测**，因为分母是唯一能被悄悄做假的地方：

* `C_measure` 的分母 = `agents` 表中 `role != 'leader'` 的行数（= 派工数），**含**那些没有 transcript 的行。不得以"没路径所以不算"为由剔除——那正是让局部冒充全貌。
* Leader 主会话单独统计，分母 = `role = 'leader'` 的行数。
* 分母永远按 `created_at` 落在查询窗口内计算，**不按 `tokens_measured_at`**（否则未测量的行会从分母里消失，覆盖率恒等于 100%）。

### 4.2 测量方法

按需触发，不设任何周期任务（P3）：

* **入口**：一个只读聚合查询 + 一个 MCP 工具 + 一个 API 端点，三者共用同一 storage 方法，返回 `TokenAttribution`；
* **触发时机**：Dashboard 打开该页时、MCP 工具被调用时、`os_health_check` 顺带输出一行摘要；
* **一致性自检**：同一份 transcript 重复解析两次结果必须逐字段相等（解析器是纯函数，这条可以做成单测而非运行时检查）。

### 4.3 基线（今天的真实数字）

| 指标 | 口径 | 实测 |
|---|---|---|
| `C_measure`（子 agent） | `usage_sum` | **11 / 2,450 = 0.4%** |
| `C_measure`（子 agent，回采后预估） | `usage_sum` | 1,922 / 2,450 = **78.4%** |
| `C_measure`（Leader 主会话） | `usage_sum` | **0 / 117 = 0%** |
| `C_measure`（workflow 自报） | `ctx_last` | 2,909 / 3,182 = 91.4% |
| `C_hop`（agent→session，路径派生） | — | 1,922 / 1,922 = **100%** |
| `C_hop`（agent→project） | — | 2,404 / 2,450 = 98.1% |
| `C_hop`（agent→workflow） | — | 2,331 / 2,450 = 95.1% |
| `C_hop`（agent→task） | — | 19 / 2,450 = **0.8%** |
| `C_e2e`（task 级归因） | `usage_sum` | ≈ **0.8%**（受 task 边限制） |

一眼可见：**瓶颈不在 token 采集，在 task 这条边。**即使把所有 transcript 回采完，task 级归因仍然接近于零。这就是 §2.4 那个"寄生在记账行为上"的方案存在的理由，也是为什么 v1 不能把"按任务归因"当成已具备的能力对外讲。

### 4.4 闸值建议（含对既有提法的一处更正）

会议裁决的三闸结构保留，但**闸 2 的门槛按字面不可满足，必须更正**。

原提法是"自报 vs transcript 实测偏差 <10%"。按 §0.2 的实测，自报值是 `ctx_last`、transcript 实测是 `usage_sum`，二者天然差 5~25 倍——这个闸不是难通过，是**永远不可能通过**，因为它在比较两个不同的量。更正后：

| 闸 | 用途 | 门槛（更正后） |
|---|---|---|
| **闸 1** | 内部可见 | 无数值门槛；但必须满足：① 每个数值带 `metric` 标签；② 分子分母同结构返回；③ 覆盖率在第一屏第一块；④ 未归因可下钻到 §3.4 四类 |
| **闸 2** | 可进入对外材料 | ① **同口径**对账：抽样 ≥30 个 agent，`usage_sum` 由两条独立路径（解析器 / 手工核 transcript）计算，逐字段偏差 **= 0**（纯函数，容差为零而非 10%）；② `ctx_last` 侧另行对账：`workflow_agents.tokens` 与重算的 `ctx_last` 偏差 **<10%**（这才是原门槛该落的地方）；③ `C_measure`（子 agent）≥ 95%；④ `C_hop` 各边 ≥ 95%，**task 边除外且必须单独标注其真实值** |
| **闸 3** | 可用于派工前判定 | 闸 2 全绿，且 `C_measure` ≥ 95% 持续两个版本。判定只能是派工前的一次性检查（`PreToolUse`），**禁止常驻监控**（P3） |

**一条额外的红线（建议进机检）**：

> 任何呈现面上的 token 数值，若其所在 scope 的 `C_measure < 100%`，必须在同屏同级显示未归因部分。缺失即视为红线违规。

以及 P1 的机检形态——用**量纲白名单**而非禁用词表，因为白名单是封闭集合、可穷举、不会漏：

> 用量相关的呈现面（API schema 字段名 + 前端展示单位）只允许四种量纲：**token（四层分列）、次数、时长毫秒、百分比**。出现任何第四类之外的量纲即失败。

白名单优于黑名单的理由：黑名单要穷举所有越界写法（多语言、符号、缩写、俚语）才能生效，漏一个就破防；白名单只需确认四个合法值，新增量纲必须显式过审。

---

## 5. Dashboard 呈现草案

### 5.1 落点：新增 `/usage` 一页

* **不**塞进 `/analytics`：那里是团队产出效率，与用量归因是两种读者心智，混在一起会让覆盖率标注被稀释。
* **不**占用侧边栏靠前位置：这是一个内部诊断页，不是首屏叙事材料。
* 新增路由会改变 `App.tsx` 的路由计数，**必须同批同步双语 README 的页面数**（I6 机检硬等式）。这是"加页面比想象中贵"的具体形态，估点时算进去。

### 5.2 页面结构（自上而下，顺序即优先级）

**① 覆盖率矩阵（第一屏第一块，不可折叠、不可关闭）**

这是页面的主体，不是页眉。表格形如：

| 派工路径 | 派工数 | 已测量 | 覆盖率 | 口径 |
|---|---:|---:|---:|---|
| 子 agent（直派） | 119 | n | n% | `usage_sum` |
| 子 agent（workflow） | 2,331 | n | n% | `usage_sum` |
| Leader 主会话 | 117 | n | n% | `usage_sum` |
| workflow 自报（历史） | 3,182 | 2,909 | 91.4% | `ctx_last` |
| 工具调用级 | 52,119 | — | 设计上不采集 | — |

设计要点：
* 每行都带口径列——`ctx_last` 行与 `usage_sum` 行在视觉上必须区分（不同底色 + 口径徽标），**并且这张表不提供"合计"行**；
* "设计上不采集"是一个正式取值，不是空白——空白会被读成 bug。

**② 未归因抽屉（紧贴矩阵，默认展开）**

按 §3.4 的四个原因码分列计数，每类可点开看样例行。这块的存在是为了让"覆盖率 78%"变成一句可行动的话——用户看完应当知道剩下 22% 能不能救、怎么救。

**③ 已归因明细（可下钻）**

层级：`project → session → workflow_run → agent`。每一级都是一张 `TokenAttribution` 卡片，四层 token 分列显示，**不显示合计**。默认排序键是 `output_tokens`（唯一与"干了多少活"强相关的一层），不是四层之和——理由见 §1.2。

task 级作为**旁支**呈现，独立卡片，标题明确写"task 级归因（覆盖率 n%，其余为 task 级未归因）"。

**④ 单次实测卡（对外样例专用通道）**

一张可复制的卡片：某一次真实派工的输入摘要、产出摘要、四层 token、耗时、模型（观测得来）。

这张卡**显式豁免于覆盖率闸**——它是单次实测定真，不是全量台账，两条链解耦。卡片上必须固定印一行：**"单次实测，非全量台账"**。这个豁免是会议明确裁决的，但豁免的**代价**必须落实：卡片数据只能来自单次 transcript 解析，禁止从聚合视图取数（否则豁免就成了绕过闸门的后门）。

**⑤ 口径说明（页脚常驻）**

一段固定文字，解释 `usage_sum` 与 `ctx_last` 的区别以及为什么二者不相加。写在页面里而不是文档里，因为看数字的人不会去翻文档。

### 5.3 其余触点

* **MCP 工具**：新增一个只读工具返回 `TokenAttribution`。注意加工具会同时触发 I6（README 工具数硬等式）与 I9（参数 description 机检），双语 README 必须同批改。
* **`os_health_check`**：顺带输出一行覆盖率摘要——按需触发，零新增守护。
* **既有页面不动**：`WorkflowsPage` / `AgentLivePage` 现在展示的 tokens 是 `ctx_last` 口径，v1 只做一件事——**给它们加口径徽标**，不改数据也不改算法。

---

## 6. 历史回采是否并入 v1

### 6.1 事实基线

回采的对象比"170 行 opus 别名"要大得多：

| 群体 | 行数 | 有 `transcript_path` | 可回采率 |
|---|---:|---:|---:|
| `workflow_agents` 中 `model='opus'` 且 tokens=0 | 170 | 138 | 81.2% |
| `workflow_agents` 中 `model='claude-fable-5'` 且 tokens=0 | 85 | 44 | 51.8% |
| `workflow_agents` 其余零 token 行 | 18 | 0 | 0% |
| **零 token 小计** | **273** | **182** | **66.7%** |
| `agents` 表全部子 agent 行 | 2,450 | 1,922 | 78.4% |

抽样 150 个已登记路径，**150 个文件全部仍在磁盘**。对 `model='opus'` 的 138 条路径逐个 `os.path.exists`，**138/138 存活**。

### 6.2 利

1. **这是覆盖率从 0.4% 提到 78.4% 的唯一一步。** 不回采，v1 上线时页面上是一片"未归因"，闸 2 永远够不着，整个方向会被判为空转。
2. **窗口在关闭。** CC 默认会清理较早的本地会话历史，`transcript_gone` 这一类只增不减。今天 150/150 存活，三个月后不是。**回采的代价随时间单调上升，收益单调下降。**
3. **技术风险极低。** 只写"从未被填过的列"（`agents` 五列 + `tokens_source`），天然幂等、可 dry-run、可重跑、失败可丢弃。子 agent transcript 中位仅 97 KB、最大 10.9 MB，1,922 份全量解析是分钟级作业。
4. **它顺带解决别名问题，且不违反刻意决策。** 138 条 opus 别名行的真实型号可从 transcript 的 `message.model` 直接读到（transcript 里永远是完整型号）——这是**观测回填**，正是"模型默认值留空、由观测回填"这条决策想要的样子，与"禁止写死型号"不冲突。剩下 32 条无 transcript 的行走 `MODEL_ALIAS_LEDGER` 读侧兜底，窗口外返回 None 不瞎猜。

### 6.3 弊

1. **口径污染风险（最大的一条）。** 回采产出的是 `usage_sum`，而 `workflow_agents.tokens` 是 `ctx_last`。若图省事把回采值写进那一列，就会把 5~25 倍的混口径**永久固化进历史数据**，且事后无法区分哪些行是哪个口径。这比不回采糟糕得多。
2. **覆盖率会"虚涨"，掩盖真正的健康指标。** 回采后总覆盖率跳到 78%，但"新派工的采集率"是另一回事——后者才是判断采集链路健不健康的指标。一个数字掩盖另一个数字。
3. **口径不齐的历史窗口。** 回采只能覆盖 transcript 尚存的行，早期行永远缺失，因此**任何跨越回采边界的时间序列图都是失真的**。
4. **它会顺手改动 2,000 行生产数据。** 即使幂等，也需要备份与 dry-run 门禁。

### 6.4 建议：**并入 v1，但带三条硬约束**

**建议做，理由是 6.2-2（窗口在关闭）压倒一切**——这是本设计里唯一一件"晚做就永远做不了"的事，其余各项晚做只是晚做。

三条硬约束：

1. **只写 `agents` 表五列 + `tokens_source`，绝不触碰 `workflow_agents.tokens`。** 该列保持 `ctx_last` 口径不变。读侧要展示 workflow agent 的 `usage_sum` 时，通过 `os_agent_id` 关联到 `agents` 行取数——**用关联解决，不用覆写解决**。
2. **覆盖率必须按 `tokens_measured_at` 分窗呈现**，把"历史回采"与"增量采集"拆成两个数。回采不得让增量采集率变得不可见（对策 6.3-2）。
3. **回采脚本按需触发、dry-run 先行、可重跑零变更。** 落法照 UTC 平移脚本的先例：先 dry-run 出报告，人审后再 `--apply`；带幂等标记，重跑不产生任何变更。

**不做的部分**：不为回采去改 agent 模板的 frontmatter（`model: opus` 是 2026-07-10 裁定固化的层级别名），不把别名解析结果写回任何 `model` 列。

---

## 7. 实施拆分（每阶段可独立验收）

顺序有依赖，但每一阶段单独可交付、可回滚、可验收。

### 阶段 0 — 口径正名与机检（S）

* `parse_transcript_usage` 复用 `session_probe` 的合成行过滤（§1.3）；
* 定义 `TokenMetric` 枚举与两种口径的常量，给 `workflow_agents.tokens` 的读侧标注 `ctx_last`；
* 落两条机检：量纲白名单（§4.4）、覆盖率同屏红线；故意改错一处验证机检真红。

**验收**：机检可复现地红、修回后绿；单测覆盖合成行过滤。

### 阶段 1 — 链路派生器（S–M）

* 实现 `transcript_path → (project_slug, session_id, wf_id, cc_agent_id)` 的解析器（正则 + 单测，含无 wf、含 worktree 路径、含非 ASCII slug 三类样本）；
* 回填 `agents.session_id`（历史 1,922 行 + 新行在 SubagentStop 顺手写）；
* 回填 Leader 行的 `transcript_path`（经 `session_probe` 反查）。

**验收**：1,922 行解析成功率 100%；随机抽 30 行人工核对 session 归属正确；非 ASCII slug 不误判 project（以 `agents.project_id` 为准，slug 仅作交叉校验）。

### 阶段 2 — 覆盖率与类型层（M）

* `TokenAttribution` / `AttributionScope` / `AttributionMethod` 定义；
* storage 层聚合方法 + 只读 API + MCP 工具；
* 契约测试：不存在裸 total、`metric` 无默认值、分子分母同层。

**验收**：契约测试全绿；API 在任何参数组合下都无法返回孤立总量；覆盖率基线数字与 §4.3 一致。

### 阶段 3 — 历史回采（M）

* 幂等回采脚本，dry-run 先行，人审后 `--apply`；
* 只写 `agents` 五列 + `tokens_source`。

**验收**：dry-run 报告与实际写入逐行一致；`--apply` 后重跑零变更；`workflow_agents.tokens` 逐行未变（校验和比对）；覆盖率按窗分列可见。

### 阶段 4 — Leader 主会话采集（M）

* `Stop` 节流测量 + `SessionEnd` / `PostCompact` 强制测量；
* snapshot 覆写语义 + 三个陷阱的针对性单测。

**验收**：一次真实会话结束后 Leader 行有值；**重复触发 10 次数值不变**（防累加）；compact 前后各有一次定格；单轮 `Stop` 的额外开销可测且在节流窗内为零。

### 阶段 5 — `/usage` 页面（M）

* 五块结构（§5.2）+ 口径徽标 + 未归因下钻；
* 同批同步双语 README 页面数（I6）与工具数（若阶段 2 新增了 MCP 工具）。

**验收**：覆盖率在第一屏第一块；未归因四类可点开；`ctx_last` 与 `usage_sum` 视觉可区分且无合计行；I6 机检绿。

### 阶段 6 — 不在 v1

派工前的用量判定闸（原 A4）。前置条件：闸 3 全绿且持续两个版本。形态只能是 `PreToolUse` 的一次性判定，**禁止常驻监控**。

### 依赖与并行度

```
阶段0 ──┬─→ 阶段1 ──→ 阶段2 ──┬─→ 阶段3
        │                      └─→ 阶段5
        └─→ 阶段4（可与 1/2 并行，只依赖阶段0 的合成行修复）
```

阶段 3 与阶段 5 可并行。阶段 4 只依赖阶段 0，可最早启动，但它的**呈现**依赖阶段 5。

---

## 8. 风险与未决问题

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 混口径复发 —— 后来者把两个 token 列相加 | 数字错一个数量级 | 类型层 `metric` 无默认值 + 页脚口径说明 + 本文件 §0.2 |
| R2 | 覆盖率被"优化"成 100%（把没数据的行移出分母） | 局部冒充全貌，纪律①失守 | 分母定义写死 + 单测钉住 + 同屏红线机检 |
| R3 | 回采把 `usage_sum` 写进 `ctx_last` 列 | 历史数据永久污染、不可分辨 | 阶段 3 验收含"`workflow_agents.tokens` 逐行未变"的校验和比对 |
| R4 | Leader 采集做成累加 | 数值随轮次线性虚高 | 阶段 4 验收含"重复触发 10 次数值不变" |
| R5 | task 级归因被当成"已具备的能力"对外表述 | 可信度风险（表是空的时候宣称已解决） | v1 只承诺"数据结构与推导器已就位"，并强制同屏披露 0.8% 的真实覆盖率 |
| R6 | `transcript_gone` 随时间扩大 | 回采窗口关闭 | 阶段 3 优先级前置，不排到 v1 末尾 |
| R7 | 主会话 transcript 增大导致解析变慢 | `Stop` 路径可感知延迟 | 节流 + 实测基线（35.1 MB / 158 ms）作为回归阈值 |

**未决问题（需要在实施前确认，但不阻塞本设计过审）**：

1. **`agents.session_id` 为何全表为空**——是从未写入，还是某处被清掉？`hook_translator.py:1904` 的注释暗示两种情况都存在。阶段 1 的回填能绕过这个问题，但根因值得顺手查清，否则新写入的值可能再次被同一个机制清掉。
2. **compact 之后 transcript 的重写行为**——`PostCompact` 之后原 transcript 是被截断、被替换还是被归档？这直接决定阶段 4 的"强制定格"是否真的能保住 compact 前那一段用量。需要一次实测。
3. **`workflow_agents.tokens` 与 wf JSON 自报值的关系**——D1 注释称二者"error agent 精确相等、done agent 偏高 3~12%"。这个 3~12% 的偏差本身是否需要在闸 2 的 `ctx_last` 对账里扣除，需要一次抽样确认。

---

## 附录 A：取证方法与原始数字

**方法**：生产库以 `sqlite3` 只读 URI（`mode=ro`）打开，全程未执行任何写语句、未创建快照副本以外的文件；transcript 文件仅读取。代码侧对照 `master@2b651f8`。

**关键原始数字**（2026-07-29）：

```
events                     247,469 行   2026-07-06 02:05 → 2026-07-29 02:14（库龄 = 观测窗口 = 23 天）
agents                       2,567 行   非 leader 2,450 / leader 117
  transcript_path 非空       1,922 行   抽样 150/150 文件仍存在（中位 97 KB，最大 10.9 MB）
  tokens_measured_at 非空       11 行   D1 于 2026-07-28 16:26 上线
  session_id 非空                1 行
  project_id 非空（非leader）  2,404 行
workflow_agents              3,182 行   tokens>0: 2,909   合计 186,995,224（ctx_last 口径）
  model='opus' 且 tokens=0     170 行   其中 138 行有 transcript 且文件全部存活
  model='claude-fable-5' 且 0   85 行   其中 44 行有 transcript
agent_activities            52,119 行   无 token 字段
tasks                          161 行   team_id 非空 1 行；assigned_to 非空 28 行（存的是 agent 名）
teams                          248 行   挂有 task 的 team：1 个
knowledge_links                        已有 from_kind ∈ {task_memo, report}，to_kind ∈ {commit, run, task, memory}
主会话 transcript                7 份   最大 42.8 MB / 次大 35.1 MB / 合计 82.5 MB
  35.1 MB 解析实测            158 ms   2,012 次 API 调用，四层合计 852,524,309
```

**两口径对照原始行**见 §0.2 表格。

**路径解析验证**：正则 `/projects/(?P<slug>[^/]+)/(?P<sid>[0-9a-f-]{36})/subagents/(?:workflows/(?P<wf>wf_[^/]+)/)?` 对 1,922 条路径全部命中，其中 1,817 条带 `wf_id`。

## 附录 B：代码触点索引

| 文件 | 现有职责 | 本设计涉及 |
|---|---|---|
| `src/aiteam/services/token_attribution.py` | `usage_sum` 解析 + 别名台账 | 阶段 0 修合成行过滤；阶段 1 新增路径解析器（或另起模块） |
| `src/aiteam/api/workflow_ingest.py:220` | `ctx_last` 计算 | 不改，只在读侧标注口径 |
| `src/aiteam/api/hook_translator.py:661` | `SubagentStop` 回填五列 | 阶段 1 顺手写 `session_id`；阶段 0 的 `tokens_source` |
| `src/aiteam/api/hook_translator.py:1710/1929` | `SessionStart` / `Stop` | 阶段 4 主会话采集挂载点 |
| `src/aiteam/api/session_probe.py` | 主会话定位 + 合成行过滤 | 阶段 0 复用；阶段 1 反查 Leader transcript |
| `src/aiteam/api/request_ledger.py` | 请求级账本 | 不改；作为"按需触发 + 事件聚合 + 不建表"的范式参考 |
| `src/aiteam/storage/models.py` | ORM | 阶段 0 加 `tokens_source` 列 |
| `src/aiteam/storage/connection.py` | `COLUMNS_TO_ENSURE` | **加列必须同步此处**（项目铁律） |
| `scripts/check_invariants.sh` | 红线机检 | 阶段 0 新增两条 |
| `dashboard/src/App.tsx` | 路由 | 阶段 5 新增 `/usage`，同步 I6 页面数 |
