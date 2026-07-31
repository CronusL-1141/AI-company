# 记忆系统 v2 设计 — 双层台账 + 按需整理

> 状态：**已实施**（P0/P1/P2 自 v1.9.0 起在产）｜ 初稿 2026-07-12 ｜ **v2.1 修订 2026-07-31**
> 来源：讨论①②（任务 f5524057 / 3a1c54aa）+ 三路工业实践调研（wf_e75cf7d4，逐路对抗核验）
> 定位红线：**OS 是 Claude Code 的治理观测台账层，记忆是子系统，不做专业记忆产品。**
>
> v2.1 改了什么（详见 §3.1，全部已实施）：方向层体量红线从「≤40 条 × 400 字」的**双轴**
> 改为**单一轴「存储上限 = 注入预算」**（分桶字符配额）；超限从一句提示改为**协议化**
> （交回全桶清单 + 用量缺口 + 当轮整理指令）；`memory_add` 加**写入侧安全扫描**；
> `memory_invalidate` 加**子串定位**；会议结论**停止自动写入方向/团队记忆**。

## 0. 证据审计（设计依据的可信度声明）

对抗核验后，本设计只站在 **confirmed 级证据**上：

| 证据（confirmed） | 支撑的设计决策 |
|---|---|
| ChatGPT 双层记忆：saved memories（显式/可审计/常驻参考）+ reference chat history（后台综合/按需召回）——OpenAI 官方帮助中心 | 双层模型（方向层常驻 + 情景层按需）有消费级官方实证 |
| Zep 论文 bi-temporal：冲突时 invalidate 边而非删除（arXiv，t_valid/t_invalid + 事务时序） | "显式失效不删除"（讨论①结论） |
| mem0 论文两阶段管道：先检索 top-K 相似记忆作候选，LLM 直接择 ADD/UPDATE/DELETE/NOOP | memory_reconcile 的四操作决策程序（BM25 替代向量） |
| OpenAI Agents SDK Sessions：写入原文逐条 append、零 LLM，SQLite 默认后端 | "写入轻、零 LLM"是一线 SDK 的合法默认，抽取是可选增强不是强制写路径 |
| Google ADK：短期原始事件（无 LLM）与长期库分离，长期化是**显式 API**（add_session_to_memory，通常会话完成时） | 整理是会话内按需显式动作，不是后台守护——正合 CC 非常驻 |
| Zep 异步入库逼出的双读：prompt 同时拼长期 context 串 + 最近几条原文 | 注入时"方向层摘要 + 最近记录"的拼装范式 |

Partial 级（主干成立、细节降权使用）：CoALA 四分类（arXiv:2309.02427 §4.1 确有 working/episodic/semantic/procedural，但"溯源 Tulving/ACT-R"是调研放水——**只借术语命名，不做认知科学背书宣称**）；Letta core memory blocks 常驻编译进 system prompt + 字符上限；Letta sleep-time compute（离线整理 agent 存在，默认参数未逐字核验）；Generative Agents reflection（arXiv:2304.03442，重要度累计过阈触发、产出带引用的高层结论）；CrewAI 旧版 LTM 用 SQLite 零 embedding（文档已改版，仅作历史先例）。

已剔除的论据：LangMem "data-independent vs data-dependent" 官方原话（核验判 unsupported，系调研员自创综合）。

## 1. 总体形态：双层 + 一工具

```
┌─ 方向层（memories 表激活）────────────────┐
│ 偏好/纠正/设计意图/约束                     │
│ 低频·高价值密度·跨任务长寿命                │
│ 写：Leader 显式 memory_add（用户给出偏好时）│
│ 读：SessionStart + SubagentStart 常驻注入  │←—— 杀手级：派出的 agent 出生即继承
└──────────────────△───────────────────────┘
                   │ 蒸馏提升（带 source_refs 溯源）
┌─ 情景层（task_memos 升表）───────△────────┐
│ 任务过程记录/结论/失败原因                  │
│ 高频·agent 自动书写·量大                   │
│ 写：task_memo_add（接口不变）              │
│ 读：unified_search 按需检索（BM25 三臂）    │
└───────────────────────────────────────────┘
        △ 两层之间：memory_reconcile（按需整理工具，①②⑧合并）
```

