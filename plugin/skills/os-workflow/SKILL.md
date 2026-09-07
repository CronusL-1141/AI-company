---
name: os-workflow
description: 在 AI Team OS 项目里使用 CC 内置 Workflow（ultracode）时，让工作流产出回写 OS 的标准做法。当 Leader 准备调用 Workflow 工具编排子 agent 时使用。
---

# OS Workflow — 用 CC 工作流，但让产出回流 OS

## 背景

调用 Workflow 后，每个内部 agent 会被 hook 自动注册成一个 OS 团队（`workflow-<wf_id>`，
一次 workflow = 一个团队）。追踪是自动的，但**工作内容不会自己入库**——下面两件事必须你做。

## 1. 总任务上墙（Leader 职责，不变）

调用 Workflow 前/后，把这次工作方向用 `task_create` 登记到任务墙并置 running。
Leader 负责决策、设计、记录；执行交给 workflow——但**账要记在 OS**。
完成后 `task_update` 置 completed 并填 result。

## 2. 在每个 workflow agent 的 prompt 里嵌入「回写指令」

把下面这段**粘进你写的 workflow 脚本里每个 `agent()` 的 prompt 末尾**（已验证 workflow
agent 能调 OS 的 MCP 工具 + HTTP API，非沙盒）：

```
【回写 OS（收尾必做）】
1. ToolSearch 加载：select:mcp__ai-team-os__task_memo_add,mcp__ai-team-os__report_save
2. 完成本职工作后：
   - task_memo_add(task_id="<总任务id>", content="<这步干了啥+关键结论>", memo_type="progress")
   - 重要产出再 report_save(...) 落库，并把 report_id 写进 memo
3. 你在项目目录运行，MCP 自动带项目头，无需关心端口/项目 id。
```

在脚本里把 `<总任务id>` 用第 1 步 `task_create` 拿到的 id 通过 prompt 字符串插值传进去。

### 脚本写法示例

```js
// Leader 先 task_create 拿到 taskId（OS MCP），再写 workflow：
const WRITEBACK = `\n【回写 OS（收尾必做）】\n1. ToolSearch: select:mcp__ai-team-os__task_memo_add\n2. 完成后 task_memo_add(task_id="${taskId}", content="...", memo_type="progress")\n3. 项目目录运行，MCP 自动带项目头。`

const r = await agent('你的实际任务……' + WRITEBACK, { schema, label })
```

## 3. 模型档位纪律（用户裁定）

两档制：**Fable = 编排层**（统筹/架构裁决/终审），**Opus = 执行层**（一切 worker）。
不传 model 即继承主会话模型，所以在 Fable 会话里裸派会整场按 Fable 价率烧：

- 每个 `agent()` **默认显式带 `model: 'opus'`**（层级别名，浮动到最新 Opus，不写死型号）；
- 仅**终审/对抗裁决/最高难度修复**的 stage 用 `model: 'fable'`（通常配 `effort: 'xhigh'`）。
- 每处 `model: 'fable'` 调用须配一条 `// fable 理由: …` 行注释；Agent 工具派工则在 prompt 首行写 `[fable 理由: …]`。**S6 派工门禁**（PreToolUse 机检）：缺省 model 直接拦，fable 无理由拦。额度溢出时的放宽是临时特例，须缔造者当次明令并注明有效期，不得沉淀为常规。

```js
// 典型分层：执行 stage 全 opus，终审 stage 才 fable
const found = await parallel(ITEMS.map(x => () =>
  agent(findPrompt(x) + WRITEBACK, { model: 'opus', schema: FINDINGS })))
// fable 理由: 终审裁决需最强模型
const verdict = await agent(judgePrompt(found) + WRITEBACK,
  { model: 'fable', effort: 'xhigh', schema: VERDICT })
```

注：effort 由脚本作者按需自选，治理层不设档位制度；本纪律只软约束，无 hook 硬拦。

### 3.1 用量七规则（2026-09-05 缔造者裁定，方向记忆【模型分层与用量平衡】指向此处）

实证：0905 单日派出 133 个 agent，产出 274 万 token，缓存读取 2.02 亿 token，比例 74:1；同日 Leader 会话 296 轮、70 次 Edit，缓存读取 1.27 亿，比例 338:1——钱花在重复读同一批全文上，不是花在干活上。

