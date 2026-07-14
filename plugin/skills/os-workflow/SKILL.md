---
name: os-workflow
description: 在 AI Team OS 项目里使用 CC 内置 Workflow（ultracode）时，让工作流产出回写 OS 的标准做法。当 Leader 准备调用 Workflow 工具编排子 agent 时使用。
---

# OS Workflow — 用 CC 工作流，但让产出回流 OS

## 背景（为什么需要这个 skill）

CC 内置的 `Workflow`（ultracode）是成熟的会话内并行编排器，OS **不与它竞争、也不拦截**——
OS 的定位是 **CC 的持久化治理层**：负责跨会话记账、任务墙、Dashboard、记忆沉淀。

两件事 OS 已**自动**做好，你无需手动操作：
- **自动追踪**：你一调用 Workflow，每个内部 agent 会被 hook 自动注册成一个 OS 团队
  （`workflow-<wf_id>`，一次 workflow = 一个团队，成员按 agent 唯一区分）。
- **委派识别**：调用 Workflow 已被视为"委派"，不会再催你"为什么不建团队"。

你**需要主动**做两件事，让 OS 看清 workflow 干了什么：

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

## 3. 模型档位纪律（编排宪章，2026-07-14 用户裁定）

机队两档制：**Fable = 编排层**（统筹/架构裁决/终审），**Opus = 执行层**（一切 worker）。
CC 的继承语义是"不传 model 即继承主会话模型"——在 Fable 会话里裸派 = 全按 Fable
价率（$10/$50，Opus 的 2 倍）烧，所以：

- 每个 `agent()` **默认显式带 `model: 'opus'`**（层级别名，浮动到最新 Opus，不写死型号）；
- 仅**终审/对抗裁决/最高难度修复**的 stage 用 `model: 'fable'`（通常配 `effort: 'xhigh'`）
  ——这就是官方 advisor 模式的 workflow 级实现（贵模型只出判断、不出产物主体）；
- 官方背书：orchestrator 模式实测 96% 性能 @ 46% 成本（BrowseComp），advisor 模式
  92% @ 63%（SWE-bench Pro）；Fable 官方提示指南明言其为并行 subagent 编排而设计。

```js
// 典型分层：执行 stage 全 opus，终审 stage 才 fable
const found = await parallel(ITEMS.map(x => () =>
  agent(findPrompt(x) + WRITEBACK, { model: 'opus', schema: FINDINGS })))
const verdict = await agent(judgePrompt(found) + WRITEBACK,
  { model: 'fable', effort: 'xhigh', schema: VERDICT })
```

注：effort 属脚本作者按需自选（治理层不设 effort 档位制度，07-10 定稿）；模型治理
"只软不拦"（07-10 用户红线），本纪律靠模板与方向记忆约束，无 hook 硬拦。

## 0. 先确认 ultracode 已开启（用户手动操作，Leader 有提示义务）

ultracode **不是常驻模式**，需要用户手动开启（调整 effort）。而被 Workflow 取代的
自建工作流（生态库 shallow/deep 调研派发、pipeline 编排）过去是随时可跑的——
所以凡是要做这类工作时：

- 会话未开启 ultracode → **先提示用户开启**，再调 Workflow；
- 已开启（有 system-reminder 确认）→ 直接编排。
- 生态调研的产物必须回写 ecosystem 表（`ecosystem_apply_shallow_summary` /
  `ecosystem_apply_quality_review`），台账和 `/ecosystem` Dashboard 是保留的数据层。

## 要点

- **不要**手动 TeamCreate 去"配合" workflow——追踪是自动的，手动建队反而重复。
- 回写走 **MCP 工具优先**（自动项目隔离）；HTTP `localhost:8000/api/*` 是等价兜底。
- 安全护栏（危险命令/敏感文件/密钥拦截）对 workflow agent 照常生效——这是治理，不受影响。