- 与 CC 记忆的关系：MEMORY.md 是 Leader 个人记忆；memories 是**团队共享方向记忆**（可观测、可治理、agent 可继承）。
- 决策不入表：保持 events append-only，推翻 = 追加 `decision.superseded` 事件，有效性为派生视图（讨论①已定）。

## 2. 情景层：task_memos 升表（P0）

memo 现状是 `tasks.config` JSON 数组——无行级 ID、无索引、无法挂失效轴/质量分。升为真表：

```sql
CREATE TABLE task_memos (
    id            TEXT PRIMARY KEY,          -- uuid，真 ID（可被 knowledge_links 引用）
    task_id       TEXT NOT NULL,             -- FK tasks.id（天然溯源）
    project_id    TEXT,
    author        TEXT DEFAULT 'leader',
    memo_type     TEXT DEFAULT 'progress',   -- progress/decision/issue/summary
    content       TEXT NOT NULL,
    scope_path    TEXT DEFAULT '',           -- ②路径作用域 /project/ecosystem/research
    quality_score INTEGER,                   -- ⑧质量分（NULL=未评，整理时补）
    invalid_at    DATETIME,                  -- ①失效轴（NULL=有效）
    invalidated_by TEXT,                     -- 取代者 memo id
    meta          JSON DEFAULT '{}',         -- entities/topics（整理时补）
    created_at    DATETIME NOT NULL
);
CREATE INDEX idx_memos_task ON task_memos(task_id);
CREATE INDEX idx_memos_valid ON task_memos(project_id, invalid_at);
```

- **接口完全不变**：`task_memo_add` 签名照旧，新增可选 `supersedes=<memo_id>`（写入即置旧条 invalid_at，零 LLM）。所有 agent 回写指令零改动。
- 迁移：一次性把各 tasks.config.memo 数组回填成行（历史 memo 无 id → 迁移时生成）。
- 读侧：unified_search BM25 臂从"全量解包 JSON"改直查表，默认过滤 `invalid_at IS NULL`，加 `include_invalidated` 开关。

## 3. 方向层：memories 表激活重定位（P1）

复用现有空表，加列不建新表：

```sql
ALTER TABLE memories ADD COLUMN kind TEXT DEFAULT 'preference';
    -- preference(偏好) / directive(指令·工作方式) / constraint(约束) / design(设计意图)
ALTER TABLE memories ADD COLUMN invalid_at DATETIME;
ALTER TABLE memories ADD COLUMN invalidated_by TEXT;
ALTER TABLE memories ADD COLUMN source_refs JSON DEFAULT '[]';  -- ④溯源：回指 memo/report/meeting id
-- scope 语义收窄：global / project / user（不再用 task 级——那是情景层的事）
```

- **写入口（本次真的要建）**：MCP `memory_add(content, kind, scope, supersedes?)` + `memory_invalidate(id)`。
  行为规则（进 Leader 规则集）：**用户给出偏好/纠正/方向设计时，Leader 当场落一条**；偏好被改 = 新条 supersede 旧条（Zep 失效语义）。
- **体量红线**（Letta block 字符上限的教训）：**v2.1 起改为单一轴，见 §3.1**。（初稿口径：每项目有效条目 ≤ 40 条、单条 ≤ 400 字，超限提示"先整理再添加"——保留在此作为被推翻的原始设计。）**方向层的价值在小而准，不在多。**
- **读侧 = 本层的存在理由**：
  - `session_bootstrap.py`（SessionStart）：简报追加「方向记忆」节（有效条目按 kind 分组）；
  - `inject_subagent_context.py`（SubagentStart）：**每个派出的 agent 出生即注入方向层**——"全中文""完成即汇报"这类偏好不再靠 Leader 手抄进 prompt；
  - 注入预算：**v2.1 起与存储上限合并为同一根轴，见 §3.1**（初稿口径：两处合计 ≤ 2000 字）。

