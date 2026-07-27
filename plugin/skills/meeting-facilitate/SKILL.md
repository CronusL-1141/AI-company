---
name: meeting-facilitate
description: 组织多 Agent 会议全生命周期——创建会议、spawn 真实参与者、推进轮次、签到校验、结束汇总。当需要多方协作做决策、评审方案、辩论分歧、复盘项目、头脑风暴或方案评估时使用本技能。
---

# Meeting Facilitate — 会议主持技能

本技能指导你（Leader 或具备主持职责的 Agent）端到端组织一场多 Agent 会议：选模板 → 创建会议 → spawn 真实参与者 → 签到 → 推进轮次 → 验证全员发言 → 结束并汇总。

## 前置要求

- 知道自己的 `agent_id`（用于以主持人身份发言）——它由 SubagentStart 注入的「你的 OS 身份」块给出；
  拿不到就 `GET /api/agents/whoami?name=<你的名字>&session_id=<会话id>` 自查（服务端按 cc id / 会话+名 反查）
- 已明确：会议目的、需要的角色、目标产出
- 已知会议涉及的关键文件路径（用于 materials/context_files）

## 核心原则（看完这三条再往下读）

1. **OS 不会自动 spawn 参与者** — `meeting_create` 只创建会议记录和 dispatch_plan，**真正让参与者到场必须靠你亲自调用 Agent tool**。光创建不 spawn = 没人到场 = 会议失败。
2. **绝不代打他人发言** — 你以主持人身份发言时，`agent_id` 和 `caller_agent_id` 必须都填**你自己的 ID**。用别人的 `agent_id` 发言会被 OS 标记为 `impersonation=true` 并写入审计日志。
3. **conclude 前必须确认全员发言** — `meeting_conclude` 默认开启 `validate_attendance`，未发言者会让 conclude 返回 400。**不要用 `force=True` 绕过**——除非有不可抗力的技术理由。

---

## 主持流程（7 步）

### Step 1: 选择会议模板

根据会议目的对照下表选模板。详细模板说明见 `templates/<name>.md`（progressive disclosure）。

| 目的 | 推荐模板 | 轮数 | 为何 |
|------|---------|------|------|
| 发散创意、产生新想法 | `brainstorm` | 4 | 独立发散 → 交叉启发 → 评估 → 汇总 |
| 多方案中做选择 | `decision` | 3 | 陈述 → 质询 → 收敛 |
| 评审代码 / PR / 交付物 | `review` | 3 | 陈述 → 独立评审 → 回应裁定 |
| 项目复盘、提取教训 | `retrospective` | 3 | 4Ls → 改进方向 → 承诺计划 |
| 每日进度同步 | `standup` | 1 | 三问：完成 / 计划 / 阻塞 |
| 决策有重大分歧或风险 | `debate` | 4 | 正方陈述 → 反方质疑 → 正方回应 → 裁决 |
| 开放议程、自由议题 | `lean_coffee` | 3 | 议题收集 → 投票 → 时间盒讨论 |
| 架构 / 方案多视角评审 | `council` | 3 | 专家视角 → 交叉质询 → 裁决 |

不确定？用 `template="free"`，OS 会根据 `topic` 关键词自动推荐。

### Step 2: 创建会议（拿到 dispatch_plan）

**必须使用结构化 `participants`**（dict 列表），否则 dispatch_plan 里的 `launch_call` 会是空的，无法 ready-to-paste。

```
meeting_create(
    topic="评审 v0.9 Prompt Registry 架构方案",
    template="council",                           # Step 1 选的模板
    team_id="repo-insight-arch",                  # 可省略，自动用活跃团队
    team_name="repo-insight-arch",                # 用于 launch_call.params.team_name
    participants=[
        {
            "name": "arch-lead",
            "agent_template": "software-architect",
            "role": "评估架构整体可行性与分层合理性",
            "context_files": ["docs/v0.9-prompt-registry.md"],
            "expected_output": "三段式：可行性 / 风险 / 建议",
        },
        {
            "name": "backend-arch",
            "agent_template": "backend-architect",
            "role": "评估存储层与 API 设计",
            "context_files": ["docs/v0.9-prompt-registry.md", "src/aiteam/storage/repository.py"],
            "expected_output": "存储方案 + 接口契约 + 迁移路径",
        },
    ],
    rounds=[                                       # 可选，省略则用模板默认 rounds
        {"topic": "立场陈述", "rule": "每人 3 段：评估视角 / 风险点 / 评分 1-5"},
    ],
    materials=["docs/v0.9-prompt-registry.md"],   # 全员必读
)
```

