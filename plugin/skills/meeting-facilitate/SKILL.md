---
name: meeting-facilitate
description: 用 meeting_* 工具跑一场多 Agent 会议：创建 → 亲自 spawn 参与者 → 推进轮次 → 签到 → conclude 并把决策上墙。仅当已决定以「开会」形式产出一个需上墙的决策时使用；典型场景是减法类提案：回退既有设计决策、砍工具删表、削弱或删除自动检查——这类结论必须留痕。加法（新增检查、加严判据、新增工具）不需要开会，写清理由直接做。用户只是让你评审代码、复盘、比较方案时不要触发——那是直接做，或派一个审查 agent。
---

# Meeting Facilitate — 会议主持技能

本技能指导你（Leader 或具备主持职责的 Agent）端到端组织一场多 Agent 会议：选模板 → 创建会议 → spawn 真实参与者 → 签到 → 推进轮次 → 验证全员发言 → 结束并汇总。

## 前置要求

`materials` 与 `context_files` 里的路径要先备好：它们会被**原样**写进参与者 prompt 的必读清单，路径写错参与者只会读不到，不会报错。

## 核心原则

**OS 不会自动 spawn 参与者** — `meeting_create` 只创建会议记录和 dispatch_plan，**真正让参与者到场必须靠你亲自调用 Agent tool**。光创建不 spawn = 没人到场 = 会议失败。

（发言署名见 Step 5，出勤校验见 Step 7。）

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
    team_name="repo-insight-arch",                # 仅 OS 侧归属；不会写进 launch_call
    participants=[
        {
            "name": "arch-lead",
            "agent_template": "software-architect",
            "role": "评估架构整体可行性与分层合理性",
            "context_files": ["docs/v0.9-prompt-registry.md"],
            "expected_output": "三段式：可行性 / 风险 / 建议",
        },
        # 其余参与者同型
    ],
    rounds=[                                       # 可选，省略则用模板默认 rounds
        {"topic": "立场陈述", "rule": "每人 3 段：评估视角 / 风险点 / 评分 1-5"},
    ],
    materials=["docs/v0.9-prompt-registry.md"],   # 全员必读
)
```

从返回里记下 `data.id`（即 `meeting_id`）与 `dispatch_plan`，后续每一步都要用。

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

`pending` 为空才进下一步。久不为空就先 `meeting_read_messages` 看他们到底发了什么，必要时对 `pending` 里的人重跑 Step 3。

⚠️ 返回的 `timeout_in_seconds` 是**本轮已过秒数**（elapsed），不是剩余时间——读成倒计时会让你在参与者早已退出后继续干等。

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

> ⚠️ **代打警告：** 以主持人身份发言时 `agent_id` 与 `caller_agent_id` 都填你自己；不一致会被标 `impersonation=true` 并写 `meeting.impersonation` 事件（OS 只留痕，**不会拦下**）。**永远不要代打他人发言**——即使你只是想"帮忙补一段"。

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

conclude 默认校验出勤，400 的 `detail` 直接带 `missing` / `spoken`——照 `missing` 重新 spawn。**别顺手 `force=true`**：响应体自带的 `hint` 正是在教你用它，而用了就把没发言的人算成到场，只留下一条 `meeting.forced_conclude_with_missing` 事件。

**`summary` 必写** — 它会自动进团队记忆（`memory_search` / `team_briefing` 可检索）；不写等于这场会没留下决策记录，下次复盘全靠考古。

---

## 端到端示例

三份完整端到端示例（council / debate / retrospective）见同目录 `examples.md`。

---

## 故障排查

### `meeting_attendance_check` 的 pending 一直不减
先 `meeting_read_messages(meeting_id=...)` 看实际收到了什么——常见成因是发言者的 `agent_name` 与 `expected_participants` 对不上，而不是没发言。反复重 spawn 同一批人每次都白烧一轮。

### Round 2 没人发言
Round 1 的 Agent 发完就退出了，Round 2 没人在场。见 Step 6：必须为新一轮重新 spawn，并说明本轮规则。

---

## 参考资料

- 模板详细说明：`templates/<name>.md`（每个模板有"何时使用"和"反模式"章节，也是模板定义的唯一来源——`src/aiteam/meeting/templates.py` 直接扫这个目录）
- 完整调用示例：`examples.md`
- 相关 MCP 工具：`meeting_create` / `meeting_send_message` / `meeting_attendance_check` / `meeting_read_messages` / `meeting_conclude` / `meeting_template_list` / `meeting_list` / `meeting_update` / `debate_start` / `debate_code_review`