**总原则（缔造者原话的规范化）**：限制的是"把任何问题都按最复杂方式做完"，不是把复杂问题做简单。**审查强度按风险定，不按预算定**：L2 该全文对抗就全文；省的是重复读、多余的镜头和反驳者、多余的轮次。**ultracode 常开不等于事事开 workflow**：缔造者习惯常开 ultracode，但它的字面定义是"穷尽正确性、token 不是约束、每个实质任务都开 workflow"，照字面执行就是给每件事按最贵的做。分级在前，模式在后：L0/L1 直接做，只有 L2 才开 workflow；开了也按下面七条控人数与读量。分档不变，加七条用量规则：

1. **机检先行。** 哈希、数字一致性、措辞残留、编号连续、禁用字符、格式，凡能写成命令的检查先跑脚本，零 token 秒出。agent 只处理脚本查不了的：逻辑、措辞立不立得住、设计取舍。
2. **审查分级。** 派出的活按风险分三档，档位决定审不审、怎么审：
   - **L0 自测闭环**：改一个函数、调配置、跑工具。执行者自测通过即交付，不触发审查。前提是**自测覆盖了改动路径**；没覆盖的自动升 L1（实锤：自测 61/61 全绿的构建在改动路径上必崩）。
   - **L1 摘要审查**：加一个模块功能、修普通 bug。Fable 只读需求、架构基线、执行者 ≤300 字回传，不读执行过程。
   - **L2 全文对抗**：跨模块重构、核心接口、安全或性能路径、对外发布物、进签名或哈希台账的产物。全文读，但**一个审稿人读一遍**，不是多镜头加多反驳者。
3. **反驳者配额。** 每条发现 1 个反驳者；它说"是真的"即接受，说"是假的"才加第二个复核（假反驳比假确认贵）。反驳机械类发现（数字、哈希）用 opus，判断类（措辞、逻辑）才用 fable。
4. **审查给定位。** prompt 写"看第 X 节，参照第 Y 行"，禁止默认"读全文加全部参照文档"。多镜头并行时先按镜头切分对象，重叠的发现在派出前合并。
5. **停机规则。** 一轮折入后先跑机检；只有上一轮还剩脚本查不了的判断级 blocker 才开下一轮 agent 审查，否则一个 fable 终审即止。
6. **回传上限。** 执行者只回变更清单、验证结果、决策与风险，≤300 字；工具输出、调试记录、中间上下文用完即弃，不回传。
7. **Leader 不在大上下文里逐条改大文档。** 一份文档超过十处改动，Leader 写成改动清单（定位 + 旧文 + 新文 + 理由）派一个 opus 在小上下文里执行，或整节 Write 一次；Leader 只亲手改一两处。实锤：0905 Leader 70 次 Edit 各在 40 万 token 上下文里重读，1.27 亿缓存读比子 agent 总和还多一半。

```js
// 分级示例：L2 全文对抗 = 一个审稿人 + 每条发现一个反驳者，被反驳才二审
const findings = await agent(reviewPrompt(SECTIONS), { model: 'opus', schema: FINDINGS })   // 定位到章节
const verified = await parallel(findings.map(f => () =>
  // fable 理由: 判断类发现的对抗核验
  agent(refutePrompt(f), { model: f.mechanical ? 'opus' : 'fable', schema: VERDICT })
    .then(async v => v.refuted
      ? { ...f, votes: [v, await agent(refutePrompt(f), { model: 'fable', schema: VERDICT })] }  // fable 理由: 被反驳才二审
      : { ...f, votes: [v] })))
```

## 4. 结构化输出体量纪律（两次生产实锤）

给 agent 配 `schema` 时，prompt 里必须写**显式体量硬约束**（每字段字符上限、条目数上限、"宁可精炼不可超限"）——只靠 schema 的 `maxLength` 拦不住：agent 超限会陷入 StructuredOutput 重试循环，耗尽重试上限（5）后整路阵亡返回 `null`。

- schema 的 `maxLength` 给出安全余量；长文本产出改让 agent 直接 Write 文件，结构化输出只返摘要与路径。
- 某一路阵亡后用 resume 修复：只改失败路的 prompt，其余路 `(prompt, opts)` 不动，走缓存零成本重放。

## 0. 先确认 ultracode 已开启

ultracode 不是常驻模式，需用户手动开启：

- 会话未开启 → **先提示用户开启**，再调 Workflow；已开启（有 system-reminder 确认）→ 直接编排。
- 生态调研的产物必须回写 ecosystem 表（`ecosystem_apply_shallow_summary` /
  `ecosystem_apply_quality_review`），否则台账与 `/ecosystem` 页面看不到。

## 要点

- 回写走 **MCP 工具优先**（自动项目隔离）；HTTP `localhost:8000/api/*` 是等价兜底。
- 安全护栏（危险命令/敏感文件/密钥拦截）对 workflow agent 照常生效。