返回结构（关键字段）：

```
{
    "data": {"id": "mtg-abc123", ...},
    "dispatch_plan": [
        {
            "participant": "arch-lead",
            "launch_call": {
                "tool": "Agent",
                "params": {
                    "subagent_type": "software-architect",
                    "name": "arch-lead",
                    "team_name": "repo-insight-arch",
                    "description": "评估架构整体可行性与分层合理性",
                    "prompt": "<OS 已生成的完整 prompt，包含 meeting_id / 角色 / 必读材料 / 发言规则 / meeting_send_message 调用示例 / 完成后 SendMessage 指令>",
                },
            },
            "ready_to_paste": True,
        },
        ...
    ],
    "expected_participants": ["arch-lead", "backend-arch"],
    "attendance_check_command": "meeting_attendance_check(meeting_id='mtg-abc123')",
}
```

记录 `meeting_id`，后续每一步都要用到。

### Step 3: Spawn 每位参与者（最关键的一步）

> ⚠️ **这是整个流程的关键。跳过这一步 = 会议没有任何人到场 = 后续所有步骤都会失败。**

遍历 `dispatch_plan`，对每个 `ready_to_paste=True` 的项目调用 Agent tool，**直接把 `launch_call.params` 整体作为 Agent tool 的参数**：

```
for item in dispatch_plan:
    if not item["ready_to_paste"]:
        # 旧字符串格式：补结构化参数后重新 meeting_create
        continue
    Agent(**item["launch_call"]["params"])
```

**约束：**
- **不要修改 `prompt` 字段的内容** — OS 已经把 meeting_id、角色、必读材料、发言规则、`meeting_send_message` 调用示例、完成后 `SendMessage("已发言")` 指令全部预设好了。手动改动反而会破坏闭环。
- **不要省略任何参与者** — 漏掉一个，Step 7 conclude 时就会被 attendance 校验拦下。
- 多个 spawn 可以**并行发出**（同一个消息里多个 Agent 调用），加快到场速度。

### Step 4: 签到 — 等待全员发言

每位参与者发完 `SendMessage("已完成发言")` 后，调用：

```
meeting_attendance_check(meeting_id="mtg-abc123")
```

返回：

```
{
    "round": 1,
    "expected": ["arch-lead", "backend-arch"],
    "spoken": ["arch-lead"],
    "pending": ["backend-arch"],
    "timeout_in_seconds": 180
}
```

**根据 pending 决定动作：**

| pending 状态 | timeout | 动作 |
|------------|---------|------|
| 空 | — | ✅ 全员到场，进入 Step 5 |
| 非空 | < 5 分钟 | ⏳ 继续等待 |
| 非空 | ≥ 5 分钟 | 🔁 用 SendMessage 催一次；若仍无响应，对 pending 列表中的 agent 重新执行 Step 3 spawn |

### Step 5: 主持人发言（可选但推荐）

每轮开始或结束时，你可以以主持身份发言引导讨论：

```
meeting_send_message(
    meeting_id="mtg-abc123",
    agent_id="team-lead",                # 你自己的 agent_id
    agent_name="team-lead",
    caller_agent_id="team-lead",         # ⚠️ 必须与 agent_id 一致
    round_number=1,
    content="【主持】Round 1 已全员发言。共识：xxx；分歧：yyy。下一轮请聚焦 yyy 的解法。",
)
```

> ⚠️ **代打警告：** 如果 `caller_agent_id ≠ agent_id`，OS 会把消息标记为 `impersonation=true` 并写入 `meeting.impersonation` 事件日志。**永远不要代打他人发言**——即使你只是想"帮忙补一段"。

### Step 6: 推进下一轮

Round 1 全员发言后，进入 Round 2/3：

1. 用 Step 5 的方式发主持人总结消息，明确进入下一轮和新轮次的发言要求
2. **为新一轮重新 spawn 参与者**（Agent 在完成 Round 1 后通常已退出，需重新唤起）
   - 注意：`meeting_create` 生成的 prompt 默认是 Round 1 的，进入 Round 2 时你需要手动构造 prompt 或在 description 里说明本轮规则
