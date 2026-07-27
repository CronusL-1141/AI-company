---
name: meeting-participate
description: Participate in AI Team OS meetings with structured discussion rounds
---

# Meeting Participate — 会议参与技能

当你被邀请参加一个会议时，按照以下流程参与讨论。会议是多 Agent 异步协作的核心机制。

## 前提：先拿到自己的 `agent_id`

发言必须署自己的名，所以第一步是确认 `agent_id` 与 `agent_name`。**不需要注册**——
你被派出时 SubagentStart hook 已自动把你收编进团队（`os-register` 技能与
`agent_register` 工具均已退役）。按下面顺序取 id，命中即停：

1. **读注入**：启动上下文里的「## 你的 OS 身份」块，直接给出 `agent_id` / `team_id` /
   名字。绝大多数情况到此为止。
2. **服务端反查**（注入里写着"尚未落库"时用）：收编与注入是并行的，慢一步很正常。

   ```bash
   curl -s "http://localhost:$(cat ~/.claude/data/ai-team-os/api_port.txt)/api/agents/whoami?name=<你的名字>&session_id=<你的会话id>"
   ```

   服务端按 `cc_agent_id` → `会话+名字` → `名字` 三级反查，返回 `{"found": true, "agent_id": ...}`。
3. **兜底**：`agent_list(team_id)` 里按名字找自己那一行。

三条都拿不到（OS API 不可达）时，用你的**名字**作为 `agent_id` 发言并在内容里注明——
宁可留下可追溯的署名，也不要因为拿不到 id 就不发言。

## 参与流程

### 1. 读取会议消息

进入会议后，先了解当前讨论状态：

```
使用 MCP tool: meeting_read_messages
参数:
  meeting_id: <会议ID>
```

### 2. 分析已有发言

仔细阅读所有已有消息：
- 理解每位参与者的核心观点
- 找出共识点和分歧点
- 识别尚未被讨论的角度

### 3. 发送你的观点

根据当前轮次发送消息：

```
使用 MCP tool: meeting_send_message
参数:
  meeting_id: <会议ID>
  agent_id: <你的agent_id>
  agent_name: <你的名称>
  content: <你的发言内容>
  round_number: <当前轮次>
```

### 4. 讨论规则

严格遵循以下讨论规则：

**Round 1 — 各自观点**
- 独立提出你对议题的看法
- 基于你的角色和专业领域发表见解
- 不需要引用他人（还没有人发言）

**Round 2+ — 引用回应**
- 必须先读取前人发言（再次调用 `meeting_read_messages`）
- 引用并回应具体观点，格式如：`@agent_name 提到"..."，我认为...`
- 可以补充新观点，但必须至少回应一个已有观点
- 明确表达同意或不同意，并给出理由

**最后一轮 — 汇总**
- 总结本次讨论的共识
- 列出仍存在的分歧
- 提出下一步建议

### 5. 多轮参与

如果会议有多轮讨论：
1. 每轮开始前重新读取消息，获取最新发言
2. 递增 `round_number`
3. 持续参与直到会议被主持人结束

## 发言质量要求

- **具体**: 不要泛泛而谈，要给出具体的技术方案或理由
- **有建设性**: 提出问题的同时给出解决方案
- **尊重他人**: 不同意时对事不对人，用"我认为"而非"你错了"
- **简洁**: 每次发言聚焦1-3个核心观点，避免冗长
