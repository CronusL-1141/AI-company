# 记忆系统 v2 设计 — 双层台账 + 按需整理

> 状态：讨论稿（待用户确认）｜ 2026-07-12
> 来源：讨论①②（任务 f5524057 / 3a1c54aa）+ 三路工业实践调研（wf_e75cf7d4，逐路对抗核验）
> 定位红线：**OS 是 Claude Code 的治理观测台账层，记忆是子系统，不做专业记忆产品。**

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
- **体量红线**（Letta block 字符上限的教训）：每项目有效条目 ≤ 40 条、单条 ≤ 400 字；超限时 memory_add 返回提示"先整理再添加"。**方向层的价值在小而准，不在多。**
- **读侧 = 本层的存在理由**：
  - `session_bootstrap.py`（SessionStart）：简报追加「方向记忆」节（有效条目按 kind 分组）；
  - `inject_subagent_context.py`（SubagentStart）：**每个派出的 agent 出生即注入方向层**——"全中文""完成即汇报"这类偏好不再靠 Leader 手抄进 prompt；
  - 注入预算：两处合计 ≤ 2000 字（Zep 双读范式：方向层全量 + 该任务最近 3 条 memo）。

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