3. 回到 Step 4 等待签到

### Step 7: 结束会议

确认本轮 `attendance_check` 的 `pending` 为空后：

```
meeting_conclude(
    meeting_id="mtg-abc123",
    summary="共识：采用方案 A；待办：backend-arch 在 03-20 前出存储 schema；遗留风险：迁移期双写一致性需进一步验证。",
)
```

**默认行为：**
- `validate_attendance=True`（默认） — 未全员发言会返回 400 + missing 列表
- `force=False`（默认） — 不允许跳过校验

**返回 400 时：** 不要立即用 `force=True`。先：
1. 调 `meeting_attendance_check` 看谁缺席
2. 重新 spawn 缺席者或追问
3. 实在无法到场再考虑 `force=True`（会触发 `meeting.forced_conclude_with_missing` 事件，留下审计痕迹）

成功后 OS 会自动把 `summary` 存入团队记忆，可通过 `memory_search` 或 `team_briefing` 检索。

---

## 反模式（绝对禁止）

- ❌ **跳过 Step 3 的 spawn** — 只调 `meeting_create` 然后自己代打所有参与者发言。这是历史上最严重的事故模式。
- ❌ **代打他人发言** — 用别人的 `agent_id` 调 `meeting_send_message`。会被打上 impersonation 标记并记录审计日志。
- ❌ **conclude 空会议** — 0 条发言就 conclude，会被 400 拒绝。
- ❌ **滥用 `force=True`** — `force=True` 是逃生口不是日常工具，每次使用都会写事件日志。
- ❌ **修改 `dispatch_plan.launch_call.params.prompt`** — OS 已经调好闭环，手动改会破坏 `meeting_send_message` 调用链。
- ❌ **用旧字符串格式 `participants=["a", "b"]`** — `launch_call` 会是空的，`ready_to_paste=False`，Step 3 没法执行。
- ❌ **会后忘记写 summary** — 没 summary 团队记忆里就没决策记录，下次复盘时全靠考古。

---

## 端到端示例

### 示例 1：架构方案 Council 评审

场景：评审 v0.9 Prompt Registry 设计文档，需要 arch-lead + backend-arch + ai-arch 三方评估。

```
# Step 1: 选模板 — council（多视角专家评审）

# Step 2: 创建会议
result = meeting_create(
    topic="Council 评审：v0.9 Prompt Registry 架构",
    template="council",
    team_id="repo-insight-arch",
    team_name="repo-insight-arch",
    materials=["docs/v0.9-prompt-registry.md"],
    participants=[
        {"name": "arch-lead", "agent_template": "software-architect",
         "role": "评估整体分层与可演进性",
         "context_files": ["docs/architecture.md"],
         "expected_output": "三段：分层评估 / 演进风险 / 评分 1-5"},
        {"name": "backend-arch", "agent_template": "backend-architect",
         "role": "评估存储与 API 契约",
         "context_files": ["src/aiteam/storage/repository.py"],
         "expected_output": "存储方案 / 接口契约 / 评分 1-5"},
        {"name": "ai-arch", "agent_template": "ai-engineer",
         "role": "评估 prompt 版本化对模型行为的影响",
         "context_files": [],
         "expected_output": "效果保留性 / 回滚策略 / 评分 1-5"},
    ],
)
meeting_id = result["data"]["id"]

# Step 3: Spawn 三位参与者（关键！）
for item in result["dispatch_plan"]:
    Agent(**item["launch_call"]["params"])

# Step 4: 等待 + 签到
status = meeting_attendance_check(meeting_id=meeting_id)
# pending=[] 后继续

# Step 5: 主持人引导（可选）
meeting_send_message(
    meeting_id=meeting_id, agent_id="team-lead", agent_name="team-lead",
    caller_agent_id="team-lead", round_number=1,
    content="【主持】Round 1 已收齐三方评估，进入 Round 2 交叉质询。",
)

# Step 6: 推进 Round 2、Round 3...

# Step 7: 结束
meeting_conclude(
    meeting_id=meeting_id,
    summary="三方一致 APPROVE，条件：backend-arch 提出的存储双写迁移方案需补 ADR；ai-arch 要求灰度验证回滚策略。",
)
```

