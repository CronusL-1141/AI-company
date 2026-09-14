---
name: os-status
description: 显示 AI Team OS 系统状态 — 项目、团队、Agent、活跃会议概览
---

# /os-status — 系统状态概览

- 整体概览：`project_summary()`（不传参即当前活跃项目）。
- 单支队：`team_status(team_id)`。默认返回 compact 投影，那是**有意裁剪**不是字段缺失。
- 活跃会议：`meeting_list(status="active")`。

**不要按"遍历团队再逐队 agent_list"的方式拼状态**：实测单支队有过 173 个成员，全量摘要
170,331 字符，超出 MCP 返回上限；拿被截断的结果作答，会把一支 173 人的队报成 3 人。
`team_status` 的投影就是为这次事故做的。

## 别误诊

- **没有团队是正常的**，不需要任何初始化命令：团队由 SubagentStart hook 在第一次派工时
  自动建，空列表只说明还没派过子 agent。
- API 不可达时才提示用户走 `/os-up`。端口见 `~/.claude/data/ai-team-os/api_port.txt`。