## 3.1 v2.1 修订：单一轴 + 超限协议 + 写入安全（2026-07-31 已实施）

### 3.1.1 病灶：存储红线与注入预算不在同一根轴上

初稿把两件事分开定：存储侧「每桶 ≤40 条 × 单条 ≤400 字」＝允许 16,000 字，注入侧
给 900 字。两个数字差 17 倍，后果不是"多存的没用"，而是**多存的等于没存**：实测
48 条有效条目里只有排在最前的 2-3 条真的进了 agent 的 system prompt，其余被折成一句
"另有 46 条，Leader 可用 memory_list 查看"。而"派出的 agent 出生即继承方向层"正是本层
存在的唯一理由——红线自己把卖点架空了，且这件事在 UI 与工具返回里都看不出来：
写入成功，注入静默截断。

### 3.1.2 裁定：单一轴「存储上限 = 注入预算」+ 分桶配额

只保留一个数字：**方向层注入池 3000 字**。分桶配额（键为 `(scope, scope_id)`）：

| 桶 | 配额 | 说明 |
|---|---|---|
| `global` / `system` | 1200 字 | 跨目录跨会话恒成立的纪律 |
| 每个 `project` | 1500 字 | 本项目专属；每项目各计一份 |
| `user` | 300 字 | 用户级偏好 |

一个会话实际继承 = global + 本项目 project + user = **3000 字**。单条 ≤400 字保留
（指针条目哲学不变：触发条件 + 指向权威文件，正文外置）。条数轴（`≤40 条`）**废除**
——字符是注入侧真正稀缺的资源，条数不是。

- **校验口径**：写入后该桶有效条目（`invalid_at IS NULL`）总字符 ≤ 配额。带
  `supersedes` 时算的是**置换之后**的总量（旧条字数计为腾出），否则"拿一条长条换一条
  短条"会被误拒——那是条数轴留下的思维惯性。
- **注入侧的 900 变 3400**（3000 + 格式开销），且语义从"常态截断线"改为**保险丝**：
  写入侧已经卡死存储量，正常情况下永不触发；它只兜"有人绕过 API 直改 DB 把方向层
  撑爆"这一种异常，防止记忆节淹掉简报/记账约定/身份块。两个 hook 是纯 stdlib 进程
  （不 import `aiteam` 包），各自定义常量，与 `plugin/hooks` 逐字节副本同步（I1 机检）。
- **过渡态（重要）**：改轴当日现库处于**超配额状态**（project 桶 33 条远超 1500 字）。
  红线**只拦新写入，不追溯、不迁移、不删任何行**——超限响应如实报告当前用量，容量
  由后续蒸馏任务在用户过目下压回配额内。治理层不黑盒改数据这条原则优先于数字好看。

### 3.1.3 超限协议化：让整理发生在被拒绝的那一刻

旧行为是回一句"先整理再添加"，实际效果是调用方换个更短的句子重试，容量压力没有
任何人处理。现在超限响应交回一整套**可当轮执行**的材料：

1. `quota`：桶名 / 配额 / 已用 / 条数 / 本次新增 / 置换腾出 / 落库后总量 / 超出多少；
2. `bucket_entries`：该桶**当前全部有效条目**的 id、kind、字数、创建时间、**全文**；
3. `error` + `next_action`：明确要求在**本轮之内**先 `memory_invalidate`
   （可用 `content_match` 子串定位）或 `memory_reconcile_apply` 腾出至少 N 字，再重试
   本次写入。

`memory_reconcile_apply` 的 `promote` 分支走同一套校验，但不回挂全桶清单
（整理流程的 `direction_inventory` 已给过全文，逐条复述只会把响应撑爆）。