### 示例 2：决策辩论（debate 模板）

场景：要决定 BM25 用 Tantivy 还是 Whoosh，存在分歧。

```
result = meeting_create(
    topic="决策辩论：BM25 选 Tantivy 还是 Whoosh",
    template="debate",
    participants=[
        {"name": "perf-advocate", "agent_template": "backend-architect",
         "role": "正方：主张 Tantivy（性能优先）",
         "context_files": ["benchmarks/bm25_compare.md"],
         "expected_output": "方案 + 数据 + 收益 + 局限"},
        {"name": "simple-critic", "agent_template": "code-reviewer",
         "role": "反方：质疑 Tantivy 引入 Rust 依赖的复杂度",
         "context_files": ["benchmarks/bm25_compare.md"],
         "expected_output": "引用正方原话 + 风险等级 + 替代方案"},
        {"name": "team-lead", "agent_template": "team-lead",
         "role": "裁决方",
         "context_files": [],
         "expected_output": "采纳点 / 最终结论 / Action Items"},
    ],
)
for item in result["dispatch_plan"]:
    Agent(**item["launch_call"]["params"])
# ... 后续 Steps 4-7
```

### 示例 3：Sprint 复盘（retrospective 模板）

场景：M6 阶段结束，全队 retrospective。

```
result = meeting_create(
    topic="M6 复盘：报告系统 DB 重构与 Dashboard 隔离",
    template="retrospective",
    materials=["docs/m6-summary.md"],
    participants=[
        {"name": "backend-dev", "agent_template": "backend-architect",
         "role": "后端开发视角", "context_files": [], "expected_output": "4Ls 各 1 条"},
        {"name": "frontend-dev", "agent_template": "frontend-developer",
         "role": "前端开发视角", "context_files": [], "expected_output": "4Ls 各 1 条"},
        {"name": "qa", "agent_template": "qa-engineer",
         "role": "测试视角", "context_files": [], "expected_output": "4Ls 各 1 条"},
    ],
)
for item in result["dispatch_plan"]:
    Agent(**item["launch_call"]["params"])
# ... Step 4 签到 → Step 6 推进到 Round 2 改进方向 → Round 3 承诺计划 → Step 7 conclude
```

---

## 故障排查

### `dispatch_plan` 为空或 `ready_to_paste=False`
**原因：** 用了旧的字符串 participants 格式 `participants=["arch-lead"]`。
**修复：** 改用结构化 dict 格式（参考 Step 2），重新 `meeting_create`。

### `meeting_attendance_check` 的 pending 一直不减
**原因：** Spawn 出去的 Agent 没成功调 `meeting_send_message` 就退出了（可能 prompt 被改坏了，或 Agent 偏离了任务）。
**修复：**
1. `meeting_read_messages(meeting_id=...)` 看实际收到了哪些消息
2. 对 pending 列表中的 agent 重新执行 Step 3 spawn
3. 如果反复失败，检查 dispatch_plan 是否被你手动修改过

### `meeting_conclude` 返回 400 + missing 列表
**原因：** Step 4 没有等到全员发言就 conclude。
**修复：** 不要用 `force=True`。先重新 spawn missing 列表中的 agent，等他们发言后再 conclude。

### 看到 `meeting.impersonation` 事件
**原因：** 某次 `meeting_send_message` 的 `caller_agent_id` 与 `agent_id` 不一致。
**修复：** 检查所有 `meeting_send_message` 调用，确保两个 ID 一致。如果是 Leader 代发系统通知，把两者都设成 `team-lead`。

### Round 2 没人发言
**原因：** Round 1 的 Agent 完成发言后已经退出，Round 2 没人在场。
**修复：** Step 6 必须为新一轮重新 spawn 参与者，并在 prompt/description 里说明本轮的发言规则（因为 OS 默认 prompt 是 Round 1 的）。

---

## 参考资料

- 模板详细说明：`plugin/skills/meeting-facilitate/templates/<name>.md`（每个模板有"何时使用"和"反模式"章节）
- 会议系统设计：`docs/meeting-templates-design.md`
- 相关 MCP 工具：`meeting_create` / `meeting_send_message` / `meeting_attendance_check` / `meeting_read_messages` / `meeting_conclude` / `meeting_template_list` / `meeting_list` / `meeting_update` / `debate_start` / `debate_code_review`
