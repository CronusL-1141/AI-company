---
name: meeting-facilitate
description: Facilitate AI Team OS meetings - create, manage rounds, and conclude
---

# Meeting Facilitate — 会议主持技能

当你需要组织多 Agent 讨论时，使用此技能主持会议。你负责创建会议、引导轮次、确保讨论质量并汇总结论。

## 前提

你必须已经完成 OS 注册（os-register），拥有自己的 `agent_id`。

## 主持流程

### 1. 创建会议

```
使用 MCP tool: meeting_create
参数:
  team_id: <团队ID>
  topic: <明确的讨论主题>
  participants: [<agent_id_1>, <agent_id_2>, ...]
```

记录返回的 `meeting_id`。

如果不确定参与者列表，先查看团队成员：

```
使用 MCP tool: agent_list
参数:
  team_id: <团队ID>
```

### 2. 通知参与者

将会议信息传达给每位参与者，告知：
- 会议 ID（`meeting_id`）
- 讨论主题
- 讨论规则（R1各自观点 → R2+引用回应 → 最终汇总）
- 预期轮次数

### 3. 引导 Round 1

让每位参与者独立发表观点。你自己也可以作为参与者发言：

```
使用 MCP tool: meeting_send_message
参数:
  meeting_id: <会议ID>
  agent_id: <你的agent_id>
  agent_name: <你的名称>
  content: "【主持】请各位就'<主题>'发表观点。本轮为独立发言轮，无需引用他人。"
  round_number: 1
```

### 4. 监控进展

定期检查发言情况：

```
使用 MCP tool: meeting_read_messages
参数:
  meeting_id: <会议ID>
```

确认：
- 所有参与者是否已发言
- 发言质量是否达标（是否具体、有建设性）
- 是否需要追问或引导

### 5. 引导 Round 2+

当 Round 1 所有人发言完毕，推进到下一轮：

```
使用 MCP tool: meeting_send_message
参数:
  meeting_id: <会议ID>
  agent_id: <你的agent_id>
  agent_name: <你的名称>
  content: "【主持】Round 1 发言完毕。请进入 Round 2，要求：1) 先读取所有前人发言 2) 引用并回应至少一个具体观点 3) 可补充新观点"
  round_number: 2
```

### 6. 汇总结论

当讨论充分后（通常2-3轮），发送汇总：

```
使用 MCP tool: meeting_send_message
参数:
  meeting_id: <会议ID>
  agent_id: <你的agent_id>
  agent_name: <你的名称>
  content: "【主持-汇总】\n共识：...\n分歧：...\n决策：...\n后续行动：..."
  round_number: <最终轮次>
```

### 7. 结束会议

```
使用 MCP tool: meeting_conclude
参数:
  meeting_id: <会议ID>
```

## 主持原则

- **中立性**: 作为主持人时，引导讨论而非主导观点
- **确保全员参与**: 如果有人未发言，主动邀请
- **控制节奏**: 每轮设定清晰的目标和时间预期
- **聚焦主题**: 发现跑题时及时拉回
- **记录决策**: 汇总时明确记录达成的共识和待定事项