### 3.1.4 写入侧安全扫描

方向层内容会被编译进**每一个**派出 agent 的 system prompt，因此 `memory_add` 是全系统
最强的注入放大器：一条被污染的条目影响此后所有子 agent。扫描放在写入侧（作者还在
现场、可以改），不放注入侧（既太晚又是热路径）。三族：

| 族 | 内容 | 作用面 |
|---|---|---|
| 不可见 Unicode | 零宽字符、双向覆盖、被弃用格式符、BOM、Tag 块（ASCII 走私） | 方向层 + 情景层 |
| 提示注入句式 | 指令覆盖（中/英）、系统提示套取、伪造角色/轮次标记、人格接管、越狱口令 | 仅方向层 |
| 凭据形态 | 私钥文件头、各家 API key 形状、`key=value` 密钥 | 仅方向层 |

- 命中即拒绝并说明命中了什么、在第几个字；**凭据命中不回显匹配内容**（否则拒绝信息
  本身成了密钥的第二份副本）。
- 模式表单一来源在服务端 `src/aiteam/memory/content_safety.py`，**不复制进 hook**
  （hook 纯 stdlib，且注入端检查为时已晚）。表内不可见字符按**码点区间**书写，不写
  字面字符——否则模式表自己就是一段不可审阅的文本，正是本检查要防的东西。
- `task_memo_add` **只扫不可见 Unicode**：情景层高频，且不进任何 system prompt，注入
  句式在那里只是被记录的文本；但肉眼不可见的内容无论进哪层都不该入库（检索会把它
  捞回模型眼前）。

### 3.1.5 `memory_invalidate` 子串定位

整理最常发生在"刚被超限协议顶回来"的那一刻，此时调用方手里有的是**条目原文**，
先查一次 id 再失效纯属多一跳。故加可选 `content_match`：在当前上下文的有效条目
（global + user + 本项目，与 `memory_list`/注入同源）正文中做子串匹配，**必须唯一命中**
——0 条或多条都不动数据，多条时交回候选要求给出更精确的子串。绝不猜。

### 3.1.6 会议结论停止自动入层

`PUT /api/meetings/{id}/conclude` 原会自动往 `team` 域 memories 塞一条
`[会议决策] <topic>: <结论>`，这是记忆层唯一的自动写入口：无人审、不挂失效轴管理、
按会议数线性增长，且与决策的正道重复。裁定：**停写**。结论的权威落点是 decision
事件 + 任务墙条目（Council 纪律④，现行制度）。已有的 126 条历史行**冻结**：不删、
不失效、不迁移——它们是当时制度下的真实记录，删了就是伪造历史。

### 3.1.7 如实记录：未启用的扩展点与未交付的分期

- `task_memos.quality_score` / `scope_path`：**列在、语义在、实际未启用**。写入面只有
  `memory_reconcile_apply` 的 `score` / `merge` 两个 op 会填，日常写入路径
  （`task_memo_add`）不带这两个字段，检索与注入侧也不读它们。**保留列，不删**
  ——I10 冻结原则：退役的表/列冻结而非删除，删列既不可逆又会让老库升级失败。
- **P3 全部未交付**：scope_path 检索切片、Dashboard 记忆页（含方向层导出 markdown）、
  CC MEMORY.md 高价值条目镜像、doctor 的 AGENTS.md 互通卫生检查——四项均未实施，
  分期表里的"可缓"至今仍然成立。

### 3.1.8 外部对照：NousResearch/hermes-agent

本次改造的机制来源。逐条对照结论：

