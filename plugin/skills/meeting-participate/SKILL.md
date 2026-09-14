---
name: meeting-participate
description: 你被派来参加 AI Team OS 会议（派你的 prompt 里带 meeting_id），且要在 Round 2 及之后发言时用：怎么拿到自己的 agent_id、怎么引用前人发言、round_number 怎么填。Round 1 的材料、发言规则与调用样例已经写在派你的 prompt 里，不必读本技能。
---

# Meeting Participate — 会议参与技能

## 前提：先拿到自己的 `agent_id`

发言要署自己的名。`agent_id` 在你的启动上下文「## 你的 OS 身份」块里，直接读即可——
**不需要注册**，SubagentStart hook 已自动把你收编进团队。那个块写着"尚未落库"时，按块内
给出的 whoami 命令自查（收编与注入是并行的，慢一步很正常）。

都拿不到（OS API 不可达）时，用你的**名字**作为 `agent_id` 发言并在内容里注明——宁可留下
可追溯的署名，也不要因为拿不到 id 就不发言。

## 讨论规则

**Round 1 — 各自观点**：按派你的 prompt 里那条 `round_rule` 发言，不需要引用他人。

**Round 2+ — 引用回应**

- 必须先 `meeting_read_messages` 读前人发言
- 引用并回应具体观点，格式如：`@agent_name 提到"..."，我认为...`
- 可以补充新观点，但必须至少回应一个已有观点
- 明确表达同意或不同意，并给出理由
- **`round_number` 要跟着轮次递增**。一直填 1 会让消息落回上一轮，主持人的出勤校验仍判
  本轮缺人，于是你被反复重新 spawn。

**最后一轮 — 汇总**：总结共识、列出仍存在的分歧、提出下一步建议。