| 该项目的做法 | 本设计的处置 |
|---|---|
| 记忆存储上限**就是**注入预算，只有一个数字，注入永不截断，超限时把当前全部条目连同"本轮内整理后重试"的指令一起回给模型 | **照搬**（§3.1.2 + §3.1.3）。这是本次改造的全部机制来源 |
| 检索走 SQLite FTS5，**零 embedding** | **双佐证**：与本设计 §5「不做向量库」的判断独立同构（我们用纯 Python BM25，因为 FTS5 的中文分词不可靠）。两处独立实践指向同一结论，该项裁定加固 |
| 写入前置 `write_approval` 暂存队列（记忆先入待批队列，人确认后落库） | **暂不实施，记为参考形态**。当前方向层只由 Leader/用户维护（§5「不让子 agent 自改方向层」），没有需要审批的第二写入方；若将来开放子 agent 提案方向层条目，这个暂存队列就是现成的通道形态 |

## 4. 按需整理：memory_reconcile（P2，①②⑧⑦合并）

CC 非常驻 ⇒ 无后台整理进程（ADK/调度器退役同一原则）。整理 = **会话内按需显式动作**：

- **触发**：用户明说"整理记忆"；或软提示——上次整理后新增有效 memo > 150 条时，工具返回/hook 附 hint（Generative Agents 重要度过阈的极简化：按量计数）。量大提示开 ultracode 用 Workflow 并发。
- **流程**（mem0 四操作管道，BM25 版）：
  1. **粗筛（零 LLM）**：同 scope_path/同任务簇内 BM25 两两相似度挑候选对（graphiti 两级去重思想，MinHash 换 BM25）；
  2. **LLM 精判**：每候选组择一——`KEEP`（都留）/ `MERGE`（合并，旧条 supersede）/ `INVALIDATE`（矛盾，旧条失效）/ `NOOP`；
  3. **蒸馏**（Generative Agents reflection，只做一层）：跨 memo 反复出现的结论/用户纠正 → 提案为方向层条目，`source_refs` 回指源 memo（④溯源在此闭环）；
  4. **打分**（⑧）：为 summary/decision 型 memo 补 quality_score（1-10 带 reason 入 meta）；
  5. **产出建议清单 → 用户确认 → 应用**。治理层原则：**不黑盒自动改**（ChatGPT chat history 式隐式综合与可审计定位相悖，明确不学）。

## 5. 明确不做（过度设计红线，全部来自调研标注）

| 不做 | 理由 |
|---|---|
| 向量库 / embedding | 语料百级，BM25 三臂 RRF 已足（Anthropic 官方记忆方案同样弃向量选透明文件） |
| 常驻后台整理进程 / 定时器 | CC 非常驻——同调度器退役裁定；Letta sleep-time 的"思想"保留，"常驻"不搬 |
| 图数据库 / 知识图 / 实体社区 | graphiti/Zep 的重资产形态，只搬失效语义 |
| 完整双时序四时间戳 | 单一失效轴（invalid_at + invalidated_by）够用，valid_at 语义并入 created_at |
| 自动黑盒综合 | 治理层要可审计：一切整理走"提案→确认→应用" |
| LLM importance 常驻打分 | 按需整理时才打分 |
| 让子 agent 自改方向层 | 方向层只由 Leader/用户维护（Letta 自编辑工具链裁掉），子 agent 只读 |

## 5.5 方向层条目内容标准（2026-07-12 增补，源自 Kun Chen 全局 AGENTS.md 案例研究）

一手案例：kunchenguid/dotfiles 的 `home/AGENTS.md`（前 Meta/Microsoft/Atlassian L8，博文《Everyone Should Have an OPINIONS.md》+ 视频 walkthrough）。该文件 7 条 bullet 管住所有 agent 的所有输出，验证方向层"价值在杠杆率不在条数"。

**写入检验（memory_add 的软门槛）**：这条能影响多少未来任务？只影响单个任务的 → 去 task_memos。

**kind 分类与范本**（全部来自 Kun 文件逐字）：
| kind | 范本 | 特征 |
|---|---|---|
| constraint（禁令/护栏） | "Never use the em dash"；"NEVER auto-add agent name as co-author" | 一句话、可机检、终身有效 |
| design（价值排序） | "技术决策不看重开发成本，偏向质量/简洁/健壮/可扩展/长期可维护" | 缺显式指令时的取舍依据 |
| directive（方法论） | "bug 先在贴近最终用户的 E2E 场景复现"；童子军军规（顺手修 lint/flaky） | 回答"怎么干" |
| preference（格式偏好） | 每句一行（semantic line breaks，利 diff） | 可选，不设默认 |

**指针条目形态**（OPINIONS.md/VOICE.md 模式的 OS 等价物）：方向层条目允许"触发条件 + 指向"形态——常驻的只是一句触发指令，大体量内容放情景层/报告由检索按需拉取。这是体量红线的泄压阀：超限内容不是删，是降级为指针+正文外置。

**外部佐证两则**：① Kun 公开主张关闭 Claude auto-memory、改存 agent-agnostic 位置（防陈旧记忆污染上下文）——与本设计"Leader 显式 memory_add、不做自动黑盒写入"同判断；② 其仓库根级 AGENTS.md 维护元规则（只放几乎每个未来 session 都用的知识 / 指向权威文件而非重复 / 优先重写精简而非追加）与本设计体量红线 + reconcile 精简哲学同构；其 OPINIONS.md 配 cron watchdog 检测陈旧观点 = 本设计 memory_reconcile 失效判定的常驻版（OS 按 CC 非常驻现实改为按需，方向正确）。

**AGENTS.md 生态事实**（一手核验）：AGENTS.md 为 Linux 基金会 Agentic AI Foundation 托管的开放标准，28+ 工具原生读取；**Claude Code 不原生读**（官方文档明示，issue #6235 开放 4300+ 👍无路线图），官方桥接法 = CLAUDE.md 首行 `@AGENTS.md` import（优先）或 symlink。→ P3 可选项：doctor 增加互通卫生检查（检测仓库有 AGENTS.md 但 CLAUDE.md 未桥接时提示 @import）。方向层整体导出 AGENTS.md 判否：语义域错配（方向层=会演化的偏好台账，AGENTS.md=稳定工程约定），仅跨工具用户需要时导出"可稳定化子集"。

## 6. 分期交付

| 期 | 内容 | 性质 |
|---|---|---|
| **P0 地基** | task_memos 升表 + 迁移 + task_memo_add 兼容（含 supersedes）+ unified_search 直查表 | 纯机械重构，风险最低，解锁①④⑧字段落点 |
| **P1 方向层** | memories 加列 + memory_add/memory_invalidate 工具 + 双 hook 注入（体量红线+注入预算）+ **种子条目**（把已知用户偏好首批落条：全中文/完成即汇报/co-author 禁令/生产只读铁律指针等，用户过目后入库） | 新能力，杀手级是 SubagentStart 注入 |
| **P2 整理** | memory_reconcile（粗筛→四操作→蒸馏→打分→提案确认流）+ 量阈软提示 + **陈旧检测**（Kun watchdog 按需版：对方向层每条问"是否仍然成立"——引用的功能已退役/版本已过时/世界已变化 → 提案失效） | LLM 按需，ultracode 提示接规则1c |
| **P3 可选** | scope_path 检索切片、Dashboard 记忆页（含方向层导出 markdown，可移植性）、CC MEMORY.md 高价值条目镜像、doctor AGENTS.md 互通卫生检查 | 增强，可缓 |

**reconcile 三守则**（Kun 根级 AGENTS.md 维护元规则的移植）：只保留对几乎每个未来任务都有用的条目；指向权威文件/工具而非复述其内容；优先重写精简而非追加。

每期独立可交付；P0 不依赖任何讨论③⑤⑥的结论。

**交付现状（2026-07-31 核对）**：P0 / P1 / P2 已实施并在产（v1.9.0 起）；P1 的体量红线
与注入预算已按 §3.1 合并为单一轴，并补上超限协议与写入安全扫描；**P3 四项全部未交付**。
